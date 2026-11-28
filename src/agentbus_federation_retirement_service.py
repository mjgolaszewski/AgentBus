"""Bounded compaction of settled outbound custody, with durable replay pins."""
from __future__ import annotations

import hashlib
import json
import time
import zlib
from datetime import datetime

MAX_ROWS = 65536
MAX_BYTES = 512 * 1024 * 1024
MIN_AGE = 14 * 86400


def initialize_archive(db):
    db.execute('''CREATE TABLE IF NOT EXISTS federation_retired_obligations (
        origin_instance_id TEXT NOT NULL, origin_message_id TEXT NOT NULL,
        destination_instance_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
        origin_payload_hash TEXT NOT NULL, record_json TEXT NOT NULL,
        payload BLOB NOT NULL, original_bytes INTEGER NOT NULL, retired_at REAL NOT NULL,
        PRIMARY KEY(origin_instance_id,origin_message_id,destination_instance_id))''')


def retired_obligation(db, origin, message, destination):
    row = db.execute('SELECT * FROM federation_retired_obligations WHERE origin_instance_id=? '
                     'AND origin_message_id=? AND destination_instance_id=?',
                     (origin, message, destination)).fetchone()
    if row is None:
        return None
    try:
        if not 1 <= row['original_bytes'] <= 48000 or len(row['payload']) > 48100:
            raise ValueError()
        inflater = zlib.decompressobj()
        raw = inflater.decompress(row['payload'], 48001)
        if (len(raw) != row['original_bytes'] or not inflater.eof or inflater.unused_data
                or inflater.unconsumed_tail or hashlib.sha256(raw).hexdigest() != row['payload_hash']):
            raise ValueError()
        if len(row['record_json'].encode()) > 8192:
            raise ValueError()
        record = json.loads(row['record_json'])
        if ((record['origin_instance_id'], record['origin_message_id'], record['destination_instance_id'])
                != (origin, message, destination) or record['state'] != 'acknowledged'
                or record['payload_hash'] != row['payload_hash'] or record['kind'] != 'message'):
            raise ValueError()
        return {**record, 'payload': raw.decode('utf-8'), 'retired': True,
                'origin_payload_hash': row['origin_payload_hash']}
    except (ValueError, KeyError, TypeError, RecursionError, zlib.error):
        raise ValueError('Federation archive integrity requires operator recovery') from None


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


class FederationRetirement:
    """Caller owns the store lock or exclusive offline database lease."""

    def __init__(self, db):
        self.db = db

    def _scope(self):
        row = self.db.execute("SELECT value FROM metadata WHERE key='inbox_id'").fetchone()
        if row is None:
            raise ValueError('Known database scope required')
        return row[0]

    def preview(self, *, now=None, scan_after=None):
        instant = time.time() if now is None else now
        if type(instant) not in (int, float) or not 0 < instant <= time.time() + 1:
            raise ValueError('Preview clock is invalid or ahead of the current clock')
        if scan_after is not None and (not isinstance(scan_after, (list, tuple)) or len(scan_after) != 3
                or any(not isinstance(value, str) or len(value) > 256 for value in scan_after)):
            raise ValueError('Invalid compaction scan checkpoint')
        rows = self.db.execute('SELECT * FROM bridge_obligations WHERE '
            '(origin_instance_id,origin_message_id,destination_instance_id) > (?,?,?) '
            'ORDER BY origin_instance_id,origin_message_id,destination_instance_id LIMIT 1001',
            tuple(scan_after) if scan_after is not None else ('', '', '')).fetchall()
        eligible: list[dict] = []
        pinned = {'unsettled': 0, 'recent_or_unknown': 0, 'reference': 0, 'unsupported_kind': 0}
        for row in rows[:1000]:
            if row['state'] != 'acknowledged':
                pinned['unsettled'] += 1
                continue
            if row['kind'] != 'message':
                pinned['unsupported_kind'] += 1
                continue
            try:
                updated = datetime.fromisoformat(row['updated_at'])
                if updated.tzinfo is None or instant - updated.timestamp() < MIN_AGE:
                    raise ValueError()
            except (ValueError, TypeError, OverflowError):
                pinned['recent_or_unknown'] += 1
                continue
            local = self.db.execute('SELECT 1 FROM local_delivery_obligations WHERE request_id=? LIMIT 1',
                                    (row['origin_message_id'],)).fetchone()
            if local is not None:
                pinned['reference'] += 1
                continue
            if len(eligible) < 100:
                eligible.append({key: row[key] for key in (
                    'origin_instance_id', 'origin_message_id', 'destination_instance_id',
                    'state', 'payload_hash', 'updated_at', 'kind')})
                if len(eligible) == 100:
                    rows = rows[:rows.index(row) + 1]
                    break
        result = {'version': 1, 'scope': self._scope(), 'as_of': instant,
                  'eligible': eligible, 'pinned': pinned,
                  'partial': len(rows) > 1000 or len(eligible) == 100,
                  'scan_after': list(scan_after) if scan_after is not None else None,
                  'scan_next': [rows[min(len(rows), 1000)-1][key] for key in
                      ('origin_instance_id', 'origin_message_id', 'destination_instance_id')] if rows else None}
        return {**result, 'digest': _digest(result)}

    def apply(self, plan):
        if (not isinstance(plan, dict) or len(json.dumps(plan).encode()) > 262144
                or set(plan) != {'version', 'scope', 'as_of', 'eligible', 'pinned', 'partial', 'digest', 'scan_after', 'scan_next'}
                or plan.get('version') != 1 or plan['digest'] != _digest({k:v for k,v in plan.items() if k != 'digest'})):
            raise ValueError('Exact retirement preview digest required')
        if plan['scope'] != self._scope():
            raise ValueError('Retirement preview database scope differs')
        if self.db.in_transaction:
            raise ValueError('Custody compaction requires its own transaction')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            if self.preview(now=plan['as_of'], scan_after=plan['scan_after']) != plan:
                raise ValueError('Retirement preview changed; inspect a fresh preview')
            initialize_archive(self.db)
            count, used = self.db.execute('SELECT count(*),coalesce(sum(length(payload)),0) '
                                          'FROM federation_retired_obligations').fetchone()
            for item in plan['eligible']:
                key = tuple(item[k] for k in ('origin_instance_id', 'origin_message_id', 'destination_instance_id'))
                row = self.db.execute('SELECT * FROM bridge_obligations WHERE origin_instance_id=? '
                    'AND origin_message_id=? AND destination_instance_id=?', key).fetchone()
                origin = self.db.execute('SELECT payload_hash FROM federation_origin_messages '
                    'WHERE origin_instance_id=? AND origin_message_id=?', key[:2]).fetchone()
                if row is None or origin is None:
                    raise ValueError('Retirement preview lost original custody')
                raw = row['payload'].encode()
                if not 1 <= len(raw) <= 48000 or hashlib.sha256(raw).hexdigest() != row['payload_hash']:
                    raise ValueError('Original custody hash mismatch')
                compressed = zlib.compress(raw)
                count += 1
                used += len(compressed)
                if count > MAX_ROWS or used > MAX_BYTES:
                    raise ValueError('Retirement archive capacity exhausted; custody retained')
                record = {k:row[k] for k in row.keys() if k != 'payload'}
                record_json = json.dumps(record, sort_keys=True)
                if len(record_json.encode()) > 8192:
                    raise ValueError('Retirement archive metadata bound exceeded; custody retained')
                self.db.execute('INSERT INTO federation_retired_obligations VALUES(?,?,?,?,?,?,?,?,?)',
                    (*key, row['payload_hash'], origin['payload_hash'], record_json,
                     compressed, len(raw), time.time()))
                self.db.execute('DELETE FROM bridge_obligations WHERE origin_instance_id=? '
                    'AND origin_message_id=? AND destination_instance_id=?', key)
                if self.db.execute('SELECT 1 FROM bridge_obligations WHERE origin_instance_id=? '
                                   'AND origin_message_id=? LIMIT 1', key[:2]).fetchone() is None:
                    self.db.execute('DELETE FROM federation_origin_messages WHERE origin_instance_id=? '
                                    'AND origin_message_id=?', key[:2])
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
        return {'archived': len(plan['eligible']), 'digest': plan['digest'],
                'inbound_retirement': 'unsupported_pinned', 'semantic_acknowledgement_inferred': False}


class AutomaticFederationCompaction:
    """Opt-in hourly maintenance using the existing delivery tick and store lock."""

    def __init__(self, owner, *, now=time.time):
        self.owner, self.now = owner, now
        self.next_at = 0.0

    def tick(self):
        instant = self.now()
        if instant < self.next_at:
            return None
        self.next_at = instant + 3600
        with self.owner._lock:
            db = self.owner._db
            row = db.execute("SELECT value FROM metadata WHERE key='federation_compaction_latest_v1'").fetchone()
            previous = json.loads(row[0]) if row else {}
            if not isinstance(previous, dict) or (previous and (
                    type(previous.get('observed_at')) not in (int, float)
                    or not 0 < previous['observed_at'] <= instant)):
                raise ValueError('Compaction checkpoint requires operator recovery')
            if previous and instant - previous['observed_at'] < 3600:
                return None
            owner = FederationRetirement(db)
            plan = owner.preview(now=instant, scan_after=previous.get('scan_next'))
            report = owner.apply(plan) if plan['eligible'] else {'archived': 0}
            report.update({'observed_at': instant, 'scan_next': plan['scan_next'],
                           'pinned': plan['pinned'], 'minimum_age_seconds': MIN_AGE,
                           'inbound_retirement': 'unsupported_pinned'})
            with db:
                db.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES(?,?)",
                           ('federation_compaction_latest_v1', json.dumps(report, sort_keys=True)))
            return report
