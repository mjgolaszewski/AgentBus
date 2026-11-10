"""Opt-in acknowledgement-safe maintenance on the existing delivery worker tick."""
import hashlib
import json
import time
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from src.agentbus_transaction_service import atomic

DEFAULT_RETENTION_DAYS = 30


@dataclass(frozen=True)
class AutomaticRetentionPolicy:
    days: int = DEFAULT_RETENTION_DAYS
    apply: bool = False

    def __post_init__(self):
        if type(self.days) is not int or not 14 <= self.days <= 365 or type(self.apply) is not bool:
            raise ValueError("Automatic retention requires14..365 days and explicit apply boolean")


class AutomaticRetention:
    def __init__(self, owner, settings, *, now=time.time):
        self.owner, self.settings, self.now = owner, settings, now
        self.next_at = 0
        self.last_report = None
        self.policy = AutomaticRetentionPolicy(settings.automatic_retention_days, settings.automatic_retention_apply)
        self.age = self.policy.days * 86400
        self.channels = tuple(settings.channels)
        bindings = [(channel, owner.channels.inbox_id(channel)) for channel in self.channels]
        self.fingerprint = hashlib.sha256(json.dumps([1, bindings, self.age], separators=(",", ":")).encode()).hexdigest()

    def _metadata(self, key, fallback):
        row = self.owner._db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else fallback

    def tick(self):
        current = self.now()
        if current < self.next_at:
            return
        self.next_at = current + 3600
        with self.owner._lock, atomic(self.owner._db):
            if not self.owner.lifecycle.enabled:
                self.last_report = {"state": "blocked_lifecycle_not_enabled", "applied": False}
                return
            cursor = self._metadata("automatic_retention_channel_v1", -1)
            if type(cursor) is not int:
                raise ValueError("Automatic retention checkpoint is invalid")
            index = (cursor + 1) % len(self.channels)
            channel = self.channels[index]
            receipts = self._metadata("automatic_retention_dry_run_v1", {})
            if not isinstance(receipts, dict) or any(not isinstance(value, dict) for value in receipts.values()):
                raise ValueError("Automatic retention dry-run receipt is invalid")
            receipts = {key: value for key, value in receipts.items() if key in self.channels}
            prepared = receipts.get(channel, {}).get("policy") == self.fingerprint
            args = {"actor": "automatic-retention-v1", "instant": current, "automatic_age_seconds": self.age}
            archive = self.owner.retention.archive(channel, **args)
            purge = self.owner.retention.purge(channel, **args)
            sample = self.owner._db.execute("""SELECT p.cursor,p.legacy_unknown,
                (SELECT count(*) FROM lifecycle_targets t WHERE t.cursor=p.cursor) AS targets,
                (SELECT count(*) FROM lifecycle_targets t WHERE t.cursor=p.cursor AND t.state!='handled') AS unsettled,
                (SELECT count(*) FROM lifecycle_targets t WHERE t.cursor=p.cursor AND t.semantic_ack_at IS NULL) AS unacknowledged
                FROM lifecycle_payloads p WHERE p.channel=? ORDER BY p.cursor LIMIT 100""", (channel,)).fetchall()
            pins = {"legacy_unknown": 0, "no_original_target": 0, "work_unsettled": 0, "actual_ack_missing": 0}
            for row in sample:
                pins["legacy_unknown"] += bool(row["legacy_unknown"])
                pins["no_original_target"] += row["targets"] == 0
                pins["work_unsettled"] += row["unsettled"] > 0
                pins["actual_ack_missing"] += row["unacknowledged"] > 0
            report = {"version": 1, "channel": channel, "policy": self.fingerprint, "observed_at": current,
                      "configured_activity_days": self.policy.days,
                      "existing_created_retention_seconds": self.owner.lifecycle.policy.retention_seconds,
                      "archive_eligible": archive["eligible"], "purge_eligible": purge["eligible"],
                      "sampled_payloads": len(sample), "sample_limit": 100, "sample_pins": pins,
                      "other_pins": "age, replay, provider, ancestry, claims and federation remain authoritative",
                      "state": "dry_run", "applied": False, "batch_limit": 100}
            if self.policy.apply:
                if not prepared:
                    report["state"] = "blocked_matching_dry_run_required"
                else:
                    action = "purge" if purge["eligible"] else "archive" if archive["eligible"] else None
                    if action is not None:
                        key = str(uuid5(NAMESPACE_URL, json.dumps(["automatic-retention-v1", self.fingerprint,
                                                                 channel, int(current // 3600), action])))
                        result = getattr(self.owner.retention, action)(channel, **args, apply=True, request_key=key)
                        report.update(state=action, applied=True, mutation_receipt=result)
                    else:
                        report["state"] = "no_eligible_payloads"
            else:
                receipts[channel] = {"policy": self.fingerprint, "observed_at": current}
                self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES('automatic_retention_dry_run_v1',?)",
                                       (json.dumps(receipts),))
            self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES('automatic_retention_channel_v1',?)", (str(index),))
            self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES('automatic_retention_latest_v1',?)", (json.dumps(report),))
            self.last_report = report
