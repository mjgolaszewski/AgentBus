"""Bounded non-authorizing access observations and explicitly requested stale notices."""
from __future__ import annotations

import json
import math
import time
from uuid import UUID

from src.agentbus_transaction_service import atomic


class ObservationStore:
    def __init__(self, owner, monitors=(), *, now=time.time):
        self.owner, self.monitors, self.now = owner, tuple(monitors), now
        self.last_error = None
        with owner._lock, atomic(owner._db):
            owner._db.execute("""CREATE TABLE IF NOT EXISTS session_api_observations (
                session_id TEXT PRIMARY KEY REFERENCES participation_sessions(session_id),
                chat_id TEXT NOT NULL, channel TEXT NOT NULL, inbox_id TEXT NOT NULL,
                last_access REAL NOT NULL, episode INTEGER NOT NULL DEFAULT 0)""")
            owner._db.execute("""CREATE TABLE IF NOT EXISTS session_stale_notices (
                session_id TEXT NOT NULL, operator TEXT NOT NULL, episode INTEGER NOT NULL,
                state TEXT NOT NULL, attempted REAL, PRIMARY KEY(session_id,operator))""")
            old = owner._db.execute("SELECT value FROM metadata WHERE key='observation_worker_tick_v1'").fetchone()
            self.gap = {"from": float(old[0]) if old else None, "to": now(),
                        "continuous_monitoring": False, "reason": "service_start_or_unknown_prior_observation"}
            owner._db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('observation_latest_gap_v1',?)",
                              (json.dumps(self.gap),))
        self.last_tick = 0

    def observe_scope(self, binding):
        instant = self.now()
        with self.owner._lock, atomic(self.owner._db):
            row = self.owner._db.execute("SELECT * FROM session_api_observations WHERE session_id=?",
                                         (binding["session_id"],)).fetchone()
            expected = (binding["chat_id"], binding["channel"], binding["inbox_id"])
            if row is not None:
                if (row["chat_id"], row["channel"], row["inbox_id"]) != expected:
                    raise ValueError("Observation scope mismatch")
                if instant - row["last_access"] < 30:
                    return
                self.owner._db.execute("UPDATE session_api_observations SET last_access=?,episode=episode+1 WHERE session_id=?",
                                       (instant, binding["session_id"]))
            else:
                self.owner._db.execute("INSERT INTO session_api_observations(session_id,chat_id,channel,inbox_id,last_access) VALUES(?,?,?,?,?)",
                                       (binding["session_id"], *expected, instant))

    def snapshot_scope(self, binding):
        with self.owner._lock:
            row = self.owner._db.execute("SELECT * FROM session_api_observations WHERE session_id=? AND chat_id=? AND channel=? AND inbox_id=?",
                (binding["session_id"], binding["chat_id"], binding["channel"], binding["inbox_id"])).fetchone()
            return {"last_actual_api_access": row["last_access"] if row else None,
                    "access_coalescing_seconds": 30, "work_observation": "not_inferred",
                    "continuous_monitoring": False, "latest_service_gap": self.gap}

    def tick(self):
        instant = self.now()
        if instant - self.last_tick < 60:
            return
        with self.owner._lock, atomic(self.owner._db):
            self.owner._db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('observation_worker_tick_v1',?)", (str(instant),))
        self.last_tick = instant

    def next_notice(self):
        instant = self.now()
        with self.owner._lock:
            for item in self.monitors:
                row = self.owner._db.execute("SELECT o.*,s.state FROM session_api_observations o JOIN participation_sessions s USING(session_id) "
                    "WHERE o.session_id=? AND o.chat_id=? AND o.channel=?", (item["session_id"], item["chat_id"], item["channel"])).fetchone()
                if row is None or row["state"] not in {"active", "overdue"} or row["channel"] not in self.owner.channels.enabled:
                    continue
                ceiling = self.owner.participation.effective_policy_for_session(row["session_id"]).values["max_interval_seconds"]
                threshold = item.get("stale_seconds", 2 * ceiling)
                if instant - row["last_access"] < threshold:
                    continue
                for operator in item["operators"]:
                    prior = self.owner._db.execute("SELECT episode FROM session_stale_notices WHERE session_id=? AND operator=?",
                                                   (row["session_id"], operator)).fetchone()
                    if prior is None and self.owner._db.execute("SELECT count(*) FROM session_stale_notices").fetchone()[0] >= 1024:
                        self.last_error = "notice_observation_capacity_requires_operator_review"
                        continue
                    if prior is None or prior[0] != row["episode"]:
                        return {"session_id": row["session_id"], "episode": row["episode"], "operator": operator,
                                "channel": row["channel"], "last_access": row["last_access"]}
        return None

    def reserve_notice(self, item):
        with self.owner._lock, atomic(self.owner._db):
            # Fresh access or another worker reservation wins before any provider effect.
            current = self.next_notice()
            if current != item:
                return False
            self.owner._db.execute("INSERT OR REPLACE INTO session_stale_notices VALUES(?,?,?,?,?)",
                (item["session_id"], item["operator"], item["episode"], "uncertain", self.now()))
            return True

    def settle_notice(self, item, state):
        with self.owner._lock, atomic(self.owner._db):
            self.owner._db.execute("UPDATE session_stale_notices SET state=? WHERE session_id=? AND operator=? AND episode=?",
                                   (state, item["session_id"], item["operator"], item["episode"]))


def load_monitors(settings):
    path = settings.stale_monitor_config
    if path is None:
        return ()
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        config = json.loads(path.read_text())
        if not isinstance(config, dict) or set(config) != {"version", "sessions"} or type(config["version"]) is not int or config["version"] != 1:
            raise ValueError()
        items = config["sessions"]
        if not isinstance(items, list) or not 1 <= len(items) <= 128:
            raise ValueError()
        seen = set()
        for item in items:
            if (not isinstance(item, dict) or set(item) - {"session_id", "chat_id", "channel", "operators", "stale_seconds"}
                    or not {"session_id", "chat_id", "channel", "operators"} <= set(item)):
                raise ValueError()
            for key in ("session_id", "chat_id"):
                if str(UUID(item[key])) != item[key]:
                    raise ValueError()
            operators = item["operators"]
            threshold = item.get("stale_seconds", 3840)
            if (item["session_id"] in seen or item["channel"] not in settings.channels
                    or not isinstance(operators, list) or not 1 <= len(operators) <= 8 or len(set(operators)) != len(operators)
                    or not set(operators) <= settings.slack_operator_user_ids
                    or isinstance(threshold, bool) or not isinstance(threshold, (int, float))
                    or not math.isfinite(threshold) or not 120 <= threshold <= 2592000):
                raise ValueError()
            seen.add(item["session_id"])
        return tuple(items)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Invalid private stale-monitor configuration; bind existing sessions/channels and allowlisted operators") from None
