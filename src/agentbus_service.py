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
from typing import Annotated, Callable, Literal
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
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
ACTIONABLE_KINDS = {"question", "request", "blocker", "handoff"}


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
    encoded = json.dumps({"agentbus": 2, **message.model_dump(include=set(SendMessage.model_fields))},
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
    audience: Literal["direct", "broadcast", "informational", "unrouted"] | None = None
    reply_to_cursor: int | None = Field(default=None, ge=1)

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
        if self.audience is None:
            if self.recipient != "all":
                self.audience = "direct"
            elif self.kind in ACTIONABLE_KINDS and "recipient" not in self.model_fields_set:
                raise ValueError("actionable messages require a recipient or explicit broadcast audience")
            else:
                self.audience = "broadcast"
        if self.audience == "direct" and self.recipient == "all":
            raise ValueError("direct messages require a named recipient")
        if self.audience == "broadcast" and self.recipient != "all":
            raise ValueError("broadcast messages must use recipient 'all'")
        if self.audience == "unrouted" and not self.sender.startswith("slack:"):
            raise ValueError("unrouted audience is reserved for Slack messages without routing metadata")
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


class InboxMessage(Message):
    actionable: bool
    action_reason: str


class InboxPage(BaseModel):
    messages: list[InboxMessage]
    next_cursor: int
    has_more: bool


class ClaimRequest(BaseModel):
    identity: str = Field(pattern=IDENTIFIER)


class Claim(BaseModel):
    cursor: int
    identity: str
    claimed_at: str


class Info(BaseModel):
    protocol_version: int = 2
    inbox_id: str
    channel: str
    high_water_cursor: int
    features: list[str]


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
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS claims (
                channel TEXT NOT NULL,
                cursor INTEGER NOT NULL,
                identity TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY(channel, cursor),
                FOREIGN KEY(cursor) REFERENCES messages(cursor)
            )
        """)
        self._db.execute("INSERT OR IGNORE INTO metadata(key, value) VALUES ('inbox_id', ?)",
                         (str(uuid.uuid4()),))
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

    def info(self, channel: str) -> Info:
        with self._lock:
            inbox_id = self._db.execute("SELECT value FROM metadata WHERE key = 'inbox_id'").fetchone()[0]
            high = self._db.execute("SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel = ?",
                                    (channel,)).fetchone()[0]
        return Info(inbox_id=inbox_id, channel=channel, high_water_cursor=high,
                    features=["profiles", "stateless-cursors", "explicit-routing",
                              "parent-replies", "legacy-claims"])

    def message(self, channel: str, cursor: int) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE channel = ? AND cursor = ?",
                                   (channel, cursor)).fetchone()
        return self._message(row) if row else None

    def claim(self, channel: str, cursor: int, identity: str) -> Claim:
        with self._lock, self._db:
            message = self.message(channel, cursor)
            if message is None:
                raise KeyError(cursor)
            if message.audience != "unrouted":
                raise ValueError("only unrouted messages can be claimed")
            now = datetime.now(timezone.utc).isoformat()
            self._db.execute("INSERT OR IGNORE INTO claims(channel, cursor, identity, claimed_at) VALUES (?, ?, ?, ?)",
                             (channel, cursor, identity, now))
            row = self._db.execute("SELECT cursor, identity, claimed_at FROM claims WHERE channel = ? AND cursor = ?",
                                   (channel, cursor)).fetchone()
        return Claim(**dict(row))

    def claimant(self, channel: str, cursor: int) -> str | None:
        with self._lock:
            row = self._db.execute("SELECT identity FROM claims WHERE channel = ? AND cursor = ?",
                                   (channel, cursor)).fetchone()
        return row[0] if row else None

    def inbox(self, channel: str, identity: str, after: int = 0, limit: int = 100,
              thread_ts: str | None = None, context: bool = False) -> InboxPage:
        clauses = ["channel = ?", "cursor > ?"]
        params: list[str | int] = [channel, after]
        if thread_ts is not None:
            clauses.append("(thread_ts = ? OR slack_ts = ?)")
            params.extend((thread_ts, thread_ts))
        with self._lock:
            high = self._db.execute("SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel = ?",
                                    (channel,)).fetchone()[0]
            rows = self._db.execute("SELECT * FROM messages WHERE " + " AND ".join(clauses) +
                                    " ORDER BY cursor", params).fetchall()
            claims = {row["cursor"]: row["identity"] for row in self._db.execute(
                "SELECT cursor, identity FROM claims WHERE channel = ?", (channel,)).fetchall()}
        visible: list[InboxMessage] = []
        for row in rows:
            message = self._message(row)
            claim = claims.get(message.cursor)
            actionable = (message.audience == "direct" and message.recipient == identity) or \
                         message.audience == "broadcast" or claim == identity
            reason = ("addressed to this identity" if message.audience == "direct" and message.recipient == identity
                      else "explicit broadcast" if message.audience == "broadcast"
                      else "claimed by this identity" if claim == identity
                      else f"claimed by {claim}" if claim
                      else f"addressed to {message.recipient}" if message.audience == "direct"
                      else "informational" if message.audience == "informational"
                      else "unrouted; claim before replying")
            if actionable or context or (message.audience == "informational" and message.recipient == identity):
                visible.append(InboxMessage(**message.model_dump(), actionable=actionable,
                                            action_reason=reason))
        has_more = len(visible) > limit
        shown = visible[:limit]
        next_cursor = shown[-1].cursor if has_more else max(after, high)
        return InboxPage(messages=shown, next_cursor=next_cursor, has_more=has_more)

    def validate_reply(self, channel: str, message: SendMessage) -> None:
        if message.reply_to_cursor is None:
            return
        parent = self.message(channel, message.reply_to_cursor)
        if parent is None:
            raise KeyError(message.reply_to_cursor)
        allowed = parent.sender == message.sender or parent.audience == "broadcast" or \
                  (parent.audience == "direct" and parent.recipient == message.sender) or \
                  (parent.audience == "unrouted" and self.claimant(channel, parent.cursor) == message.sender)
        if not allowed:
            raise PermissionError("sender is not an intended responder for the parent message")
        if message.recipient != parent.sender or message.audience != "direct":
            raise ValueError("replies must be direct messages to the parent sender")

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
        if isinstance(envelope, dict) and envelope.get("agentbus") in (1, 2):
            version = envelope.pop("agentbus")
            if version == 1:
                envelope.pop("audience", None)
                envelope.pop("reply_to_cursor", None)
            envelope["thread_ts"] = thread_ts
            return ts, SendMessage.model_validate(envelope)
    except (ValueError, RecursionError):
        pass
    identity = event.get("user") or event.get("bot_id") or "unknown"
    identity = identity if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identity) else "unknown"
    # Human messages can exceed the API's send limit, including escaped Unicode.
    text = text.encode("utf-8", errors="replace").decode("utf-8").strip()[:MAX_TEXT]
    try:
        return ts, SendMessage(sender=f"slack:{identity}", text=text, thread_ts=thread_ts,
                               audience="unrouted")
    except ValidationError:
        # Even 3000 astral Unicode characters fit as JSON surrogate pairs.
        return ts, SendMessage(sender=f"slack:{identity}", text=text[:3000], thread_ts=thread_ts,
                               audience="unrouted")


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
        label = f"{message.sender} → {message.recipient} · {message.kind} · {message.audience}"
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


def authenticate(request: Request,
                 authorization: Annotated[str | None, Header()] = None) -> None:
    """Authenticate every versioned API operation against the app's settings."""
    scheme, _, token = (authorization or "").partition(" ")
    expected = request.app.state.settings.api_token
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(401, "Bearer authentication required",
                            headers={"WWW-Authenticate": "Bearer"})


def api_health(request: Request) -> dict:
    return {"status": "ok", "slack_connected": request.app.state.socket.is_connected()}


async def api_send(request: Request, message: SendMessage) -> Message:
    if message.audience == "unrouted":
        raise HTTPException(422, "unrouted audience is reserved for Slack ingestion")
    settings = request.app.state.settings
    try:
        request.app.state.store.validate_reply(settings.slack_channel, message)
    except KeyError:
        raise HTTPException(422, "reply parent does not exist in this inbox") from None
    except PermissionError as exc:
        raise HTTPException(409, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    ts = await request.app.state.poster.post(message)
    try:
        return await asyncio.to_thread(
            request.app.state.store.append, settings.slack_channel, ts, message
        )
    except (sqlite3.Error, OSError):
        raise HTTPException(
            503, "Message posted to Slack but local persistence failed; do not resend blindly"
        ) from None


def api_read(request: Request, after: Annotated[int, Query(ge=0)] = 0,
             recipient: Annotated[str | None, Query(pattern=IDENTIFIER)] = None,
             limit: Annotated[int, Query(ge=1, le=200)] = 100,
             thread_ts: Annotated[str | None, Query(pattern=SLACK_TS)] = None) -> MessagePage:
    settings = request.app.state.settings
    return request.app.state.store.read(
        settings.slack_channel, after, recipient, limit, thread_ts
    )


def api_info(request: Request) -> Info:
    settings = request.app.state.settings
    return request.app.state.store.info(settings.slack_channel)


def api_inbox(request: Request, identity: Annotated[str, Query(pattern=IDENTIFIER)],
              after: Annotated[int, Query(ge=0)] = 0,
              limit: Annotated[int, Query(ge=1, le=200)] = 100,
              thread_ts: Annotated[str | None, Query(pattern=SLACK_TS)] = None,
              context: bool = False) -> InboxPage:
    settings = request.app.state.settings
    return request.app.state.store.inbox(
        settings.slack_channel, identity, after, limit, thread_ts, context
    )


def api_claim(request: Request, cursor: int, claim: ClaimRequest) -> Claim:
    settings = request.app.state.settings
    try:
        result = request.app.state.store.claim(settings.slack_channel, cursor, claim.identity)
    except KeyError:
        raise HTTPException(404, "message not found") from None
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    if result.identity != claim.identity:
        raise HTTPException(
            409,
            f"message already claimed by {result.identity}",
            headers={"X-AgentBus-Claimed-By": result.identity},
        )
    return result


# BCF inventories this closed population, and FastAPI registers these exact
# callables below. The governance inventory and runtime dispatch cannot drift.
API_OPERATIONS = {
    "health": api_health,
    "send": api_send,
    "read": api_read,
    "info": api_info,
    "inbox": api_inbox,
    "claim": api_claim,
}


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

    app = FastAPI(title="AgentBus", version="0.3.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    auth = [Depends(authenticate)]
    app.add_api_route("/healthz", API_OPERATIONS["health"], methods=["GET"])
    app.add_api_route("/v1/messages", API_OPERATIONS["send"], methods=["POST"],
                      dependencies=auth, response_model=Message, status_code=201)
    app.add_api_route("/v1/messages", API_OPERATIONS["read"], methods=["GET"],
                      dependencies=auth, response_model=MessagePage)
    app.add_api_route("/v1/info", API_OPERATIONS["info"], methods=["GET"],
                      dependencies=auth, response_model=Info)
    app.add_api_route("/v1/inbox", API_OPERATIONS["inbox"], methods=["GET"],
                      dependencies=auth, response_model=InboxPage)
    app.add_api_route("/v1/messages/{cursor}/claim", API_OPERATIONS["claim"], methods=["POST"],
                      dependencies=auth, response_model=Claim)

    return app
