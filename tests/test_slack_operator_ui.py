"""Private operator controls preserve complete results and scoped authority."""
import json

import pytest

from src.agentbus_slack_ui_service import (
    OperatorAction,
    action_button,
    approval_command,
    enrollment_reply,
    parse_action,
    recent_polls_reply,
    results_reply,
)

CID = '00000000-0000-4000-8000-000000000099'
CHANNEL = 'C123456'


def session_id(number):
    return f'00000000-0000-4000-8000-{number:012d}'


def result_rows():
    return [{'control_id': CID, 'session_id': session_id(i), 'kind': 'poll',
             'reason': 'Ready?', 'state': 'effective' if i in {8, 9, 10, 12, 15, 19} else 'pending',
             'answer': f'ANSWER-{i:02}' if i in {8, 9, 10, 12, 15, 19} else None}
            for i in range(1, 20)]


def test_all_nineteen_targets_and_six_answers_are_reachable_once():
    rows = result_rows()
    names = {session_id(i): f'worker-{i:02}' for i in range(1, 20)}
    pages = [results_reply(CID, rows, names, channel=CHANNEL, page=i) for i in range(5)]
    rendered = '\n'.join(page.text for page in pages)
    for i in range(1, 20):
        assert rendered.count(f'*worker-{i:02}*') == 1
    for i in (8, 9, 10, 12, 15, 19):
        assert rendered.count(f'ANSWER-{i:02}') == 1
    assert all('6/19 answered; 13 pending.' in page.text for page in pages)
    assert all(CID not in page.text for page in pages)


def test_long_escaped_unicode_answers_are_complete_in_bounded_blocks():
    rows = result_rows()[:4]
    answer = '<&>🌱' * 500
    for row in rows:
        row['answer'] = answer
        row['state'] = 'effective'
    reply = results_reply(CID, rows, {}, channel=CHANNEL)
    encoded = ''.join(block['text']['text'] for block in reply.blocks if block['type'] == 'section')
    assert encoded.count('&lt;&amp;&gt;🌱' * 500) == 4
    assert len(reply.blocks) <= 49
    assert all(len(block['text']['text']) <= 3000 for block in reply.blocks if block['type'] == 'section')


def test_navigation_preserves_exact_control_and_rendered_channel():
    reply = results_reply(CID, result_rows(), {}, channel=CHANNEL, page=2)
    buttons = [button for block in reply.blocks if block['type'] == 'actions'
               for button in block['elements']]
    actions = [parse_action(button) for button in buttons]
    assert OperatorAction('results_page', CHANNEL, CID, 1) in actions
    assert OperatorAction('results_page', CHANNEL, CID, 3) in actions
    assert all(action.channel == CHANNEL for action in actions)


def test_pending_is_not_presented_as_an_empty_answer():
    reply = results_reply(CID, result_rows()[:2], {}, channel=CHANNEL)
    assert reply.text.count('Awaiting answer') == 2
    assert '0/2 answered; 2 pending.' in reply.text


def test_enrollment_command_is_a_separate_exact_plain_message():
    rows = [{'request_id': CID, 'display_name': 'Flower', 'route': 'agentbus:flower', 'repo': 'agentbus'}]
    reply = enrollment_reply(rows, '/workspace-bus', channel=CHANNEL)
    assert reply.command_messages == (f'/workspace-bus approve {CID}',)
    assert '/workspace-bus approve' not in reply.text
    assert '/workspace-bus approve' not in json.dumps(reply.blocks)
    assert '\n' not in reply.command_messages[0] and '`' not in reply.command_messages[0]


def test_legacy_receipt_keeps_profile_binding_separate_from_human_display():
    chat_id = session_id(42)
    rows = [{'request_id': CID, 'chat_id': chat_id, 'display_name': 'Flower',
             'route': 'agentbus:flower', 'repo': 'agentbus'}]
    reply = enrollment_reply(rows, '/agentbus', channel=CHANNEL)
    assert isinstance(reply, str) and chat_id in reply
    assert reply.splitlines()[-1] == f'`/agentbus approve {CID}`'
    assert '/agentbus approve' not in reply.text
    assert reply.command_messages == (f'/agentbus approve {CID}',)


def test_each_enrollment_page_pairs_only_its_own_command():
    rows = [{'request_id': session_id(i), 'display_name': f'Flower {i}',
             'route': f'agentbus:flower-{i}', 'repo': 'agentbus'} for i in (1, 2)]
    for page in (0, 1):
        reply = enrollment_reply(rows, '/agentbus', channel=CHANNEL, page=page)
        assert reply.command_messages == (f'/agentbus approve {session_id(page + 1)}',)


def test_recent_polls_are_discoverable_without_typing_an_identifier():
    reply = recent_polls_reply([{'control_id': CID, 'reason': 'Ready?'}], channel=CHANNEL)
    assert CID not in reply.text
    buttons = [button for block in reply.blocks if block['type'] == 'actions'
               for button in block['elements']]
    assert parse_action(buttons[0]) == OperatorAction('results_page', CHANNEL, CID)


@pytest.mark.parametrize('page', [-1, 5, True, '1'])
def test_invalid_result_pages_fail_closed(page):
    with pytest.raises(ValueError):
        results_reply(CID, result_rows(), {}, channel=CHANNEL, page=page)


@pytest.mark.parametrize('payload', [
    {'type': 'button', 'action_id': 'foreign.results', 'value': '{}'},
    {'type': 'button', 'action_id': 'agentbus.operator.results', 'value': 'x' * 257},
    {'type': 'button', 'action_id': 'agentbus.operator.results', 'value': '{'},
    {'type': 'button', 'action_id': 'agentbus.operator.results', 'value': '{}'},
    {'type': 'button', 'action_id': 'agentbus.operator.results',
     'value': json.dumps({'channel': CHANNEL, 'target': '', 'page': True})},
    {'type': 'button', 'action_id': 'agentbus.operator.approve',
     'value': json.dumps({'channel': CHANNEL, 'target': 'partial', 'page': 0})},
])
def test_malformed_button_locators_are_rejected(payload):
    with pytest.raises(ValueError):
        parse_action(payload)


def test_button_locator_contains_no_authority_or_credentials():
    button = action_button('Approve enrollment', OperatorAction('approve', CHANNEL, CID))
    value = json.loads(button['value'])
    assert set(value) == {'channel', 'target', 'page'}
    assert parse_action(button) == OperatorAction('approve', CHANNEL, CID)


@pytest.mark.parametrize('command', ['agentbus', '/bad command', '/agentbus\napprove'])
def test_approval_command_cannot_inject_another_line(command):
    with pytest.raises(ValueError):
        approval_command(command, CID)
