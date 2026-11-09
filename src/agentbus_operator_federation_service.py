"""Bounded operator federation uses existing signed transport and shared controls."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Callable, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat

from src.agentbus_federation_wire_service import canonical, encode_frame
from src.agentbus_transaction_service import atomic


class OperatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
    actor: str = Field(pattern=r"^[UW][A-Z0-9]{1,79}$")
    source_channel: str = Field(pattern=r"^[CG][A-Z0-9]{1,79}$")
    channel: str = Field(pattern=r"^[CG][A-Z0-9]{1,79}$")
    operation: Literal["health", "broadcast"]
    text: str = Field(max_length=1000)
    expires_at: FiniteFloat


class OperatorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(pattern=r"^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$")
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    revision: int = Field(ge=1)
    sampled_at: FiniteFloat
    result: dict[str, Any]


class OperatorFederation:
    def __init__(self, store, bridge, policy):
        if (
            not isinstance(policy, dict)
            or set(policy) != {"version", "grants"}
            or type(policy["version"]) is not int
            or policy["version"] != 1
        ):
            raise ValueError("Invalid operator federation policy")
        grants = policy["grants"]
        if not isinstance(grants, list) or len(grants) > 256:
            raise ValueError("Invalid bounded operator grants")
        for grant in grants:
            if (
                not isinstance(grant, dict)
                or set(grant) != {"origin", "actor", "source_channel", "channel", "operations"}
                or grant["origin"] not in bridge.bindings
                or bridge.bindings[grant["origin"]].operator_version != 4
                or not isinstance(grant["actor"], str)
                or not re.fullmatch(r"[UW][A-Z0-9]{1,79}", grant["actor"])
                or grant["source_channel"] != bridge.channel
                or grant["channel"] != bridge.channel
                or not isinstance(grant["operations"], list)
                or not grant["operations"]
                or any(x not in {"health", "broadcast"} for x in grant["operations"])
            ):
                raise ValueError("Invalid exact origin actor operation or channel grant")
        self.store, self.bridge, self.grants = store, bridge, grants
        self.health: Callable[[], dict[str, Any]] = lambda: {"state": "not_observed"}
        self.refresh_after = 0
        with store._lock, atomic(store._db):
            store._db.execute("""CREATE TABLE IF NOT EXISTS operator_fanout (
                request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, channel TEXT NOT NULL,
                spec TEXT NOT NULL, created REAL NOT NULL)""")
            store._db.execute("""CREATE TABLE IF NOT EXISTS operator_fanout_targets (
                request_id TEXT NOT NULL, origin TEXT NOT NULL, request_hash TEXT,
                result TEXT, revision INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(request_id,origin))""")
            store._db.execute("""CREATE TABLE IF NOT EXISTS operator_remote_requests (
                origin TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                payload TEXT NOT NULL, control_id TEXT, result TEXT NOT NULL,
                revision INTEGER NOT NULL, created REAL NOT NULL, sampled_at REAL NOT NULL,
                PRIMARY KEY(origin,request_id))""")

    def start(self, *, actor, channel, operation, location, text, request_key):
        if channel != self.bridge.channel or operation not in {"health", "broadcast"}:
            raise ValueError("Operator federation is unavailable in this channel")
        if not isinstance(request_key, str) or not 1 <= len(request_key) <= 500:
            raise ValueError("Verified Slack request identity required")
        if operation == "broadcast" and (not text or len(text) > 1000):
            raise ValueError("Broadcast requires1..1000 characters")
        if (
            location is not None
            and location not in self.bridge.bindings
            and location != self.bridge.identity.instance_id
        ):
            raise ValueError("Unknown approved location")
        request_id = str(
            uuid5(NAMESPACE_URL, canonical([self.bridge.identity.instance_id, actor, channel, operation, request_key]))
        )
        spec = canonical(
            {"actor": actor, "channel": channel, "operation": operation, "location": location, "text": text}
        )
        with self.store._lock, atomic(self.store._db):
            prior = self.store._db.execute(
                "SELECT spec FROM operator_fanout WHERE request_id=?", (request_id,)
            ).fetchone()
            if prior:
                if prior["spec"] != spec:
                    raise ValueError("Operator request conflicts with retained identity")
                return request_id
            if self.store._db.execute("SELECT count(*) FROM operator_fanout").fetchone()[0] >= 1024:
                raise ValueError("Operator federation history capacity requires archival")
            peers = [location] if location else [self.bridge.identity.instance_id, *sorted(self.bridge.bindings)]
            if len(peers) > 64 or (location is None and not self.bridge.bindings):
                raise ValueError("No approved remote locations configured")
            now = self.bridge.now()
            self.store._db.execute(
                "INSERT INTO operator_fanout VALUES (?,?,?,?,?)", (request_id, actor, channel, spec, now)
            )
            for peer in peers:
                body = canonical(
                    {
                        "request_id": request_id,
                        "actor": actor,
                        "source_channel": channel,
                        "channel": self.bridge.channel,
                        "operation": operation,
                        "text": text,
                        "expires_at": now + 600,
                    }
                )
                digest = hashlib.sha256(body.encode()).hexdigest()
                local = peer == self.bridge.identity.instance_id
                supported = local or self.bridge.bindings[peer].operator_version == 4
                result = None if supported else canonical({"state": "unsupported"})
                self.store._db.execute(
                    "INSERT INTO operator_fanout_targets(request_id,origin,request_hash,result) VALUES (?,?,?,?)",
                    (request_id, peer, digest, result),
                )
                if local:
                    self.receive({"origin_instance_id": peer, "kind": "operator_request", "payload": body})
                elif supported:
                    self._enqueue("operator_request", str(uuid5(UUID(request_id), peer)), peer, body)
        return request_id

    def _enqueue(self, kind, message_id, destination, body):
        frame = encode_frame(self.bridge.identity, kind, message_id, destination, body, version=4)
        self.bridge.store.accept(
            origin_instance_id=self.bridge.identity.instance_id,
            origin_message_id=message_id,
            destination_instance_id=destination,
            payload=frame,
            local_payload=body,
        )

    def _authorize(self, origin, body):
        return origin == self.bridge.identity.instance_id or any(
            g["origin"] == origin
            and g["actor"] == body["actor"]
            and g["source_channel"] == body["source_channel"]
            and g["channel"] == body["channel"]
            and body["operation"] in g["operations"]
            for g in self.grants
        )

    def _result(self, row):
        if row["control_id"]:
            payload = json.loads(row["payload"])
            targets = self.store.controls.status(row["control_id"], channel=payload["channel"])
            return {
                "state": "accepted",
                "targets": len(targets),
                "read_acknowledged": sum(
                    t["acknowledged_at"] is not None and t["receipt"] is not None for t in targets
                ),
                "expired_unread": sum(t.get("lifecycle_state") == "expired_unread" for t in targets),
                "execution": "not_inferred",
            }
        return json.loads(row["result"])

    def _send_result(self, row, result, revision):
        body = canonical(
            {
                "request_id": row["request_id"],
                "request_hash": row["request_hash"],
                "revision": revision,
                "sampled_at": row["sampled_at"],
                "result": result,
            }
        )
        if row["origin"] == self.bridge.identity.instance_id:
            self._receive_result(row["origin"], json.loads(body))
            return
        message = str(uuid5(UUID(row["request_id"]), row["origin"] + ":result:" + str(revision)))
        self._enqueue("operator_result", message, row["origin"], body)

    def receive(self, frame):
        origin = frame["origin_instance_id"]
        try:
            body = json.loads(frame["payload"])
        except (ValueError, RecursionError):
            raise ValueError("Invalid bounded operator payload") from None
        if not isinstance(body, dict):
            raise ValueError("Invalid operator payload")
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or str(UUID(request_id)) != request_id:
            raise ValueError("Invalid operator request identity")
        with self.store._lock, atomic(self.store._db):
            if frame["kind"] == "operator_result":
                body = OperatorResponse.model_validate(body).model_dump()
                self._receive_result(origin, body)
                return
            body = OperatorRequest.model_validate(body).model_dump()
            if set(body) != {"request_id", "actor", "source_channel", "channel", "operation", "text", "expires_at"}:
                raise ValueError("Invalid operator request fields")
            if (
                body["channel"] != self.bridge.channel
                or body["source_channel"] != self.bridge.channel
                or body["operation"] not in {"health", "broadcast"}
                or not isinstance(body["actor"], str)
                or not isinstance(body["text"], str)
                or len(body["text"]) > 1000
                or type(body["expires_at"]) not in (int, float)
                or not math.isfinite(body["expires_at"])
            ):
                raise ValueError("Invalid operator request scope")
            digest = hashlib.sha256(frame["payload"].encode()).hexdigest()
            prior = self.store._db.execute(
                "SELECT * FROM operator_remote_requests WHERE origin=? AND request_id=?", (origin, request_id)
            ).fetchone()
            if prior:
                if prior["request_hash"] != digest:
                    raise ValueError("Conflicting duplicate operator request")
                self._send_result(prior, json.loads(prior["result"]), prior["revision"])
                return
            if self.store._db.execute("SELECT count(*) FROM operator_remote_requests").fetchone()[0] >= 1024:
                raise ValueError("Remote operator history capacity requires archival")
            control_id = None
            result: dict[str, Any]
            if not self._authorize(origin, body):
                result = {"state": "denied"}
            elif not self.bridge.now() <= body["expires_at"] <= self.bridge.now() + 720:
                result = {"state": "expired"}
            elif body["operation"] == "health":
                snapshot = self.health()
                permitted = {
                    "version",
                    "runtime",
                    "protocol_version",
                    "participation_version",
                    "location",
                    "command",
                    "slack_connected",
                    "workers",
                    "federation",
                    "delivery_mode",
                    "qualification",
                }
                sample = {key: value for key, value in snapshot.items() if key in permitted}
                sample["channels"] = [c for c in snapshot.get("channels", []) if c.get("channel") == body["channel"]]
                result = {"state": "observed" if sample.get("version") else "not_observed", "health": sample}
                if len(canonical(result).encode()) > 12000:
                    result = {"state": "unsupported", "reason": "health_sample_exceeds_bound"}
            else:
                if not body["text"].strip():
                    raise ValueError("Empty operator broadcast")
                targets = [
                    r["session_id"]
                    for r in self.store.participation.roster(include_stopped=False, channel=body["channel"])
                ]
                if targets:
                    control_id, _ = self.store.controls.issue_auxiliary(
                        kind="broadcast",
                        routes=None,
                        session_ids=targets,
                        reason=body["text"],
                        actor="federated:" + origin + ":" + body["actor"],
                        request_key=request_id,
                    )
                result = {
                    "state": "accepted",
                    "targets": len(targets),
                    "read_acknowledged": 0,
                    "execution": "not_inferred",
                }
            self.store._db.execute(
                "INSERT INTO operator_remote_requests VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    origin,
                    request_id,
                    digest,
                    frame["payload"],
                    control_id,
                    canonical(result),
                    1,
                    self.bridge.now(),
                    self.bridge.now(),
                ),
            )
            row = self.store._db.execute(
                "SELECT * FROM operator_remote_requests WHERE origin=? AND request_id=?", (origin, request_id)
            ).fetchone()
            self._send_result(row, result, 1)

    def _receive_result(self, origin, body):
        if (
            set(body) != {"request_id", "request_hash", "revision", "sampled_at", "result"}
            or type(body["revision"]) is not int
            or body["revision"] < 1
        ):
            raise ValueError("Invalid operator result")
        if (
            type(body["sampled_at"]) not in (int, float)
            or not math.isfinite(body["sampled_at"])
            or not 0 <= body["sampled_at"] <= self.bridge.now() + 120
            or not isinstance(body["result"], dict)
            or len(canonical(body["result"]).encode()) > 16000
        ):
            raise ValueError("Invalid bounded health or broadcast result")
        result_payload = body["result"]
        state = result_payload.get("state")
        if not isinstance(state, str) or state not in {
            "observed",
            "not_observed",
            "unsupported",
            "accepted",
            "denied",
            "expired",
        }:
            raise ValueError("Invalid operator result state")
        if state in {"observed", "not_observed"} and not isinstance(result_payload.get("health"), dict):
            raise ValueError("Invalid remote health sample")
        if state in {"observed", "not_observed"}:
            health = result_payload["health"]
            for key in ("runtime", "federation", "location", "workers"):
                if key in health and health[key] is not None and not isinstance(health[key], dict):
                    raise ValueError("Invalid nested health observation")
            channels = health.get("channels", [])
            if not isinstance(channels, list) or len(channels) > 16 or any(not isinstance(c, dict) for c in channels):
                raise ValueError("Invalid scoped health channels")
        if state == "accepted":
            for key in ("targets", "read_acknowledged"):
                if type(result_payload.get(key)) is not int or not 0 <= result_payload[key] <= 65536:
                    raise ValueError("Invalid bounded read receipt count")
            if (
                result_payload["read_acknowledged"] > result_payload["targets"]
                or result_payload.get("execution") != "not_inferred"
            ):
                raise ValueError("Conflicting read or execution outcome")
        row = self.store._db.execute(
            "SELECT * FROM operator_fanout_targets WHERE request_id=? AND origin=?", (body["request_id"], origin)
        ).fetchone()
        if not row or row["request_hash"] != body["request_hash"]:
            raise ValueError("Unbound operator result")
        result = canonical({**body["result"], "sampled_at": body["sampled_at"]})
        if body["revision"] == row["revision"] and row["result"] != result:
            raise ValueError("Conflicting operator result revision")
        if body["revision"] > row["revision"]:
            self.store._db.execute(
                "UPDATE operator_fanout_targets SET result=?,revision=? WHERE request_id=? AND origin=?",
                (result, body["revision"], body["request_id"], origin),
            )

    def refresh(self):
        with self.store._lock, atomic(self.store._db):
            rows = self.store._db.execute(
                "SELECT rowid,* FROM operator_remote_requests WHERE control_id IS NOT NULL AND rowid>? ORDER BY rowid LIMIT 8",
                (self.refresh_after,),
            ).fetchall()
            if not rows:
                self.refresh_after = 0
            for row in rows:
                self.refresh_after = row["rowid"]
                result = self._result(row)
                if canonical(result) != row["result"]:
                    revision = row["revision"] + 1
                    self.store._db.execute(
                        "UPDATE operator_remote_requests SET result=?,revision=?,sampled_at=? WHERE origin=? AND request_id=?",
                        (canonical(result), revision, self.bridge.now(), row["origin"], row["request_id"]),
                    )
                    updated = self.store._db.execute(
                        "SELECT * FROM operator_remote_requests WHERE origin=? AND request_id=?",
                        (row["origin"], row["request_id"]),
                    ).fetchone()
                    self._send_result(updated, result, revision)

    def results(self, request_id, actor, channel):
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT * FROM operator_fanout WHERE request_id=? AND actor=? AND channel=?",
                (request_id, actor, channel),
            ).fetchone()
            if row is None:
                raise ValueError("Unknown operator request in this scope")
            rows = self.store._db.execute(
                "SELECT origin,result FROM operator_fanout_targets WHERE request_id=? ORDER BY origin", (request_id,)
            ).fetchall()
            return [
                {
                    "location": r["origin"],
                    **(
                        json.loads(r["result"])
                        if r["result"]
                        else {
                            "state": "unreachable_or_unknown" if self.bridge.now() - row["created"] > 60 else "pending"
                        }
                    ),
                }
                for r in rows
            ]
