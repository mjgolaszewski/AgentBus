"""Quiet, grouped Slack operator reports with explicit observation limits."""
from __future__ import annotations

from datetime import datetime, timezone


def _timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec='seconds') if value is not None else 'not observed'


def workers_text(snapshot):
    rows = snapshot.get('workers')
    lines = ['*Workers*']
    if not rows:
        lines.append('Service delivery task: not observed')
    else:
        for key, label in (('task', 'Service delivery task'), ('delivery', 'Delivery / lifecycle cycle'),
                           ('federation', 'Federation cycle')):
            row = rows[key]
            lines.append(f"{label}: `{row['state']}`")
            if key != 'task':
                lines.append(f"Last attempt: `{_timestamp(row['last_attempt_at'])}`; completed cycle: `{_timestamp(row['last_success_at'])}`")
            if row['error']:
                lines.append(f"Warning: `{row['error']}` at `{_timestamp(row['last_error_at'])}`")
    lines.extend(['Workspace poll workers: not observed', 'Host wake supervisor: not observed',
                  'Process-local observations reset on restart. A completed cycle is not proof of delivery or execution.'])
    return '\n'.join(lines)


def health_text(snapshot):
    runtime, federation = snapshot['runtime'], snapshot['federation']
    connected = snapshot['slack_connected']
    connection = 'not observed' if connected is None else 'connected' if connected else 'DISCONNECTED'
    uptime = runtime['uptime_seconds']
    sections = ['*AgentBus health*', '*Service*\n'
        f"Version `{snapshot['version']}`; protocol `{snapshot['protocol_version']}`; participation `{snapshot['participation_version']}`\n"
        f"{runtime['implementation']} Python `{runtime['python']}` on {runtime['platform']}; PID `{runtime['pid']}`\n"
        f"Uptime `{str(uptime)+'s' if uptime is not None else 'not observed'}`; Slack `{connection}`\n"
        f"Delivery mode: `{snapshot['delivery_mode']}`", workers_text(snapshot),
        f"*Channels*\n`{sum(c['enabled'] for c in snapshot['channels'])}` enabled; `{snapshot['retained_channel_count']}` retained"]
    channels = snapshot['channels'][:8]
    for c in channels:
        sections.append(f"*{c['channel']}* — {'enabled' if c['enabled'] else 'disabled / retained'}\n"
            f"Messages `{c['messages']}`; local high-water `{c['high_water_cursor']}`; sessions `{sum(c['sessions'].values())}`")
    if snapshot['channel_details_truncated'] or len(channels) < len(snapshot['channels']):
        sections.append('Additional retained channel details omitted from this bounded dashboard.')
    sections.append('*Queues*\nTransport custody, recipient work and shared read receipts are separate.')
    for c in channels:
        delivery = '; '.join(f"{key} `{value}`" for key, value in sorted(c['delivery'].items())) or 'none'
        lines = [f"*{c['channel']}*", f'Delivery custody: {delivery}']
        lifecycle = c.get('lifecycle', {})
        if lifecycle.get('enabled'):
            lines.append('Recipient lifecycle: ' + '; '.join(f"{state.replace('_',' ')} `{lifecycle['recipients'].get(state,0)}`"
                for state in ('unclaimed','picked_up','handled','expired_unclaimed','recovery_required')))
            announcements = lifecycle.get('announcements', {})
            lines.append('Broadcast / poll receipts: ' + '; '.join(f"{label} `{announcements.get(state,0)}`"
                for state, label in (('unread','unread'),('read_acknowledged','acknowledged'),('expired_unread','expired unread'))))
            lines.append(f"Payloads `{lifecycle['retained_payloads']}`; legacy unknown / pinned `{lifecycle['legacy_unknown_pinned']}`; "
                f"pinned bytes `{lifecycle['legacy_pinned_payload_bytes']}`; archived `{lifecycle['archived_payloads']}` / `{lifecycle['archive_bytes']}` bytes")
        else:
            lines.append('Recipient lifecycle and retention policy: not enabled; delivery is not acknowledgement.')
        sections.append('\n'.join(lines))
    if capacity := snapshot.get('local_delivery_capacity'):
        lines = [f"Local custody: outstanding `{capacity['outstanding']}/{capacity['outstanding_limit']}`; retained requests "
            f"`{capacity['retained_requests']}/{capacity['retained_request_limit']}`; provider observations `{capacity['provider_observations']}/{capacity['provider_observation_limit']}`"]
        if capacity['warnings']:
            lines.append('WARNING — capacity at or above 80%: ' + ', '.join(capacity['warnings']))
        sections.append('\n'.join(lines))
    lines = ['*Storage*', f"Database including WAL sidecars: `{snapshot['database_bytes']}` bytes"]
    if storage := snapshot.get('lifecycle_storage'):
        lines.extend([f"Lifecycle admission budget `{storage['accounted_bytes']}/{storage['limit_bytes']}` bytes (archives included)",
            f"Physical allocation `{storage['physical_bytes']}` bytes; reusable database pages `{storage['reusable_bytes']}` bytes",
            f"Archive-only maintenance headroom `{storage['archive_maintenance_bytes']}` bytes; archive limit `{storage['archive_limit_bytes']}` bytes"])
        if storage['accounted_bytes']*5 >= storage['limit_bytes']*4:
            lines.append('WARNING — lifecycle storage at or above 80%; inspect retention before admission exhausts headroom.')
    if shared := snapshot.get('announcement_capacity'):
        lines.append(f"Pinned shared history: payloads `{shared['retained_payloads']}/{shared['payload_limit']}`; "
            f"target receipts `{shared['target_receipts']}/{shared['target_limit']}`; retirement unqualified")
        if shared['retained_payloads']*5 >= shared['payload_limit']*4 or shared['target_receipts']*5 >= shared['target_limit']*4:
            lines.append('WARNING — shared history at or above 80%.')
    if maintenance := snapshot.get('storage_maintenance'):
        lines.append(f"Last WAL maintenance: `{maintenance['status']}`; semantic custody unchanged")
    sections.append('\n'.join(lines))
    if policy := snapshot.get('lifecycle_policy'):
        sections.append('*Lifecycle policy*\n' + '; '.join(f"{label} `{policy[key]}s`" for key, label in (
            ('pickup_seconds','pickup'),('work_lease_seconds','work lease'),('epoch_seconds','epoch'),
            ('replay_seconds','replay'),('archive_seconds','archive'),('retention_seconds','retention'))))
    state = 'disabled' if not federation['configured'] else 'initialized' if federation['initialized'] else 'configured / awaiting transport'
    sections.extend([f"*Federation*\nLocal bridge: `{state}`; channel `{federation['channel'] or 'none'}`\n"
        f"Pinned origins: {', '.join(federation['origins']) or 'none'}\nCheckpoints `{federation['checkpoints']}`; "
        f"retained obligations `{sum(federation['custody'].values())}`\nRemote health: not observed",
        '*Qualification*\nProvider delivery, destination custody and semantic ACK are distinct. '
        'Owner interoperability and atomic idle-only wake remain unqualified.'])
    return '\n\n'.join(sections)


def pending_text(snapshot):
    sections = ['*Pending — this channel*']
    if snapshot['session_filtered']:
        sections.append('Selected identity work and exact-session controls; transport omitted from this filtered view.')
    sections.append('*Recipient work*\n' + ('\n'.join(f"Cursor `{r['position']}`: {r['current_route']} / {r['state']}" for r in snapshot['targets']) or
        ('No recorded open recipient outcomes.' if snapshot['lifecycle_enabled'] else 'Lifecycle not enabled; legacy work is not classified.')))
    sections.append('*Controls / shared reads*\n' + ('\n'.join(f"`{r['control_id']}`: {r['kind']} / {r['read_state'] or r['state']} / session `{r['session_id']}`"
        for r in snapshot['controls']) or 'None recorded.'))
    if not snapshot['session_filtered']:
        sections.append('*Transport custody*\n' + ('\n'.join(f"Cursor `{r['position']}`: {r['state']}; attempts `{r['attempts']}`"
            for r in snapshot['delivery']) or 'None recorded.'))
    if snapshot['truncated']:
        sections.append('Further entries omitted; each group is bounded to 20 rows.')
    sections.append('Recorded states only; elapsed deadlines are not settled and no work is acknowledged or retried.')
    return '\n\n'.join(sections)


def explain_text(snapshot):
    if snapshot is None:
        return 'Message unavailable in this channel (unknown or retired cursor).'
    transport = snapshot['transport']
    sections = [f"*Cursor `{snapshot['position']}` — retained evidence*\nLocal custody recorded: `{snapshot['received_at']}`",
        '*Transport*\n' + (f"State `{transport['state']}`; provider attempts `{transport['attempts']}`" if transport else
        'No retained ordinary delivery obligation.') + f"\nRetained provider observations: `{snapshot['observations']}`",
        '*Recipient outcomes*\n' + ('; '.join(f"{state} `{count}`" for state, count in sorted(snapshot['recipients'].items())) or
        'No classified recipient outcomes retained.'),
        f"*Retention*\nArchived: `{snapshot['archived']}`; legacy unknown / pinned: `{snapshot['legacy_unknown']}`",
        'Retained-state summary, not a complete event log. Missing stages remain unknown; posting is not execution. '
        'Reads do not settle deadlines or acknowledge work.']
    return '\n\n'.join(sections)
