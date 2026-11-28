"""Optional private threshold notices on the existing transport budget."""
from __future__ import annotations

import time

import httpx

from src.agentbus_federation_slack_service import bounded_response


class CapacityNoticeWorker:
    def __init__(self, store, settings, client):
        self.store, self.settings, self.client = store, settings, client
        self.channel = None
        self.next_sample = 0.0
        with store._lock, store._db:
            store._db.execute('''CREATE TABLE IF NOT EXISTS federation_capacity_notices (
                table_name TEXT NOT NULL, operator TEXT NOT NULL, channel TEXT NOT NULL,
                sent_level INTEGER NOT NULL DEFAULT 0, pending_level INTEGER,
                observed_count INTEGER NOT NULL, row_limit INTEGER NOT NULL, sampled_at REAL NOT NULL,
                state TEXT NOT NULL, PRIMARY KEY(table_name,operator))''')

    def bind_channel(self, channel):
        if channel not in self.settings.channels or self.channel not in {None, channel}:
            raise ValueError('Capacity notice channel differs from configured federation')
        self.channel = channel

    def sample(self, *, now=None):
        instant = time.time() if now is None else now
        if self.channel is None or instant < self.next_sample:
            return
        self.next_sample = instant + 60
        with self.store._lock, self.store._db:
            snapshot = self.store.federation.capacity()
            limit = snapshot['per_table_limit']
            for table, count in snapshot['rows'].items():
                level = 100 if count >= limit else 95 if count * 100 >= limit * 95 else 80 if count * 5 >= limit * 4 else 0
                for actor in sorted(self.settings.slack_operator_user_ids):
                    row = self.store._db.execute('SELECT * FROM federation_capacity_notices '
                        'WHERE table_name=? AND operator=?', (table, actor)).fetchone()
                    if row is None:
                        total = self.store._db.execute('SELECT count(*) FROM federation_capacity_notices').fetchone()[0]
                        if total >= 64:
                            raise ValueError('Capacity notice pair bound exhausted; no custody changed')
                        self.store._db.execute('INSERT INTO federation_capacity_notices VALUES(?,?,?,0,NULL,?,?,?,?)',
                            (table, actor, self.channel, count, limit, instant, 'observed'))
                        sent, pending = 0, None
                    else:
                        sent, pending = row['sent_level'], row['pending_level']
                    if level > sent:
                        pending = max(level, pending or 0)
                    elif count * 10 < limit * 7:
                        pending = 0 if sent else None
                    self.store._db.execute('UPDATE federation_capacity_notices SET pending_level=?, '
                        'observed_count=?,row_limit=?,sampled_at=? WHERE table_name=? AND operator=?',
                        (pending, count, limit, instant, table, actor))

    def _next(self):
        with self.store._lock:
            rows = self.store._db.execute('SELECT * FROM federation_capacity_notices '
                'WHERE pending_level IS NOT NULL AND channel=? ORDER BY sampled_at,table_name,operator LIMIT 64',
                (self.channel,)).fetchall()
            return next((dict(row) for row in rows if row['operator'] in self.settings.slack_operator_user_ids), None)

    def pending(self):
        return self._next() is not None

    async def submit_once(self, *, budget=1, deadline=None):
        deadline = time.monotonic() + 15 if deadline is None else deadline
        if budget < 1 or time.monotonic() >= deadline:
            return 0
        with self.store._lock, self.store._db:
            item = self._next()
            if item is None:
                return 0
            # Reserve before the network call. Uncertain outcomes never blindly repeat.
            self.store._db.execute('UPDATE federation_capacity_notices SET sent_level=?, '
                "pending_level=NULL,state='uncertain' WHERE table_name=? AND operator=?",
                (item['pending_level'], item['table_name'], item['operator']))
        level = item['pending_level']
        label = f'at or above {level}%' if level else 'recovered below 70%'
        text = (f"AgentBus federation custody {label}: {item['table_name']} "
                f"{item['observed_count']}/{item['row_limit']} retained rows; "
                f"{max(0, item['row_limit'] - item['observed_count'])} remaining. "
                f"Sample {item['sampled_at']}. Review private health and custodian capacity planning. "
                'No automatic increase, pruning, replay or work acknowledgement is authorized.')
        state = 'uncertain'
        try:
            response = await bounded_response(self.client, self.settings.slack_bot_token,
                'chat.postEphemeral', {'channel':item['channel'], 'user':item['operator'],
                                      'text':text, 'parse':'none'},
                max_bytes=16384, timeout_seconds=max(.001, min(15, deadline - time.monotonic())))
            result = response.json()
            if isinstance(result, dict) and result.get('ok') is True and response.is_success:
                state = 'submitted_private_notice'
            elif isinstance(result, dict) and result.get('ok') is False:
                state = 'provider_rejected_no_retry'
        except (httpx.HTTPError, TimeoutError, ValueError, RecursionError):
            pass
        with self.store._lock, self.store._db:
            self.store._db.execute('UPDATE federation_capacity_notices SET state=? '
                'WHERE table_name=? AND operator=?', (state, item['table_name'], item['operator']))
        return 1
