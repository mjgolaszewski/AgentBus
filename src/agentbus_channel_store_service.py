"""Channel registry and public positions in the existing SQLite custody writer."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from src.agentbus_transaction_service import atomic


@dataclass(frozen=True)
class ChannelPrincipal:
    chat_id: str
    session_id: str
    route: str
    channel: str
    inbox_id: str


class ChannelStore:
    def __init__(self, owner):
        self.owner = owner
        self.enabled: frozenset[str] = frozenset()
        with owner._lock, atomic(owner._db):
            owner._db.execute('''CREATE TABLE IF NOT EXISTS bus_channels (
                channel TEXT PRIMARY KEY, inbox_id TEXT NOT NULL UNIQUE,
                next_position INTEGER NOT NULL DEFAULT 1)''')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS chat_channels (
                chat_id TEXT PRIMARY KEY REFERENCES chat_identities(chat_id),
                channel TEXT NOT NULL REFERENCES bus_channels(channel))''')
            owner._db.execute('''CREATE TRIGGER IF NOT EXISTS immutable_chat_channel
                BEFORE UPDATE ON chat_channels BEGIN SELECT RAISE(ABORT,'immutable channel membership'); END''')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS channel_positions (
                channel TEXT NOT NULL REFERENCES bus_channels(channel), position INTEGER NOT NULL,
                message_id INTEGER NOT NULL UNIQUE REFERENCES messages(cursor),
                PRIMARY KEY(channel,position))''')
            owner._db.execute('''CREATE TABLE IF NOT EXISTS channel_enrollment_grants (
                token_hash TEXT PRIMARY KEY, channel TEXT NOT NULL REFERENCES bus_channels(channel),
                repo TEXT NOT NULL, route TEXT NOT NULL, chat_id TEXT,
                expires_at REAL NOT NULL, actor TEXT NOT NULL, consumed_session TEXT,
                consumed_secret TEXT)''')
            from src.agentbus_enrollment_approval_service import initialize
            initialize(self)

    @staticmethod
    def validate_channels(channels):
        channels = tuple(channels)
        if (not 1 <= len(channels) <= 16 or len(set(channels)) != len(channels)
                or any(not isinstance(c, str) or not re.fullmatch(r'[CG][A-Z0-9]+', c) for c in channels)):
            raise ValueError('Configure 1..16 unique Slack channel IDs')
        return channels

    def configure(self, channels, *, legacy_channel=None):
        channels = self.validate_channels(channels)
        store = self.owner
        with store._lock, atomic(store._db):
            old = store._db.execute('SELECT count(*) FROM bus_channels').fetchone()[0]
            populated = store._db.execute('SELECT count(*) FROM messages').fetchone()[0] or store._db.execute(
                'SELECT count(*) FROM chat_identities').fetchone()[0]
            if not old and populated and legacy_channel is None:
                raise ValueError('Legacy database requires explicit offline channel migration')
            for channel in channels:
                identity = str(uuid.uuid4())
                if channel == legacy_channel:
                    identity = store._db.execute("SELECT value FROM metadata WHERE key='inbox_id'").fetchone()[0]
                store._db.execute('INSERT OR IGNORE INTO bus_channels VALUES (?,?,1)', (channel, identity))
            if legacy_channel is not None:
                if legacy_channel not in channels:
                    raise ValueError('Legacy channel must be configured for migration')
                if store._db.execute('SELECT 1 FROM messages WHERE channel!=? LIMIT 1', (legacy_channel,)).fetchone():
                    raise ValueError('Unexplained legacy channel data')
                for row in store._db.execute('SELECT cursor FROM messages WHERE channel=? ORDER BY cursor', (legacy_channel,)):
                    store._db.execute('INSERT OR IGNORE INTO channel_positions VALUES (?,?,?)',
                                      (legacy_channel, row['cursor'], row['cursor']))
                store._db.execute('UPDATE bus_channels SET next_position=MAX(next_position, '
                    '(SELECT COALESCE(MAX(position),0)+1 FROM channel_positions WHERE channel=?)) WHERE channel=?',
                    (legacy_channel, legacy_channel))
                store._db.execute('INSERT OR IGNORE INTO chat_channels SELECT chat_id,? FROM chat_identities', (legacy_channel,))
                store._db.execute('''INSERT OR IGNORE INTO channel_enrollment_grants
                    SELECT token_sha256,?,repo,route,chat_id,CAST(strftime('%s',expires_at) AS REAL),actor,
                        session_id,NULL FROM profile_handoffs''', (legacy_channel,))
            if old and store._db.execute('SELECT 1 FROM chat_identities c LEFT JOIN chat_channels b USING(chat_id) WHERE b.chat_id IS NULL LIMIT 1').fetchone():
                raise ValueError('Incomplete identity channel migration')
            if store._db.execute('SELECT 1 FROM channel_positions p JOIN messages m ON m.cursor=p.message_id WHERE p.channel!=m.channel LIMIT 1').fetchone():
                raise ValueError('Conflicting message channel positions')
            duplicates = store._db.execute('SELECT 1 FROM participation_sessions GROUP BY secret_sha256 HAVING count(*)>1 LIMIT 1').fetchone()
            if duplicates:
                raise ValueError('Conflicting legacy session credentials require operator recovery')
            store._db.execute('CREATE UNIQUE INDEX IF NOT EXISTS unique_session_credential ON participation_sessions(secret_sha256)')
        self.enabled = frozenset(channels)

    def inbox_id(self, channel):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT inbox_id FROM bus_channels WHERE channel=?', (channel,)).fetchone()
            if row is None:
                raise PermissionError('Channel scope unavailable')
            return row[0]

    def channel_for_chat(self, chat_id):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT channel FROM chat_channels WHERE chat_id=?', (chat_id,)).fetchone()
            if row is None:
                raise PermissionError('Channel scope unavailable')
            return row[0]

    def bind(self, chat_id, channel):
        with self.owner._lock:
            if channel not in self.enabled:
                raise PermissionError('Channel scope unavailable')
            prior = self.owner._db.execute('SELECT channel FROM chat_channels WHERE chat_id=?', (chat_id,)).fetchone()
            if prior is not None and prior[0] != channel:
                raise PermissionError('Immutable channel membership')
            self.owner._db.execute('INSERT OR IGNORE INTO chat_channels VALUES (?,?)', (chat_id, channel))

    def allocate(self, channel, message_id):
        with self.owner._lock:
            db = self.owner._db
            prior = db.execute('SELECT position FROM channel_positions WHERE channel=? AND message_id=?', (channel,message_id)).fetchone()
            if prior:
                return prior[0]
            row = db.execute('SELECT next_position FROM bus_channels WHERE channel=?', (channel,)).fetchone()
            if row is None:
                return None  # Unconfigured legacy stores are migratable but not API-authoritative.
            position = row[0]
            db.execute('INSERT INTO channel_positions VALUES (?,?,?)', (channel, position, message_id))
            db.execute('UPDATE bus_channels SET next_position=next_position+1 WHERE channel=?', (channel,))
            return position

    def public_cursor(self, channel, message_id):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT position FROM channel_positions WHERE channel=? AND message_id=?',
                                         (channel,message_id)).fetchone()
            if row is None:
                raise PermissionError('Message scope unavailable')
            return row[0]

    def internal_cursor(self, channel, position):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT message_id FROM channel_positions WHERE channel=? AND position=?',
                                         (channel,position)).fetchone()
            return row[0] if row else None

    def after_cursor(self, channel, position):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT COALESCE(MAX(message_id),0) FROM channel_positions WHERE channel=? AND position<=?',
                                         (channel,position)).fetchone()
            return row[0]

    def high_water(self, channel):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT next_position-1 FROM bus_channels WHERE channel=?', (channel,)).fetchone()
            return row[0] if row else 0

    def principal(self, secret, route=None, *, allow_joining=False, receipt_recovery=False):
        with self.owner._lock:
            import hashlib

            from src.agentbus_participation_store_service import StoppedMessageSession
            if not secret or len(secret)>512:
                raise PermissionError('Matching session authority required')
            row = self.owner._db.execute('''SELECT s.*,c.current_route FROM participation_sessions s
                JOIN chat_identities c ON c.chat_id=s.chat_id WHERE s.secret_sha256=?''',
                (hashlib.sha256(secret.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise PermissionError('Matching session authority required')
            if row['state']=='stopped' and not receipt_recovery:
                raise StoppedMessageSession('Session is stopped')
            if not receipt_recovery and row['state'] not in ({'joining','active','overdue','stopping'} if allow_joining else {'active','overdue','stopping'}):
                raise PermissionError('Matching session authority required')
            if route is not None and route not in self.owner.participation.routes_for(row['current_route']):
                raise PermissionError('Matching session authority required')
            channel = self.channel_for_chat(row['chat_id'])
            if channel not in self.enabled:
                raise PermissionError('Channel scope unavailable')
            return ChannelPrincipal(row['chat_id'],row['session_id'],row['current_route'],channel,self.inbox_id(channel))

    def local_target(self, channel, route):
        with self.owner._lock:
            row = self.owner._db.execute('''SELECT c.chat_id FROM chat_identities c WHERE current_route=?
                UNION SELECT chat_id FROM routing_aliases WHERE route=?''', (route,route)).fetchone()
            return row is not None and self.channel_for_chat(row[0]) == channel

    def issue_grant(self, **kwargs):
        from src.agentbus_channel_grants_service import issue_grant
        return issue_grant(self, **kwargs)

    def enroll(self, token, **kwargs):
        from src.agentbus_channel_grants_service import enroll
        return enroll(self, token, **kwargs)

    def request_enrollment(self, **kwargs):
        from src.agentbus_enrollment_approval_service import request_enrollment
        return request_enrollment(self, **kwargs)

    def approve_enrollment(self, request_id, **kwargs):
        from src.agentbus_enrollment_approval_service import approve_enrollment
        return approve_enrollment(self, request_id, **kwargs)

    def pending_enrollments(self, *, channel=None):
        from src.agentbus_enrollment_approval_service import pending_enrollments
        return pending_enrollments(self, channel=channel)
