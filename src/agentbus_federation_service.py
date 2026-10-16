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
from decimal import Decimal
from threading import RLock
from typing import Any

INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ROUTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$")
MAX_METADATA = 4096
MAX_PAYLOAD = 48_000
MAX_DIRECTORY_ROUTES_PER_ORIGIN = 1024
MAX_DIRECTORY_ORIGINS = 65
MAX_DIRECTORY_REVISION = 2 ** 63 - 1
SIGNED_FRAME_FIELDS = {
    "agentbus_federation", "kind", "origin_instance_id", "origin_message_id",
    "destination_instance_id", "key_generation", "payload", "signature",
}
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


def _looks_like_signed_receipt_frame(value: object) -> bool:
    """Recognize the complete persisted wire envelope without verifying it.

    Migration has no provider binding with which to verify a historical
    signature. It must nevertheless require the strict envelope shape so an
    ordinary application payload containing ``kind=receipt`` is never hidden
    by receipt retry policy.
    """
    if not isinstance(value, dict) or set(value) != SIGNED_FRAME_FIELDS:
        return False
    return (
        value.get("agentbus_federation") == 1
        and value.get("kind") == "receipt"
        and isinstance(value.get("origin_instance_id"), str)
        and isinstance(value.get("origin_message_id"), str)
        and isinstance(value.get("destination_instance_id"), str)
        and isinstance(value.get("key_generation"), str)
        and re.fullmatch(r"[0-9a-f]{64}", value["key_generation"]) is not None
        and isinstance(value.get("payload"), str) and bool(value["payload"])
        and isinstance(value.get("signature"), str) and bool(value["signature"])
    )


class DirectoryCapacityError(RuntimeError):
    """Directory allocation needs operator archival; existing rows are retained."""


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
            if "kind" not in columns:
                self._db.execute("ALTER TABLE bridge_obligations ADD COLUMN kind TEXT NOT NULL DEFAULT 'message'")
            # Older installations persisted receipt frames without a frame-kind
            # column. Recover that distinction before the worker projects them
            # into the new retry policy. Repeat this scan on every construction so
            # a process restart can complete classification after an interrupted
            # migration; only a signed federation frame shape qualifies as receipt.
            legacy = self._db.execute(
                "SELECT origin_instance_id, origin_message_id, destination_instance_id, payload "
                "FROM bridge_obligations WHERE kind='message'"
            ).fetchall()
            for row in legacy:
                try:
                    frame = json.loads(row["payload"])
                    is_receipt = _looks_like_signed_receipt_frame(frame)
                except (RecursionError, TypeError, ValueError):
                    is_receipt = False
                if is_receipt:
                    self._db.execute(
                        "UPDATE bridge_obligations SET kind='receipt' WHERE origin_instance_id=? "
                        "AND origin_message_id=? AND destination_instance_id=?",
                        (row["origin_instance_id"], row["origin_message_id"],
                         row["destination_instance_id"]),
                    )
            if "provider_ref" not in columns:
                self._db.execute("ALTER TABLE bridge_obligations ADD COLUMN provider_ref TEXT")
            if "provider_failure" not in columns:
                self._db.execute("ALTER TABLE bridge_obligations ADD COLUMN provider_failure TEXT")
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
            self._db.execute("""CREATE TABLE IF NOT EXISTS remote_directory_revisions (
                origin_instance_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                gap_revision INTEGER NOT NULL, snapshot_hash TEXT NOT NULL)""")
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
            self._db.execute("""CREATE TABLE IF NOT EXISTS federation_origin_messages (
                origin_instance_id TEXT NOT NULL, origin_message_id TEXT NOT NULL,
                payload TEXT NOT NULL, payload_hash TEXT NOT NULL,
                PRIMARY KEY(origin_instance_id, origin_message_id))""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS federation_directory_local (
                origin_instance_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                entries TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS federation_directory_requests (
                origin_instance_id TEXT PRIMARY KEY, request_id TEXT NOT NULL,
                next_response_at REAL NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS federation_directory_outgoing_requests (
                origin_instance_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                gap_revision INTEGER NOT NULL, request_id TEXT NOT NULL)""")
            self._db.execute("""CREATE TABLE IF NOT EXISTS federation_checkpoints (
                reader TEXT PRIMARY KEY, cursor TEXT NOT NULL)""")
            columns = {row['name'] for row in self._db.execute('PRAGMA table_info(bridge_obligations)')}
            if 'next_attempt_at' not in columns:
                self._db.execute('ALTER TABLE bridge_obligations ADD COLUMN next_attempt_at REAL NOT NULL DEFAULT 0')

    def accept(self, *, origin_instance_id: str, origin_message_id: str,
               destination_instance_id: str, payload: bytes | str,
               local_payload: str | None = None,
               kind: str = "message") -> dict[str, Any]:
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        _check_id(destination_instance_id, INSTANCE_RE, "destination instance")
        if not isinstance(origin_message_id, str) or not 1 <= len(origin_message_id) <= 128:
            raise ValueError("invalid federation origin message")
        if kind not in {"message", "receipt", "directory"}:
            raise ValueError("invalid federation frame kind")
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
            local = encoded if local_payload is None else local_payload
            local_hash = hashlib.sha256(local.encode()).hexdigest()
            self._db.execute('INSERT OR IGNORE INTO federation_origin_messages VALUES (?, ?, ?, ?)',
                             (origin_instance_id, origin_message_id, local, local_hash))
            existing = self._db.execute('SELECT payload_hash FROM federation_origin_messages WHERE origin_instance_id=? AND origin_message_id=?',
                                        (origin_instance_id, origin_message_id)).fetchone()
            if existing['payload_hash'] != local_hash:
                raise ValueError('conflicting federation payload for origin message')
            self._db.execute("""
                INSERT OR IGNORE INTO bridge_obligations
                (origin_instance_id, origin_message_id, destination_instance_id, state, payload, payload_hash, updated_at, kind)
                VALUES (?, ?, ?, 'origin_accepted', ?, ?, ?, ?)
            """, (origin_instance_id, origin_message_id, destination_instance_id, encoded, digest, now, kind))
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
        allowed = {"origin_accepted", "submitted", "uncertain", "provider_blocked", "destination_accepted", "acknowledged"}
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
                "provider_blocked": {"provider_blocked", "destination_accepted"},
                "destination_accepted": {"destination_accepted", "acknowledged"},
                "acknowledged": {"acknowledged"},
            }
            if state not in transitions[current]:
                raise ValueError(f"invalid bridge transition {current} -> {state}")
            if state == "acknowledged" and current not in {"destination_accepted", "acknowledged"}:
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

    def _directory_entry(self, origin: str, route: str, revision: int,
                         state: str, metadata: dict[str, Any]) -> None:
        _check_id(origin, INSTANCE_RE, "origin instance")
        _check_id(route, ROUTE_RE, "route")
        if type(revision) is not int or not 1 <= revision <= MAX_DIRECTORY_REVISION:
            raise ValueError("directory revision must be positive")
        if not isinstance(state, str) or state not in {"current", "stale", "withdrawn", "gap", "resync_required"}:
            raise ValueError("invalid directory state")
        if not isinstance(metadata, dict) or len(json.dumps(metadata, separators=(",", ":"), allow_nan=False)) > MAX_METADATA:
            raise ValueError("directory metadata exceeds limit")
        if set(metadata) - ALLOWED_DIRECTORY_FIELDS:
            raise ValueError("directory metadata contains unapproved authority fields")
        if state == "current" and metadata.get("registration_state", "active") != "active":
            raise ValueError("non-active registration cannot be current")

    def _directory_row(self, origin: str, route: str) -> dict[str, Any] | None:
        row = self._db.execute("SELECT * FROM remote_directory WHERE origin_instance_id=? AND route=?",
                               (origin, route)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        return result

    def _directory_origin_capacity(self, origin: str) -> None:
        known = self._db.execute("""SELECT 1 FROM remote_directory WHERE origin_instance_id=?
            UNION SELECT 1 FROM remote_directory_revisions WHERE origin_instance_id=? LIMIT 1""",
            (origin, origin)).fetchone()
        if known:
            return
        origins = self._db.execute("""SELECT COUNT(*) FROM (
            SELECT origin_instance_id FROM remote_directory UNION
            SELECT origin_instance_id FROM remote_directory_revisions)""").fetchone()[0]
        if origins >= MAX_DIRECTORY_ORIGINS:
            raise DirectoryCapacityError("directory origin capacity reached; archival requires operator reconciliation")

    def _directory_capacity(self, origin: str) -> None:
        routes = self._db.execute("SELECT COUNT(*) FROM remote_directory WHERE origin_instance_id=?",
                                  (origin,)).fetchone()[0]
        if routes >= MAX_DIRECTORY_ROUTES_PER_ORIGIN:
            raise DirectoryCapacityError("directory route capacity reached; archival requires operator reconciliation")
        self._directory_origin_capacity(origin)

    def _write_directory(self, origin: str, route: str, revision: int, state: str,
                         metadata: dict[str, Any], *, resync: bool = False) -> dict[str, Any]:
        existing = self._directory_row(origin, route)
        if existing is None:
            self._directory_capacity(origin)
        if existing and revision < existing["revision"]:
            return existing
        if existing and revision == existing["revision"]:
            if existing["state"] == state and existing["metadata"] == metadata:
                return existing
            if not (resync and existing["state"] in {"gap", "resync_required"}):
                raise ValueError("conflicting directory revision")
        self._db.execute("""INSERT INTO remote_directory VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(origin_instance_id, route) DO UPDATE SET revision=excluded.revision,
                state=excluded.state, metadata=excluded.metadata, updated_at=excluded.updated_at""",
            (origin, route, revision, state, json.dumps(metadata, sort_keys=True), _now()))
        result = self._directory_row(origin, route)
        assert result is not None
        return result

    def _directory_revision(self, origin: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM remote_directory_revisions WHERE origin_instance_id=?",
                               (origin,)).fetchone()
        if row:
            return dict(row)
        # Existing projection rows are retained on upgrade. An inherited gap is
        # never promoted to a contiguous accepted stream by reconstruction.
        rows = self.directory(origin_instance_id=origin)
        revision = max((r["revision"] for r in rows if r["state"] not in {"gap", "resync_required"}), default=0)
        gap = max((r["revision"] for r in rows if r["state"] in {"gap", "resync_required"}), default=0)
        return {"revision": revision, "gap_revision": gap, "snapshot_hash": ""}

    def _save_directory_revision(self, origin: str, revision: int, gap: int,
                                 snapshot_hash: str = "") -> None:
        if self._db.execute("SELECT 1 FROM remote_directory_revisions WHERE origin_instance_id=?", (origin,)).fetchone() is None:
            self._directory_origin_capacity(origin)
        self._db.execute("""INSERT INTO remote_directory_revisions VALUES (?, ?, ?, ?)
            ON CONFLICT(origin_instance_id) DO UPDATE SET revision=excluded.revision,
                gap_revision=excluded.gap_revision, snapshot_hash=excluded.snapshot_hash""",
            (origin, revision, gap, snapshot_hash))

    def apply_directory(self, *, origin_instance_id: str, route: str, revision: int,
                        state: str, metadata: dict[str, Any]) -> dict[str, Any]:
        self._directory_entry(origin_instance_id, route, revision, state, metadata)
        with self._lock, self._db:
            return self._write_directory(origin_instance_id, route, revision, state, metadata)

    def directory_snapshot(self, *, origin_instance_id: str, revision: int,
                           entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Validate a complete snapshot before one atomic projection transaction."""
        _check_id(origin_instance_id, INSTANCE_RE, "origin instance")
        if type(revision) is not int or not 1 <= revision <= MAX_DIRECTORY_REVISION:
            raise ValueError("directory revision must be positive")
        if not isinstance(entries, list) or len(entries) > 256:
            raise ValueError("directory snapshot exceeds limit")
        validated: dict[str, tuple[str, dict]] = {}
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"route", "state", "metadata"}:
                raise ValueError("invalid directory snapshot entry")
            route, state, metadata = entry.get("route"), entry.get("state", "current"), entry.get("metadata", {})
            if not isinstance(route, str):
                raise ValueError("invalid directory snapshot route")
            if not isinstance(state, str) or state not in {"current", "withdrawn"}:
                raise ValueError("directory snapshot requires current or withdrawn entries")
            self._directory_entry(origin_instance_id, route, revision, state, metadata)
            if route in validated:
                raise ValueError("duplicate directory snapshot route")
            validated[route] = (state, metadata)
        digest = hashlib.sha256(json.dumps(validated, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self._lock, self._db:
            stream = self._directory_revision(origin_instance_id)
            if revision < max(stream["revision"], stream["gap_revision"]):
                return self.directory(origin_instance_id=origin_instance_id)
            if revision == stream["revision"] and stream["snapshot_hash"]:
                if digest != stream["snapshot_hash"]:
                    raise ValueError("conflicting directory snapshot revision")
                return self.directory(origin_instance_id=origin_instance_id)
            for route, (state, metadata) in validated.items():
                self._write_directory(origin_instance_id, route, revision, state, metadata, resync=True)
            for row in self.directory(origin_instance_id=origin_instance_id):
                if row["route"] not in validated:
                    self._write_directory(origin_instance_id, row["route"], revision, "withdrawn", {}, resync=True)
            self._save_directory_revision(origin_instance_id, revision, 0, digest)
            return self.directory(origin_instance_id=origin_instance_id)

    def _mark_directory_gap(self, origin: str, route: str, revision: int,
                            stream: dict[str, Any]) -> dict[str, Any]:
        self._db.execute("UPDATE remote_directory SET state='gap' WHERE origin_instance_id=? AND state!='withdrawn'",
                         (origin,))
        existing = self._directory_row(origin, route)
        result = self._write_directory(origin, route, max(revision, existing["revision"] if existing else 0),
                                       "gap", existing["metadata"] if existing else {}, resync=True)
        self._save_directory_revision(origin, stream["revision"], max(revision, stream["gap_revision"]))
        return result

    def mark_directory_gap(self, *, origin_instance_id: str, route: str,
                           revision: int) -> dict[str, Any]:
        """Keep last valid metadata while marking the entire origin non-current."""
        self._directory_entry(origin_instance_id, route, revision, "gap", {})
        with self._lock, self._db:
            stream = self._directory_revision(origin_instance_id)
            if revision <= stream["revision"]:
                row = self._directory_row(origin_instance_id, route)
                if row is not None:
                    return row
                raise ValueError("stale directory gap for unknown route")
            return self._mark_directory_gap(origin_instance_id, route, revision, stream)

    def apply_directory_delta(self, *, origin_instance_id: str, route: str,
                              revision: int, state: str,
                              metadata: dict[str, Any]) -> dict[str, Any]:
        """Only a contiguous per-origin delta can advance a current stream."""
        self._directory_entry(origin_instance_id, route, revision, state, metadata)
        with self._lock, self._db:
            stream = self._directory_revision(origin_instance_id)
            if stream["gap_revision"] or revision > stream["revision"] + 1:
                return self._mark_directory_gap(origin_instance_id, route, revision, stream)
            if revision <= stream["revision"]:
                existing = self._directory_row(origin_instance_id, route)
                if revision == stream["revision"] and (existing is None or existing["revision"] != revision
                        or existing["state"] != state or existing["metadata"] != metadata):
                    raise ValueError("conflicting directory revision")
                if existing is None:
                    raise ValueError("stale directory delta for unknown route")
                return existing
            result = self._write_directory(origin_instance_id, route, revision, state, metadata)
            self._save_directory_revision(origin_instance_id, revision, 0)
            return result

    def local_directory_snapshot(self, origin: str, entries: list[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
        """Coalesce approved committed registrations before provider publication."""
        _check_id(origin, INSTANCE_RE, "origin instance")
        if len(entries) > 256:
            raise ValueError("directory snapshot exceeds limit")
        for entry in entries:
            self._directory_entry(origin, entry["route"], 1, "current", entry["metadata"])
        encoded = json.dumps(sorted(entries, key=lambda entry: entry["route"]), sort_keys=True)
        with self._lock, self._db:
            row = self._db.execute("SELECT revision, entries FROM federation_directory_local WHERE origin_instance_id=?", (origin,)).fetchone()
            revision = int(row["revision"]) if row else 0
            if row is None or row["entries"] != encoded:
                revision += 1
                self._db.execute("""INSERT INTO federation_directory_local VALUES (?, ?, ?)
                    ON CONFLICT(origin_instance_id) DO UPDATE SET revision=excluded.revision,
                    entries=excluded.entries""", (origin, revision, encoded))
        return revision, json.loads(encoded)

    def directory_revision(self, origin: str) -> dict[str, Any]:
        with self._lock:
            return self._directory_revision(origin)

    def reserve_directory_response(self, origin: str, request: str, now: float) -> bool:
        """One fresh snapshot response per peer/minute; identical retries coalesce."""
        with self._lock, self._db:
            row = self._db.execute("SELECT * FROM federation_directory_requests WHERE origin_instance_id=?", (origin,)).fetchone()
            if row and row["request_id"] == request:
                return True
            if row and now < row["next_response_at"]:
                return False
            self._db.execute("""INSERT INTO federation_directory_requests VALUES (?, ?, ?)
                ON CONFLICT(origin_instance_id) DO UPDATE SET request_id=excluded.request_id,
                next_response_at=excluded.next_response_at""", (origin, request, now + 60))
            return True

    def directory_request(self, origin: str, revision: int, gap: int, candidate: str) -> str:
        """Retain one outgoing request identity per observed peer position."""
        with self._lock, self._db:
            row = self._db.execute('SELECT * FROM federation_directory_outgoing_requests WHERE origin_instance_id=?',
                                   (origin,)).fetchone()
            if row and (row['revision'], row['gap_revision']) == (revision, gap):
                return row['request_id']
            self._db.execute('''INSERT INTO federation_directory_outgoing_requests VALUES (?, ?, ?, ?)
                ON CONFLICT(origin_instance_id) DO UPDATE SET revision=excluded.revision,
                gap_revision=excluded.gap_revision, request_id=excluded.request_id''',
                (origin, revision, gap, candidate))
        return candidate

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

    def pending(self, *, now: float, max_attempts: int, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= limit <= 100:
            raise ValueError('invalid bridge page limit')
        with self._lock:
            rows = self._db.execute("""SELECT * FROM bridge_obligations
                WHERE state IN ('origin_accepted','submitted','uncertain')
                AND (kind IN ('message', 'directory') OR (kind = 'receipt' AND state IN ('origin_accepted', 'uncertain')))
                AND attempts < ? AND next_attempt_at <= ? ORDER BY updated_at LIMIT ?""",
                (max_attempts, now, limit)).fetchall()
        return [dict(row) for row in rows]

    def reserve_submission(self, origin: str, message: str, destination: str, *,
                           now: float, delay: float, max_attempts: int) -> bool:
        with self._lock, self._db:
            result = self._db.execute("""UPDATE bridge_obligations
                SET state='uncertain', attempts=attempts+1, next_attempt_at=?, updated_at=?
                WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?
                AND state IN ('origin_accepted','submitted','uncertain')
                AND attempts < ? AND next_attempt_at <= ?""",
                (now + delay, _now(), origin, message, destination, max_attempts, now))
        return result.rowcount == 1

    def record_submission(self, origin: str, message: str, destination: str, provider_ref: str) -> None:
        with self._lock, self._db:
            self._db.execute("""UPDATE bridge_obligations SET state='submitted', provider_ref=?, updated_at=?
                WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?
                AND state='uncertain'""", (provider_ref, _now(), origin, message, destination))

    def defer_submission(self, origin: str, message: str, destination: str, *, until: float) -> None:
        with self._lock, self._db:
            self._db.execute('UPDATE bridge_obligations SET next_attempt_at=MAX(next_attempt_at, ?) WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?',
                             (until, origin, message, destination))

    def block_submission(self, origin: str, message: str, destination: str, *,
                         reason: str, provider_ref: str) -> None:
        """Retain failed carrier custody without retry or overwriting a receipt race."""
        if reason not in {'missing_echo', 'changed_echo', 'truncation_warning'}:
            raise ValueError('invalid provider carrier failure')
        with self._lock, self._db:
            self._db.execute("""UPDATE bridge_obligations SET state='provider_blocked',
                provider_failure=?, provider_ref=?, updated_at=?
                WHERE origin_instance_id=? AND origin_message_id=? AND destination_instance_id=?
                AND state='uncertain'""", (reason, provider_ref, _now(), origin, message, destination))

    def checkpoint(self, reader: str) -> str:
        with self._lock:
            row = self._db.execute('SELECT cursor FROM federation_checkpoints WHERE reader=?', (reader,)).fetchone()
        return row['cursor'] if row else '0'

    def advance_checkpoint(self, reader: str, cursor: str) -> None:
        with self._lock, self._db:
            previous = self.checkpoint(reader)
            if not Decimal(cursor).is_finite() or Decimal(cursor) < Decimal(previous):
                raise ValueError('provider checkpoint cannot move backward')
            self._db.execute('INSERT INTO federation_checkpoints VALUES (?,?) ON CONFLICT(reader) DO UPDATE SET cursor=excluded.cursor',
                             (reader, cursor))

