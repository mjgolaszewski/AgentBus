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
MAX_PAYLOAD = 48_000
ALLOWED_DIRECTORY_FIELDS = {
    "agent_id", "display_name", "role", "protocol", "registration_state",
    "capabilities", "origin_revision", "workspace_label",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_id(value: str, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError(f"invalid federation {label}")
    return value


class FederationStore:
    """Local federation state, persisted in AgentBus's canonical SQLite store."""

    def __init__(self, db: sqlite3.Connection, lock: RLock,
                 *, trusted_origins: dict[str, str] | None = None):
        self._db, self._lock = db, lock
        self._trusted_origins = dict(trusted_origins or {})
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
            if "provider_ref" not in columns:
                self._db.execute("ALTER TABLE bridge_obligations ADD COLUMN provider_ref TEXT")
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
            self._db.execute("""
                CREATE TABLE IF NOT EXISTS federated_messages (
                    origin_instance_id TEXT NOT NULL,
                    origin_message_id TEXT NOT NULL,
                    destination_instance_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    PRIMARY KEY(origin_instance_id, origin_message_id, destination_instance_id)
                )
            """)

    def accept(self, *, origin_instance_id: str, origin_message_id: str,
               destination_instance_id: str, payload: bytes | str) -> dict[str, Any]:
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        _check_id(destination_instance_id, INSTANCE_RE, "destination instance")
        if not isinstance(origin_message_id, str) or not 1 <= len(origin_message_id) <= 128:
            raise ValueError("invalid federation origin message")
        raw = payload.encode() if isinstance(payload, str) else payload
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PAYLOAD:
            raise ValueError("federation payload exceeds limit")
        try:
            encoded = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("federation payload must be UTF-8") from exc
        digest = hashlib.sha256(raw).hexdigest()
        now = _now()
        with self._lock, self._db:
            self._db.execute("""
                INSERT OR IGNORE INTO bridge_obligations
                (origin_instance_id, origin_message_id, destination_instance_id, state, payload, payload_hash, updated_at)
                VALUES (?, ?, ?, 'origin_accepted', ?, ?, ?)
            """, (origin_instance_id, origin_message_id, destination_instance_id, encoded, digest, now))
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

    def bind_origin(self, *, origin_instance_id: str, publisher_id: str) -> None:
        """Install an operator-owned publisher binding for one origin namespace."""
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        if not isinstance(publisher_id, str) or not publisher_id.strip():
            raise ValueError("invalid federation publisher")
        existing = self._trusted_origins.get(origin_instance_id)
        if existing is not None and existing != publisher_id:
            raise ValueError("conflicting origin publisher binding")
        self._trusted_origins[origin_instance_id] = publisher_id

    def verify_origin(self, *, origin_instance_id: str, publisher_id: str) -> bool:
        """Return whether a provider publisher is authorized for an origin."""
        return self._trusted_origins.get(origin_instance_id) == publisher_id

    def accept_destination(self, *, origin_instance_id: str, publisher_id: str,
                           origin_message_id: str, destination_instance_id: str,
                           payload: bytes | str) -> dict[str, Any]:
        """Persist one authenticated destination message exactly once."""
        if not self.verify_origin(origin_instance_id=origin_instance_id,
                                  publisher_id=publisher_id):
            raise ValueError("untrusted federation origin publisher")
        _check_id(destination_instance_id, INSTANCE_RE, "destination instance")
        raw = payload.encode() if isinstance(payload, str) else payload
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_PAYLOAD:
            raise ValueError("federation payload exceeds limit")
        try:
            encoded = raw.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise ValueError("federation payload must be UTF-8") from exc
        digest = hashlib.sha256(raw).hexdigest()
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        if not isinstance(origin_message_id, str) or not 1 <= len(origin_message_id) <= 128:
            raise ValueError("invalid federation origin message")
        with self._lock, self._db:
            self._db.execute(
                """INSERT OR IGNORE INTO federated_messages
                (origin_instance_id, origin_message_id, destination_instance_id,
                 payload, payload_hash, received_at)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (origin_instance_id, origin_message_id, destination_instance_id,
                 encoded, digest, _now()),
            )
            row = self._db.execute(
                """SELECT * FROM federated_messages
                WHERE origin_instance_id=? AND origin_message_id=?
                  AND destination_instance_id=?""",
                (origin_instance_id, origin_message_id, destination_instance_id),
            ).fetchone()
        if row["payload_hash"] != digest:
            raise ValueError("conflicting destination federation payload")
        return dict(row)

    def publish_registration(self, *, origin_instance_id: str, route: str,
                             revision: int, registration_state: str,
                             metadata: dict[str, Any], share: bool = True) -> dict[str, Any] | None:
        """Project committed local participation state into the remote directory.

        Joining and unknown states are deliberately not advertised.  A stopped
        or withdrawn registration produces an explicit withdrawal so a stale
        provider copy cannot be mistaken for a live agent.
        """
        if not share:
            return None
        if registration_state in {"joining", "unknown", "stale"}:
            return None
        if registration_state in {"stopped", "withdrawn"}:
            return self.apply_directory(origin_instance_id=origin_instance_id,
                                        route=route, revision=revision,
                                        state="withdrawn", metadata={})
        if registration_state != "active":
            raise ValueError("invalid registration state")
        projected = dict(metadata)
        projected["registration_state"] = "active"
        return self.apply_directory(origin_instance_id=origin_instance_id,
                                    route=route, revision=revision,
                                    state="current", metadata=projected)

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
        if set(metadata) - ALLOWED_DIRECTORY_FIELDS:
            raise ValueError("directory metadata contains unapproved authority fields")
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

    def directory_snapshot(self, *, origin_instance_id: str, revision: int,
                           entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Apply a complete owner snapshot without deleting on malformed input."""
        if not isinstance(entries, list) or len(entries) > 256:
            raise ValueError("directory snapshot exceeds limit")
        validated = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError("invalid directory snapshot entry")
            validated.append(self.apply_directory(
                origin_instance_id=origin_instance_id,
                route=entry["route"], revision=revision,
                state=entry.get("state", "current"),
                metadata=entry.get("metadata", {})))
        return validated

    def mark_directory_gap(self, *, origin_instance_id: str, route: str,
                           revision: int) -> dict[str, Any]:
        """Record an explicit gap; never infer withdrawal from missing pages."""
        return self.apply_directory(origin_instance_id=origin_instance_id,
                                     route=route, revision=revision,
                                     state="gap", metadata={})

    def apply_directory_delta(self, *, origin_instance_id: str, route: str,
                              revision: int, state: str,
                              metadata: dict[str, Any]) -> dict[str, Any]:
        """Apply one contiguous delta or record a bounded resync gap."""
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        _check_id(route, ROUTE_RE, "route")
        existing = self._db.execute(
            "SELECT revision FROM remote_directory WHERE origin_instance_id=? AND route=?",
            (origin_instance_id, route),
        ).fetchone()
        if existing is not None and revision > existing["revision"] + 1:
            return self.mark_directory_gap(origin_instance_id=origin_instance_id,
                                           route=route, revision=revision)
        return self.apply_directory(origin_instance_id=origin_instance_id,
                                    route=route, revision=revision,
                                    state=state, metadata=metadata)

    def directory(self, *, origin_instance_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM remote_directory"
        args: tuple[str, ...] = ()
        if origin_instance_id is not None:
            _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
            query += " WHERE origin_instance_id=?"
            args = (origin_instance_id,)
        query += " ORDER BY origin_instance_id, route"
        rows = self._db.execute(query, args).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["metadata"] = json.loads(item["metadata"])
            result.append(item)
        return result

    def obligation(self, origin_instance_id: str, origin_message_id: str, destination_instance_id: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM bridge_obligations WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?", (origin_instance_id, origin_message_id, destination_instance_id)).fetchone()
        return dict(row) if row else None

