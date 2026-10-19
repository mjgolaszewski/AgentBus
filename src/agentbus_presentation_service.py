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
    blocks = [block for paragraph in text.split('\n\n') for block in section_blocks(paragraph) if paragraph]
    if len(blocks) > 49:
        blocks = section_blocks(text)
    if len(blocks) > 49:
        blocks = blocks[:48] + section_blocks('Further output omitted from this bounded Slack view.')
    return [{'type':'header','text':{'type':'plain_text','text':'AgentBus','emoji':False}}, *blocks]


def health_text(snapshot: dict) -> str:
    from src.agentbus_operator_presentation_service import health_text as render
    return render(snapshot)
