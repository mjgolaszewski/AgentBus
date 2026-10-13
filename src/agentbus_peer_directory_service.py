"""Bounded, source-labelled directory exchange between configured Slack peers."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from src.agentbus_message_assurance_service import route_parts

MAX_ENTRIES = 100
WIRE_KEY = "agentbus_directory"
BOT_ID = re.compile(r"B[A-Z0-9]+")


def wire(payload: dict) -> str:
    encoded = json.dumps({WIRE_KEY: 1, **payload}, ensure_ascii=True, separators=(",", ":"))
    for literal, escape in (("<", r"\u003c"), (">", r"\u003e"), ("&", r"\u0026")):
        encoded = encoded.replace(literal, escape)
    return encoded


def parse_wire(event: dict, channel: str, peers: dict[str, str]) -> tuple[str, str, dict] | None:
    """Only the Slack-supplied bot ID, not JSON text, selects a peer."""
    bot = event.get("bot_id")
    if (event.get("type") != "message" or event.get("channel") != channel
            or event.get("subtype") not in ("bot_message", "thread_broadcast")
            or event.get("hidden") or not isinstance(bot, str)):
        return None
    prefix = next((name for name, value in peers.items() if value == bot), None)
    if prefix is None:
        return None
    try:
        payload = json.loads(event.get("text", ""))
    except (TypeError, ValueError):
        return None
    if (not isinstance(payload, dict) or payload.get(WIRE_KEY) != 1
            or payload.get("kind") not in {"request", "snapshot"}
            or set(payload) - {WIRE_KEY, "kind", "nonce", "entries", "complete"}):
        return None
    try:
        uuid.UUID(payload.get("nonce", ""))
    except (ValueError, AttributeError, TypeError):
        return None
    return bot, prefix, payload


def missing_origin_peer(event: dict, channel: str,
                        peers: dict[str, str]) -> tuple[str, str] | None:
    """Recognize an old peer envelope for diagnostics, never for routing."""
    bot = event.get("bot_id")
    if (event.get("type") != "message" or event.get("channel") != channel
            or event.get("subtype") not in ("bot_message", "thread_broadcast")
            or event.get("hidden") or not isinstance(bot, str)):
        return None
    prefix = next((name for name, value in peers.items() if value == bot), None)
    if prefix is None:
        return None
    try:
        envelope = json.loads(event.get("text", ""))
    except (TypeError, ValueError):
        return None
    if (not isinstance(envelope, dict) or envelope.get("agentbus") not in (1, 2)
            or "origin_assurance" in envelope):
        return None
    sender = envelope.get("sender")
    parts = route_parts(sender) if isinstance(sender, str) else None
    return (bot, prefix) if parts is not None and parts[0] == prefix else None


class PeerDirectoryStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self._db, self._lock = db, lock
        with lock, db:
            db.execute("""CREATE TABLE IF NOT EXISTS peer_directory (
                source_bot_id TEXT NOT NULL,
                source_prefix TEXT NOT NULL,
                route TEXT NOT NULL,
                display_name TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY(source_bot_id, route)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS peer_directory_snapshots (
                source_bot_id TEXT PRIMARY KEY,
                source_prefix TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                complete INTEGER NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS peer_directory_requests (
                requester TEXT PRIMARY KEY,
                requested_at TEXT NOT NULL,
                nonce TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS peer_origin_compatibility (
                source_bot_id TEXT PRIMARY KEY,
                source_prefix TEXT NOT NULL,
                last_missing_origin_at TEXT NOT NULL
            )""")

    def record_missing_origin(self, bot: str, prefix: str, observed_at: str) -> None:
        with self._lock, self._db:
            self._db.execute("""INSERT INTO peer_origin_compatibility VALUES (?, ?, ?)
                ON CONFLICT(source_bot_id) DO UPDATE SET
                    source_prefix=excluded.source_prefix,
                    last_missing_origin_at=MAX(last_missing_origin_at, excluded.last_missing_origin_at)""",
                (bot, prefix, observed_at))

    def claim_request(self, requester: str, nonce: str, *, interval_seconds: int = 30) -> bool:
        """Bound local refreshes and replies without process-local rate memory."""
        now = datetime.now(timezone.utc)
        with self._lock, self._db:
            row = self._db.execute("SELECT requested_at FROM peer_directory_requests WHERE requester = ?",
                                   (requester,)).fetchone()
            if row and now - datetime.fromisoformat(row["requested_at"]) < timedelta(seconds=interval_seconds):
                return False
            self._db.execute("""INSERT INTO peer_directory_requests VALUES (?, ?, ?)
                ON CONFLICT(requester) DO UPDATE SET requested_at=excluded.requested_at,
                    nonce=excluded.nonce""", (requester, now.isoformat(), nonce))
            return True

    def local_entries(self, prefix: str) -> tuple[list[dict], bool]:
        with self._lock:
            rows = self._db.execute("""
                SELECT DISTINCT c.current_route, c.display_name
                FROM chat_identities AS c
                JOIN participation_sessions AS s ON s.chat_id = c.chat_id
                WHERE s.state != 'stopped' AND c.current_route LIKE ?
                ORDER BY c.current_route LIMIT ?
            """, (prefix + ":%", MAX_ENTRIES + 1)).fetchall()
        entries = [{"route": row["current_route"], "display_name": row["display_name"]}
                   for row in rows[:MAX_ENTRIES]
                   if (parts := route_parts(row["current_route"])) is not None and parts[0] == prefix]
        return entries, len(rows) <= MAX_ENTRIES

    def apply_snapshot(self, bot: str, prefix: str, payload: dict) -> bool:
        entries = payload.get("entries")
        complete = payload.get("complete")
        if (payload.get("kind") != "snapshot" or not isinstance(entries, list)
                or len(entries) > MAX_ENTRIES or not isinstance(complete, bool)):
            return False
        routes: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"route", "display_name"}:
                return False
            route, name = entry["route"], entry["display_name"]
            if (not isinstance(route, str) or route in routes
                    or (parts := route_parts(route)) is None or parts[0] != prefix
                    or not isinstance(name, str) or not name or len(name) > 120):
                return False
            routes.add(route)
        observed = datetime.now(timezone.utc).isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM peer_directory WHERE source_bot_id = ?", (bot,))
            self._db.executemany(
                "INSERT INTO peer_directory VALUES (?, ?, ?, ?, ?)",
                [(bot, prefix, entry["route"], entry["display_name"], observed) for entry in entries],
            )
            self._db.execute("""INSERT INTO peer_directory_snapshots VALUES (?, ?, ?, ?)
                ON CONFLICT(source_bot_id) DO UPDATE SET source_prefix=excluded.source_prefix,
                    observed_at=excluded.observed_at, complete=excluded.complete""",
                (bot, prefix, observed, int(complete)),
            )
        return True

    def list(self, local_prefix: str | None, configured: dict[str, str]) -> dict:
        local, local_complete = self.local_entries(local_prefix) if local_prefix else ([], True)
        with self._lock:
            rows = self._db.execute("SELECT * FROM peer_directory ORDER BY source_prefix, route").fetchall()
            snapshots = {row["source_bot_id"]: row for row in self._db.execute(
                "SELECT * FROM peer_directory_snapshots")}
            old_origins = {row["source_bot_id"]: row for row in self._db.execute(
                "SELECT * FROM peer_origin_compatibility")}
        entries = [dict(entry, source_prefix=local_prefix, source="local") for entry in local]
        sources = [{"prefix": local_prefix, "source": "local", "complete": local_complete}]
        for prefix, bot in sorted(configured.items()):
            snapshot = snapshots.get(bot)
            if snapshot is not None and snapshot["source_prefix"] != prefix:
                snapshot = None
            sources.append({"prefix": prefix, "source": "peer", "bot_id": bot,
                            "observed_at": snapshot["observed_at"] if snapshot else None,
                            "complete": bool(snapshot["complete"]) if snapshot else None,
                            "last_missing_origin_at": (
                                old_origins[bot]["last_missing_origin_at"]
                                if bot in old_origins and old_origins[bot]["source_prefix"] == prefix else None)})
            if snapshot and snapshot["source_prefix"] == prefix:
                entries.extend({"route": row["route"], "display_name": row["display_name"],
                                "source_prefix": prefix, "source": "peer",
                                "observed_at": row["observed_at"]}
                               for row in rows if row["source_bot_id"] == bot)
        return {"agents": entries, "sources": sources}
