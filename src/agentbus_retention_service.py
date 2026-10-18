"""Bounded cold payload custody in the same SQLite writer; no cache owns message truth."""
from __future__ import annotations

import hashlib
import json
import re
import zlib
from decimal import Decimal
from pathlib import Path

from src.agentbus_message_lifecycle_service import LifecycleRejected
from src.agentbus_transaction_service import atomic

MAX_PAYLOAD = 65536


class RetentionStore:
    def __init__(self, owner):
        self.owner = owner
        self.last_checkpoint = None
        with owner._lock, atomic(owner._db):
            if 'sender' not in {row['name'] for row in owner._db.execute('PRAGMA table_info(messages)')}:
                owner._db.execute('ALTER TABLE messages ADD COLUMN sender TEXT')
            owner._db.execute("UPDATE messages SET sender=json_extract(payload,'$.sender') WHERE sender IS NULL AND payload!='null'")
            owner._db.execute('CREATE INDEX IF NOT EXISTS messages_sender_cursor ON messages(channel,sender,cursor)')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS lifecycle_edges (
                cursor INTEGER PRIMARY KEY REFERENCES messages(cursor),
                parent INTEGER NOT NULL REFERENCES messages(cursor))''')
            owner._db.execute('CREATE INDEX IF NOT EXISTS lifecycle_parent ON lifecycle_edges(parent)')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS lifecycle_retired_positions (
                channel TEXT NOT NULL,first_position INTEGER NOT NULL,last_position INTEGER NOT NULL,
                PRIMARY KEY(channel,first_position))''')

    def has_retired_positions(self, channel, after, through):
        with self.owner._lock:
            return self.owner._db.execute('SELECT 1 FROM lifecycle_retired_positions WHERE channel=? '
                'AND last_position>? AND first_position<=? LIMIT 1', (channel, after, through)).fetchone() is not None

    def _retire_position(self, channel, position):
        ranges = self.owner._db.execute('SELECT first_position,last_position FROM lifecycle_retired_positions '
            'WHERE channel=? AND first_position<=? AND last_position>=?', (channel, position+1, position-1)).fetchall()
        first = min([position, *(row[0] for row in ranges)])
        last = max([position, *(row[1] for row in ranges)])
        self.owner._db.executemany('DELETE FROM lifecycle_retired_positions WHERE channel=? AND first_position=?',
                                  [(channel, row[0]) for row in ranges])
        self.owner._db.execute('INSERT INTO lifecycle_retired_positions VALUES (?,?,?)', (channel, first, last))

    def storage(self):
        """Account allocated database pages and WAL/SHM; archives live inside those pages."""
        with self.owner._lock:
            path = Path(self.owner._db.execute('PRAGMA database_list').fetchone()['file'])
            pages = self.owner._db.execute('PRAGMA page_count').fetchone()[0]
            page_size = self.owner._db.execute('PRAGMA page_size').fetchone()[0]
            sidecars = sum(candidate.stat().st_size for suffix in ('-wal','-shm')
                if (candidate := Path(str(path) + suffix)).exists())
            reusable = self.owner._db.execute('PRAGMA freelist_count').fetchone()[0] * page_size
            allocated = max(path.stat().st_size, pages * page_size)
            return {'accounted_bytes':allocated - reusable + sidecars,
                    'physical_bytes':allocated + sidecars, 'reusable_bytes':reusable,
                    'limit_bytes':self.owner.lifecycle.policy.storage_bytes,
                    'page_size':page_size}

    def checkpoint(self):
        """Service-wide bounded SQLite maintenance; never commits a caller or deletes custody."""
        with self.owner._lock:
            if self.owner._db.in_transaction:
                raise ValueError('Checkpoint deferred: a writer transaction is active')
            previous = self.owner._db.execute('PRAGMA busy_timeout').fetchone()[0]
            try:
                self.owner._db.execute('PRAGMA busy_timeout=0')
                busy, frames, copied = self.owner._db.execute('PRAGMA wal_checkpoint(PASSIVE)').fetchone()
                if not busy and frames == copied:
                    busy, frames, copied = self.owner._db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                result = {'status':'deferred' if busy or frames != copied else 'completed',
                          'busy':busy, 'log_frames':frames, 'checkpointed_frames':copied,
                          **self.storage()}
                self.last_checkpoint = result
                return result
            finally:
                self.owner._db.execute(f'PRAGMA busy_timeout={previous}')

    def reserve_storage(self, payload_bytes):
        if not self.owner.lifecycle.enabled:
            return
        status = self.storage()
        # Reserve index/WAL headroom before accepting a new durable payload.
        required = 2 * payload_bytes + 16 * status['page_size']
        if status['accounted_bytes'] + required > status['limit_bytes']:
            raise LifecycleRejected(503, 'Lifecycle storage capacity requires operator retention or capacity recovery')

    def payload(self, channel, cursor, raw):
        if raw != 'null':
            return raw
        with self.owner._lock:
            row = self.owner._db.execute('SELECT * FROM lifecycle_archive WHERE channel=? AND cursor=?',
                                         (channel, cursor)).fetchone()
            if row is None or not 1 <= row['original_bytes'] <= MAX_PAYLOAD or len(row['payload']) > MAX_PAYLOAD:
                raise ValueError('Archived payload custody is missing or exceeds bounds')
            inflater = zlib.decompressobj()
            decoded = inflater.decompress(row['payload'], MAX_PAYLOAD + 1)
            if (len(decoded) != row['original_bytes'] or not inflater.eof
                    or inflater.unused_data or inflater.unconsumed_tail
                    or hashlib.sha256(decoded).hexdigest() != row['sha256']):
                raise ValueError('Archived payload failed integrity verification')
            result = decoded.decode('utf-8')
            if not isinstance(json.loads(result), dict):
                raise ValueError('Archived payload is not a message')
            return result

    def _eligible(self, channel, current, *, purge=False):
        # Legacy and federated custody stay pinned; no remote expiry is inferred.
        field = 'purge_after' if purge else 'archive_after'
        return self.owner._db.execute(f'''SELECT m.* FROM messages m
            JOIN lifecycle_payloads p ON p.cursor=m.cursor
            WHERE m.channel=? AND p.legacy_unknown=0 AND p.{field}<=?
            AND NOT EXISTS (SELECT 1 FROM lifecycle_targets t WHERE t.cursor=m.cursor
                AND t.state NOT IN ('handled','expired_unclaimed'))
            AND NOT EXISTS (SELECT 1 FROM lifecycle_requests r WHERE r.cursor=m.cursor AND r.replay_until>?)
            AND NOT EXISTS (SELECT 1 FROM local_delivery_obligations o WHERE o.local_cursor=m.cursor
                AND (o.state NOT IN ('submitted','expired_unsubmitted') OR o.federation_frame_hash IS NOT NULL))
            AND NOT EXISTS (SELECT 1 FROM federation_inbox_projection f WHERE f.cursor=m.cursor)
            AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.cursor=m.cursor)
            AND NOT EXISTS (SELECT 1 FROM claim_recovery_audit c WHERE c.channel=m.channel AND c.cursor=m.cursor)
            AND NOT EXISTS (SELECT 1 FROM lifecycle_edges e WHERE e.parent=m.cursor)
            AND NOT EXISTS (SELECT 1 FROM messages child WHERE child.channel=m.channel
                AND child.cursor!=m.cursor AND (child.thread_ts=m.slack_ts OR child.thread_ts IN
                    (SELECT provider_ref FROM local_delivery_observations o WHERE o.channel=m.channel AND o.local_cursor=m.cursor)))
            AND m.payload {'=' if purge else '!='} 'null'
            ORDER BY m.cursor LIMIT 100''', (channel, current, current)).fetchall()

    def archive(self, channel, *, actor, request_key=None, apply=False, instant=None):
        lifecycle = self.owner.lifecycle
        if not lifecycle.enabled:
            raise LifecycleRejected(409, 'Message lifecycle is not enabled')
        if not isinstance(actor, str) or not actor or len(actor) > 256:
            raise LifecycleRejected(422, 'An operator actor is required')
        if apply and (not isinstance(request_key, str) or not request_key or len(request_key) > 256):
            raise LifecycleRejected(422, 'A bounded operation identity is required')
        with self.owner._lock, atomic(self.owner._db):
            if not self.owner._db.execute('SELECT 1 FROM bus_channels WHERE channel=?', (channel,)).fetchone():
                raise LifecycleRejected(404, 'Channel is not configured')
            if apply:
                prior = self.owner._db.execute('SELECT * FROM lifecycle_operations WHERE request_key=?', (request_key,)).fetchone()
                if prior:
                    if (prior['channel'], prior['actor'], prior['action']) != (channel, actor, 'archive'):
                        raise LifecycleRejected(409, 'Operation identity already binds another command')
                    return json.loads(prior['result'])
            current = lifecycle.now(instant)
            rows = self._eligible(channel, current)
            result = {'channel':channel, 'applied':apply, 'eligible':len(rows), 'batch_limit':100}
            if apply:
                for row in rows:
                    raw = row['payload'].encode('utf-8')
                    if not 1 <= len(raw) <= MAX_PAYLOAD:
                        raise ValueError('Payload exceeds archive byte bound')
                    self.reserve_storage(len(raw))
                    self.owner._db.execute('INSERT INTO lifecycle_archive VALUES (?,?,?,?,?,?)',
                        (row['cursor'], channel, zlib.compress(raw), hashlib.sha256(raw).hexdigest(), len(raw), current))
                    self.owner._db.execute("UPDATE messages SET payload='null' WHERE cursor=?", (row['cursor'],))
                    self.payload(channel, row['cursor'], 'null')  # Verify before publishing the transaction.
                lifecycle.now(current, persist=True)
                self.owner._db.execute('INSERT INTO lifecycle_operations VALUES (?,?,?,?,?,?)',
                    (request_key, channel, actor, 'archive', current, json.dumps(result, sort_keys=True)))
            return result

    def purge(self, channel, *, actor, request_key=None, apply=False, instant=None):
        """Retire verified closed payloads and their retry rows after reference closure."""
        lifecycle = self.owner.lifecycle
        if not lifecycle.enabled:
            raise LifecycleRejected(409, 'Message lifecycle is not enabled')
        if not isinstance(actor, str) or not actor or len(actor) > 256:
            raise LifecycleRejected(422, 'An operator actor is required')
        if apply and (not isinstance(request_key, str) or not request_key or len(request_key) > 256):
            raise LifecycleRejected(422, 'A bounded operation identity is required')
        with self.owner._lock, atomic(self.owner._db):
            if not self.owner._db.execute('SELECT 1 FROM bus_channels WHERE channel=?', (channel,)).fetchone():
                raise LifecycleRejected(404, 'Channel is not configured')
            if apply:
                prior = self.owner._db.execute('SELECT * FROM lifecycle_operations WHERE request_key=?', (request_key,)).fetchone()
                if prior:
                    if (prior['channel'], prior['actor'], prior['action']) != (channel, actor, 'purge'):
                        raise LifecycleRejected(409, 'Operation identity already binds another command')
                    return json.loads(prior['result'])
            current = lifecycle.now(instant)
            rows = self._eligible(channel, current, purge=True)
            # Refuse future schema references instead of guessing their retirement policy.
            owned = {'lifecycle_targets','lifecycle_payloads','lifecycle_archive','lifecycle_requests',
                     'local_delivery_obligations','local_delivery_observations','channel_positions',
                     'lifecycle_edges'}
            foreign = []
            for table, in self.owner._db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
                if table in owned:
                    continue
                quoted = '"' + table.replace('"', '""') + '"'
                for fk in self.owner._db.execute(f'PRAGMA foreign_key_list({quoted})'):
                    if fk['table'] == 'messages':
                        column = '"' + fk['from'].replace('"', '""') + '"'
                        foreign.append((quoted, column))
            rows = [row for row in rows if not any(self.owner._db.execute(
                f'SELECT 1 FROM {table} WHERE {column}=? LIMIT 1', (row['cursor'],)).fetchone()
                for table, column in foreign)]
            result = {'channel':channel, 'applied':apply, 'eligible':len(rows), 'batch_limit':100}
            if apply:
                for row in rows:
                    self.payload(channel, row['cursor'], row['payload'])
                    position = self.owner._db.execute('SELECT position FROM channel_positions WHERE channel=? AND message_id=?',
                                                      (channel, row['cursor'])).fetchone()
                    if position is None:
                        raise ValueError('Retirement requires its original channel-local position')
                    refs = [row['slack_ts'], *(item[0] for item in self.owner._db.execute(
                        'SELECT provider_ref FROM local_delivery_observations WHERE channel=? AND local_cursor=?',
                        (channel, row['cursor'])))]
                    floor = max([Decimal('0'), *(min(Decimal(ref), Decimal(str(max(0, current-lifecycle.policy.retention_seconds))))
                        for ref in refs if re.fullmatch(r'[0-9]{1,20}\.[0-9]{1,20}', ref))])
                    old = self.owner._db.execute('SELECT provider_floor FROM lifecycle_ingress WHERE channel=?', (channel,)).fetchone()
                    floor = max(floor, Decimal(old[0]) if old else Decimal('0'))
                    self.owner._db.execute('INSERT OR REPLACE INTO lifecycle_ingress VALUES (?,?)', (channel, str(floor)))
                    for table, column in (('lifecycle_edges','cursor'), ('lifecycle_targets','cursor'),
                        ('lifecycle_requests','cursor'), ('lifecycle_archive','cursor'),
                        ('lifecycle_payloads','cursor'), ('local_delivery_observations','local_cursor'),
                        ('local_delivery_obligations','local_cursor'), ('channel_positions','message_id')):
                        self.owner._db.execute(f'DELETE FROM {table} WHERE {column}=?', (row['cursor'],))
                    self.owner._db.execute('DELETE FROM messages WHERE cursor=?', (row['cursor'],))
                    self._retire_position(channel, position[0])
                lifecycle.now(current, persist=True)
                self.owner._db.execute('INSERT INTO lifecycle_operations VALUES (?,?,?,?,?,?)',
                    (request_key, channel, actor, 'purge', current, json.dumps(result, sort_keys=True)))
            return result

    def accepts_ingress(self, channel, timestamp):
        if not self.owner.lifecycle.enabled:
            return True
        with self.owner._lock:
            row = self.owner._db.execute('SELECT provider_floor FROM lifecycle_ingress WHERE channel=?', (channel,)).fetchone()
            if row is None:
                return True
            return bool(isinstance(timestamp, str) and re.fullmatch(r'[0-9]{1,20}\.[0-9]{1,20}', timestamp)
                        and Decimal(timestamp) > Decimal(row[0]))
