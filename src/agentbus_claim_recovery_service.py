"""Atomic, audited operator recovery of abandoned unrouted message claims."""

from __future__ import annotations

import re
import sqlite3
import threading
from datetime import datetime, timezone


class ClaimRecoveryStore:
    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self._db = db
        self._lock = lock
        with lock, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS claim_recovery_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel TEXT NOT NULL,
                    cursor INTEGER NOT NULL,
                    previous_identity TEXT,
                    new_identity TEXT,
                    actor TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    changed_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS claim_recovery_cursor ON claim_recovery_audit(channel, cursor)")

    def reassign(self, *, channel: str, cursor: int, new_identity: str | None,
                 actor: str, reason: str) -> dict:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and recovery reason are required")
        if new_identity is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", new_identity):
            raise ValueError("new claimant identity is invalid")
        with self._lock, self._db:
            message = self._db.execute(
                "SELECT audience FROM messages WHERE channel = ? AND cursor = ?",
                (channel, cursor),
            ).fetchone()
            if message is None:
                raise KeyError(cursor)
            if message["audience"] != "unrouted":
                raise ValueError("only unrouted message claims can be recovered")
            current = self._db.execute(
                "SELECT identity FROM claims WHERE channel = ? AND cursor = ?",
                (channel, cursor),
            ).fetchone()
            previous = current["identity"] if current else None
            if previous is None:
                raise ValueError("message has no claim to recover")
            if new_identity == previous:
                return {"cursor": cursor, "previous_identity": previous,
                        "new_identity": previous, "changed": False}
            changed = datetime.now(timezone.utc).isoformat()
            if new_identity is None:
                self._db.execute("DELETE FROM claims WHERE channel = ? AND cursor = ?",
                                 (channel, cursor))
            else:
                self._db.execute(
                    "UPDATE claims SET identity = ?, claimed_at = ? WHERE channel = ? AND cursor = ?",
                    (new_identity, changed, channel, cursor),
                )
            self._db.execute("""
                INSERT INTO claim_recovery_audit
                (channel, cursor, previous_identity, new_identity, actor, reason, changed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (channel, cursor, previous, new_identity, actor, reason, changed))
            return {"cursor": cursor, "previous_identity": previous,
                    "new_identity": new_identity, "changed": True, "changed_at": changed}

    def history(self, channel: str, cursor: int) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM claim_recovery_audit WHERE channel = ? AND cursor = ? ORDER BY audit_id",
                (channel, cursor),
            ).fetchall()
            return [dict(row) for row in rows]
