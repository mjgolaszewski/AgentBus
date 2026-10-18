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
    if snapshot['channel_details_truncated']:
        lines.append('Additional retained channel details omitted from this bounded dashboard.')
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
