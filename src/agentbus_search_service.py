"""Scoped retained-text search and atomic SQLite index ownership."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from typing import Annotated

from fastapi import HTTPException, Query, Request

from src.agentbus_channel_api_service import resolve_principal, serialize_message
from src.agentbus_transaction_service import atomic


def require_fts5() -> None:
    """Probe the selected SQLite runtime before creating any persistent state."""
    connection = sqlite3.connect(":memory:")
    try:
        if connection.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0] != 1:
            raise RuntimeError("SQLite FTS5 is required before mailbox migration")
    finally:
        connection.close()


class SearchStore:
    def __init__(self, owner):
        self.owner = owner
        db = owner._db
        with owner._lock:
            if db.execute("SELECT sqlite_compileoption_used('ENABLE_FTS5')").fetchone()[0] != 1:
                raise RuntimeError("SQLite FTS5 is required before mailbox migration")
            with atomic(db):
                exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='mailbox_search'").fetchone()
                db.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS mailbox_search USING fts5(text,channel UNINDEXED,message_id UNINDEXED)"
                )
                db.execute("""CREATE TRIGGER IF NOT EXISTS mailbox_search_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO mailbox_search(rowid,text,channel,message_id)
                    VALUES(new.cursor,json_extract(new.payload,'$.text'),new.channel,new.cursor); END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS mailbox_search_delete AFTER DELETE ON messages BEGIN
                    DELETE FROM mailbox_search WHERE rowid=old.cursor; END""")
                db.execute("""CREATE TRIGGER IF NOT EXISTS mailbox_search_update AFTER UPDATE OF payload ON messages BEGIN
                    DELETE FROM mailbox_search WHERE rowid=old.cursor;
                    INSERT INTO mailbox_search(rowid,text,channel,message_id)
                    VALUES(new.cursor,json_extract(new.payload,'$.text'),new.channel,new.cursor); END""")
                if not exists:
                    db.execute(
                        "INSERT INTO mailbox_search(rowid,text,channel,message_id) SELECT cursor,json_extract(payload,'$.text'),channel,cursor FROM messages"
                    )

    def search(self, channel, query, *, after=0, limit=20):
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("Search requires1..256 characters")
        if type(limit) is not int or not 1 <= limit <= 100 or type(after) is not int or after < 0:
            raise ValueError("Invalid bounded search position or page size")
        literal = " AND ".join('"' + term.replace('"', '""') + '"' for term in query.split())
        with self.owner._lock:
            return self.owner._db.execute(
                """SELECT p.position FROM mailbox_search s
                JOIN channel_positions p ON p.message_id=s.message_id AND p.channel=s.channel
                WHERE mailbox_search MATCH ? AND s.channel=? AND p.position>?
                ORDER BY p.position LIMIT ?""",
                (literal, channel, after, limit + 1),
            ).fetchall()


def search_token(secret, scope, query, position):
    raw = json.dumps([scope, hashlib.sha256(query.encode()).hexdigest(), position], separators=(",", ":")).encode()
    mac = hmac.digest(secret.encode(), raw, "sha256")
    return base64.urlsafe_b64encode(raw + mac).decode()


def decode_token(secret, scope, query, token):
    if not isinstance(token, str) or len(token) > 2048:
        raise ValueError("Invalid search continuation")
    try:
        data = base64.b64decode(token, altchars=b"-_", validate=True)
        raw, mac = data[:-32], data[-32:]
        expected = hmac.digest(secret.encode(), raw, "sha256")
        if not hmac.compare_digest(mac, expected):
            raise ValueError()
        binding, digest, position = json.loads(raw)
        if (
            binding != scope
            or digest != hashlib.sha256(query.encode()).hexdigest()
            or type(position) is not int
            or position < 0
        ):
            raise ValueError()
        return position
    except (ValueError, TypeError, UnicodeError):
        raise ValueError("Invalid search continuation") from None


def search_page(request, principal, query, limit, after):
    store = request.app.state.store
    scope = [principal.chat_id, principal.session_id, principal.channel, principal.inbox_id]
    secret = request.app.state.settings.api_token
    try:
        position = decode_token(secret, scope, query, after) if after else 0
        with store.snapshot():
            rows = store.search.search(principal.channel, query, after=position, limit=limit)
            more = len(rows) > limit
            positions = [row["position"] for row in rows[:limit]]
            messages = [
                serialize_message(
                    store,
                    principal.channel,
                    store.message(principal.channel, store.channels.internal_cursor(principal.channel, value)),
                )
                for value in positions
            ]
        return {
            "messages": messages,
            "has_more": more,
            "after": search_token(secret, scope, query, positions[-1]) if more else None,
        }
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def api_search(
    request: Request,
    query: Annotated[str, Query(min_length=1, max_length=256)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    after: str | None = None,
):
    return search_page(request, resolve_principal(request), query, limit, after)
