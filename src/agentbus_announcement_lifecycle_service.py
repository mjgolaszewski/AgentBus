"""Per-target announcement read outcomes; safety controls have no ordinary expiry."""
from __future__ import annotations

from src.agentbus_transaction_service import atomic


class AnnouncementLifecycle:
    def __init__(self, owner):
        self.owner = owner
        with owner._lock, atomic(owner._db):
            owner._db.execute('''CREATE TABLE IF NOT EXISTS lifecycle_announcements (
                control_id TEXT NOT NULL,session_id TEXT NOT NULL,channel TEXT NOT NULL,
                state TEXT NOT NULL,deadline REAL NOT NULL,outcome_at REAL,
                PRIMARY KEY(control_id,session_id),
                FOREIGN KEY(control_id,session_id) REFERENCES control_targets(control_id,session_id))''')
            owner._db.execute('CREATE INDEX IF NOT EXISTS lifecycle_announcement_deadline ON lifecycle_announcements(state,deadline,channel)')
            # The control owner may have recovered a committed ACK before this owner was attached.
            recovery = ("state='unread' AND EXISTS (SELECT 1 FROM control_targets t WHERE "
                        "t.control_id=lifecycle_announcements.control_id AND t.session_id=lifecycle_announcements.session_id "
                        "AND t.state IN ('acknowledged','effective'))")
            if owner._db.execute('SELECT 1 FROM lifecycle_announcements WHERE ' + recovery + ' LIMIT 1').fetchone():
                owner._db.execute("UPDATE lifecycle_announcements SET state='read_acknowledged',outcome_at=? WHERE "
                                  + recovery, (owner.lifecycle.now(persist=True),))

    def reserve(self, target_count, reason):
        if not self.owner.lifecycle.enabled:
            return
        payloads = self.owner._db.execute("SELECT count(*) FROM operator_controls WHERE kind IN ('broadcast','poll')").fetchone()[0]
        targets = self.owner._db.execute("SELECT count(*) FROM control_targets t JOIN operator_controls c USING(control_id) "
                                         "WHERE c.kind IN ('broadcast','poll')").fetchone()[0]
        if payloads >= 65536 or targets + target_count > 131072:
            raise ValueError('Retained announcement capacity requires operator retention review; no safety control is affected')
        self.owner.retention.reserve_storage(len(reason.encode('utf-8')) + target_count * 512)

    def capacity(self):
        with self.owner._lock:
            payloads = self.owner._db.execute("SELECT count(*) FROM operator_controls WHERE kind IN ('broadcast','poll')").fetchone()[0]
            targets = self.owner._db.execute("SELECT count(*) FROM control_targets JOIN operator_controls USING(control_id) "
                                             "WHERE kind IN ('broadcast','poll')").fetchone()[0]
            return {'retained_payloads':payloads, 'payload_limit':65536,
                    'target_receipts':targets, 'target_limit':131072, 'retirement_qualified':False}

    def register(self, control_id):
        if not self.owner.lifecycle.enabled:
            return
        current = self.owner.lifecycle.now(persist=True)
        self.owner._db.execute('''INSERT INTO lifecycle_announcements
            SELECT t.control_id,t.session_id,b.channel,'unread',?,NULL
            FROM control_targets t JOIN operator_controls c USING(control_id)
            JOIN participation_sessions s USING(session_id) JOIN chat_channels b USING(chat_id)
            WHERE t.control_id=? AND c.kind IN ('broadcast','poll')''',
            (current + self.owner.lifecycle.policy.pickup_seconds, control_id))

    def projection(self, control_id, session_id):
        with self.owner._lock:
            row = self.owner._db.execute('SELECT state,deadline FROM lifecycle_announcements '
                'WHERE control_id=? AND session_id=?', (control_id, session_id)).fetchone()
            return {'lifecycle_state':row['state'], 'read_deadline':row['deadline']} if row else {}

    def pending(self, control_id, session_id):
        projection = self.projection(control_id, session_id)
        return not projection or (projection['lifecycle_state'] == 'unread'
                                  and projection['read_deadline'] > self.owner.lifecycle.now())

    def acknowledge(self, control_id, session_id):
        self.owner._db.execute("UPDATE lifecycle_announcements SET state='read_acknowledged',outcome_at=? "
            "WHERE control_id=? AND session_id=? AND state='unread'",
            (self.owner.lifecycle.now(persist=True), control_id, session_id))

    def settle(self, channel, current):
        self.owner._db.execute("UPDATE lifecycle_announcements SET state='expired_unread',outcome_at=? "
            "WHERE state='unread' AND deadline<=? AND (? IS NULL OR channel=?) AND NOT EXISTS "
            "(SELECT 1 FROM control_targets t WHERE t.control_id=lifecycle_announcements.control_id "
            "AND t.session_id=lifecycle_announcements.session_id AND t.state IN ('acknowledged','effective'))",
            (current, current, channel, channel))

    def counts(self, channel):
        with self.owner._lock:
            return dict(self.owner._db.execute('SELECT state,count(*) FROM lifecycle_announcements '
                'WHERE channel=? GROUP BY state', (channel,)).fetchall())

    def due(self, channel, current):
        return self.owner._db.execute("SELECT count(*) FROM lifecycle_announcements a WHERE channel=? "
            "AND state='unread' AND deadline<=? AND NOT EXISTS (SELECT 1 FROM control_targets t "
            "WHERE t.control_id=a.control_id AND t.session_id=a.session_id AND t.state IN ('acknowledged','effective'))",
            (channel, current)).fetchone()[0]
