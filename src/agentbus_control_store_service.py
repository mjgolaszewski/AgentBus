"""Durable operator controls and per-session receipts."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from src.agentbus_participation_service import resolve_policy
from src.agentbus_participation_store_service import ParticipationStore


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ControlStore:
    """Control state is separate from messages and uses the same SQLite custody."""

    def __init__(self, db: sqlite3.Connection, lock: threading.RLock, participation: ParticipationStore):
        self._db = db
        self._lock = lock
        self._participation = participation
        with lock, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS operator_controls (
                    control_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    override_values_json TEXT,
                    expires_at TEXT
                )
            """)
            control_columns = {row["name"] for row in db.execute("PRAGMA table_info(operator_controls)")}
            if "override_values_json" not in control_columns:
                db.execute("ALTER TABLE operator_controls ADD COLUMN override_values_json TEXT")
            if "expires_at" not in control_columns:
                db.execute("ALTER TABLE operator_controls ADD COLUMN expires_at TEXT")
            db.execute("""
                CREATE TABLE IF NOT EXISTS control_targets (
                    control_id TEXT NOT NULL REFERENCES operator_controls(control_id),
                    session_id TEXT NOT NULL REFERENCES participation_sessions(session_id),
                    state TEXT NOT NULL CHECK(state IN ('issued', 'delivered', 'acknowledged', 'effective')),
                    delivered_at TEXT,
                    acknowledged_at TEXT,
                    effective_at TEXT,
                    receipt TEXT,
                    report_json TEXT,
                    PRIMARY KEY(control_id, session_id)
                )
            """)
            if "report_json" not in {row["name"] for row in db.execute("PRAGMA table_info(control_targets)")}:
                db.execute("ALTER TABLE control_targets ADD COLUMN report_json TEXT")
            db.execute("CREATE INDEX IF NOT EXISTS control_targets_session ON control_targets(session_id, state)")
        self.recover_acknowledged_stops()

    def issue_stop(self, *, routes: list[str] | None, reason: str, actor: str,
                   session_ids: list[str] | None = None) -> tuple[str, list[str]]:
        """Resolve a fixed target set, including overdue sessions, at issuance."""
        return self._issue("stop_end_turn", routes=routes, session_ids=session_ids,
                           reason=reason, actor=actor)

    def issue_auxiliary(self, *, kind: str, routes: list[str] | None,
                        reason: str, actor: str,
                        session_ids: list[str] | None = None,
                        override_values: dict[str, int | float | None] | None = None,
                        duration_seconds: int | None = None) -> tuple[str, list[str]]:
        if kind not in {"nudge", "checkpoint_request", "temporary_policy_override"}:
            raise ValueError("unsupported auxiliary control kind")
        if kind == "temporary_policy_override":
            if not override_values or isinstance(duration_seconds, bool) or not isinstance(duration_seconds, int) or not 1 <= duration_seconds <= 86400:
                raise ValueError("temporary override needs policy values and a duration of 1..86400 seconds")
        elif override_values is not None or duration_seconds is not None:
            raise ValueError("only temporary policy overrides accept values and duration")
        return self._issue(kind, routes=routes, session_ids=session_ids, reason=reason, actor=actor,
                           override_values=override_values, duration_seconds=duration_seconds)

    def _issue(self, kind: str, *, routes: list[str] | None,
               reason: str, actor: str,
               session_ids: list[str] | None = None,
               override_values: dict[str, int | float | None] | None = None,
               duration_seconds: int | None = None) -> tuple[str, list[str]]:
        if not reason.strip() or not actor.strip():
            raise ValueError("stop reason and actor are required")
        if routes is not None and not routes:
            raise ValueError("explicit stop target list must not be empty")
        if session_ids is not None and not session_ids:
            raise ValueError("explicit session target list must not be empty")
        with self._lock, self._db:
            if session_ids is not None:
                targets = sorted(set(session_ids))
                rows = self._db.execute(
                    f"SELECT session_id FROM participation_sessions WHERE session_id IN ({','.join('?' for _ in targets)}) AND state != 'stopped'",
                    targets,
                ).fetchall()
                if {row["session_id"] for row in rows} != set(targets):
                    raise ValueError("unknown or stopped session target")
            elif routes is None:
                rows = self._db.execute(
                    "SELECT session_id FROM participation_sessions WHERE state != 'stopped' ORDER BY session_id"
                ).fetchall()
                targets = [row["session_id"] for row in rows]
            else:
                targets = []
                for route in routes:
                    chat = self._db.execute(
                        "SELECT chat_id FROM chat_identities WHERE current_route = ?", (route,)
                    ).fetchone()
                    if chat is None:
                        chat = self._db.execute(
                            "SELECT chat_id FROM routing_aliases WHERE route = ?", (route,)
                        ).fetchone()
                    if chat is None:
                        raise ValueError(f"unknown chat route {route}")
                    rows = self._db.execute(
                        "SELECT session_id FROM participation_sessions WHERE chat_id = ? AND state != 'stopped'",
                        (chat["chat_id"],),
                    ).fetchall()
                    targets.extend(row["session_id"] for row in rows)
                targets = sorted(set(targets))
            control_id = str(uuid.uuid4())
            issued = datetime.now(timezone.utc)
            expires_at = None
            if override_values is not None:
                assert duration_seconds is not None
                if not targets:
                    raise ValueError("temporary policy override needs an active target")
                for session_id in targets:
                    active = self._db.execute(
                        "SELECT c.expires_at FROM control_targets AS t JOIN operator_controls AS c USING(control_id) "
                        "WHERE t.session_id = ? AND c.kind = 'temporary_policy_override'",
                        (session_id,),
                    ).fetchall()
                    if any(datetime.fromisoformat(row["expires_at"]) > issued for row in active):
                        raise ValueError("target already has an active temporary policy override")
                    base = self._participation.effective_policy_for_session(session_id, observed_at=issued)
                    resolve_policy((base.revision, base.values), temporary_revision=(control_id, override_values))
                expires_at = (issued + timedelta(seconds=duration_seconds)).isoformat()
            self._db.execute(
                "INSERT INTO operator_controls(control_id, kind, reason, actor, issued_at, override_values_json, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (control_id, kind, reason, actor, issued.isoformat(),
                 json.dumps(override_values, sort_keys=True) if override_values is not None else None,
                 expires_at),
            )
            self._db.executemany(
                "INSERT INTO control_targets(control_id, session_id, state) VALUES (?, ?, 'issued')",
                [(control_id, session_id) for session_id in targets],
            )
            if override_values is not None:
                for session_id in targets:
                    effective = self._participation.effective_policy_for_session(session_id, observed_at=issued)
                    self._db.execute(
                        "UPDATE participation_sessions SET effective_policy_revision = ? WHERE session_id = ?",
                        (effective.revision, session_id),
                    )
            return control_id, targets

    def deliver(self, session_id: str, session_secret: str) -> list[dict[str, str]]:
        """Control delivery precedes ordinary inbox reads and never implies ack."""
        with self._lock, self._db:
            self._participation.authorize_session(session_id, session_secret)
            rows = self._db.execute("""
                SELECT c.control_id, c.kind, c.reason, c.expires_at, t.state
                FROM control_targets AS t JOIN operator_controls AS c USING(control_id)
                WHERE t.session_id = ? AND t.state IN ('issued', 'delivered')
                ORDER BY c.issued_at, c.control_id
            """, (session_id,)).fetchall()
            if rows:
                self._db.execute(
                    "UPDATE control_targets SET state = 'delivered', delivered_at = COALESCE(delivered_at, ?) WHERE session_id = ? AND state = 'issued'",
                    (_now(), session_id),
                )
            return [{"control_id": row["control_id"], "kind": row["kind"],
                     "reason": row["reason"], **({"expires_at": row["expires_at"]}
                     if row["expires_at"] is not None else {})} for row in rows]

    def acknowledge_stop(self, control_id: str, session_id: str, session_secret: str) -> str:
        """Commit target ack, then effect; retries recover a lost response."""
        receipt, _ = self.acknowledge_control(control_id, session_id, session_secret,
                                              expected_kind="stop_end_turn")
        return receipt

    def acknowledge_control(self, control_id: str, session_id: str, session_secret: str,
                            report: dict | None = None,
                            expected_kind: str | None = None) -> tuple[str, str]:
        with self._lock, self._db:
            self._participation.authorize_session(session_id, session_secret)
            target = self._db.execute(
                "SELECT t.state, t.receipt, t.report_json, c.kind FROM control_targets AS t "
                "JOIN operator_controls AS c USING(control_id) WHERE t.control_id = ? AND t.session_id = ?",
                (control_id, session_id),
            ).fetchone()
            if target is None:
                raise PermissionError("control is not targeted to this session")
            if target["state"] == "issued":
                raise ValueError("control delivery is pending")
            kind = target["kind"]
            if expected_kind is not None and kind != expected_kind:
                raise ValueError("control has a different kind")
            if kind == "checkpoint_request" and report is None:
                raise ValueError("checkpoint report is required")
            if kind != "checkpoint_request" and report is not None:
                raise ValueError("this control does not accept a checkpoint report")
            report_json = json.dumps(report, sort_keys=True) if report is not None else None
            if target["state"] in {"acknowledged", "effective"} and target["report_json"] != report_json:
                raise ValueError("checkpoint report differs from committed acknowledgement")
            receipt = target["receipt"] or str(uuid.uuid5(
                uuid.NAMESPACE_URL, f"agentbus:control-ack:{control_id}:{session_id}"
            ))
            if target["state"] == "delivered":
                self._db.execute(
                    "UPDATE control_targets SET state = 'acknowledged', acknowledged_at = ?, receipt = ?, report_json = ? WHERE control_id = ? AND session_id = ?",
                    (_now(), receipt, report_json, control_id, session_id),
                )
        if kind == "stop_end_turn":
            self._apply_stop_effect(control_id, session_id)
        else:
            self._apply_aux_effect(control_id, session_id)
        return str(receipt), kind

    def recover_acknowledged_stops(self) -> None:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.control_id, t.session_id, c.kind FROM control_targets AS t "
                "JOIN operator_controls AS c USING(control_id) WHERE t.state = 'acknowledged'"
            ).fetchall()
        for row in rows:
            if row["kind"] == "stop_end_turn":
                self._apply_stop_effect(row["control_id"], row["session_id"])
            else:
                self._apply_aux_effect(row["control_id"], row["session_id"])

    def _apply_aux_effect(self, control_id: str, session_id: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE control_targets SET state = 'effective', effective_at = ? "
                "WHERE control_id = ? AND session_id = ? AND state = 'acknowledged'",
                (_now(), control_id, session_id),
            )

    def _apply_stop_effect(self, control_id: str, session_id: str) -> None:
        with self._lock, self._db:
            target = self._db.execute(
                "SELECT state FROM control_targets WHERE control_id = ? AND session_id = ?",
                (control_id, session_id),
            ).fetchone()
            if target is None or target["state"] not in {"acknowledged", "effective"}:
                raise ValueError("stop is not acknowledged")
            if target["state"] == "effective":
                return
            applied = _now()
            self._db.execute(
                "UPDATE participation_sessions SET state = 'stopped', stopped_at = ? WHERE session_id = ?",
                (applied, session_id),
            )
            self._db.execute(
                "UPDATE control_targets SET state = 'effective', effective_at = ? WHERE control_id = ? AND session_id = ?",
                (applied, control_id, session_id),
            )

    def status(self, control_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT t.*, c.kind, c.reason, c.actor, c.issued_at, c.expires_at FROM control_targets AS t "
                "JOIN operator_controls AS c USING(control_id) "
                "WHERE t.control_id = ? ORDER BY t.session_id", (control_id,)
            ).fetchall()
            return [
                {**{key: row[key] for key in (
                    "control_id", "session_id", "kind", "reason", "actor", "issued_at", "state", "expires_at",
                    "delivered_at", "acknowledged_at", "effective_at", "receipt",
                )}, "override_active": row["expires_at"] is not None and
                    datetime.fromisoformat(row["expires_at"]) > datetime.now(timezone.utc),
                 "report": json.loads(row["report_json"]) if row["report_json"] else None}
                for row in rows
            ]
