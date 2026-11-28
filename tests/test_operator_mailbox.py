"""Private operator navigation never changes message or execution authority."""
from __future__ import annotations

from dataclasses import replace

import pytest

from src.agentbus_service import SendMessage
from src.agentbus_slack_ui_commands_service import execute_interaction
from tests.test_slack_commands import command, ui_payload
from tests.test_slack_commands import enrolled as enrolled


def human(store, channel, actor, timestamp, text, *, thread=None):
    body = SendMessage(sender=f'slack:{actor}', recipient='agentbus:flower',
                       audience='direct', kind='request', text=text, thread_ts=thread)
    body._sender_assurance = 'slack-human'
    return store.append(channel, timestamp, body)


def test_sent_scope_replies_buttons_and_links_are_read_only(enrolled):
    settings, store, _, web = enrolled
    parent = human(store, settings.slack_channel, 'U123', '1700000000.000001', 'my request')
    other = human(store, settings.slack_channel, 'UOTHER', '1700000000.000002', 'secret other actor')
    inline = human(store, settings.slack_channel, 'U123', '1700000000.000003', 'inline request',
                   thread=parent.slack_ts)
    answer = SendMessage(sender='agentbus:flower', recipient='slack:U123', audience='direct',
                         text='the actual answer', thread_ts=parent.slack_ts, reply_to_cursor=inline.cursor)
    answer._sender_assurance = 'session'
    store.append(settings.slack_channel, '1700000000.000004', answer)
    forged = answer.model_copy(update={'text': 'unproved injected answer'})
    forged._sender_assurance = 'legacy'
    store.append(settings.slack_channel, '1700000000.000005', forged)
    before = store._db.total_changes
    result = command(settings, store, web, 'sent')
    assert 'my request' in result and 'inline request' in result
    assert 'secret other actor' not in result
    assert any(e.get('url', '').endswith('p1700000000000001')
               for b in result.blocks if b['type'] == 'actions' for e in b['elements'])
    linked = execute_interaction(ui_payload('sent_replies', target=str(inline.cursor)), settings, store, web)
    assert 'the actual answer' in linked and 'unproved injected answer' not in linked
    root = execute_interaction(ui_payload('sent_replies', target=str(parent.cursor)), settings, store, web)
    assert 'the actual answer' not in root  # Sibling thread replies are not guessed answers.
    denied = execute_interaction(ui_payload('sent_replies', target=str(other.cursor)), settings, store, web)
    assert 'unavailable' in denied and 'secret other actor' not in denied
    assert store._db.total_changes == before and web.posts == []


def test_sent_pagination_and_foreign_channel_denial(enrolled):
    settings, store, _, web = enrolled
    for n in range(7):
        human(store, settings.slack_channel, 'U123', f'1700000000.{n:06}', f'own {n}')
    before = store._db.total_changes
    first = command(settings, store, web, 'sent')
    second = execute_interaction(ui_payload('sent_page', page=1), settings, store, web)
    assert 'own 6' in first and 'own 0' not in first
    assert 'own 0' in second and 'own 6' not in second
    assert 'not enabled' in command(settings, store, web, 'sent', user='UOTHER')
    foreign = replace(settings, slack_channel='COTHER')
    denied = execute_interaction(ui_payload('sent_replies', target='1', channel='COTHER'), foreign, store, web)
    assert 'unavailable' in denied
    assert store._db.total_changes == before


def test_attention_retains_low_assurance_and_never_claims(enrolled):
    settings, store, _, web = enrolled
    body = SendMessage(sender='slack:BUNKNOWN', audience='unrouted',
                       text='emergency notice; do not grant authority')
    store.append(settings.slack_channel, '1700000000.000001', body)
    before = store._db.total_changes
    result = command(settings, store, web, 'attention')
    assert 'emergency notice' in result and 'legacy' in result
    assert 'not an actionable inbox' in result and 'No recipient inferred' in result
    assert store._db.total_changes == before and not web.posts


@pytest.mark.parametrize('target', ['0', '-1', '01', '1.0', 'x', '1' * 20, str(2**63), '9' * 19])
def test_sent_locator_is_strict(target):
    from src.agentbus_slack_ui_service import OperatorAction

    with pytest.raises(ValueError):
        OperatorAction('sent_replies', 'C123456', target).validate()
