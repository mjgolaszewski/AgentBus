"""Versioned local custody in the existing SQLite writer, before Slack effects."""
from __future__ import annotations

import hashlib
import json
import re
import time
from typing import TYPE_CHECKING
from uuid import UUID

from src.agentbus_message_assurance_service import route_parts

if TYPE_CHECKING:
    from src.agentbus_service import Message, MessageStore, SendMessage

LOCATOR_PREFIX = 'agentbus-local-'
LOCAL_REFERENCE_PREFIX = 'local:'
MAX_LOCAL_OBLIGATIONS = 1024
MAX_LOCAL_PAYLOAD_BYTES = 48000


class LocalDeliveryRejected(ValueError):
    def __init__(self, status: int, reason: str):
        self.status = status
        super().__init__(reason)


def request_identity(value: str | None) -> str:
    try:
        if value is None or str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, AttributeError):
        raise LocalDeliveryRejected(422, 'A canonical UUID Idempotency-Key is required') from None
    return value


def fingerprint(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _locator_key(blocks: object) -> str | None:
    if not isinstance(blocks, list):
        return None
    marked = [b for b in blocks if isinstance(b, dict)
              and isinstance(b.get('block_id'), str) and b['block_id'].startswith(LOCATOR_PREFIX)]
    if any(b.get('type') != 'context' for b in marked):
        raise ValueError('local locator must retain its context block type')
    locators = [b['block_id'] for b in marked]
    if not locators:
        return None
    if len(locators) != 1:
        raise ValueError('ambiguous local delivery locator')
    return request_identity(locators[0][len(LOCATOR_PREFIX):])


class LocalDeliveryStore:
    def __init__(self, owner: MessageStore):
        self.owner = owner
        with owner._lock, owner._db:
            owner._db.execute('''CREATE TABLE IF NOT EXISTS local_delivery_obligations (
                channel TEXT NOT NULL, request_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                local_cursor INTEGER NOT NULL UNIQUE REFERENCES messages(cursor),
                state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, provider_ref TEXT,
                provider_failure TEXT, failure_ref TEXT,
                PRIMARY KEY(channel, request_id))''')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS local_delivery_observations (
                channel TEXT NOT NULL, provider_ref TEXT NOT NULL,
                local_cursor INTEGER NOT NULL REFERENCES messages(cursor),
                PRIMARY KEY(channel, provider_ref))''')

    def accept(self, channel: str, key: str, request_hash: str, message: SendMessage,
               principal: str, *, bridge=None) -> Message:
        store = self.owner
        request_hash = fingerprint({'principal': principal, 'request': request_hash})
        if len(message.model_dump_json().encode()) > MAX_LOCAL_PAYLOAD_BYTES:
            raise LocalDeliveryRejected(422, 'Local custody payload exceeds its byte limit')
        with store._lock, store._db:
            store._db.execute('BEGIN IMMEDIATE')
            prior = store._db.execute(
                'SELECT * FROM local_delivery_obligations WHERE channel=? AND request_id=?',
                (channel, key)).fetchone()
            if prior is not None:
                if prior['request_hash'] != request_hash:
                    raise LocalDeliveryRejected(409, 'Idempotency-Key already binds another request')
                existing = store.message(channel, prior['local_cursor'])
                if existing is None:
                    raise ValueError('accepted local custody lost its message')
                return existing
            if store._db.execute('SELECT count(*) FROM local_delivery_obligations').fetchone()[0] >= MAX_LOCAL_OBLIGATIONS:
                raise LocalDeliveryRejected(503, 'Local custody capacity requires operator archival')
            sender, recipient = route_parts(message.sender), route_parts(message.recipient)
            local_prefix = bridge.identity.route_prefix if bridge is not None else sender[0] if sender else None
            destination = None
            if recipient is not None and recipient[0] is not None and recipient[0] != local_prefix:
                if bridge is None:
                    raise LocalDeliveryRejected(503, 'Remote custody requires an approved federation binding')
                destination = next((origin for origin, binding in bridge.bindings.items()
                                    if binding.route_prefix == recipient[0]), None)
                if destination is None:
                    raise LocalDeliveryRejected(422, 'Remote route has no approved federation binding')
                if message.reply_to_cursor is not None or message.thread_ts is not None:
                    raise LocalDeliveryRejected(422, 'Remote ancestry requires a versioned origin-parent mapping')
            store.send_rate._reserve(principal)
            accepted = store._append(channel, LOCAL_REFERENCE_PREFIX + key, message)
            store._db.execute('''INSERT INTO local_delivery_obligations
                (channel,request_id,request_hash,local_cursor,state) VALUES (?,?,?,?,?)''',
                (channel, key, request_hash, accepted.cursor, 'federated' if destination else 'pending'))
            if destination is not None:
                bridge.enqueue(key, destination, message.model_dump_json())
            return accepted

    def observation_cursor(self, channel: str, provider_ref: str) -> int | None:
        with self.owner._lock:
            row = self.owner._db.execute('''SELECT local_cursor FROM local_delivery_observations
                WHERE channel=? AND provider_ref=?''', (channel, provider_ref)).fetchone()
        return row[0] if row else None

    def pending(self, *, channel: str | None = None, now: float | None = None) -> list[dict]:
        instant = time.time() if now is None else now
        with self.owner._lock:
            return [dict(row) for row in self.owner._db.execute('''
                SELECT o.*, m.payload, m.received_at FROM local_delivery_obligations o
                JOIN messages m ON m.cursor=o.local_cursor
                WHERE o.state IN ('pending','uncertain','submitting') AND o.attempts<8
                    AND o.next_attempt<=? AND (? IS NULL OR o.channel=?)
                ORDER BY CASE WHEN o.attempts=0 THEN 0 ELSE 1 END,o.local_cursor LIMIT 100''',
                (instant, channel, channel))]

    def recovery_pending(self, channel: str, *, now: float | None = None) -> list[dict]:
        instant = time.time() if now is None else now
        with self.owner._lock:
            return [dict(row) for row in self.owner._db.execute("""
                SELECT o.*,m.payload,m.received_at FROM local_delivery_obligations o
                JOIN messages m ON m.cursor=o.local_cursor
                WHERE o.channel=? AND o.state IN ('uncertain','submitting','blocked') AND o.next_attempt<=?
                ORDER BY CASE WHEN o.state='blocked' OR o.attempts>=8 THEN 1 ELSE 0 END,o.local_cursor LIMIT 100""",
                (channel, instant))]

    def defer_recovery(self, rows: list[dict], *, now: float | None = None) -> None:
        instant = time.time() if now is None else now
        with self.owner._lock, self.owner._db:
            self.owner._db.executemany("""UPDATE local_delivery_obligations
                SET next_attempt=MAX(next_attempt,?) WHERE channel=? AND request_id=?
                AND state IN ('uncertain','submitting','blocked')""",
                [(instant + 60, row['channel'], row['request_id']) for row in rows])

    def reserve(self, channel: str, key: str, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        with self.owner._lock, self.owner._db:
            row = self.owner._db.execute('''SELECT attempts,next_attempt,state
                FROM local_delivery_obligations WHERE channel=? AND request_id=?''', (channel, key)).fetchone()
            if (row is None or row['state'] not in {'pending', 'uncertain', 'submitting'}
                    or row['attempts'] >= 8 or row['next_attempt'] > instant):
                return False
            self.owner._db.execute('''UPDATE local_delivery_obligations
                SET attempts=attempts+1,state='submitting',next_attempt=? WHERE channel=? AND request_id=?''',
                (instant + min(600, 60 * 2 ** row['attempts']), channel, key))
            return True

    def uncertain(self, channel: str, key: str) -> None:
        with self.owner._lock, self.owner._db:
            self.owner._db.execute('''UPDATE local_delivery_obligations SET state='uncertain'
                WHERE channel=? AND request_id=? AND state='submitting' ''', (channel, key))

    def blocked(self, channel: str, key: str, reason: str, reference: str) -> None:
        if reason not in {'missing_echo', 'changed_echo', 'truncation_warning'}:
            raise ValueError('invalid local carrier failure')
        with self.owner._lock, self.owner._db:
            self.owner._db.execute('''UPDATE local_delivery_obligations
                SET state='blocked',provider_failure=?,failure_ref=?
                WHERE channel=? AND request_id=? AND state!='submitted' ''',
                (reason, reference, channel, key))

    def complete(self, channel: str, key: str, provider_ref: str) -> None:
        if not isinstance(provider_ref, str) or not re.fullmatch(r'[1-9][0-9]{0,19}\.[0-9]{1,20}', provider_ref):
            raise ValueError('invalid provider reference')
        store = self.owner
        with store._lock, store._db:
            store._db.execute('BEGIN IMMEDIATE')
            row = store._db.execute('SELECT * FROM local_delivery_obligations WHERE channel=? AND request_id=?',
                                    (channel, key)).fetchone()
            if row is None:
                raise ValueError('unknown local delivery')
            existing = self.observation_cursor(channel, provider_ref)
            if existing is not None and existing != row['local_cursor']:
                raise ValueError('provider observation already belongs to another message')
            occupied = store._db.execute('SELECT cursor FROM messages WHERE channel=? AND slack_ts=?',
                                         (channel, provider_ref)).fetchone()
            if occupied is not None and occupied[0] != row['local_cursor']:
                raise ValueError('provider timestamp collision')
            if existing is None:
                if store._db.execute('SELECT count(*) FROM local_delivery_observations').fetchone()[0] >= 8192:
                    raise ValueError('local provider observation capacity exhausted')
                store._db.execute('INSERT INTO local_delivery_observations VALUES (?,?,?)',
                                  (channel, provider_ref, row['local_cursor']))
            if row['provider_ref'] is None:
                store._db.execute('UPDATE messages SET slack_ts=? WHERE cursor=?',
                                  (provider_ref, row['local_cursor']))
            store._db.execute('''UPDATE local_delivery_obligations SET state='submitted',
                provider_ref=COALESCE(provider_ref,?) WHERE channel=? AND request_id=?''',
                (provider_ref, channel, key))

    def accept_echo(self, channel: str, provider_ref: str, message: SendMessage, blocks: object) -> bool:
        key = _locator_key(blocks)
        if key is None:
            return False
        with self.owner._lock:
            row = self.owner._db.execute('''SELECT m.payload FROM local_delivery_obligations o
                JOIN messages m ON m.cursor=o.local_cursor WHERE o.channel=? AND o.request_id=?''',
                (channel, key)).fetchone()
            if row is None:
                return False
            if fingerprint(json.loads(row['payload'])) != fingerprint(message.model_dump()):
                raise ValueError('provider echo conflicts with accepted local content')
            self.complete(channel, key, provider_ref)
            return True


    def accept_event_echo(self, channel: str, provider_ref: str, text: object,
                          blocks: object, thread_ts: object = None) -> bool:
        """Caller proves the bot; exact stored wire content proves the local obligation."""
        from src.agentbus_service import SendMessage, encode_envelope

        key = _locator_key(blocks)
        if key is None:
            return False
        with self.owner._lock:
            row = self.owner._db.execute("""SELECT m.payload FROM local_delivery_obligations o
                JOIN messages m ON m.cursor=o.local_cursor WHERE o.channel=? AND o.request_id=?""",
                (channel, key)).fetchone()
            if row is None:
                return False
            expected = SendMessage.model_validate_json(row['payload'])
            if text != encode_envelope(expected):
                raise ValueError('provider wire echo conflicts with accepted local content')
            if thread_ts is not None and not isinstance(thread_ts, str):
                raise ValueError('invalid provider thread reference')
            allowed = {expected.thread_ts} if expected.thread_ts else {None, provider_ref}
            if thread_ts not in allowed:
                raise ValueError('provider thread conflicts with accepted local ancestry')
            return self.accept_echo(channel, provider_ref, expected, blocks)
