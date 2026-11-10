"""Opt-in A2A 1.0 JSON-RPC translation to existing durable mailbox admission."""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from src.agentbus_transaction_service import atomic


class A2ARejected(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def canonical_uuid(value):
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise A2ARejected(-32602, "Canonical UUID required") from None
    return value


def exact(value, allowed):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise A2ARejected(-32602, "Unsupported or malformed parameters")
    return value


def zero_history(value):
    """The local mailbox profile accepts only an explicit integer-zero history."""
    return type(value) is int and value == 0


def task_scope(principal):
    return [principal.chat_id, principal.session_id, principal.channel, principal.inbox_id]



@dataclass(frozen=True)
class MailboxIntent:
    message_id: str
    context_id: str | None
    recipient: str
    text: str
    digest: str


def decode_intent(params):
    exact(params, {"message", "configuration"})
    message = exact(params.get("message"), {"messageId", "role", "parts", "metadata", "contextId"})
    if message.get("role") != "ROLE_USER":
        raise A2ARejected(-32602, "ROLE_USER required")
    key = canonical_uuid(message.get("messageId"))
    context = canonical_uuid(message["contextId"]) if "contextId" in message else None
    metadata = exact(message.get("metadata"), {"operation", "recipient"})
    if metadata.get("operation") != "mailbox.accept" or not isinstance(metadata.get("recipient"), str):
        raise A2ARejected(-32602, "Named mailbox.accept operation and exact recipient required")
    parts = message.get("parts")
    if not isinstance(parts, list) or len(parts) != 1 or set(exact(parts[0], {"text"})) != {"text"}:
        raise A2ARejected(-32602, "Exactly one bounded text part is supported")
    config = exact(params.get("configuration", {}), {"acceptedOutputModes", "returnImmediately", "historyLength"})
    if ("returnImmediately" in config and type(config["returnImmediately"]) is not bool
            or not zero_history(config.get("historyLength", 0))
            or "acceptedOutputModes" in config and config["acceptedOutputModes"] not in ([], ["application/json"])):
        raise A2ARejected(-32602, "Unsupported send configuration")
    text = parts[0]["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > 6000:
        raise A2ARejected(-32602, "Text must contain1..6000 characters")
    digest = hashlib.sha256(json.dumps(message, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return MailboxIntent(key, context, metadata["recipient"], text, digest)


class A2AStore:
    """Only a bounded adapter index; payload and delivery stay in canonical custody."""
    def __init__(self, owner):
        self.owner = owner
        with owner._lock, atomic(owner._db):
            owner._db.execute("""CREATE TABLE IF NOT EXISTS a2a_tasks (
                task_id TEXT PRIMARY KEY, scope TEXT NOT NULL, message_id TEXT NOT NULL,
                custody_id TEXT NOT NULL, context_id TEXT NOT NULL, digest TEXT NOT NULL,
                public_cursor INTEGER NOT NULL, admitted REAL NOT NULL,
                UNIQUE(scope,message_id))""")

    def _scope(self, principal):
        return json.dumps(task_scope(principal), separators=(",", ":"))

    def row(self, principal, task_id):
        canonical_uuid(task_id)
        with self.owner._lock:
            row = self.owner._db.execute("SELECT * FROM a2a_tasks WHERE task_id=? AND scope=?",
                                         (task_id, self._scope(principal))).fetchone()
            if row is None:
                raise A2ARejected(-32001, "Task not found")
            return row

    def task(self, principal, row):
        facts = self.owner.lifecycle.request_outcome(principal, row["custody_id"])
        receipt = {"operation": "mailbox.accept", "boundary": "origin durable custody",
                   "originalMessageId": row["message_id"], "cursor": row["public_cursor"],
                   "acceptedAt": datetime.fromtimestamp(row["admitted"], timezone.utc).isoformat().replace("+00:00", "Z"),
                   "accepted": True, "retainedTransport": facts,
                   "recipientRead": "unknown", "recipientWork": "unknown", "semanticAcknowledgement": "unknown"}
        return {"id": row["task_id"], "contextId": row["context_id"],
                "status": {"state": "TASK_STATE_COMPLETED", "timestamp": receipt["acceptedAt"]},
                "artifacts": [{"artifactId": row["task_id"], "name": "Mailbox admission receipt",
                               "parts": [{"data": receipt}]}]}

    async def send(self, principal, params, admit):
        from src.agentbus_service import SendMessage
        intent = decode_intent(params)
        key, context, digest = intent.message_id, intent.context_id, intent.digest
        body = SendMessage(sender=principal.route, recipient=intent.recipient, text=intent.text,
                           kind="request", audience="direct")
        custody_key = str(uuid5(NAMESPACE_URL, json.dumps(["agentbus:a2a:1", task_scope(principal), key])))
        scope = self._scope(principal)
        with self.owner._lock, atomic(self.owner._db):
            old = self.owner._db.execute("SELECT * FROM a2a_tasks WHERE scope=? AND message_id=?", (scope, key)).fetchone()
            if old is not None:
                if old["digest"] != digest:
                    raise A2ARejected(-32602, "Original message conflicts with retained task")
                return {"task": self.task(principal, old)}
            if self.owner._db.execute("SELECT count(*) FROM a2a_tasks").fetchone()[0] >= 4096:
                raise A2ARejected(-32603, "Adapter index capacity requires operator review; preserve original message identity")
            self.owner.lifecycle.protect_extensions()
            self.owner.retention.reserve_storage(2048)
            accepted = await admit(body, custody_key)
            task_id = str(uuid4())
            self.owner._db.execute("INSERT INTO a2a_tasks VALUES(?,?,?,?,?,?,?,?)",
                (task_id, scope, key, custody_key, context or str(uuid4()), digest, accepted["cursor"], time.time()))
            return {"task": self.task(principal, self.row(principal, task_id))}

    def list_tasks(self, principal, params):
        # Tokens are random retained task IDs, validated inside the exact scope;
        # no global position, timestamp or foreign activity count is exposed.
        exact(params, {"pageSize", "pageToken", "contextId", "status", "historyLength"})
        limit = params.get("pageSize", 20)
        if type(limit) is not int or not 1 <= limit <= 100 or not zero_history(params.get("historyLength", 0)):
            raise A2ARejected(-32602, "Bounded page size and zero history required")
        if params.get("status", "TASK_STATE_COMPLETED") != "TASK_STATE_COMPLETED":
            raise A2ARejected(-32602, "Only completed mailbox-admission tasks are supported")
        if "pageToken" in params and not isinstance(params["pageToken"], str):
            raise A2ARejected(-32602, "Pagination token must be a string")
        scope = self._scope(principal)
        where, values = "scope=?", [scope]
        if "contextId" in params:
            where += " AND context_id=?"
            values.append(canonical_uuid(params["contextId"]))
        with self.owner._lock:
            total = self.owner._db.execute("SELECT count(*) FROM a2a_tasks WHERE " + where, values).fetchone()[0]
            if params.get("pageToken"):
                row = self.row(principal, params["pageToken"])
                if "contextId" in params and row["context_id"] != params["contextId"]:
                    raise A2ARejected(-32602, "Pagination scope differs")
                where += " AND (admitted<? OR (admitted=? AND task_id<?))"
                values += [row["admitted"], row["admitted"], row["task_id"]]
            rows = self.owner._db.execute("SELECT * FROM a2a_tasks WHERE " + where +
                " ORDER BY admitted DESC,task_id DESC LIMIT ?", [*values, limit + 1]).fetchall()
            return {"tasks": [self.task(principal, row) for row in rows[:limit]], "totalSize": total,
                    "pageSize": limit, "nextPageToken": rows[limit - 1]["task_id"] if len(rows) > limit else ""}
