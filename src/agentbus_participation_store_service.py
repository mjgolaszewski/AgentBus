"""SQLite custody for stable chat identity and participation sessions."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone

from src.agentbus_participation_service import EffectivePolicy, resolve_policy

ROUTE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}:[a-z0-9][a-z0-9-]{0,31}$")


class StoppedMessageSession(PermissionError):
    """A proved session whose acknowledged stop bars message-plane actions."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ParticipationStore:
    """Share the service's SQLite transaction lock with its durable inbox."""

    def __init__(self, db: sqlite3.Connection, lock: threading.RLock):
        self._db = db
        self._lock = lock
        db.row_factory = sqlite3.Row
        with lock, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS chat_identities (
                    chat_id TEXT PRIMARY KEY,
                    repo TEXT NOT NULL,
                    current_route TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS routing_aliases (
                    route TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chat_identities(chat_id),
                    reserved_at TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS participation_sessions (
                    session_id TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL REFERENCES chat_identities(chat_id),
                    secret_sha256 TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('joining', 'active', 'overdue', 'stopping', 'stopped')),
                    effective_policy_revision TEXT NOT NULL,
                    delivered_policy_revision TEXT NOT NULL,
                    acknowledged_policy_revision TEXT,
                    joined_at TEXT NOT NULL,
                    last_client_contact_at TEXT NOT NULL,
                    last_semantic_ack_at TEXT,
                    stopped_at TEXT,
                    current_backoff_seconds REAL
                )
            """)
            if "current_backoff_seconds" not in {
                row["name"] for row in db.execute("PRAGMA table_info(participation_sessions)")
            }:
                db.execute("ALTER TABLE participation_sessions ADD COLUMN current_backoff_seconds REAL")
            db.execute("""
                CREATE TABLE IF NOT EXISTS participation_audit (
                    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    session_id TEXT,
                    revision TEXT,
                    at TEXT NOT NULL,
                    result TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS participation_sessions_chat ON participation_sessions(chat_id, state)")
            db.execute("CREATE INDEX IF NOT EXISTS participation_sessions_secret ON participation_sessions(secret_sha256)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS poll_policy_revisions (
                    revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL CHECK(scope IN ('global', 'repo', 'chat')),
                    scope_key TEXT NOT NULL,
                    values_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            db.execute("CREATE INDEX IF NOT EXISTS poll_policy_scope ON poll_policy_revisions(scope, scope_key, revision_id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS profile_handoffs (
                    token_sha256 TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    repo TEXT NOT NULL,
                    route TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    session_id TEXT,
                    actor TEXT NOT NULL
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS session_rotation_grants (
                    token_sha256 TEXT PRIMARY KEY,
                    chat_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    new_secret_sha256 TEXT,
                    actor TEXT NOT NULL
                )
            """)

    def issue_profile_handoff(self, *, chat_id: str, repo: str, route: str, actor: str) -> str:
        """Operator authorizes one legacy profile UUID to enter service custody."""
        try:
            parsed = uuid.UUID(chat_id)
        except ValueError:
            raise ValueError("legacy chat ID must be a UUID") from None
        if str(parsed) != chat_id or not ROUTE.fullmatch(route) or route.split(":", 1)[0] != repo:
            raise ValueError("legacy profile identity and route must match")
        if not actor:
            raise ValueError("handoff actor is required")
        token = secrets.token_urlsafe(32)
        issued = datetime.now(timezone.utc)
        with self._lock, self._db:
            if self._db.execute(
                "SELECT 1 FROM chat_identities WHERE chat_id = ? OR current_route = ?", (chat_id, route)
            ).fetchone() or self._db.execute(
                "SELECT 1 FROM routing_aliases WHERE route = ?", (route,)
            ).fetchone():
                raise ValueError("legacy chat ID or route is already enrolled or reserved")
            self._db.execute(
                "INSERT INTO profile_handoffs VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), chat_id, repo, route,
                 issued.isoformat(), (issued + timedelta(minutes=10)).isoformat(), actor),
            )
            self._audit("handoff_issued", "issued", chat_id, None, "", issued.isoformat(), actor=actor)
        return token

    def issue_session_rotation(self, *, chat_id: str, session_id: str, actor: str) -> str:
        """Grant one private, short-lived replacement for an enrolled session."""
        if not actor:
            raise ValueError("rotation actor is required")
        token = secrets.token_urlsafe(32)
        issued = datetime.now(timezone.utc)
        with self._lock, self._db:
            session = self._db.execute(
                "SELECT chat_id, state FROM participation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if session is None or session["chat_id"] != chat_id or session["state"] == "stopped":
                raise ValueError("rotation target is not an enrolled live chat/session")
            if self._db.execute(
                "SELECT 1 FROM session_rotation_grants WHERE session_id = ? AND consumed_at IS NULL AND expires_at > ?",
                (session_id, issued.isoformat()),
            ).fetchone():
                raise ValueError("an unexpired rotation grant already exists for this session")
            self._db.execute(
                "INSERT INTO session_rotation_grants VALUES (?, ?, ?, ?, ?, NULL, NULL, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), chat_id, session_id,
                 issued.isoformat(), (issued + timedelta(minutes=10)).isoformat(), actor),
            )
            self._audit("session_rotation_grant", "issued", chat_id, session_id, "", issued.isoformat(), actor=actor)
        return token

    def rotate_session_secret(self, *, session_id: str, token: str, new_secret: str) -> str:
        """Commit replacement without trusting the credential being replaced."""
        if len(token) < 32 or len(new_secret) < 32:
            raise ValueError("rotation token and new session secret must be at least 32 characters")
        token_digest = hashlib.sha256(token.encode()).hexdigest()
        new_digest = hashlib.sha256(new_secret.encode()).hexdigest()
        with self._lock, self._db:
            grant = self._db.execute(
                "SELECT * FROM session_rotation_grants WHERE token_sha256 = ?", (token_digest,)
            ).fetchone()
            session = self._db.execute(
                "SELECT chat_id, secret_sha256, state FROM participation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if grant is None or session is None or grant["session_id"] != session_id or grant["chat_id"] != session["chat_id"]:
                raise ValueError("rotation grant is not valid for this session")
            receipt = str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentbus:rotation:{session_id}:{token_digest}"))
            if grant["consumed_at"] is not None:
                if grant["new_secret_sha256"] == new_digest and session["secret_sha256"] == new_digest:
                    return receipt
                raise ValueError("rotation grant is already consumed")
            if datetime.fromisoformat(grant["expires_at"]) <= datetime.now(timezone.utc):
                raise ValueError("rotation grant has expired")
            if session["state"] == "stopped":
                raise ValueError("stopped session cannot rotate credentials")
            if hmac.compare_digest(session["secret_sha256"], new_digest):
                raise ValueError("new credential must differ from the current credential")
            committed = _now()
            self._db.execute(
                "UPDATE participation_sessions SET secret_sha256 = ? WHERE session_id = ?", (new_digest, session_id)
            )
            self._db.execute(
                "UPDATE session_rotation_grants SET consumed_at = ?, new_secret_sha256 = ? WHERE token_sha256 = ?",
                (committed, new_digest, token_digest),
            )
            self._audit("session_secret_rotation", "committed", session["chat_id"], session_id, "", committed,
                        actor=grant["actor"])
        return receipt

    def set_policy(
        self, *, scope: str, scope_key: str, values: dict[str, int | float | None], actor: str,
    ) -> int:
        """Append one immutable policy revision after validating its resolved effect."""
        if scope not in {"global", "repo", "chat"} or not scope_key or not actor:
            raise ValueError("policy scope, key, and actor are required")
        if scope == "global" and scope_key != "*":
            raise ValueError("global policy key must be *")
        with self._lock, self._db:
            if scope == "global":
                resolve_policy(("candidate", values))
            else:
                global_revision = self._latest_policy("global", "*")
                if global_revision is None:
                    raise ValueError("global policy must be configured first")
                if scope == "chat":
                    chat = self._db.execute(
                        "SELECT repo FROM chat_identities WHERE chat_id = ?", (scope_key,)
                    ).fetchone()
                    if chat is None:
                        raise ValueError("chat policy target is not enrolled")
                    resolve_policy(
                        global_revision, self._latest_policy("repo", chat["repo"]),
                        ("candidate", values),
                    )
                else:
                    resolve_policy(global_revision, ("candidate", values))
            created = _now()
            cursor = self._db.execute(
                "INSERT INTO poll_policy_revisions(scope, scope_key, values_json, actor, created_at) VALUES (?, ?, ?, ?, ?)",
                (scope, scope_key, json.dumps(values, sort_keys=True), actor, created),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a policy revision ID")
            sessions = self._db.execute("""
                SELECT s.session_id, c.repo, c.chat_id
                FROM participation_sessions AS s
                JOIN chat_identities AS c ON c.chat_id = s.chat_id
                WHERE s.state != 'stopped'
            """).fetchall()
            for session in sessions:
                if scope == "repo" and session["repo"] != scope_key:
                    continue
                if scope == "chat" and session["chat_id"] != scope_key:
                    continue
                effective = self.effective_policy_for_session(session["session_id"])
                self._db.execute(
                    "UPDATE participation_sessions SET effective_policy_revision = ? WHERE session_id = ?",
                    (effective.revision, session["session_id"]),
                )
            return cursor.lastrowid

    def effective_policy(self, repo: str, chat_id: str) -> EffectivePolicy:
        with self._lock:
            global_revision = self._latest_policy("global", "*")
            if global_revision is None:
                raise ValueError("global policy must be configured first")
            return resolve_policy(
                global_revision,
                self._latest_policy("repo", repo),
                self._latest_policy("chat", chat_id),
            )

    def effective_policy_for_session(
        self, session_id: str, *, observed_at: datetime | None = None,
    ) -> EffectivePolicy:
        """Project inherited policy plus an unexpired, session-bound control."""
        observed = observed_at or datetime.now(timezone.utc)
        with self._lock:
            chat = self._db.execute(
                "SELECT c.repo, c.chat_id FROM participation_sessions AS s "
                "JOIN chat_identities AS c ON c.chat_id = s.chat_id WHERE s.session_id = ?",
                (session_id,),
            ).fetchone()
            if chat is None:
                raise KeyError(session_id)
            global_revision = self._latest_policy("global", "*")
            if global_revision is None:
                raise ValueError("global policy must be configured first")
            control_tables = self._db.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' "
                "AND name IN ('control_targets', 'operator_controls')"
            ).fetchone()[0]
            rows = self._db.execute(
                "SELECT c.control_id, c.override_values_json, c.expires_at "
                "FROM control_targets AS t JOIN operator_controls AS c USING(control_id) "
                "WHERE t.session_id = ? AND c.kind = 'temporary_policy_override' "
                "ORDER BY c.issued_at, c.control_id",
                (session_id,),
            ).fetchall() if control_tables == 2 else []
            active = [row for row in rows if row["expires_at"] is not None and
                      datetime.fromisoformat(row["expires_at"]) > observed]
            if len(active) > 1:
                raise ValueError("overlapping temporary policy overrides")
            temporary = (active[0]["control_id"], json.loads(active[0]["override_values_json"])) if active else None
            return resolve_policy(
                global_revision, self._latest_policy("repo", chat["repo"]),
                self._latest_policy("chat", chat["chat_id"]), temporary,
            )

    def _latest_policy(self, scope: str, key: str) -> tuple[str, dict[str, int | float | None]] | None:
        row = self._db.execute(
            "SELECT revision_id, values_json FROM poll_policy_revisions WHERE scope = ? AND scope_key = ? ORDER BY revision_id DESC LIMIT 1",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        return str(row["revision_id"]), json.loads(row["values_json"])

    def enroll(
        self, *, repo: str, route: str, display_name: str,
        session_secret: str, handoff_token: str | None = None,
    ) -> tuple[str, str, str]:
        """Create a new identity and joining session; no ordinary participation yet."""
        if not ROUTE.fullmatch(route) or route.split(":", 1)[0] != repo:
            raise ValueError("invalid chat route")
        if not display_name.strip():
            raise ValueError("display name is required")
        if len(session_secret) < 32:
            raise ValueError("session secret must contain at least 32 characters")
        created = _now()
        chat_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        secret_digest = hashlib.sha256(session_secret.encode()).hexdigest()
        with self._lock, self._db:
            if handoff_token is None:
                existing = self._db.execute("""
                    SELECT c.chat_id, s.session_id, s.secret_sha256, s.state
                    FROM chat_identities AS c JOIN participation_sessions AS s ON s.chat_id = c.chat_id
                    WHERE c.current_route = ? ORDER BY s.joined_at DESC LIMIT 1
                """, (route,)).fetchone()
                if existing is not None:
                    if (existing["state"] != "stopped" and
                            hmac.compare_digest(existing["secret_sha256"], secret_digest)):
                        current = self.deliver_policy(existing["session_id"], session_secret)
                        return existing["chat_id"], existing["session_id"], current.revision
                    raise ValueError("route is already enrolled; choose another identity")
            if handoff_token is not None:
                token_digest = hashlib.sha256(handoff_token.encode()).hexdigest()
                handoff = self._db.execute(
                    "SELECT * FROM profile_handoffs WHERE token_sha256 = ?", (token_digest,)
                ).fetchone()
                if (handoff is None or
                        handoff["repo"] != repo or handoff["route"] != route):
                    raise ValueError("profile handoff is invalid or already used")
                if handoff["consumed_at"] is not None:
                    previous = self._db.execute(
                        "SELECT session_id, secret_sha256, effective_policy_revision FROM participation_sessions WHERE session_id = ?",
                        (handoff["session_id"],),
                    ).fetchone()
                    if previous is not None and hmac.compare_digest(previous["secret_sha256"], secret_digest):
                        current = self.deliver_policy(previous["session_id"], session_secret)
                        return handoff["chat_id"], previous["session_id"], current.revision
                    raise ValueError("profile handoff is already used")
                if datetime.fromisoformat(handoff["expires_at"]) <= datetime.now(timezone.utc):
                    raise ValueError("profile handoff is expired")
                chat_id = handoff["chat_id"]
            if self._db.execute("SELECT 1 FROM routing_aliases WHERE route = ?", (route,)).fetchone():
                raise ValueError("route is reserved by an existing chat")
            policy_revision = self.effective_policy(repo, chat_id).revision
            try:
                self._db.execute(
                    "INSERT INTO chat_identities VALUES (?, ?, ?, ?, ?, ?)",
                    (chat_id, repo, route, display_name, created, created),
                )
            except sqlite3.IntegrityError:
                raise ValueError("chat ID or route is already enrolled") from None
            self._db.execute(
                "INSERT INTO participation_sessions (session_id, chat_id, secret_sha256, state, "
                "effective_policy_revision, delivered_policy_revision, acknowledged_policy_revision, "
                "joined_at, last_client_contact_at, last_semantic_ack_at, stopped_at, current_backoff_seconds) "
                "VALUES (?, ?, ?, 'joining', ?, ?, NULL, ?, ?, NULL, NULL, NULL)",
                (session_id, chat_id, secret_digest, policy_revision, policy_revision, created, created),
            )
            if handoff_token is not None:
                self._db.execute(
                    "UPDATE profile_handoffs SET consumed_at = ?, session_id = ? WHERE token_sha256 = ?",
                    (created, session_id, token_digest),
                )
            self._audit("enrollment", "issued", chat_id, session_id, policy_revision, created)
        return chat_id, session_id, policy_revision

    def acknowledge_policy(self, session_id: str, session_secret: str, revision: str) -> str:
        """Only exact-session proof may activate the delivered policy revision."""
        with self._lock, self._db:
            session = self._authorized_session(session_id, session_secret)
            if session["state"] == "stopped":
                raise ValueError("stopped session cannot be reactivated")
            current_revision = self.effective_policy_for_session(session_id).revision
            if revision != session["delivered_policy_revision"] or revision != current_revision:
                raise ValueError("unknown or stale policy revision")
            self._db.execute(
                "UPDATE participation_sessions SET effective_policy_revision = ? WHERE session_id = ?",
                (current_revision, session_id),
            )
            if session["acknowledged_policy_revision"] == revision:
                return self._policy_receipt(session_id, revision)
            if session["state"] not in {"joining", "active", "overdue", "stopping"}:
                raise ValueError("policy acknowledgement is not applicable")
            committed = _now()
            self._db.execute(
                "UPDATE participation_sessions SET state = CASE WHEN state = 'joining' THEN 'active' ELSE state END, acknowledged_policy_revision = ?, last_semantic_ack_at = ? WHERE session_id = ?",
                (revision, committed, session_id),
            )
            self._audit("policy_ack", "recorded", session["chat_id"], session_id, revision, committed)
            return self._policy_receipt(session_id, revision)

    def deliver_policy(self, session_id: str, session_secret: str) -> EffectivePolicy:
        """Deliver the current effective revision without treating read as acknowledgement."""
        with self._lock, self._db:
            session = self._authorized_session(session_id, session_secret)
            if session["state"] == "stopped":
                raise ValueError("stopped session cannot receive policy")
            chat = self._db.execute(
                "SELECT repo FROM chat_identities WHERE chat_id = ?", (session["chat_id"],)
            ).fetchone()
            if chat is None:
                raise RuntimeError("session identity is missing")
            policy = self.effective_policy_for_session(session_id)
            self._db.execute(
                "UPDATE participation_sessions SET effective_policy_revision = ?, delivered_policy_revision = ? WHERE session_id = ?",
                (policy.revision, policy.revision, session_id),
            )
            return policy

    def contact(self, session_id: str, session_secret: str,
                current_backoff_seconds: float | None = None) -> str:
        """Record target client contact without claiming model attention."""
        with self._lock, self._db:
            session = self._authorized_session(session_id, session_secret)
            if session["state"] not in {"active", "overdue", "stopping"}:
                raise ValueError("session is not active")
            if current_backoff_seconds is not None:
                # The client may still be observing its prior acknowledged
                # policy while a tighter revision awaits delivery and ack.
                if (isinstance(current_backoff_seconds, bool)
                        or not math.isfinite(current_backoff_seconds)
                        or current_backoff_seconds <= 0):
                    raise ValueError("reported inbox backoff must be finite and positive")
            contacted = _now()
            self._db.execute(
                "UPDATE participation_sessions SET last_client_contact_at = ?, current_backoff_seconds = COALESCE(?, current_backoff_seconds), state = CASE WHEN state = 'overdue' THEN 'active' ELSE state END WHERE session_id = ?",
                (contacted, current_backoff_seconds, session_id),
            )
            return contacted

    def session_state(self, session_id: str) -> sqlite3.Row:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM participation_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise KeyError(session_id)
        return row

    def roster(self, *, include_stopped: bool = False) -> list[dict]:
        """List service-joined sessions, projecting each session's current presence."""
        with self._lock:
            rows = self._db.execute("""
                SELECT s.session_id, c.display_name FROM participation_sessions AS s
                JOIN chat_identities AS c ON c.chat_id = s.chat_id
                WHERE (? OR s.state != 'stopped') ORDER BY c.current_route, s.joined_at
            """, (include_stopped,)).fetchall()
            return [dict(self.presence(row["session_id"]), display_name=row["display_name"])
                    for row in rows]

    def presence(self, session_id: str, *, observed_at: datetime | None = None) -> dict:
        """Project participation from target contact; an operator read writes nothing."""
        observed = observed_at or datetime.now(timezone.utc)
        with self._lock:
            row = self._db.execute("""
                SELECT s.*, c.repo, c.current_route FROM participation_sessions AS s
                JOIN chat_identities AS c ON c.chat_id = s.chat_id
                WHERE s.session_id = ?
            """, (session_id,)).fetchone()
            if row is None:
                raise KeyError(session_id)
            policy = self.effective_policy_for_session(session_id, observed_at=observed)
            outstanding = self._db.execute("""
                SELECT control_id, state FROM control_targets
                WHERE session_id = ? AND state != 'effective' ORDER BY control_id
            """, (session_id,)).fetchall()
        last_contact = datetime.fromisoformat(row["last_client_contact_at"])
        expected = last_contact + timedelta(seconds=policy.control_check_max_seconds)
        overdue_from = expected + timedelta(seconds=float(policy.values["overdue_grace_seconds"] or 0))
        overdue_seconds = max(0.0, (observed - overdue_from).total_seconds())
        if row["state"] == "stopped":
            state = "stopped"
        elif row["state"] == "stopping":
            state = "stopping"
        elif row["state"] == "joining":
            state = "joining"
        elif overdue_seconds > 0:
            state = "overdue"
        elif outstanding:
            state = "control_pending"
        else:
            state = "active_compliant"
        return {
            "chat_id": row["chat_id"], "session_id": session_id, "route": row["current_route"],
            "state": state, "last_client_contact_at": row["last_client_contact_at"],
            "last_semantic_ack_at": row["last_semantic_ack_at"],
            "next_expected_check_at": expected.isoformat() if row["state"] != "stopped" else None,
            "overdue_duration_seconds": overdue_seconds if row["state"] != "stopped" else 0.0,
            "overdue_reason": "control check missed" if state == "overdue" else None,
            "effective_policy_revision": policy.revision,
            "acknowledged_policy_revision": row["acknowledged_policy_revision"],
            "polling_required": row["state"] != "stopped",
            "outstanding_controls": [dict(item) for item in outstanding],
            "current_backoff_seconds": row["current_backoff_seconds"],
        }

    def authorize_session(self, session_id: str, secret: str) -> sqlite3.Row:
        """Verify session proof for another service-side durable owner."""
        with self._lock:
            return self._authorized_session(session_id, secret)

    def rename(self, session_id: str, session_secret: str, new_route: str) -> tuple[str, str, str]:
        """Move the route atomically; the former address remains reserved."""
        with self._lock, self._db:
            session = self._authorized_session(session_id, session_secret)
            if session["state"] == "stopped":
                raise ValueError("stopped session cannot rename")
            chat = self._db.execute(
                "SELECT repo, current_route FROM chat_identities WHERE chat_id = ?",
                (session["chat_id"],),
            ).fetchone()
            if chat is None:
                raise RuntimeError("session identity is missing")
            if not ROUTE.fullmatch(new_route) or new_route.split(":", 1)[0] != chat["repo"]:
                raise ValueError("new route must keep the same repository")
            old_route = chat["current_route"]
            if new_route == old_route:
                return session["chat_id"], old_route, new_route
            current = self._db.execute(
                "SELECT chat_id FROM chat_identities WHERE current_route = ?", (new_route,)
            ).fetchone()
            alias = self._db.execute(
                "SELECT chat_id FROM routing_aliases WHERE route = ?", (new_route,)
            ).fetchone()
            if current is not None or (alias is not None and alias["chat_id"] != session["chat_id"]):
                raise ValueError("new route collides with an existing or reserved address")
            if alias is not None:
                self._db.execute("DELETE FROM routing_aliases WHERE route = ?", (new_route,))
            changed = _now()
            self._db.execute(
                "INSERT OR IGNORE INTO routing_aliases(route, chat_id, reserved_at) VALUES (?, ?, ?)",
                (old_route, session["chat_id"], changed),
            )
            self._db.execute(
                "UPDATE chat_identities SET current_route = ?, updated_at = ? WHERE chat_id = ?",
                (new_route, changed, session["chat_id"]),
            )
            self._audit("rename", f"{old_route} -> {new_route}", session["chat_id"], session_id, "", changed)
            return session["chat_id"], old_route, new_route

    def routes_for(self, route: str) -> tuple[str, ...]:
        """Resolve current or former route to all addresses of one stable chat."""
        with self._lock:
            chat = self._db.execute(
                "SELECT chat_id, current_route FROM chat_identities WHERE current_route = ?", (route,)
            ).fetchone()
            if chat is None:
                chat = self._db.execute("""
                    SELECT c.chat_id, c.current_route FROM routing_aliases AS a
                    JOIN chat_identities AS c ON c.chat_id = a.chat_id WHERE a.route = ?
                """, (route,)).fetchone()
            if chat is None:
                return (route,)
            aliases = self._db.execute(
                "SELECT route FROM routing_aliases WHERE chat_id = ? ORDER BY route",
                (chat["chat_id"],),
            ).fetchall()
            return tuple(sorted({chat["current_route"], *(row["route"] for row in aliases)}))

    def same_chat(self, first_route: str, second_route: str) -> bool:
        return second_route in self.routes_for(first_route)

    def session_for_secret(self, secret: str, route: str) -> sqlite3.Row:
        """Resolve an optional message-plane proof without trusting a sender label."""
        if len(secret) > 512:
            raise PermissionError("session credential exceeds maximum length")
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with self._lock:
            row = self._db.execute(
                "SELECT s.*, c.current_route FROM participation_sessions AS s "
                "JOIN chat_identities AS c ON c.chat_id = s.chat_id "
                "WHERE s.secret_sha256 = ?", (digest,),
            ).fetchone()
            if row is None or route not in self.routes_for(row["current_route"]):
                raise PermissionError("session authority does not match sender")
            if row["state"] not in {"active", "overdue", "stopping"}:
                raise PermissionError("session is not active")
            return row

    def message_principal(self, route: str, secret: str | None) -> tuple[str, str]:
        """Resolve enrolled message authority; only unjoined routes may be legacy."""
        if secret is not None and len(secret) > 512:
            raise PermissionError("session credential exceeds maximum length")
        with self._lock:
            owner = self._db.execute(
                "SELECT chat_id FROM chat_identities WHERE current_route = ? "
                "UNION SELECT chat_id FROM routing_aliases WHERE route = ?",
                (route, route),
            ).fetchone()
            if owner is None and not secret:
                return "legacy", f"legacy:{route}"
            if not secret:
                raise PermissionError("enrolled route requires its session credential")
            digest = hashlib.sha256(secret.encode()).hexdigest()
            row = self._db.execute(
                "SELECT s.*, c.current_route FROM participation_sessions AS s "
                "JOIN chat_identities AS c ON c.chat_id = s.chat_id "
                "WHERE s.secret_sha256 = ?", (digest,),
            ).fetchone()
            if row is None or owner is None or row["chat_id"] != owner["chat_id"]:
                raise PermissionError("session authority does not match route")
            if row["state"] == "stopped":
                raise StoppedMessageSession("session is stopped")
            if row["state"] not in {"active", "overdue", "stopping"}:
                raise PermissionError("session is not active")
            return "session", f"session:{row['session_id']}"

    def _authorized_session(self, session_id: str, secret: str) -> sqlite3.Row:
        session = self._db.execute(
            "SELECT * FROM participation_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        digest = hashlib.sha256(secret.encode()).hexdigest()
        if session is None or not hmac.compare_digest(session["secret_sha256"], digest):
            raise PermissionError("session authority required")
        return session

    @staticmethod
    def _policy_receipt(session_id: str, revision: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"agentbus:policy-ack:{session_id}:{revision}"))

    def _audit(
        self, action: str, result: str, chat_id: str, session_id: str | None,
        revision: str, timestamp: str, *, actor: str | None = None,
    ) -> None:
        self._db.execute(
            "INSERT INTO participation_audit(actor, action, chat_id, session_id, revision, at, result) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (actor or chat_id, action, chat_id, session_id, revision, timestamp, result),
        )
