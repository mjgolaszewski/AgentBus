"""A local, authenticated message log backed by one Slack channel.

Run a single worker with ``uvicorn agentbus_service:create_app --factory``.
Agent names are self-reported labels, not authenticated Slack identities.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hmac
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Annotated, Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse


LOGGER = logging.getLogger("agentbus")
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
SLACK_TS = r"^[0-9]{1,20}\.[0-9]{1,20}$"
MAX_TEXT = 6000


@dataclass(frozen=True)
class Settings:
    api_token: str = field(repr=False)
    slack_bot_token: str = field(repr=False)
    slack_app_token: str = field(repr=False)
    slack_channel: str
    db_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        names = ("API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL")
        values = {name.lower(): os.environ.get(f"AGENTBUS_{name}", "").strip() for name in names}
        missing = [f"AGENTBUS_{name}" for name in names if not values[name.lower()]]
        if missing:
            raise ValueError("Missing required configuration: " + ", ".join(missing))
        if not re.fullmatch(r"[!-~]{32,}", values["api_token"]):
            raise ValueError("AGENTBUS_API_TOKEN must contain at least 32 printable non-whitespace ASCII characters")
        if not values["slack_bot_token"].startswith("xoxb-"):
            raise ValueError("AGENTBUS_SLACK_BOT_TOKEN must be a bot token (xoxb-)")
        if not values["slack_app_token"].startswith("xapp-"):
            raise ValueError("AGENTBUS_SLACK_APP_TOKEN must be an app token (xapp-)")
        if not re.fullmatch(r"[CG][A-Z0-9]+", values["slack_channel"]):
            raise ValueError("AGENTBUS_SLACK_CHANNEL must be a channel ID, not a name")
        state = Path(os.environ.get("XDG_STATE_HOME", str(Path(os.path.expanduser("~")) / ".local/state")))
        db_path = Path(os.environ.get("AGENTBUS_DB_PATH", str(state / "agentbus/messages.sqlite3")))
        return cls(**values, db_path=db_path.expanduser())


def encode_envelope(message: SendMessage) -> str:
    """Keep Slack's link/mention processing out of the machine-readable fallback."""
    encoded = json.dumps({"agentbus": 1, **message.model_dump(include=set(SendMessage.model_fields))},
                         ensure_ascii=True, separators=(",", ":"))
    for literal, escape in (("<", r"\u003c"), (">", r"\u003e"), ("&", r"\u0026"), ("/", r"\/")):
        encoded = encoded.replace(literal, escape)
    return encoded


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender: str = Field(pattern=IDENTIFIER)
    recipient: str = Field(default="all", pattern=IDENTIFIER)
    text: str = Field(min_length=1, max_length=MAX_TEXT)
    repo: str | None = Field(default=None, max_length=240)
    kind: str = Field(default="message", pattern=IDENTIFIER)
    correlation_id: str | None = Field(default=None, max_length=128)
    thread_ts: str | None = Field(default=None, pattern=SLACK_TS)

    @model_validator(mode="after")
    def check_text(self) -> SendMessage:
        if not self.text.strip():
            raise ValueError("text must not be blank")
        for value in (self.text, self.repo, self.correlation_id):
            if value is not None:
                try:
                    value.encode("utf-8")
                except UnicodeEncodeError:
                    raise ValueError("message fields must contain valid Unicode") from None
        if len(encode_envelope(self)) > 39000:
            raise ValueError("encoded message exceeds Slack's safe text size; shorten text")
        return self


class Message(SendMessage):
    cursor: int
    slack_ts: str
    received_at: str


class MessagePage(BaseModel):
    messages: list[Message]
    next_cursor: int
    has_more: bool


class MessageStore:
    """SQLite is shared by the HTTP event loop and Socket Mode's callback thread."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                slack_ts TEXT NOT NULL,
                thread_ts TEXT,
                recipient TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(channel, slack_ts)
            )
        """)
        self._db.commit()

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        return Message(**json.loads(row["payload"]), cursor=row["cursor"],
                       slack_ts=row["slack_ts"], received_at=row["received_at"])

    def append(self, channel: str, slack_ts: str, message: SendMessage) -> Message:
        with self._lock, self._db:
            self._db.execute("""
                INSERT OR IGNORE INTO messages
                    (channel, slack_ts, thread_ts, recipient, received_at, payload)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (channel, slack_ts, message.thread_ts, message.recipient,
                  datetime.now(timezone.utc).isoformat(), message.model_dump_json()))
            row = self._db.execute("SELECT * FROM messages WHERE channel = ? AND slack_ts = ?",
                                   (channel, slack_ts)).fetchone()
            return self._message(row)

    def read(self, channel: str, after: int = 0, recipient: str | None = None,
             limit: int = 100, thread_ts: str | None = None) -> MessagePage:
        clauses = ["channel = ?", "cursor > ?"]
        params: list[str | int] = [channel, after]
        if recipient is not None:
            clauses.append("recipient IN (?, 'all')")
            params.append(recipient)
        if thread_ts is not None:
            clauses.append("(thread_ts = ? OR slack_ts = ?)")
            params.extend((thread_ts, thread_ts))
        with self._lock:
            high = self._db.execute("SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel = ?",
                                    (channel,)).fetchone()[0]
            rows = self._db.execute(
                "SELECT * FROM messages WHERE " + " AND ".join(clauses) + " ORDER BY cursor LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        messages = [self._message(row) for row in rows[:limit]]
        next_cursor = messages[-1].cursor if has_more else max(after, high)
        return MessagePage(messages=messages, next_cursor=next_cursor, has_more=has_more)

    def close(self) -> None:
        with self._lock:
            self._db.close()


def normalize_event(event: dict, channel: str) -> tuple[str, SendMessage] | None:
    if event.get("type") != "message" or event.get("channel") != channel:
        return None
    if event.get("subtype") not in (None, "bot_message", "thread_broadcast") or event.get("hidden"):
        return None
    ts, text = event.get("ts"), event.get("text")
    if not isinstance(ts, str) or not re.fullmatch(SLACK_TS, ts) or not isinstance(text, str) or not text.strip():
        return None
    thread_ts = event.get("thread_ts")
    if not isinstance(thread_ts, str) or not re.fullmatch(SLACK_TS, thread_ts):
        thread_ts = None
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and type(envelope.get("agentbus")) is int and envelope.pop("agentbus") == 1:
            envelope["thread_ts"] = thread_ts
            return ts, SendMessage.model_validate(envelope)
    except (ValueError, RecursionError):
        pass
    identity = event.get("user") or event.get("bot_id") or "unknown"
    identity = identity if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identity) else "unknown"
    # Human messages can exceed the API's send limit, including escaped Unicode.
    text = text.encode("utf-8", errors="replace").decode("utf-8").strip()[:MAX_TEXT]
    try:
        return ts, SendMessage(sender=f"slack:{identity}", text=text, thread_ts=thread_ts)
    except ValidationError:
        # Even 3000 astral Unicode characters fit as JSON surrogate pairs.
        return ts, SendMessage(sender=f"slack:{identity}", text=text[:3000], thread_ts=thread_ts)


class SlackReceiver:
    def __init__(self, store: MessageStore, channel: str):
        self.store, self.channel = store, channel

    def __call__(self, client: SocketModeClient, request: SocketModeRequest) -> None:
        if request.type == "events_api":
            event = request.payload.get("event")
            normalized = normalize_event(event, self.channel) if isinstance(event, dict) else None
            if normalized is not None:
                ts, message = normalized
                try:
                    self.store.append(self.channel, ts, message)
                except (sqlite3.Error, OSError):
                    LOGGER.error("Could not durably store Slack event; withholding acknowledgement")
                    return
        client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))


def build_socket(settings: Settings, receiver: SlackReceiver) -> SocketModeClient:
    client = SocketModeClient(
        app_token=settings.slack_app_token,
        web_client=WebClient(token=settings.slack_bot_token, timeout=15, retry_handlers=[]),
        concurrency=1,
    )
    client.socket_mode_request_listeners.append(receiver)
    return client


class SlackPoster:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def post(self, message: SendMessage) -> str:
        label = f"{message.sender} → {message.recipient} · {message.kind}"
        if message.repo:
            label += f" · {message.repo}"
        blocks = [{"type": "context", "elements": [{"type": "plain_text", "text": label, "emoji": False}]}]
        blocks.extend({"type": "section", "text": {"type": "plain_text", "text": message.text[i:i + 3000], "emoji": False}}
                      for i in range(0, len(message.text), 3000))
        payload = {"channel": self.settings.slack_channel, "text": encode_envelope(message), "blocks": blocks,
                   "parse": "none", "mrkdwn": False, "unfurl_links": False, "unfurl_media": False}
        if message.thread_ts:
            payload["thread_ts"] = message.thread_ts
        try:
            response = await self.client.post("https://slack.com/api/chat.postMessage", json=payload,
                                              headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"})
        except httpx.TimeoutException:
            raise HTTPException(504, "Slack timed out; delivery is uncertain. Check the channel before retrying.") from None
        except httpx.RequestError:
            raise HTTPException(502, "Slack request failed; delivery may be uncertain. Check the channel before retrying.") from None
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "1")
            retry = retry if re.fullmatch(r"[0-9]{1,6}", retry) else "1"
            raise HTTPException(429, "Slack rate limit reached", headers={"Retry-After": retry})
        if not response.is_success:
            raise HTTPException(502, "Slack request failed; delivery may be uncertain. Check the channel before retrying.")
        try:
            data = response.json()
        except ValueError:
            raise HTTPException(502, "Slack returned an invalid response; delivery is uncertain") from None
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise HTTPException(502, "Slack rejected the message; check app permissions and channel membership")
        ts = data.get("ts")
        if not isinstance(ts, str) or not re.fullmatch(SLACK_TS, ts) or data.get("channel") != self.settings.slack_channel:
            raise HTTPException(502, "Slack returned an unexpected message response; delivery is uncertain")
        return ts


def create_app(settings: Settings | None = None, *, http_client: httpx.AsyncClient | None = None,
               socket_factory: Callable = build_socket) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = MessageStore(settings.db_path)
        client = http_client or httpx.AsyncClient(timeout=15, follow_redirects=False)
        socket = None
        try:
            socket = socket_factory(settings, SlackReceiver(store, settings.slack_channel))
            app.state.store = store
            app.state.poster = SlackPoster(settings, client)
            app.state.socket = socket
            try:
                await asyncio.to_thread(socket.connect)
            except Exception:
                raise RuntimeError("Slack Socket Mode connection failed; check app configuration and connectivity") from None
            yield
        finally:
            if socket is not None:
                await asyncio.to_thread(socket.close)
            if http_client is None:
                await client.aclose()
            store.close()

    app = FastAPI(title="Superworkspace AgentBus", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    def authenticate(authorization: Annotated[str | None, Header()] = None) -> None:
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), settings.api_token.encode()):
            raise HTTPException(401, "Bearer authentication required", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/healthz")
    def health() -> dict:
        return {"status": "ok", "slack_connected": app.state.socket.is_connected()}

    @app.post("/v1/messages", dependencies=[Depends(authenticate)], response_model=Message, status_code=201)
    async def send(message: SendMessage) -> Message:
        ts = await app.state.poster.post(message)
        try:
            return await asyncio.to_thread(app.state.store.append, settings.slack_channel, ts, message)
        except (sqlite3.Error, OSError):
            raise HTTPException(503, "Message posted to Slack but local persistence failed; do not resend blindly") from None

    @app.get("/v1/messages", dependencies=[Depends(authenticate)], response_model=MessagePage)
    def read(after: Annotated[int, Query(ge=0)] = 0,
             recipient: Annotated[str | None, Query(pattern=IDENTIFIER)] = None,
             limit: Annotated[int, Query(ge=1, le=200)] = 100,
             thread_ts: Annotated[str | None, Query(pattern=SLACK_TS)] = None) -> MessagePage:
        return app.state.store.read(settings.slack_channel, after, recipient, limit, thread_ts)

    return app
