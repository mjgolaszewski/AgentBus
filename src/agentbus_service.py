"""An authenticated Slack-backed message log with service-derived sender assurance."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Callable, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError, model_validator

from src.agentbus_claim_recovery_service import ClaimRecoveryStore
from src.agentbus_control_store_service import ControlStore
from src.agentbus_message_authorization_service import require_message_route
from src.agentbus_participation_api_service import (
    UPGRADE_NOTICE,
    api_claim_recovery,
    api_control_ack,
    api_control_issue,
    api_control_status,
    api_control_stop,
    api_policy_effective,
    api_policy_set,
    api_profile_handoff,
    api_rotation_grant,
    api_session_check_in,
    api_session_enroll,
    api_session_policy_ack,
    api_session_policy_explain,
    api_session_presence,
    api_session_rename,
    api_session_roster,
    api_session_rotate_secret,
    api_session_self_presence,
    authenticate_operator,
    upgrade_notice,
)
from src.agentbus_participation_store_service import ParticipationStore, StoppedMessageSession
from src.agentbus_presentation_service import visible_text
from src.agentbus_reply_policy_service import validate_reply
from src.agentbus_request_limits_service import RequestBodyLimit
from src.agentbus_send_policy_service import SessionAuthorityError, validate_new_send
from src.agentbus_send_rate_service import SendRateExceeded, SendRateStore
from src.agentbus_settings_service import Settings
from src.agentbus_slack_socket_service import SlackReceiver, build_socket
from src.agentbus_status_api_service import api_health, api_status

LOGGER = logging.getLogger("agentbus")
IDENTIFIER = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
SLACK_TS = r"^[0-9]{1,20}\.[0-9]{1,20}$"
MAX_TEXT = 6000
ACTIONABLE_KINDS = {"question", "request", "blocker", "handoff"}


def encode_envelope(message: SendMessage) -> str:
    """Keep Slack's link/mention processing out of the machine-readable fallback."""
    encoded = json.dumps({"agentbus": 2, **message.model_dump(include=set(SendMessage.model_fields))},
                         ensure_ascii=True, separators=(",", ":"))
    for literal, escape in (("<", r"\u003c"), (">", r"\u003e"), ("&", r"\u0026"), ("/", r"\/")):
        encoded = encoded.replace(literal, escape)
    return encoded


class SendMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    _sender_assurance: str = PrivateAttr(default="legacy")
    _authored_fields: frozenset[str] = PrivateAttr(default_factory=frozenset)
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
        self._authored_fields = self._authored_fields or frozenset(self.model_fields_set)
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
    sender_assurance: Literal["session", "slack-human", "legacy"] = "legacy"


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
    upgrade_notice: str | None = Field(default=None, exclude_if=lambda value: value is None)


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
    upgrade_notice: str | None = Field(default=None, exclude_if=lambda value: value is None)


class MessageStore:
    """SQLite is shared by the HTTP event loop and Socket Mode's callback thread."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.close(descriptor)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False, timeout=5)
        self._db.row_factory = sqlite3.Row
        path.chmod(0o600)
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=FULL")
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                cursor INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                slack_ts TEXT NOT NULL,
                thread_ts TEXT,
                recipient TEXT NOT NULL,
                audience TEXT NOT NULL,
                received_at TEXT NOT NULL,
                payload TEXT NOT NULL,
                UNIQUE(channel, slack_ts)
            )
        """)
        columns = {
            row["name"] for row in self._db.execute("PRAGMA table_info(messages)").fetchall()
        }
        if "sender_assurance" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN sender_assurance TEXT NOT NULL DEFAULT 'legacy'")
        if "audience" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN audience TEXT")
            for row in self._db.execute("SELECT cursor, payload FROM messages").fetchall():
                message = SendMessage.model_validate_json(row["payload"])
                self._db.execute(
                    "UPDATE messages SET audience = ? WHERE cursor = ?",
                    (message.audience, row["cursor"]),
                )
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS legacy_upgrade_notices (
                channel TEXT NOT NULL,
                identity TEXT NOT NULL,
                PRIMARY KEY(channel, identity)
            )
        """)
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS slack_command_receipts (
                command_id TEXT PRIMARY KEY, received_at TEXT NOT NULL
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
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS messages_channel_cursor "
            "ON messages(channel, cursor)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS messages_inbox_route "
            "ON messages(channel, audience, recipient, cursor)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS messages_thread_cursor "
            "ON messages(channel, thread_ts, cursor)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS claims_inbox_identity "
            "ON claims(channel, identity, cursor)"
        )
        self._db.commit()
        self.participation = ParticipationStore(self._db, self._lock)
        self.send_rate = SendRateStore(self._db, self._lock)
        self.controls = ControlStore(self._db, self._lock, self.participation)
        self.claim_recovery = ClaimRecoveryStore(self._db, self._lock)

    def first_legacy_notice(self, channel: str, identity: str) -> bool:
        """Return an onboarding notice once per legacy inbox identity."""
        with self._lock, self._db:
            seen = self._db.execute(
                "SELECT 1 FROM legacy_upgrade_notices WHERE channel = ? AND identity = ?",
                (channel, identity),
            ).fetchone()
            if seen:
                return False
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO legacy_upgrade_notices(channel, identity) VALUES (?, ?)",
                (channel, identity),
            )
            return inserted.rowcount == 1

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        return Message(**json.loads(row["payload"]), cursor=row["cursor"],
                       slack_ts=row["slack_ts"], received_at=row["received_at"],
                       sender_assurance=row["sender_assurance"])

    def append(self, channel: str, slack_ts: str, message: SendMessage) -> Message:
        with self._lock, self._db:
            self._db.execute("""
                INSERT OR IGNORE INTO messages
                    (channel, slack_ts, thread_ts, recipient, audience, received_at, payload, sender_assurance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (channel, slack_ts, message.thread_ts, message.recipient,
                  message.audience, datetime.now(timezone.utc).isoformat(),
                  message.model_dump_json(), message._sender_assurance))
            if message._sender_assurance in {"session", "slack-human"}:
                self._db.execute(
                    "UPDATE messages SET sender_assurance = ?, payload = ?, recipient = ?, audience = ?, thread_ts = ? "
                    "WHERE channel = ? AND slack_ts = ? AND sender_assurance = 'legacy'",
                    (message._sender_assurance, message.model_dump_json(), message.recipient,
                     message.audience, message.thread_ts, channel, slack_ts),
                )
            row = self._db.execute("SELECT * FROM messages WHERE channel = ? AND slack_ts = ?",
                                   (channel, slack_ts)).fetchone()
            return self._message(row)

    def claim_slash_command(self, command_id: str) -> bool:
        """Admit one Slack invocation before its external side effects."""
        with self._lock, self._db:
            inserted = self._db.execute(
                "INSERT OR IGNORE INTO slack_command_receipts VALUES (?, ?)",
                (command_id, datetime.now(timezone.utc).isoformat()),
            )
            return inserted.rowcount == 1

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
        routes = self.participation.routes_for(identity)
        route_marks = ",".join("?" for _ in routes)
        clauses: list[str] = []
        params: list[str | int] = []
        if thread_ts is not None:
            # Thread lookup is selective even when this identity has a huge
            # unrelated backlog. Include the root by its Slack timestamp.
            source = (
                "(SELECT cursor FROM messages WHERE channel = ? AND thread_ts = ? AND cursor > ? "
                "UNION SELECT cursor FROM messages WHERE channel = ? AND slack_ts = ? AND cursor > ?) "
                "AS candidate JOIN messages AS m ON m.cursor = candidate.cursor"
            )
            params.extend((channel, thread_ts, after, channel, thread_ts, after))
            if not context:
                clauses.append(
                    f"((m.audience IN ('direct', 'informational') AND m.recipient IN ({route_marks})) "
                    "OR (m.audience = 'broadcast' AND m.recipient = 'all') "
                    f"OR c.identity IN ({route_marks}))"
                )
                params.extend((*routes, *routes))
        elif context:
            source = "messages AS m"
            clauses.extend(("m.channel = ?", "m.cursor > ?"))
            params.extend((channel, after))
        else:
            # Each arm has a selective index. A busy channel with many messages
            # for other chats should not force every poll to scan those rows.
            direct = ("SELECT cursor FROM messages WHERE channel = ? AND audience = 'direct' "
                      f"AND recipient IN ({route_marks}) AND cursor > ?")
            informational = ("SELECT cursor FROM messages WHERE channel = ? AND audience = 'informational' "
                             f"AND recipient IN ({route_marks}) AND cursor > ?")
            broadcast = (
                "SELECT cursor FROM messages WHERE channel = ? AND audience = 'broadcast' "
                "AND recipient = 'all' AND cursor > ?"
            )
            claimed = f"SELECT cursor FROM claims WHERE channel = ? AND identity IN ({route_marks}) AND cursor > ?"
            params.extend((channel, *routes, after, channel, *routes, after,
                           channel, after, channel, *routes, after))
            if thread_ts is None:
                # A page needs at most limit+1 from each disjoint route class.
                # Bound the candidate set even when this chat has a huge backlog.
                page_limit = limit + 1
                direct = f"SELECT cursor FROM ({direct} ORDER BY cursor LIMIT ?)"
                informational = f"SELECT cursor FROM ({informational} ORDER BY cursor LIMIT ?)"
                broadcast = f"SELECT cursor FROM ({broadcast} ORDER BY cursor LIMIT ?)"
                claimed = f"SELECT cursor FROM ({claimed} ORDER BY cursor LIMIT ?)"
                params = [channel, *routes, after, page_limit,
                          channel, *routes, after, page_limit,
                          channel, after, page_limit,
                          channel, *routes, after, page_limit]
            source = (
                f"({direct} UNION {informational} UNION {broadcast} UNION {claimed}) AS candidate "
                "JOIN messages AS m ON m.cursor = candidate.cursor"
            )
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            high = self._db.execute("SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel = ?",
                                    (channel,)).fetchone()[0]
            rows = self._db.execute(
                f"SELECT m.*, c.identity AS claim_identity FROM {source} "
                "LEFT JOIN claims AS c ON c.channel = m.channel AND c.cursor = m.cursor "
                + where + " ORDER BY m.cursor LIMIT ?",
                (*params, limit + 1),
            ).fetchall()
        visible: list[InboxMessage] = []
        for row in rows:
            message = self._message(row)
            claim = row["claim_identity"]
            addressed = message.audience in {"direct", "informational"} and message.recipient in routes
            actionable = addressed or message.audience == "broadcast" or claim in routes
            reason = ("addressed to this identity" if addressed
                      else "explicit broadcast" if message.audience == "broadcast"
                      else "claimed by this identity" if claim in routes
                      else f"claimed by {claim}" if claim
                      else f"addressed to {message.recipient}" if message.audience == "direct"
                      else "informational" if message.audience == "informational"
                      else "unrouted; claim before replying")
            visible.append(InboxMessage(**message.model_dump(), actionable=actionable,
                                        action_reason=reason))
        has_more = len(visible) > limit
        shown = visible[:limit]
        next_cursor = shown[-1].cursor if has_more else max(after, high)
        return InboxPage(messages=shown, next_cursor=next_cursor, has_more=has_more)

    def validate_reply(self, channel: str, message: SendMessage) -> None:
        validate_reply(self, channel, message)

    def message_by_ts(self, channel: str, slack_ts: str) -> Message | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM messages WHERE channel = ? AND slack_ts = ?",
                                   (channel, slack_ts)).fetchone()
            return self._message(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._db.close()


def normalize_event(event: dict, channel: str, trusted_bot_id: str) -> tuple[str, SendMessage] | None:
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
    trusted_envelope = (
        event.get("bot_id") == trusted_bot_id
        and event.get("subtype") in ("bot_message", "thread_broadcast")
    )
    if trusted_envelope:
        try:
            envelope = json.loads(text)
            if isinstance(envelope, dict) and envelope.get("agentbus") in (1, 2):
                version = envelope.pop("agentbus")
                if version == 1:
                    envelope.pop("audience", None)
                    envelope.pop("reply_to_cursor", None)
                envelope["thread_ts"] = thread_ts
                message = SendMessage.model_validate(envelope)
                if not message.sender.startswith("slack:"):
                    return ts, message
        except (ValueError, RecursionError):
            pass
    identity = event.get("bot_id") or event.get("user") or "unknown"
    identity = identity if isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,60}", identity) else "unknown"
    verified_human = (isinstance(event.get("user"), str)
                      and re.fullmatch(r"[UW][A-Z0-9]{2,59}", event["user"]) is not None
                      and not event.get("bot_id") and event.get("subtype") != "bot_message")
    text = text.encode("utf-8", errors="replace").decode("utf-8").strip()[:MAX_TEXT]
    try:
        message = SendMessage(sender=f"slack:{identity}", text=text, thread_ts=thread_ts,
                              audience="unrouted")
    except ValidationError:
        message = SendMessage(sender=f"slack:{identity}", text=text[:3000], thread_ts=thread_ts,
                              audience="unrouted")
    message._sender_assurance = "slack-human" if verified_human else "legacy"
    return ts, message


class SlackPoster:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings, self.client = settings, client

    async def authenticated_bot_id(self) -> str:
        try:
            response = await self.client.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": f"Bearer {self.settings.slack_bot_token}"},
            )
            data = response.json()
        except (httpx.HTTPError, ValueError):
            raise RuntimeError("Slack bot identity verification failed") from None
        bot_id = data.get("bot_id") if response.is_success and isinstance(data, dict) else None
        if (
            not isinstance(data, dict) or data.get("ok") is not True
            or not isinstance(bot_id, str) or not re.fullmatch(r"B[A-Z0-9]+", bot_id)
        ):
            raise RuntimeError("Slack bot identity verification failed")
        return bot_id

    async def post(self, message: SendMessage) -> str:
        label = f"{message.sender} → {message.recipient} · {message.kind} · {message.audience}"
        if message.repo:
            label += f" · {message.repo}"
        blocks: list[dict[str, object]] = [
            {"type": "context", "elements": [{"type": "plain_text", "text": label, "emoji": False}]}
        ]
        display = visible_text(message.text)
        blocks.extend({"type": "section", "text": {"type": "plain_text", "text": display[i:i + 3000], "emoji": False}}
                      for i in range(0, len(display), 3000))
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


async def api_send(request: Request, message: SendMessage,
                   x_agentbus_session_token: Annotated[str | None, Header()] = None) -> Message:
    settings = request.app.state.settings
    try:
        rate_key = validate_new_send(request.app.state.store, settings.slack_channel, message,
                                     x_agentbus_session_token)
        request.app.state.store.send_rate.reserve(rate_key)
    except KeyError:
        raise HTTPException(422, "reply parent does not exist in this inbox") from None
    except StoppedMessageSession as exc:
        raise HTTPException(423, str(exc)) from None
    except SendRateExceeded as exc:
        raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from None
    except SessionAuthorityError as exc:
        raise HTTPException(403, str(exc)) from None
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
    page = request.app.state.store.read(
        settings.slack_channel, after, recipient, limit, thread_ts
    )
    return page


def api_info(request: Request) -> Info:
    settings = request.app.state.settings
    info = request.app.state.store.info(settings.slack_channel)
    info.upgrade_notice = upgrade_notice(request)
    return info


def api_inbox(request: Request, identity: Annotated[str, Query(pattern=IDENTIFIER)],
              after: Annotated[int, Query(ge=0)] = 0,
              limit: Annotated[int, Query(ge=1, le=200)] = 100,
              thread_ts: Annotated[str | None, Query(pattern=SLACK_TS)] = None,
              context: bool = False,
              x_agentbus_session_token: Annotated[str | None, Header()] = None) -> InboxPage:
    settings = request.app.state.settings
    require_message_route(request.app.state.store, identity, x_agentbus_session_token)
    page = request.app.state.store.inbox(
        settings.slack_channel, identity, after, limit, thread_ts, context
    )
    if upgrade_notice(request) and request.app.state.store.first_legacy_notice(
        settings.slack_channel, identity,
    ):
        page.upgrade_notice = UPGRADE_NOTICE
    return page


def api_claim(request: Request, cursor: int, claim: ClaimRequest,
              x_agentbus_session_token: Annotated[str | None, Header()] = None) -> Claim:
    settings = request.app.state.settings
    require_message_route(request.app.state.store, claim.identity, x_agentbus_session_token)
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
API_OPERATIONS: dict[str, Callable[..., object]] = {
    "health": api_health,
    "status": api_status,
    "send": api_send,
    "read": api_read,
    "info": api_info,
    "inbox": api_inbox,
    "claim": api_claim,
    "policy_set": api_policy_set,
    "policy_effective": api_policy_effective,
    "session_enroll": api_session_enroll,
    "profile_handoff": api_profile_handoff,
    "rotation_grant": api_rotation_grant,
    "session_presence": api_session_presence,
    "session_self_presence": api_session_self_presence,
    "session_roster": api_session_roster,
    "session_policy_ack": api_session_policy_ack,
    "session_policy_explain": api_session_policy_explain,
    "session_check_in": api_session_check_in,
    "control_stop": api_control_stop,
    "control_issue": api_control_issue,
    "control_status": api_control_status,
    "control_ack": api_control_ack,
    "session_rename": api_session_rename,
    "session_rotate_secret": api_session_rotate_secret,
    "claim_recovery": api_claim_recovery,
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
            poster = SlackPoster(settings, client)
            trusted_bot_id = await poster.authenticated_bot_id()
            socket = socket_factory(
                settings, SlackReceiver(store, settings.slack_channel, trusted_bot_id, settings)
            )
            app.state.store = store
            app.state.poster = poster
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

    app = FastAPI(title="AgentBus", version="0.7.3", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.add_middleware(RequestBodyLimit)
    auth = [Depends(authenticate)]
    app.add_api_route("/healthz", API_OPERATIONS["health"], methods=["GET"])
    app.add_api_route("/v1/status", API_OPERATIONS["status"], methods=["GET"], dependencies=auth)
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
    app.add_api_route("/v1/messages/{cursor}/claim-recovery", API_OPERATIONS["claim_recovery"],
                      methods=["POST"], dependencies=[Depends(authenticate_operator)])
    app.add_api_route("/v1/policy/revisions", API_OPERATIONS["policy_set"], methods=["POST"],
                      dependencies=[Depends(authenticate_operator)], status_code=201)
    app.add_api_route("/v1/policy/effective", API_OPERATIONS["policy_effective"], methods=["GET"],
                      dependencies=auth)
    app.add_api_route("/v1/sessions", API_OPERATIONS["session_enroll"], methods=["POST"],
                      dependencies=auth, status_code=201)
    app.add_api_route("/v1/sessions/profile-handoffs", API_OPERATIONS["profile_handoff"],
                      methods=["POST"], dependencies=[Depends(authenticate_operator)], status_code=201)
    app.add_api_route("/v1/sessions/{session_id}/rotation-grants", API_OPERATIONS["rotation_grant"],
                      methods=["POST"], dependencies=[Depends(authenticate_operator)], status_code=201)
    app.add_api_route("/v1/sessions/{session_id}/rotate-secret", API_OPERATIONS["session_rotate_secret"],
                      methods=["POST"], dependencies=auth)
    app.add_api_route("/v1/sessions/{session_id}/presence", API_OPERATIONS["session_presence"],
                      methods=["GET"], dependencies=[Depends(authenticate_operator)])
    app.add_api_route("/v1/sessions/{session_id}/self-presence", API_OPERATIONS["session_self_presence"],
                      methods=["GET"], dependencies=auth)
    app.add_api_route("/v1/sessions/roster", API_OPERATIONS["session_roster"], methods=["GET"], dependencies=[Depends(authenticate_operator)])
    app.add_api_route("/v1/sessions/{session_id}/policy", API_OPERATIONS["session_policy_explain"],
                      methods=["GET"], dependencies=[Depends(authenticate_operator)])
    app.add_api_route("/v1/sessions/{session_id}/policy-ack", API_OPERATIONS["session_policy_ack"],
                      methods=["POST"], dependencies=auth)
    app.add_api_route("/v1/sessions/{session_id}/check-in", API_OPERATIONS["session_check_in"],
                      methods=["POST"], dependencies=auth)
    app.add_api_route("/v1/controls/stop", API_OPERATIONS["control_stop"], methods=["POST"],
                      dependencies=[Depends(authenticate_operator)], status_code=201)
    app.add_api_route("/v1/controls", API_OPERATIONS["control_issue"], methods=["POST"],
                      dependencies=[Depends(authenticate_operator)], status_code=201)
    app.add_api_route("/v1/controls/{control_id}", API_OPERATIONS["control_status"], methods=["GET"],
                      dependencies=[Depends(authenticate_operator)])
    app.add_api_route("/v1/controls/{control_id}/targets/{session_id}/ack", API_OPERATIONS["control_ack"],
                      methods=["POST"], dependencies=auth)
    app.add_api_route("/v1/sessions/{session_id}/rename", API_OPERATIONS["session_rename"],
                      methods=["POST"], dependencies=auth)

    return app
