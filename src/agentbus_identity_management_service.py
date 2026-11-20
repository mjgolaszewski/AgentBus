"""Operator identity lifecycle over the existing membership and retention writers."""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime, timezone
from uuid import uuid4

from src.agentbus_identity_intent_service import (
    DENIAL,
    decode_identity_intent,
    encode_identity_intent,
    intent_digest,
    require_fresh_intent,
)
from src.agentbus_transaction_service import atomic

MAX_OPERATIONS = 4096


def identity_capacity(store):
    """Privileged service read model; no expiry, deletion or resource admission."""
    with store._lock:
        states=dict(store._db.execute('SELECT state,count(*) FROM identity_management_operations GROUP BY state').fetchall())
        retained=sum(states.values())
        return {'retained':retained,'limit':MAX_OPERATIONS,'states':states,'warning':retained*5>=MAX_OPERATIONS*4}


def initialize_identity_management(participation):
    db = participation._db
    with participation._lock, atomic(db):
        for table, columns in {
            'chat_identities': {'identity_revision': 'INTEGER NOT NULL DEFAULT 0',
                                'retired_at': 'TEXT', 'retired_by': 'TEXT'},
            'participation_sessions': {'operator_revoked_at': 'TEXT', 'operator_revoked_by': 'TEXT'},
        }.items():
            existing = {row['name'] for row in db.execute('PRAGMA table_info('+table+')')}
            for name, definition in columns.items():
                if name not in existing:
                    db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+definition)
        db.execute("""CREATE TABLE IF NOT EXISTS identity_management_operations (
            request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, channel TEXT NOT NULL,
            chat_id TEXT NOT NULL REFERENCES chat_identities(chat_id), action TEXT NOT NULL,
            intent_json TEXT NOT NULL, digest TEXT NOT NULL, expires_at REAL NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('preview','committed')),
            result_json TEXT, result_sha256 TEXT, created_at REAL NOT NULL, completed_at REAL)""")
        db.execute('CREATE INDEX IF NOT EXISTS identity_operation_actor ON identity_management_operations(channel,actor,created_at)')
        db.execute("""CREATE TRIGGER IF NOT EXISTS identity_route_revision
            AFTER UPDATE OF current_route,retired_at ON chat_identities
            WHEN NEW.current_route IS NOT OLD.current_route OR NEW.retired_at IS NOT OLD.retired_at
            BEGIN UPDATE chat_identities SET identity_revision=identity_revision+1 WHERE chat_id=NEW.chat_id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS identity_session_insert_revision
            AFTER INSERT ON participation_sessions
            BEGIN UPDATE chat_identities SET identity_revision=identity_revision+1 WHERE chat_id=NEW.chat_id; END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS identity_session_authority_revision
            AFTER UPDATE OF state,secret_sha256 ON participation_sessions
            WHEN NEW.state IS NOT OLD.state OR NEW.secret_sha256 IS NOT OLD.secret_sha256
            BEGIN UPDATE chat_identities SET identity_revision=identity_revision+1 WHERE chat_id=NEW.chat_id; END""")


def rename_identity(participation, chat_id, new_route, *, session_id=None, actor=None):
    """One canonical route transition for exact-session and operator commands."""
    from src.agentbus_message_assurance_service import route_parts
    from src.agentbus_participation_store_service import MAX_ALIASES_PER_CHAT, _now
    db = participation._db
    with participation._lock, atomic(db):
        chat = db.execute('SELECT * FROM chat_identities WHERE chat_id=?', (chat_id,)).fetchone()
        if chat is None or chat['retired_at'] is not None:
            raise ValueError(DENIAL)
        parts = route_parts(new_route)
        if parts is None or parts[1] != chat['repo']:
            raise ValueError('New route must keep the same repository')
        old_route = chat['current_route']
        if new_route == old_route:
            return chat_id, old_route, new_route
        current = db.execute('SELECT chat_id FROM chat_identities WHERE current_route=?', (new_route,)).fetchone()
        alias = db.execute('SELECT chat_id FROM routing_aliases WHERE route=?', (new_route,)).fetchone()
        if current is not None or (alias is not None and alias['chat_id'] != chat_id):
            raise ValueError('New route collides with an existing or reserved address')
        count = db.execute('SELECT count(*) FROM routing_aliases WHERE chat_id=?', (chat_id,)).fetchone()[0]
        if alias is None and count >= MAX_ALIASES_PER_CHAT:
            raise ValueError('Alias capacity exhausted; existing addresses remain reserved')
        if alias is not None:
            db.execute('DELETE FROM routing_aliases WHERE route=?', (new_route,))
        changed = _now()
        db.execute('INSERT OR IGNORE INTO routing_aliases(route,chat_id,reserved_at) VALUES(?,?,?)',
                   (old_route, chat_id, changed))
        db.execute('UPDATE chat_identities SET current_route=?,updated_at=? WHERE chat_id=?',
                   (new_route, changed, chat_id))
        participation._audit('rename', old_route+' -> '+new_route, chat_id, session_id, '', changed, actor=actor)
        return chat_id, old_route, new_route


class IdentityManagement:
    """Caller supplies a verified Socket Mode user; current scope is checked again."""
    def __init__(self, store, settings, *, clock=time.time):
        self.store, self.settings, self.clock = store, settings, clock
        self.participation, self.db, self.lock = store.participation, store._db, store._lock
        if self.participation._db is not self.db or self.participation._lock is not self.lock:
            raise ValueError('Identity management requires the canonical participation writer')

    def _authority(self, actor, channel):
        if (not isinstance(actor, str) or actor not in self.settings.slack_operator_user_ids
                or channel not in self.settings.channels or channel not in self.store.channels.enabled):
            raise PermissionError(DENIAL)
        return 'slack:'+actor, self.store.channels.inbox_id(channel)

    def _row(self, channel, chat_id):
        row = self.db.execute('SELECT c.* FROM chat_identities c JOIN chat_channels b USING(chat_id) '
                              'WHERE b.channel=? AND c.chat_id=?', (channel, chat_id)).fetchone()
        if row is None:
            raise ValueError(DENIAL)
        return row

    def _sessions(self, chat_id):
        rows = self.db.execute("SELECT session_id FROM participation_sessions WHERE chat_id=? AND state!='stopped' "
                               'ORDER BY session_id LIMIT 257', (chat_id,)).fetchall()
        if len(rows) > 256:
            raise ValueError('Identity has too many active sessions for one bounded operation')
        return [row[0] for row in rows]

    def _instant(self):
        instant = self.clock()
        if type(instant) not in (int, float) or not math.isfinite(instant) or instant < 0:
            raise ValueError(DENIAL)
        return float(instant)

    def _idle_writer(self):
        if self.db.in_transaction:
            raise ValueError('Identity operation unavailable while transaction custody needs recovery')

    def resolve(self, actor, channel, reference):
        self._authority(actor, channel)
        if not isinstance(reference, str) or not 1 <= len(reference) <= 80:
            raise ValueError(DENIAL)
        with self.lock:
            self._idle_writer()
            base = ('SELECT DISTINCT c.chat_id FROM chat_identities c JOIN chat_channels b USING(chat_id) '
                    'WHERE b.channel=? AND ')
            exact = self.db.execute(base+'(c.current_route=? OR c.chat_id=? OR EXISTS '
                '(SELECT 1 FROM routing_aliases a WHERE a.chat_id=c.chat_id AND a.route=?)) LIMIT 2',
                (channel, reference, reference, reference)).fetchall()
            rows = exact or self.db.execute(base+'(instr(lower(c.current_route),?)>0 OR EXISTS '
                '(SELECT 1 FROM routing_aliases a WHERE a.chat_id=c.chat_id AND instr(lower(a.route),?)>0)) LIMIT 2',
                (channel, reference.casefold(), reference.casefold())).fetchall()
            if len(rows) != 1:
                raise ValueError('Choose one identity from the private identity manager' if rows else DENIAL)
            return rows[0][0]

    def identities(self, actor, channel, *, page=0, include_retired=False):
        self._authority(actor, channel)
        if type(page) is not int or not 0 <= page <= 65535 or type(include_retired) is not bool:
            raise ValueError(DENIAL)
        with self.lock:
            self._idle_writer()
            rows = self.db.execute('SELECT c.chat_id,c.current_route,c.display_name,c.retired_at,c.identity_revision '
                'FROM chat_identities c JOIN chat_channels b USING(chat_id) WHERE b.channel=? '
                'AND (? OR c.retired_at IS NULL) ORDER BY c.current_route LIMIT 11 OFFSET ?',
                (channel, include_retired, page*10)).fetchall()
            return {'identities':[dict(row) for row in rows[:10]],'page':page,'has_more':len(rows)>10,
                    'include_retired':include_retired,'channel':channel,'location':self.settings.local_prefix}

    def detail(self, actor, channel, chat_id):
        self._authority(actor, channel)
        with self.lock:
            self._idle_writer()
            row = self._row(channel, chat_id)
            sessions = self._sessions(chat_id)
            history = self.store.retention.identity_history(channel, chat_id)
            return {'chat_id':chat_id,'route':row['current_route'],'display_name':row['display_name'],
                    'retired':row['retired_at'] is not None,'revision':row['identity_revision'],
                    'sessions':len(sessions),'history':history,'channel':channel}

    def preview(self, actor, channel, chat_id, action, *, name=None):
        label, inbox = self._authority(actor, channel)
        with self.lock:
            self._idle_writer()
            with atomic(self.db):
                row = self._row(channel, chat_id)
                if action == 'rename' and row['retired_at'] is not None:
                    raise ValueError(DENIAL)
                new_route = None
                if action == 'rename':
                    if not self.settings.local_prefix or not isinstance(name,str):
                        raise ValueError('Configure the local prefix and exact new name before preview')
                    new_route = self.settings.local_prefix+':'+row['repo']+':'+name
                instant = self._instant()
                history = self.store.retention.identity_history(channel, chat_id, instant=instant)
                positions = history['positions'] if action == 'retire_with_history' else []
                if action == 'retire_with_history' and not history['supported']:
                    raise ValueError('History eligibility is unavailable; retire without deleting history')
                value = {'version':1,'request_id':str(uuid4()),'actor':label,'channel':channel,'inbox_id':inbox,
                         'chat_id':chat_id,'revision':row['identity_revision'],'action':action,'created_at':instant,
                         'expires_at':instant+600,'name':name,'new_route':new_route,'session_ids':self._sessions(chat_id),'positions':positions}
                intent = decode_identity_intent(value, actor=label, channel=channel, inbox_id=inbox)
                raw = encode_identity_intent(intent)
                if len(raw.encode()) > 32768:
                    raise ValueError('Identity preview exceeds its bounded size')
                self.db.execute("DELETE FROM identity_management_operations WHERE request_id IN "
                    "(SELECT request_id FROM identity_management_operations WHERE state='preview' AND expires_at<=? LIMIT 100)",
                    (instant,))
                if self.db.execute('SELECT count(*) FROM identity_management_operations').fetchone()[0] >= MAX_OPERATIONS:
                    raise ValueError('Identity operation history capacity requires operator review')
                self.db.execute('INSERT INTO identity_management_operations VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,?,NULL)',
                    (intent.request_id,label,channel,chat_id,action,raw,intent_digest(intent),intent.expires_at,'preview',instant))
                return {'intent':value,'digest':intent_digest(intent),'route':row['current_route'],
                        'retired':row['retired_at'] is not None,'history':history}

    def _operation(self, label, channel, inbox, request_id):
        row = self.db.execute('SELECT * FROM identity_management_operations WHERE request_id=? AND actor=? AND channel=?',
                              (request_id,label,channel)).fetchone()
        if row is None or len(row['intent_json'].encode()) > 32768:
            raise ValueError(DENIAL)
        intent = decode_identity_intent(json.loads(row['intent_json']), actor=label, channel=channel, inbox_id=inbox)
        if (encode_identity_intent(intent) != row['intent_json'] or intent_digest(intent) != row['digest']
                or (intent.request_id,intent.chat_id,intent.action,intent.expires_at) !=
                   (row['request_id'],row['chat_id'],row['action'],row['expires_at'])):
            raise ValueError('Identity operation receipt is inconsistent; preserve it for recovery')
        return row, intent

    def receipt(self, actor, channel, request_id):
        label, inbox = self._authority(actor, channel)
        with self.lock:
            self._idle_writer()
            row, intent = self._operation(label,channel,inbox,request_id)
            if row['state'] == 'committed':
                raw = row['result_json']
                if (not isinstance(raw,str) or len(raw.encode()) > 32768
                        or hashlib.sha256(raw.encode()).hexdigest() != row['result_sha256']):
                    raise ValueError('Identity operation result is inconsistent; preserve it for recovery')
                result = json.loads(raw)
                if (not isinstance(result,dict) or (result.get('request_id'), result.get('state'), result.get('action'),
                        result.get('chat_id'), result.get('channel'), result.get('inbox_id')) !=
                        (request_id, 'committed', intent.action, intent.chat_id, channel, inbox)):
                    raise ValueError(DENIAL)
                return result
            return {'request_id':request_id,'state':'preview','intent':json.loads(row['intent_json']),
                    'digest':row['digest'],'expired':not intent.created_at <= self._instant() < intent.expires_at}

    def perform(self, actor, channel, request_id, digest):
        label, inbox = self._authority(actor, channel)
        with self.lock:
            self._idle_writer()
            # Read known committed results outside a new writer transaction.
            prior = self.receipt(actor,channel,request_id)
            if prior['state'] == 'committed':
                row,_ = self._operation(label,channel,inbox,request_id)
                if digest != row['digest']:
                    raise ValueError(DENIAL)
                return prior
            with atomic(self.db):
                operation,intent = self._operation(label,channel,inbox,request_id)
                if not isinstance(digest,str) or digest != operation['digest']:
                    raise ValueError(DENIAL)
                instant = self._instant()
                require_fresh_intent(intent,instant)
                row = self._row(channel,intent.chat_id)
                if row['identity_revision'] != intent.revision or self._sessions(intent.chat_id) != list(intent.session_ids):
                    raise ValueError('Identity changed after preview; reopen the identity manager')
                removed = 0
                if intent.action == 'retire_with_history':
                    selected = self.store.retention.identity_history(channel,intent.chat_id,positions=intent.positions,instant=instant)
                    if tuple(selected['positions']) != intent.positions:
                        raise ValueError('History eligibility changed after preview; reopen the identity manager')
                timestamp = datetime.fromtimestamp(instant,timezone.utc).isoformat()
                route = row['current_route']
                was_retired = row['retired_at'] is not None
                if intent.action == 'rename':
                    prefix = self.settings.local_prefix
                    if not prefix:
                        raise ValueError('Configure the local location prefix before operator rename')
                    route = prefix+':'+row['repo']+':'+intent.name
                    if route != intent.new_route:
                        raise ValueError('Rename configuration changed after preview; reopen the identity manager')
                    _,_,route = rename_identity(self.participation,intent.chat_id,route,actor=label)
                else:
                    if not was_retired:
                        self.db.execute('UPDATE chat_identities SET retired_at=?,retired_by=?,updated_at=? WHERE chat_id=?',
                                        (timestamp,label,timestamp,intent.chat_id))
                    self.db.execute("UPDATE participation_sessions SET state='stopped',stopped_at=?,operator_revoked_at=?,"
                                    "operator_revoked_by=? WHERE chat_id=? AND state!='stopped'",
                                    (timestamp,timestamp,label,intent.chat_id))
                    self.participation._audit('operator_identity_revocation','bus_access_revoked_without_semantic_ack',
                        intent.chat_id,None,str(intent.revision),timestamp,actor=label)
                    if intent.action == 'retire_with_history':
                        result = self.store.retention.remove_identity_history(channel,intent.chat_id,
                            positions=intent.positions,actor=label,request_key='identity:'+request_id,instant=instant)
                        removed = result['removed']
                remaining = self.store.retention.identity_history(channel,intent.chat_id,positions=())
                result = {'request_id':request_id,'state':'committed','action':intent.action,'chat_id':intent.chat_id,
                          'channel':channel,'inbox_id':inbox,'route':route,'retired':intent.action!='rename',
                          'already_retired':was_retired,'sessions_revoked':len(intent.session_ids) if intent.action!='rename' else 0,
                          'history':{'removed':removed,'retained':remaining['retained'],'scope':'eligible local service history only'},
                          'semantic_acknowledgement':'not_inferred','host_execution':'not_requested',
                          'uncertain_custody':'retained_not_reclassified'}
                raw = json.dumps(result,sort_keys=True,separators=(',',':'))
                self.db.execute("UPDATE identity_management_operations SET state='committed',result_json=?,"
                                'result_sha256=?,completed_at=? WHERE request_id=?',
                                (raw,hashlib.sha256(raw.encode()).hexdigest(),instant,request_id))
            return result
