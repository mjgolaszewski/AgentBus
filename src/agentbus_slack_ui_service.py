"""Private operator presentation; callers authorize user and channel first."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from src.agentbus_presentation_service import section_blocks

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
ACTIONS = frozenset({'results', 'results_page', 'enrollments_page', 'approve'})
ACTION_PREFIX = 'agentbus.operator.'


@dataclass(frozen=True)
class OperatorAction:
    """A bounded locator, never a grant of operator or channel authority."""
    kind: str
    channel: str
    target: str = ''
    page: int = 0

    def validate(self) -> None:
        if self.kind not in ACTIONS or type(self.page) is not int or not 0 <= self.page <= 65535:
            raise ValueError('Unsupported operator action')
        if (not isinstance(self.channel, str) or len(self.channel) > 80
                or not re.fullmatch(r'[CG][A-Z0-9]+', self.channel)):
            raise ValueError('Invalid operator action channel')
        needs_target = self.kind in {'results_page', 'approve'}
        if needs_target and not UUID.fullmatch(self.target):
            raise ValueError('Invalid operator action reference')
        if not needs_target and self.target:
            raise ValueError('Unexpected operator action reference')
        if self.kind == 'approve' and self.page:
            raise ValueError('Unexpected approval page')


class OperatorReply(str):
    """Keep existing string callers compatible while carrying private UI blocks."""
    blocks: list[dict[str, object]]
    command_messages: tuple[str, ...]
    display_text: str

    def __new__(cls, text: str, blocks: list[dict[str, object]],
                command_messages: tuple[str, ...] = (), *, legacy_text: str | None = None) -> OperatorReply:
        result = super().__new__(cls, text if legacy_text is None else legacy_text)
        result.blocks = blocks
        result.command_messages = command_messages
        result.display_text = text
        return result

    @property
    def text(self) -> str:
        return self.display_text


def action_button(label: str, action: OperatorAction) -> dict[str, object]:
    action.validate()
    if not isinstance(label, str) or not 1 <= len(label) <= 75:
        raise ValueError('Invalid operator button label')
    return {
        'type': 'button',
        'text': {'type': 'plain_text', 'text': label, 'emoji': False},
        'action_id': ACTION_PREFIX + action.kind,
        'value': json.dumps({'channel': action.channel, 'target': action.target, 'page': action.page}, separators=(',', ':')),
    }


def parse_action(value: object) -> OperatorAction:
    """Parse one action only after trusted Socket Mode user/channel checks."""
    if not isinstance(value, dict) or value.get('type') != 'button':
        raise ValueError('Expected one operator button')
    action_id = value.get('action_id')
    encoded = value.get('value')
    if (not isinstance(action_id, str) or not action_id.startswith(ACTION_PREFIX)
            or not isinstance(encoded, str) or len(encoded) > 256):
        raise ValueError('Invalid operator button')
    try:
        payload = json.loads(encoded)
    except ValueError:
        raise ValueError('Invalid operator button value') from None
    if (not isinstance(payload, dict) or set(payload) != {'channel', 'target', 'page'}
            or not isinstance(payload['target'], str)):
        raise ValueError('Invalid operator button value')
    action = OperatorAction(action_id[len(ACTION_PREFIX):], payload['channel'], payload['target'], payload['page'])
    action.validate()
    return action


def navigation(kind: str, target: str, page: int, total: int, page_size: int, channel: str) -> list[dict[str, object]]:
    if type(total) is not int or total < 0 or type(page_size) is not int or page_size <= 0:
        raise ValueError('Invalid operator page bounds')
    last = max(0, (total - 1) // page_size)
    if type(page) is not int or not 0 <= page <= last:
        raise ValueError('Operator page no longer exists; refresh the list')
    buttons = []
    if page:
        buttons.append(action_button('Previous', OperatorAction(kind, channel, target, page - 1)))
    if page < last:
        buttons.append(action_button('Next', OperatorAction(kind, channel, target, page + 1)))
    return [{'type': 'actions', 'elements': buttons}] if buttons else []


def approval_command(command: str, request_id: str) -> str:
    """Entire follow-up message is copyable; no markdown or explanatory text."""
    if not re.fullmatch(r'/[a-z][a-z0-9_-]{0,31}', command) or not UUID.fullmatch(request_id):
        raise ValueError('Invalid enrollment approval command')
    return f'{command} approve {request_id}'


def enrollment_reply(rows: list[dict], command: str, *, channel: str, page: int = 0) -> OperatorReply:
    """Receive only the authorized pending-request snapshot from its owner."""
    # One card followed by its one command-only message keeps mobile copies unambiguous.
    size = 1
    controls = navigation('enrollments_page', '', page, len(rows), size, channel)
    selected = rows[page * size:(page + 1) * size]
    text = f'Pending enrollment requests: {len(rows)}. Page {page + 1}.'
    blocks = section_blocks('*Enrollment requests*\n' + text)
    commands = []
    legacy = ['Showing pending requests; approved requests await join.']
    for row in selected:
        request_id = row['request_id']
        text += f"\n\n{row['display_name']}\n{row['route']}\nRepository: {row['repo']}"
        legacy.append(f"{request_id} | {row['route']} | {row['display_name']} | profile {row.get('chat_id', '')}")
        legacy.append(f'`{approval_command(command, request_id)}`')
        blocks.extend(section_blocks(f"*{row['display_name']}*\n{row['route']}\n"
                                     f"Repository: {row['repo']}"))
        if row.get('approved_channel') is not None:
            blocks.extend(section_blocks('Already approved; waiting for the requester to finish joining.'))
        else:
            commands.append(approval_command(command, request_id))
            blocks.append({'type': 'actions', 'elements': [
                action_button('Approve enrollment', OperatorAction('approve', channel, request_id))]})
    blocks.extend(controls)
    if not rows:
        text = 'No pending enrollment requests.'
        blocks = section_blocks(text)
        legacy = [text]
    return OperatorReply(text, blocks, tuple(commands), legacy_text='\n'.join(legacy))


def results_reply(control_id: str, rows: list[dict], names: dict[str, str], *, channel: str, page: int = 0) -> OperatorReply:
    """Render a fixed control's channel-filtered target snapshot without omission."""
    if not UUID.fullmatch(control_id):
        raise ValueError('Invalid result reference')
    size = 4
    controls = navigation('results_page', control_id, page, len(rows), size, channel)
    if not rows:
        text = 'No announcement results in this channel.'
        return OperatorReply(text, section_blocks(text))
    kind = rows[0]['kind']
    if kind not in {'poll', 'broadcast'} or any(row['kind'] != kind for row in rows):
        raise ValueError('Unsupported result kind')
    answered = sum(row.get('answer') is not None for row in rows)
    acknowledged = sum(row['state'] == 'effective' for row in rows)
    last = max(1, (len(rows) + size - 1) // size)
    heading = '*Poll results*' if kind == 'poll' else '*Broadcast read receipts*'
    count = (f'{answered}/{len(rows)} answered; {len(rows) - answered} pending.' if kind == 'poll'
             else f'{acknowledged}/{len(rows)} acknowledged.')
    paragraphs = [heading, rows[0]['reason'], f'{count} Page {page + 1}/{last}.']
    for row in rows[page * size:(page + 1) * size]:
        sid = row['session_id']
        name = names.get(sid, 'Retired session · ' + sid[-6:])
        if not isinstance(name, str) or len(name) > 240:
            raise ValueError('Invalid result identity label')
        answer = row.get('answer')
        if answer is not None and (not isinstance(answer, str) or not 1 <= len(answer) <= 2000):
            raise ValueError('Invalid stored poll answer')
        if kind == 'poll':
            body = answer if answer is not None else 'Awaiting answer'
        else:
            body = row.get('lifecycle_state', row['state'])
        paragraphs.append(f'*{name}*\n{body}')
    blocks = [block for paragraph in paragraphs for block in section_blocks(paragraph)]
    blocks.extend(controls)
    blocks.append({'type': 'actions', 'elements': [
        action_button('Refresh', OperatorAction('results_page', channel, control_id, page)),
        action_button('Recent polls', OperatorAction('results', channel))]})
    if len(blocks) > 49:
        raise ValueError('Result page exceeds Slack block bound')
    return OperatorReply('\n\n'.join(paragraphs), blocks)


def recent_polls_reply(rows: list[dict], *, channel: str, page: int = 0) -> OperatorReply:
    """Only authorized channel-local polls supplied by the control-store owner."""
    size = 4
    controls = navigation('results', '', page, len(rows), size, channel)
    text = f'Most recent 40 retained polls. Page {page + 1}. Choose Results to read the answers.'
    blocks = section_blocks('*Recent polls*\n' + text)
    for row in rows[page * size:(page + 1) * size]:
        if not isinstance(row['reason'], str) or not 1 <= len(row['reason']) <= 1000:
            raise ValueError('Invalid poll question')
        blocks.extend(section_blocks(row['reason']))
        text += '\n\n' + row['reason']
        blocks.append({'type': 'actions', 'elements': [
            action_button('Results', OperatorAction('results_page', channel, row['control_id']))]})
    blocks.extend(controls)
    if not rows:
        text = 'No retained polls in this channel.'
        blocks = section_blocks(text)
    return OperatorReply(text, blocks)
