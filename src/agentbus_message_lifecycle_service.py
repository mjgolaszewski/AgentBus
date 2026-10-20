"""Owning transaction for local pickup, terminal outcomes and replay epochs."""
from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass
from uuid import UUID

from src.agentbus_transaction_service import atomic


class LifecycleRejected(ValueError):
    def __init__(self, status, detail):
        self.status = status
        super().__init__(detail)


@dataclass(frozen=True)
class LifecyclePolicy:
    pickup_seconds: int = 86400
    work_lease_seconds: int = 86400
    epoch_seconds: int = 86400
    replay_seconds: int = 604800
    archive_seconds: int = 604800
    retention_seconds: int = 2592000
    storage_bytes: int = 1073741824

    def __post_init__(self):
        values = vars(self)
        if any(type(v) is not int or v <= 0 for v in values.values()):
            raise ValueError('Lifecycle policy requires finite positive integers')
        if (self.pickup_seconds > 604800 or self.work_lease_seconds > 604800
                or self.epoch_seconds > self.replay_seconds
                or self.replay_seconds > self.archive_seconds
                or self.archive_seconds >= self.retention_seconds
                or self.retention_seconds > 31536000
                or not 1048576 <= self.storage_bytes <= 1099511627776):
            raise ValueError('Lifecycle policy windows or storage bounds are invalid')


class MessageLifecycleStore:
    def __init__(self, owner):
        self.owner = owner
        self.policy = LifecyclePolicy()
        self._clock_floor = 0.0
        owner._db.create_function("agentbus_message_lifecycle_v1", 0, lambda: 1, deterministic=True)
        with owner._lock, atomic(owner._db):
            for statement in (
                '''CREATE TABLE IF NOT EXISTS lifecycle_epochs (
                    epoch TEXT PRIMARY KEY,channel TEXT NOT NULL,inbox_id TEXT NOT NULL,
                    accepts_until REAL NOT NULL,retries_until REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_requests (
                    channel TEXT NOT NULL,request_id TEXT NOT NULL,epoch TEXT,
                    cursor INTEGER NOT NULL REFERENCES messages(cursor),replay_until REAL NOT NULL,
                    PRIMARY KEY(channel,request_id))''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_targets (
                    cursor INTEGER NOT NULL REFERENCES messages(cursor),chat_id TEXT NOT NULL,
                    channel TEXT NOT NULL,state TEXT NOT NULL,deadline REAL NOT NULL,
                    pickup_session TEXT,pickup_token TEXT,lease_until REAL,outcome_at REAL,
                    PRIMARY KEY(cursor,chat_id))''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_payloads (
                    cursor INTEGER PRIMARY KEY REFERENCES messages(cursor),channel TEXT NOT NULL,
                    created_at REAL NOT NULL,archive_after REAL NOT NULL,purge_after REAL NOT NULL,
                    legacy_unknown INTEGER NOT NULL DEFAULT 0)''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_archive (
                    cursor INTEGER PRIMARY KEY REFERENCES messages(cursor),channel TEXT NOT NULL,
                    payload BLOB NOT NULL,sha256 TEXT NOT NULL,original_bytes INTEGER NOT NULL,
                    archived_at REAL NOT NULL)''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_ingress (
                    channel TEXT PRIMARY KEY,provider_floor TEXT NOT NULL DEFAULT '0')''',
                '''CREATE TABLE IF NOT EXISTS lifecycle_operations (
                    request_key TEXT PRIMARY KEY,channel TEXT NOT NULL,actor TEXT NOT NULL,
                    action TEXT NOT NULL,at REAL NOT NULL,result TEXT NOT NULL)''',
                'CREATE INDEX IF NOT EXISTS lifecycle_target_deadline ON lifecycle_targets(state,deadline)',
            ):
                owner._db.execute(statement)
        marker = owner._db.execute("SELECT value FROM metadata WHERE key='message_lifecycle_version'").fetchone()
        if marker is not None and marker[0] != '1':
            raise ValueError('Unsupported message lifecycle version')
        self.enabled = marker is not None
        if self.enabled:
            policy = owner._db.execute("SELECT value FROM metadata WHERE key='message_lifecycle_policy'").fetchone()
            if policy is None:
                raise ValueError('Lifecycle policy custody is missing')
            self.policy = LifecyclePolicy(**json.loads(policy[0]))

    def status(self, channel):
        """Read durable outcomes without settling deadlines or inferring acknowledgements."""
        with self.owner._lock:
            if not self.enabled:
                return {'enabled': False}
            recipients = dict(self.owner._db.execute(
                'SELECT state,count(*) FROM lifecycle_targets WHERE channel=? GROUP BY state',
                (channel,)).fetchall())
            payloads = self.owner._db.execute(
                'SELECT count(*),COALESCE(sum(legacy_unknown),0) FROM lifecycle_payloads WHERE channel=?',
                (channel,)).fetchone()
            archive = self.owner._db.execute(
                'SELECT count(*),COALESCE(sum(length(payload)),0) FROM lifecycle_archive WHERE channel=?',
                (channel,)).fetchone()
            pinned = self.owner._db.execute('SELECT COALESCE(sum(length(CAST(m.payload AS BLOB))),0) '
                'FROM messages m JOIN lifecycle_payloads p USING(cursor) WHERE m.channel=? AND p.legacy_unknown=1',
                (channel,)).fetchone()[0]
            announcements = getattr(self.owner, 'announcements', None)
            return {'enabled': True, 'recipients': recipients, 'retained_payloads': payloads[0],
                    'announcements':announcements.counts(channel) if announcements is not None else {},
                    'legacy_unknown_pinned': payloads[1], 'archived_payloads': archive[0],
                    'legacy_pinned_payload_bytes':pinned,
                    'archive_bytes': archive[1]}

    def reconcile(self, channel, *, actor, request_key=None, apply=False, instant=None):
        """Operator command: preview or idempotently settle only one explicit channel."""
        if not self.enabled:
            raise LifecycleRejected(409, 'Message lifecycle is not enabled')
        if not isinstance(actor, str) or not actor or len(actor) > 256:
            raise LifecycleRejected(422, 'An operator actor is required')
        if apply and (not isinstance(request_key, str) or not request_key or len(request_key) > 256):
            raise LifecycleRejected(422, 'A bounded operation identity is required')
        with self.owner._lock, atomic(self.owner._db):
            if self.owner._db.execute('SELECT 1 FROM bus_channels WHERE channel=?', (channel,)).fetchone() is None:
                raise LifecycleRejected(404, 'Channel is not configured')
            if apply:
                prior = self.owner._db.execute('SELECT * FROM lifecycle_operations WHERE request_key=?',
                                               (request_key,)).fetchone()
                if prior:
                    if (prior['channel'], prior['actor'], prior['action']) != (channel, actor, 'settle'):
                        raise LifecycleRejected(409, 'Operation identity already binds another command')
                    return json.loads(prior['result'])
            current = self.now(instant)
            due = dict(self.owner._db.execute(
                "SELECT state,count(*) FROM lifecycle_targets WHERE channel=? AND "
                "((state='unclaimed' AND deadline<=?) OR (state='picked_up' AND lease_until<=?)) GROUP BY state",
                (channel, current, current)).fetchall())
            result = {'channel': channel, 'applied': apply,
                      'expired_unclaimed': due.get('unclaimed', 0),
                      'recovery_required': due.get('picked_up', 0)}
            announcements = getattr(self.owner, 'announcements', None)
            result['expired_unread'] = announcements.due(channel, current) if announcements is not None else 0
            if apply:
                self.settle(current, channel=channel)
                self.owner._db.execute('INSERT INTO lifecycle_operations VALUES (?,?,?,?,?,?)',
                    (request_key, channel, actor, 'settle', current, json.dumps(result, sort_keys=True)))
            return result

    def activate_fresh(self, policy=None, *, instant=None):
        """Fresh empty stores only; populated upgrades require offline migration."""
        with self.owner._lock, atomic(self.owner._db):
            if self.owner._db.execute('SELECT 1 FROM messages LIMIT 1').fetchone():
                raise ValueError('Populated state requires offline lifecycle migration')
            self.activate(policy or self.policy, instant=instant)

    def activate(self, policy, *, instant=None, legacy=False):
        """Caller owns stopped-service migration or fresh-start transaction."""
        with self.owner._lock, atomic(self.owner._db):
            if not self.enabled:
                # Old released startup checks require bus_channels to be a TABLE.
                # A compatibility view preserves reads/writes for the new binary,
                # while the old binary fails its existing startup guard.
                self.owner._db.execute('ALTER TABLE bus_channels RENAME TO lifecycle_channels')
                self.owner._db.execute('CREATE VIEW bus_channels AS SELECT channel,inbox_id,next_position FROM lifecycle_channels')
                self.owner._db.execute("""CREATE TRIGGER lifecycle_channel_insert INSTEAD OF INSERT ON bus_channels
                    BEGIN SELECT agentbus_message_lifecycle_v1(); INSERT OR IGNORE INTO lifecycle_channels VALUES (NEW.channel,NEW.inbox_id,NEW.next_position); END""")
                self.owner._db.execute("""CREATE TRIGGER lifecycle_channel_update INSTEAD OF UPDATE ON bus_channels
                    BEGIN SELECT agentbus_message_lifecycle_v1(); SELECT CASE WHEN NEW.channel!=OLD.channel OR NEW.inbox_id!=OLD.inbox_id
                    THEN RAISE(ABORT,'immutable channel inbox') END;
                    UPDATE lifecycle_channels SET next_position=NEW.next_position WHERE channel=OLD.channel; END""")
                for table, in self.owner._db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                    quoted = '"' + table.replace('"', '""') + '"'
                    for action in ('INSERT','UPDATE','DELETE'):
                        name = '"' + f'lifecycle_writer_{table}_{action}'.replace('"', '""') + '"'
                        self.owner._db.execute(f'CREATE TRIGGER {name} BEFORE {action} ON {quoted} '
                            'BEGIN SELECT agentbus_message_lifecycle_v1(); END')
            self.policy = policy
            self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES ('message_lifecycle_version','1')")
            self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES ('message_lifecycle_policy',?)",
                                   (json.dumps(vars(policy), sort_keys=True),))
            current = self.now(instant, persist=True)
            if legacy:
                self.owner._db.execute('INSERT OR IGNORE INTO lifecycle_requests '
                    'SELECT channel,request_id,NULL,local_cursor,? FROM local_delivery_obligations',
                    (current + policy.replay_seconds,))
                self.owner._db.execute('INSERT OR IGNORE INTO lifecycle_payloads '
                    'SELECT cursor,channel,?,?,?,1 FROM messages',
                    (current, current + policy.archive_seconds, current + policy.retention_seconds))
            self.rotate_epochs(current)
        self.enabled = True

    def now(self, instant=None, *, persist=False):
        current = time.time() if instant is None else instant
        row = self.owner._db.execute("SELECT value FROM metadata WHERE key='lifecycle_clock'").fetchone()
        current = max(current, float(row[0]) if row else current, self._clock_floor)
        self._clock_floor = current  # Clock observation only; recipient outcomes remain durable rows.
        if persist:
            self.owner._db.execute("INSERT OR REPLACE INTO metadata VALUES ('lifecycle_clock',?)", (str(current),))
        return current

    def observe_clock(self):
        """Retain an observed closed deadline even when its admission transaction rolls back."""
        with self.owner._lock, atomic(self.owner._db):
            self.now(persist=True)

    def rotate_epochs(self, instant=None):
        """Command only: queries never create a fresh admission epoch."""
        with self.owner._lock, atomic(self.owner._db):
            current = self.now(instant, persist=True)
            for row in self.owner._db.execute('SELECT channel,inbox_id FROM bus_channels').fetchall():
                channels = getattr(self.owner, 'channels', None)
                if channels is not None and row['channel'] not in channels.enabled:
                    continue
                existing = self.owner._db.execute('SELECT 1 FROM lifecycle_epochs WHERE channel=? AND accepts_until>?',
                                                   (row['channel'], current)).fetchone()
                if not existing:
                    end = current + self.policy.epoch_seconds
                    self.owner._db.execute('INSERT INTO lifecycle_epochs VALUES (?,?,?,?,?)',
                                           (secrets.token_hex(32), row['channel'], row['inbox_id'], end,
                                            end + self.policy.replay_seconds))
            self.owner._db.execute('DELETE FROM lifecycle_epochs WHERE retries_until<=? AND NOT EXISTS '
                '(SELECT 1 FROM lifecycle_requests r WHERE r.epoch=lifecycle_epochs.epoch)', (current,))

    def epoch(self, channel, instant=None):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT * FROM lifecycle_epochs WHERE channel=? AND accepts_until>? '
                                         'ORDER BY accepts_until DESC LIMIT 1', (channel, self.now(instant))).fetchone()
            return dict(row) if row else None

    def validate_request(self, channel, key, epoch, *, existing=False, instant=None):
        if not self.enabled:
            return
        current = self.now(instant, persist=True)
        prior = self.owner._db.execute('SELECT * FROM lifecycle_requests WHERE channel=? AND request_id=?',
                                       (channel, key)).fetchone()
        if prior:
            if prior['epoch'] != epoch:
                raise LifecycleRejected(409, 'Retry must preserve its original request epoch')
            if prior['replay_until'] <= current:
                raise LifecycleRejected(410, 'Request replay window expired; do not resend as new work')
            return
        if epoch is None:
            # Unknown pre-cutover UUIDs never acquire fresh allocation authority.
            raise LifecycleRejected(426, 'Versioned request epoch required; legacy allocation is closed')
        row = self.owner._db.execute('SELECT * FROM lifecycle_epochs WHERE epoch=? AND channel=?',
                                     (epoch, channel)).fetchone()
        if row is None or row['accepts_until'] <= current or existing:
            raise LifecycleRejected(410, 'Request epoch is expired or unavailable in this scope')

    def bind_request(self, channel, key, epoch, cursor, instant=None):
        if self.enabled:
            current = self.now(instant, persist=True)
            self.owner._db.execute('INSERT INTO lifecycle_requests VALUES (?,?,?,?,?)',
                                   (channel, key, epoch, cursor, current + self.policy.replay_seconds))

    def register(self, channel, cursor, message, *, ttl=None, instant=None):
        if not self.enabled:
            return
        current = self.now(instant, persist=True)
        ttl = self.policy.pickup_seconds if ttl is None else ttl
        if type(ttl) is not int or not 1 <= ttl <= self.policy.pickup_seconds:
            raise LifecycleRejected(422, 'Pickup TTL exceeds the declared bounded policy')
        self.owner._db.execute('INSERT OR IGNORE INTO lifecycle_payloads VALUES (?,?,?,?,?,0)',
                               (cursor, channel, current, current + self.policy.archive_seconds,
                                current + self.policy.retention_seconds))
        if message.reply_to_cursor is not None:
            self.owner._db.execute('INSERT OR IGNORE INTO lifecycle_edges VALUES (?,?)',
                                   (cursor, message.reply_to_cursor))
        if message._sender_assurance not in {'session', 'slack-human'}:
            self.owner._db.execute('UPDATE lifecycle_payloads SET legacy_unknown=1 WHERE cursor=?', (cursor,))
            return
        if message.audience == 'broadcast':
            targets = self.owner._db.execute('SELECT DISTINCT s.chat_id FROM participation_sessions s '
                "JOIN chat_channels c USING(chat_id) WHERE c.channel=? AND s.state!='stopped'", (channel,)).fetchall()
        else:
            targets = self.owner._db.execute('SELECT c.chat_id FROM chat_identities c JOIN chat_channels b USING(chat_id) '
                'WHERE b.channel=? AND (c.current_route=? OR c.chat_id IN '
                '(SELECT chat_id FROM routing_aliases WHERE route=?))',
                (channel, message.recipient, message.recipient)).fetchall()
        self.owner._db.executemany('INSERT OR IGNORE INTO lifecycle_targets '
            '(cursor,chat_id,channel,state,deadline) VALUES (?,?,?,\'unclaimed\',?)',
            [(cursor, row['chat_id'], channel, current + ttl) for row in targets])

    def settle(self, instant=None, *, channel=None):
        if not self.enabled:
            return
        with self.owner._lock, atomic(self.owner._db):
            current = self.now(instant, persist=True)
            scope = ' AND channel=?' if channel is not None else ''
            args = (current, current, channel) if channel is not None else (current, current)
            self.owner._db.execute("UPDATE lifecycle_targets SET state='expired_unclaimed',outcome_at=? "
                "WHERE state='unclaimed' AND deadline<=?" + scope, args)
            self.owner._db.execute("UPDATE lifecycle_targets SET state='recovery_required',outcome_at=? "
                "WHERE state='picked_up' AND lease_until<=?" + scope, args)
            announcements = getattr(self.owner, 'announcements', None)
            if announcements is not None:
                announcements.settle(channel, current)

    def target(self, principal, cursor, instant=None):
        """Scoped read model; derived deadline state is not a write or a read receipt."""
        with self.owner._lock:
            row = self.owner._db.execute('SELECT * FROM lifecycle_targets WHERE channel=? AND cursor=? AND chat_id=?',
                                         (principal.channel, cursor, principal.chat_id)).fetchone()
            if row is None:
                raise LifecycleRejected(404, 'Recipient outcome unavailable in this scope')
            result = dict(row)
            current = self.now(instant)
            result['deadline_elapsed'] = result['state'] in {'unclaimed', 'expired_unclaimed'} and result['deadline'] <= current
            result['lease_elapsed'] = result['state'] in {'picked_up', 'recovery_required'} and result['lease_until'] <= current
            return result

    def outcome(self, principal, cursor):
        with self.owner._lock:
            message = self.owner.message(principal.channel, cursor)
            if message is None:
                raise LifecycleRejected(404, 'Recipient outcome unavailable in this scope')
            self_addressed = (message.audience in {'direct','informational'}
                              and self.owner.participation.same_chat(principal.route, message.recipient))
            if (message.sender_assurance == 'session' and self.owner.participation.same_chat(principal.route, message.sender)
                    and not self_addressed):
                recipients = dict(self.owner._db.execute(
                    'SELECT state,count(*) FROM lifecycle_targets WHERE channel=? AND cursor=? GROUP BY state',
                    (principal.channel, cursor)).fetchall())
                return {'recipients':recipients, 'scope':'sender', 'acknowledgement_inferred':False}
            target = self.target(principal, cursor)
            return {'state':target['state'], 'deadline':target['deadline'],
                    'deadline_elapsed':target['deadline_elapsed'], 'scope':'recipient',
                    'lease_elapsed':target['lease_elapsed'],
                    'owned_by_this_session':target['pickup_session'] == principal.session_id}

    def verify_session(self, principal, secret, *, allow_stopped=False):
        row = self.owner.participation.authorize_session(principal.session_id, secret)
        if row['chat_id'] != principal.chat_id:
            raise PermissionError('Matching session scope required')
        if not allow_stopped and row['state'] not in {'active','overdue','stopping'}:
            raise LifecycleRejected(423, 'Session is not active for new work')
        return row

    def pickup(self, principal, cursor, token, instant=None, *, session_secret=None):
        try:
            if str(UUID(token)) != token:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise LifecycleRejected(422, 'Pickup receipt must be a canonical UUID') from None
        rejection = None
        with self.owner._lock, atomic(self.owner._db):
            if session_secret is not None:
                self.verify_session(principal, session_secret)
            result = self.target(principal, cursor, instant)
            current = self.now(instant, persist=True)
            if result['state'] == 'unclaimed' and result['deadline'] <= current:
                self.owner._db.execute("UPDATE lifecycle_targets SET state='expired_unclaimed',outcome_at=? "
                    'WHERE cursor=? AND chat_id=?', (current, cursor, principal.chat_id))
                result = self.target(principal, cursor, instant)
            if result['state'] == 'picked_up' and result['lease_until'] <= current:
                self.owner._db.execute("UPDATE lifecycle_targets SET state='recovery_required',outcome_at=? "
                    'WHERE cursor=? AND chat_id=?', (current, cursor, principal.chat_id))
                result = self.target(principal, cursor, instant)
            if result['pickup_token'] == token and result['pickup_session'] == principal.session_id:
                return result
            if result['state'] != 'unclaimed':
                rejection = LifecycleRejected(410 if result['state'] == 'expired_unclaimed' else 409,
                                              'Recipient work is terminal or already owned')
            else:
                until = current + self.policy.work_lease_seconds
                self.owner._db.execute("UPDATE lifecycle_targets SET state='picked_up',pickup_session=?,pickup_token=?,lease_until=? "
                    'WHERE cursor=? AND chat_id=?', (principal.session_id, token, until, cursor, principal.chat_id))
                result = self.target(principal, cursor, instant)
        if rejection:
            raise rejection
        return result

    def finish(self, principal, cursor, token, instant=None, *, session_secret=None):
        with self.owner._lock, atomic(self.owner._db):
            if session_secret is not None:
                self.verify_session(principal, session_secret, allow_stopped=True)
            result = self.target(principal, cursor, instant)
            if result['pickup_token'] != token or result['pickup_session'] != principal.session_id:
                raise LifecycleRejected(409, 'Completion requires the original pickup receipt')
            if result['state'] == 'handled':
                return result
            if result['state'] not in {'picked_up', 'recovery_required'}:
                raise LifecycleRejected(410, 'Recipient work is terminal')
            self.owner._db.execute("UPDATE lifecycle_targets SET state='handled',outcome_at=? WHERE cursor=? AND chat_id=?",
                                   (self.now(instant, persist=True), cursor, principal.chat_id))
            return self.target(principal, cursor, instant)

    def handle_batch(self, principal, receipts, *, allow_pickup=True, session_secret=None):
        """Explicit semantic ACKs commit in a bounded transaction; reads never enter here."""
        if not 1 <= len(receipts) <= 200 or len({cursor for cursor, _ in receipts}) != len(receipts):
            raise LifecycleRejected(422, 'Handling receipt batch requires 1..200 distinct cursors')
        result = {'handled':0, 'already_handled':0, 'expired_unclaimed':0}
        with self.owner._lock, atomic(self.owner._db):
            if session_secret is not None:
                session = self.verify_session(principal, session_secret, allow_stopped=True)
                allow_pickup = allow_pickup and session['state'] in {'active','overdue','stopping'}
            current = self.now(persist=True)
            for cursor, token in receipts:
                target = self.target(principal, cursor, current)
                if target['state'] == 'handled':
                    result['already_handled'] += 1
                    continue
                if target['state'] == 'expired_unclaimed' or target['deadline_elapsed']:
                    if target['state'] != 'expired_unclaimed':
                        self.owner._db.execute("UPDATE lifecycle_targets SET state='expired_unclaimed',outcome_at=? "
                            'WHERE cursor=? AND chat_id=?', (current, cursor, principal.chat_id))
                    result['expired_unclaimed'] += 1
                    continue
                if target['state'] == 'unclaimed' and not allow_pickup:
                    raise LifecycleRejected(423, 'Stopped sessions cannot pick up new work')
                self.pickup(principal, cursor, token, current)
                self.finish(principal, cursor, token, current)
                result['handled'] += 1
        return result

    def actionable(self, channel, cursor, chat_id, instant=None):
        if not self.enabled:
            return True
        with self.owner._lock:
            row = self.owner._db.execute('SELECT state,deadline FROM lifecycle_targets WHERE channel=? AND cursor=? AND chat_id=?',
                                         (channel, cursor, chat_id)).fetchone()
            if row is not None:
                return row['state'] == 'unclaimed' and row['deadline'] > self.now(instant)
            legacy = self.owner._db.execute(
                'SELECT legacy_unknown FROM lifecycle_payloads WHERE channel=? AND cursor=?',
                (channel, cursor)).fetchone()
            return bool(legacy and legacy['legacy_unknown'])

    def all_targets_expired(self, channel, cursor):
        if not self.enabled:
            return False
        with self.owner._lock:
            row = self.owner._db.execute("SELECT count(*),sum(state='expired_unclaimed') "
                'FROM lifecycle_targets WHERE channel=? AND cursor=?', (channel, cursor)).fetchone()
            return bool(row[0] and row[0] == row[1])

    def require_live_parent(self, channel, cursor, identity):
        if not self.enabled or cursor is None:
            return
        current = self.now()
        chat = self.identity_chat(channel, identity)
        target = self.owner._db.execute('SELECT state,deadline FROM lifecycle_targets WHERE channel=? AND cursor=? AND chat_id=?',
                                        (channel, cursor, chat)).fetchone()
        if ((target is not None and (target['state'] == 'expired_unclaimed'
                or (target['state'] == 'unclaimed' and target['deadline'] <= current)))
                or self.all_targets_expired(channel, cursor)):
            raise LifecycleRejected(410, 'Reply parent work is expired; no new work allocated')
        metadata = self.owner._db.execute('SELECT created_at,legacy_unknown FROM lifecycle_payloads WHERE channel=? AND cursor=?',
                                          (channel, cursor)).fetchone()
        if metadata is not None and not metadata['legacy_unknown'] and target is None:
            targets = self.owner._db.execute('SELECT 1 FROM lifecycle_targets WHERE channel=? AND cursor=? LIMIT 1',
                                             (channel, cursor)).fetchone()
            if targets is None and metadata['created_at'] + self.policy.pickup_seconds <= current:
                raise LifecycleRejected(410, 'Reply parent admission window expired')

    def identity_chat(self, channel, route):
        row = self.owner._db.execute('SELECT c.chat_id FROM chat_identities c JOIN chat_channels b USING(chat_id) '
            'WHERE b.channel=? AND (c.current_route=? OR c.chat_id IN '
            '(SELECT chat_id FROM routing_aliases WHERE route=?))', (channel, route, route)).fetchone()
        return row[0] if row else None

    def claim_target(self, channel, cursor, identity):
        if not self.enabled:
            return
        row = self.owner._db.execute('SELECT created_at,legacy_unknown FROM lifecycle_payloads WHERE channel=? AND cursor=?',
                                     (channel, cursor)).fetchone()
        if row is None or row['legacy_unknown']:
            return  # No retroactive deadline or handling inference for inherited unknown work.
        chat = self.identity_chat(channel, identity)
        deadline = row['created_at'] + self.policy.pickup_seconds
        if chat is None or deadline <= self.now():
            raise LifecycleRejected(410, 'Claim work is expired or unavailable in this scope')
        self.owner._db.execute('INSERT OR IGNORE INTO lifecycle_targets '
            '(cursor,chat_id,channel,state,deadline) VALUES (?,?,?,\'unclaimed\',?)', (cursor, chat, channel, deadline))

    def recover_claim(self, channel, cursor, identity):
        if not self.enabled:
            return
        if self.owner._db.execute("SELECT 1 FROM lifecycle_targets WHERE channel=? AND cursor=? "
                "AND state IN ('picked_up','recovery_required','handled')", (channel, cursor)).fetchone():
            raise LifecycleRejected(409, 'Owned or handled work cannot be reassigned as unclaimed')
        if identity is not None:
            self.claim_target(channel, cursor, identity)
        chat = self.identity_chat(channel, identity) if identity is not None else None
        self.owner._db.execute("DELETE FROM lifecycle_targets WHERE channel=? AND cursor=? AND state='unclaimed' "
                               'AND (? IS NULL OR chat_id!=?)', (channel, cursor, chat, chat))

    def inbox_projection(self, channel, cursor, identity, actionable):
        if not self.enabled:
            return actionable, None
        with self.owner._lock:
            chat = self.identity_chat(channel, identity)
            row = self.owner._db.execute('SELECT state,deadline FROM lifecycle_targets WHERE channel=? AND cursor=? AND chat_id=?',
                                         (channel, cursor, chat)).fetchone()
            if row is not None:
                return actionable and self.actionable(channel, cursor, chat), dict(row)
            legacy = self.owner._db.execute('SELECT legacy_unknown FROM lifecycle_payloads WHERE channel=? AND cursor=?',
                                           (channel, cursor)).fetchone()
            unknown = bool(legacy and legacy['legacy_unknown'])
            return actionable and unknown, {'state':'legacy_unknown' if unknown else 'not_targeted'}
