"""Escape invisible controls in Slack's human-facing projection only."""

from __future__ import annotations

import unicodedata

SLACK_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def visible_text(value: str) -> str:
    return "".join(
        f"\\u{ord(char):04x}" if unicodedata.category(char) in {"Cc", "Cf"} and char not in "\n\t"
        else char for char in value
    )


def slack_escape(value: str) -> str:
    """Escape Slack control markup while retaining native emphasis and code styles."""
    return ''.join(SLACK_ESCAPES.get(char,char) for char in visible_text(value))


def section_blocks(value: str) -> list[dict[str, object]]:
    """Bound encoded text objects without splitting an escape entity."""
    chunks: list[str] = []
    current = ''
    for char in visible_text(value):
        escaped = SLACK_ESCAPES.get(char,char)
        if len(current)+len(escaped)>3000:
            chunks.append(current)
            current=''
        current+=escaped
    if current:
        chunks.append(current)
    return [{'type':'section','text':{'type':'mrkdwn','text':chunk,'verbatim':True}} for chunk in chunks]


def message_blocks(message, *, locator: str | None = None) -> list[dict[str, object]]:
    label=f'📤 `{message.sender}` → 📥 `{message.recipient}` · {message.kind} · {message.audience}'
    if message.repo:
        label+=f' · {message.repo}'
    context: dict[str, object]={'type':'context','elements':[{'type':'mrkdwn','text':slack_escape(label),'verbatim':True}]}
    if locator is not None:
        context['block_id']=locator
    return [context,*section_blocks(message.text)]


def command_blocks(text: str) -> list[dict[str, object]]:
    return [{'type':'header','text':{'type':'plain_text','text':'AgentBus','emoji':False}},*section_blocks(text)]


def health_text(snapshot: dict) -> str:
    runtime=snapshot['runtime']
    federation=snapshot['federation']
    connected=snapshot['slack_connected']
    slack='🟡 unknown' if connected is None else '🟢 connected' if connected else '🔴 disconnected'
    uptime=runtime['uptime_seconds']
    lines=['*🩺 AgentBus health*',f"📦 *Version:* `{snapshot['version']}` · protocol `{snapshot['protocol_version']}` · participation `{snapshot['participation_version']}`",
           f"🖥️ *Runtime:* {runtime['implementation']} Python `{runtime['python']}` · {runtime['platform']} · PID `{runtime['pid']}`",
           f"⏱️ *Uptime:* `{str(uptime)+'s' if uptime is not None else 'unknown'}` · Slack {slack} · delivery {snapshot['delivery_mode']}",
           f"📬 *Channels:* `{sum(c['enabled'] for c in snapshot['channels'])}` enabled · `{snapshot['retained_channel_count']}` retained"]
    for channel in snapshot['channels']:
        delivery=', '.join(f'{key}:{value}' for key,value in sorted(channel['delivery'].items())) or 'none'
        lines.append(f"`{channel['channel']}` · {'🟢 enabled' if channel['enabled'] else '⚪ disabled / retained'} · messages `{channel['messages']}` · local high-water `{channel['high_water_cursor']}` · sessions `{sum(channel['sessions'].values())}`\nDelivery custody: {delivery}")
        lifecycle = channel.get('lifecycle', {})
        if lifecycle.get('enabled'):
            targets = lifecycle['recipients']
            lines.append('*Recipient lifecycle*\n' + ' · '.join(
                f"{label} `{targets.get(state, 0)}`" for state, label in (
                    ('unclaimed', '📥 unclaimed'), ('picked_up', '🔵 picked up'),
                    ('handled', '🟢 handled'), ('expired_unclaimed', '⚪ expired unclaimed'),
                    ('recovery_required', '🟡 recovery required'))))
            lines.append(f"*Retention:* payloads `{lifecycle['retained_payloads']}` · "
                         f"legacy unknown / pinned `{lifecycle['legacy_unknown_pinned']}` · "
                         f"pinned payload bytes `{lifecycle['legacy_pinned_payload_bytes']}` · "
                         f"archived `{lifecycle['archived_payloads']}` / `{lifecycle['archive_bytes']}` bytes")
            announcements = lifecycle.get('announcements', {})
            lines.append('*Broadcast / poll receipts:* ' + ' · '.join(
                f"{label} `{announcements.get(state, 0)}`" for state, label in
                (('unread','📥 unread'), ('read_acknowledged','🟢 acknowledged'), ('expired_unread','⚪ expired unread'))))
        else:
            lines.append('⚪ Recipient lifecycle and retention policy: not enabled; delivery is not acknowledgement.')
    if snapshot['channel_details_truncated']:
        lines.append('Additional retained channel details omitted from this bounded dashboard.')
    if storage := snapshot.get('lifecycle_storage'):
        lines.append(f"💾 *Lifecycle admission budget:* `{storage['accounted_bytes']}/{storage['limit_bytes']}` bytes · occupied SQLite pages + WAL/SHM; archives included\n"
                     f"Physical allocation `{storage['physical_bytes']}` bytes · reusable database pages `{storage['reusable_bytes']}` bytes\n"
                     f"Operator archive-only maintenance headroom `{storage['archive_maintenance_bytes']}` bytes; maximum accounted archive footprint `{storage['archive_limit_bytes']}` bytes")
        if storage['accounted_bytes'] * 5 >= storage['limit_bytes'] * 4:
            lines.append('🟡 Lifecycle storage at or above 80%; inspect retention before admission runs out of headroom.')
    if policy := snapshot.get('lifecycle_policy'):
        lines.append(f"⏳ *Lifecycle policy:* pickup `{policy['pickup_seconds']}s` · work lease `{policy['work_lease_seconds']}s` · "
                     f"epoch `{policy['epoch_seconds']}s` · replay `{policy['replay_seconds']}s` · archive `{policy['archive_seconds']}s` · retention `{policy['retention_seconds']}s`")
    if shared := snapshot.get('announcement_capacity'):
        lines.append(f"📣 *Pinned shared history:* payloads `{shared['retained_payloads']}/{shared['payload_limit']}` · "
                     f"target receipts `{shared['target_receipts']}/{shared['target_limit']}` · retirement unqualified")
        if shared['retained_payloads'] * 5 >= shared['payload_limit'] * 4 or shared['target_receipts'] * 5 >= shared['target_limit'] * 4:
            lines.append('🟡 Shared history at or above 80%; finite retained-history backpressure remains.')
    if maintenance := snapshot.get('storage_maintenance'):
        lines.append(f"🧹 *Last WAL maintenance:* {maintenance['status']} · semantic custody unchanged")
    if capacity := snapshot.get('local_delivery_capacity'):
        lines.append(f"📊 *Local custody:* outstanding `{capacity['outstanding']}/{capacity['outstanding_limit']}` · "
                     f"retained requests `{capacity['retained_requests']}/{capacity['retained_request_limit']}` · "
                     f"provider observations `{capacity['provider_observations']}/{capacity['provider_observation_limit']}`")
        if capacity['warnings']:
            lines.append('🟡 Capacity at or above 80%: ' + ', '.join(capacity['warnings']) +
                         '. Operator capacity planning required; retained request identities do not expire.')
    state='⚪ disabled' if not federation['configured'] else '🟢 initialized' if federation['initialized'] else '🟡 configured / awaiting transport'
    lines.extend([f"🌐 *Federation:* {state} · bridge {federation['channel'] or 'none'} · pinned origins {', '.join(federation['origins']) or 'none'}",
                  f"🟡 Remote health: not observed · checkpoints `{federation['checkpoints']}` · retained obligations `{sum(federation['custody'].values())}`",
                  f"💾 *Storage:* `{snapshot['database_bytes']}` bytes including WAL sidecars",
                  'Provider delivery, destination custody and semantic ACK are distinct. Owner interoperability and atomic idle-only wake remain unqualified.'])
    return '\n'.join(lines)
