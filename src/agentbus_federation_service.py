"""Durable, provider-neutral federation primitives on the existing SQLite store.

This module owns local bridge obligations and remote-directory projections.  It
does not submit provider requests and it never grants local execution authority.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_METADATA = 4096


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid federation {label}")
    return value


class FederationStore:
    """Local federation state, persisted in AgentBus's canonical SQLite store."""

    def __init__(self, db: sqlite3.Connection, lock: RLock):
        self._db, self._lock = db, lock
        with self._lock, self._db:
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS bridge_obligations (
                    origin_instance_id TEXT NOT NULL,
                    origin_message_id TEXT NOT NULL,
                    destination_instance_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(origin_instance_id, origin_message_id, destination_instance_id)
                )
            """)
            columns = {row["name"] for row in self._db.execute("PRAGMA table_info(bridge_obligations)")}
            if "payload" not in columns:
                self._db.execute("ALTER TABLE bridge_obligations ADD COLUMN payload TEXT NOT NULL DEFAULT ''")
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS remote_directory (
                    origin_instance_id TEXT NOT NULL,
                    route TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    metadata TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(origin_instance_id, route)
                )
            """)

    def accept(self, *, origin_instance_id: str, origin_message_id: str,
               destination_instance_id: str, payload: bytes | str) -> dict[str, Any]:
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        _check_id(destination_instance_id, INSTANCE_RE, "destination instance")
        if not isinstance(origin_message_id, str) or not 1 <= len(origin_message_id) <= 128:
            raise ValueError("invalid federation origin message")
        raw = payload.encode() if isinstance(payload, str) else payload
        digest = hashlib.sha256(raw).hexdigest()
        now = _now()
        with self._lock, self._db:
            self._db.execute("""
                INSERT OR IGNORE INTO bridge_obligations
                (origin_instance_id, origin_message_id, destination_instance_id, state, payload, payload_hash, updated_at)
                VALUES (?, ?, ?, 'origin_accepted', ?, ?, ?)
            """, (origin_instance_id, origin_message_id, destination_instance_id, raw.decode("utf-8", "strict"), digest, now))
            row = self._db.execute("""
                SELECT origin_instance_id, origin_message_id, destination_instance_id,
                       state, payload, payload_hash, attempts, updated_at
                FROM bridge_obligations WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?
            """, (origin_instance_id, origin_message_id, destination_instance_id)).fetchone()
        if row["payload_hash"] != digest:
            raise ValueError("conflicting federation payload for obligation")
        return dict(row)

    def transition(self, origin_instance_id: str, origin_message_id: str,
                   destination_instance_id: str, state: str, *, attempt: bool = False) -> dict[str, Any]:
        allowed = {"origin_accepted", "submitted", "uncertain", "destination_accepted", "acknowledged"}
        if state not in allowed:
            raise ValueError("invalid bridge state")
        key = (origin_instance_id, origin_message_id, destination_instance_id)
        with self._lock, self._db:
            row = self._db.execute("SELECT state FROM bridge_obligations WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?", key).fetchone()
            if row is None:
                raise KeyError("unknown bridge obligation")
            current = row["state"]
            transitions = {
                "origin_accepted": {"origin_accepted", "submitted", "uncertain"},
                "submitted": {"submitted", "uncertain", "destination_accepted"},
                "uncertain": {"uncertain", "submitted", "destination_accepted"},
                "destination_accepted": {"destination_accepted", "acknowledged"},
                "acknowledged": {"acknowledged"},
            }
            if state not in transitions[current]:
                raise ValueError(f"invalid bridge transition {current} -> {state}")
            if state == "acknowledged" and current != "destination_accepted":
                raise ValueError("destination acceptance is required before acknowledgement")
            attempts = ", attempts = attempts + 1" if attempt else ""
            self._db.execute(f"UPDATE bridge_obligations SET state=?, updated_at=?{attempts} WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?", (state, _now(), *key))
            return dict(self._db.execute("SELECT * FROM bridge_obligations WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?", key).fetchone())

    def apply_directory(self, *, origin_instance_id: str, route: str, revision: int,
                        state: str, metadata: dict[str, Any]) -> dict[str, Any]:
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        _check_id(route, ROUTE_RE, "route")
        if not isinstance(revision, int) or revision < 1:
            raise ValueError("directory revision must be positive")
        if state not in {"current", "stale", "withdrawn", "gap", "resync_required"}:
            raise ValueError("invalid directory state")
        if not isinstance(metadata, dict) or len(json.dumps(metadata, separators=(",", ":"))) > MAX_METADATA:
            raise ValueError("directory metadata exceeds limit")
        forbidden = {"session_secret", "credential", "privilege", "execution", "wake"}
        if forbidden.intersection(metadata):
            raise ValueError("directory metadata contains authority fields")
        with self._lock, self._db:
            existing = self._db.execute("SELECT revision FROM remote_directory WHERE origin_instance_id=? AND route=?", (origin_instance_id, route)).fetchone()
            if existing and revision < existing["revision"]:
                return dict(self._db.execute("SELECT * FROM remote_directory WHERE origin_instance_id=? AND route=?", (origin_instance_id, route)).fetchone())
            if existing and revision == existing["revision"]:
                current = self._db.execute("SELECT state, metadata FROM remote_directory WHERE origin_instance_id=? AND route=?", (origin_instance_id, route)).fetchone()
                if current["state"] != state or json.loads(current["metadata"]) != metadata:
                    raise ValueError("conflicting directory revision")
                result = dict(self._db.execute("SELECT * FROM remote_directory WHERE origin_instance_id=? AND route=?", (origin_instance_id, route)).fetchone())
                result["metadata"] = json.loads(result["metadata"])
                return result
            self._db.execute("""
                INSERT INTO remote_directory(origin_instance_id, route, revision, state, metadata, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(origin_instance_id, route) DO UPDATE SET revision=excluded.revision,
                    state=excluded.state, metadata=excluded.metadata, updated_at=excluded.updated_at
            """, (origin_instance_id, route, revision, state, json.dumps(metadata, sort_keys=True), _now()))
            row = self._db.execute("SELECT * FROM remote_directory WHERE origin_instance_id=? AND route=?", (origin_instance_id, route)).fetchone()
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def obligation(self, origin_instance_id: str, origin_message_id: str, destination_instance_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM bridge_obligations WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?", (origin_instance_id, origin_message_id, destination_instance_id)).fetchone()
        return dict(row) if row else None

