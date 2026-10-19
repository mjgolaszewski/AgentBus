"""Bounded channel-scoped operator reads; no probes, settlement or acknowledgement."""
from __future__ import annotations


def pending_snapshot(store, channel, session_id=None):
    with store._lock:
        presence = store.participation.presence(session_id) if session_id else None
        if presence and presence['channel'] != channel:
            raise KeyError('Session unavailable in this channel')
        chat_id = presence['chat_id'] if presence else None
        targets = store._db.execute(
            'SELECT p.position,t.state,t.deadline,t.lease_until,i.current_route FROM lifecycle_targets t '
            'JOIN chat_identities i ON i.chat_id=t.chat_id '
            'JOIN channel_positions p ON p.message_id=t.cursor AND p.channel=t.channel '
            "WHERE t.channel=? AND t.state IN ('unclaimed','picked_up','recovery_required') "
            'AND (? IS NULL OR t.chat_id=?) ORDER BY p.position,t.chat_id LIMIT 21',
            (channel, chat_id, chat_id)).fetchall()
        controls = store._db.execute(
            'SELECT c.control_id,c.kind,t.state,s.session_id,a.state AS read_state FROM control_targets t '
            'JOIN operator_controls c USING(control_id) '
            'JOIN participation_sessions s USING(session_id) JOIN chat_channels b USING(chat_id) '
            'LEFT JOIN lifecycle_announcements a ON a.control_id=t.control_id AND a.session_id=t.session_id '
            "WHERE b.channel=? AND t.state!='effective' AND (? IS NULL OR t.session_id=?) "
            "AND (a.state IS NULL OR a.state='unread') ORDER BY c.issued_at,c.control_id,t.session_id LIMIT 21",
            (channel, session_id, session_id)).fetchall()
        delivery = [] if session_id else store._db.execute(
            'SELECT p.position,o.state,o.attempts FROM local_delivery_obligations o '
            'JOIN channel_positions p ON p.channel=o.channel AND p.message_id=o.local_cursor '
            "WHERE o.channel=? AND o.state!='submitted' ORDER BY p.position LIMIT 21", (channel,)).fetchall()
        return {'targets':[dict(row) for row in targets[:20]], 'controls':[dict(row) for row in controls[:20]],
                'delivery':[dict(row) for row in delivery[:20]],
                'truncated':any(len(rows)>20 for rows in (targets, controls, delivery)),
                'lifecycle_enabled':store.lifecycle.enabled, 'session_filtered':session_id is not None}


def explain_snapshot(store, channel, position):
    with store._lock:
        cursor = store.channels.internal_cursor(channel, position)
        row = store._db.execute('SELECT received_at FROM messages WHERE channel=? AND cursor=?',
                                (channel, cursor)).fetchone() if cursor is not None else None
        if row is None:
            return None
        transport = store._db.execute('SELECT state,attempts FROM local_delivery_obligations '
                                      'WHERE channel=? AND local_cursor=?', (channel, cursor)).fetchone()
        recipients = dict(store._db.execute('SELECT state,count(*) FROM lifecycle_targets '
            'WHERE channel=? AND cursor=? GROUP BY state', (channel, cursor)).fetchall())
        observed = store._db.execute('SELECT count(*) FROM local_delivery_observations '
                                    'WHERE channel=? AND local_cursor=?', (channel, cursor)).fetchone()[0]
        archived = store._db.execute('SELECT 1 FROM lifecycle_archive WHERE channel=? AND cursor=?',
                                    (channel, cursor)).fetchone() is not None
        legacy = store._db.execute('SELECT legacy_unknown FROM lifecycle_payloads WHERE channel=? AND cursor=?',
                                  (channel, cursor)).fetchone()
        return {'position':position, 'received_at':row['received_at'],
                'transport':dict(transport) if transport else None, 'observations':observed,
                'recipients':recipients, 'archived':archived, 'legacy_unknown':bool(legacy and legacy[0])}


def doctor_checks(snapshot):
    """Inspect an already-produced dashboard; never connect to a provider or repair state."""
    checks = []
    connected = snapshot['slack_connected']
    checks.append(('Slack connection', 'OK' if connected else 'UNKNOWN' if connected is None else 'WARN',
                   'Observed socket state only; not end-to-end delivery.'))
    workers = snapshot.get('workers')
    state = workers['task']['state'] if workers else 'not_observed'
    checks.append(('Delivery task', 'OK' if state == 'running' else 'WARN' if state == 'failed' else 'UNKNOWN',
                   f'{state}; inspect /agentbus workers.'))
    if workers and workers['delivery']['error']:
        checks.append(('Delivery cycle', 'WARN', 'Last cycle reported an error; inspect retained queues and worker status.'))
    if workers and workers['federation']['error']:
        checks.append(('Federation cycle', 'WARN', 'Last local cycle reported an error; preserve checkpoints and retained obligations.'))
    capacity = snapshot['local_delivery_capacity']
    checks.append(('Custody capacity', 'WARN' if capacity['warnings'] else 'OK',
                   'Inspect /agentbus pending and /agentbus storage; never delete retry identities.'))
    storage = snapshot.get('lifecycle_storage')
    if storage:
        checks.append(('Storage headroom', 'WARN' if storage['accounted_bytes']*5 >= storage['limit_bytes']*4 else 'OK',
                       'Use retention previews and custodian capacity planning before applying maintenance.'))
    recovery = sum(c.get('lifecycle', {}).get('recipients', {}).get('recovery_required', 0)
                   for c in snapshot['channels'])
    checks.append(('Recipient recovery', 'WARN' if recovery else 'OK',
                   f'{recovery} recovery-required outcomes; no automatic retry or completion is inferred.'))
    checks.extend([('External poll / wake workers', 'UNKNOWN', 'Not observed by this service; inspect on the owning host.'),
                   ('Remote federation health', 'UNKNOWN', 'No remote probe or health guarantee.'),
                   ('Owner wake qualification', 'UNQUALIFIED', 'AHP interoperability and atomic idle-only admission remain unqualified.')])
    return checks
