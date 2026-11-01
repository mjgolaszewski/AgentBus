"""Authorized operator queries and button dispatch over existing application owners."""
from __future__ import annotations

import re

from src.agentbus_presentation_service import section_blocks
from src.agentbus_slack_ui_service import (
    OperatorAction,
    OperatorReply,
    action_button,
    enrollment_reply,
    parse_action,
    recent_polls_reply,
    results_reply,
)


def page_number(value: str) -> int:
    if not re.fullmatch(r'[1-9][0-9]{0,4}', value) or int(value) > 65536:
        raise ValueError('Choose a page with the Previous and Next buttons.')
    return int(value) - 1


def query_reply(parts: list[str], settings, store, channel: str) -> str:
    """Caller must authorize the verified Slack operator before any query."""
    try:
        if parts[0] == 'enrollments':
            if len(parts) > 2:
                raise ValueError(f'Use {settings.slack_command} enrollments, then choose a request.')
            page = page_number(parts[1]) if len(parts) == 2 else 0
            from src.agentbus_enrollment_approval_service import MAX_REQUESTS
            rows = store.channels.pending_enrollments(channel=channel, limit=MAX_REQUESTS)
            return enrollment_reply(rows, settings.slack_command, channel=channel, page=page)
        if len(parts) == 1 or parts[1] == 'recent':
            if len(parts) == 2 or len(parts) > 3:
                raise ValueError(f'Use {settings.slack_command} results, then choose Results.')
            page = page_number(parts[2]) if len(parts) == 3 else 0
            return recent_polls_reply(store.controls.recent_polls(channel), channel=channel, page=page)
        from src.agentbus_slack_ui_service import UUID
        if len(parts) > 3 or not UUID.fullmatch(parts[1]):
            raise ValueError(f'Use {settings.slack_command} results, then choose a poll. No UUID typing required.')
        page = page_number(parts[2]) if len(parts) == 3 else 0
        rows = store.controls.status(parts[1], channel=channel)
        roster = store.participation.roster(include_stopped=True, channel=channel)
        names = {row['session_id']: row['route'] for row in roster}
        return results_reply(parts[1], rows, names, channel=channel, page=page)
    except ValueError as exc:
        return str(exc)


def issued_reply(kind: str, control_id: str, count: int, channel: str) -> OperatorReply:
    requirement = 'answer' if kind == 'poll' else 'read acknowledgement'
    text = f'{kind.title()} created: {count} session(s); explicit {requirement} required.'
    blocks = section_blocks(f'*{kind.title()} created*\n{text}')
    blocks.append({'type': 'actions', 'elements': [
        action_button('Results', OperatorAction('results_page', channel, control_id))]})
    legacy = f'{kind.title()} {control_id}: {count} session(s); explicit {requirement} required.'
    return OperatorReply(text, blocks, legacy_text=legacy)


def interaction_principal(payload: dict, settings) -> tuple[str, str] | None:
    """Only message buttons from an allowed channel and operator are eligible."""
    if settings is None or payload.get('type') != 'block_actions':
        return None
    user = payload.get('user')
    channel = payload.get('channel')
    container = payload.get('container')
    if not isinstance(user, dict) or not isinstance(channel, dict) or not isinstance(container, dict):
        return None
    uid, cid = user.get('id'), channel.get('id')
    if (container.get('type') != 'message' or container.get('channel_id') != cid
            or not isinstance(cid, str) or cid not in settings.channels
            or not isinstance(uid, str) or uid not in settings.slack_operator_user_ids):
        return None
    return uid, cid


def execute_interaction(payload: dict, settings, store, web_client) -> str:
    """A button is a locator; repeat current authorization before target lookup."""
    principal = interaction_principal(payload, settings)
    if principal is None:
        return 'AgentBus operator button access is not enabled in this channel.'
    uid, channel = principal
    actions = payload.get('actions')
    try:
        if not isinstance(actions, list) or len(actions) != 1:
            raise ValueError('Choose one AgentBus button.')
        action = parse_action(actions[0])
        if action.channel != channel:
            raise ValueError('This button belongs to another channel. Open the local command again.')
        if action.kind == 'approve':
            text = f'approve {action.target}'
        elif action.kind == 'enrollments_page':
            text = f'enrollments {action.page + 1}'
        elif action.kind == 'results_page':
            text = f'results {action.target} {action.page + 1}'
        else:
            text = f'results recent {action.page + 1}'
    except ValueError as exc:
        return str(exc)
    from src.agentbus_slack_command_service import execute_command
    return execute_command({'command': settings.slack_command, 'channel_id': channel,
                            'user_id': uid, 'text': text}, settings, store, web_client)
