"""Issue108 recovery observations preserve exact authority and custody."""
from __future__ import annotations

import json

import pytest

from src import agentbus_poll_client as poll


def test_cached_own_broadcast_uses_only_exact_recipient_projection(tmp_path):
    from tests.test_poll_client import profile_at

    path, profile = profile_at(tmp_path)
    profile['message_lifecycle_version'] = 1
    event = {'kind': 'MESSAGE', 'id': '7', 'message': {
        'cursor': 7, 'sender': profile['identity'], 'audience': 'broadcast',
        'lifecycle': {'state': 'unclaimed'}}}
    poll.change_spool(path, profile, lambda spool: poll._append(spool, event))
    recipient = {'scope': 'recipient', 'state': 'handled',
                 'chat_id': profile['chat_id'],
                 'session_id': profile['participation']['session_id']}
    response = {'cursor': 7, 'scope': 'sender', 'recipients': {'handled': 3},
                'recipient_outcome': recipient}
    values = {'AGENTBUS_URL': profile['service_url']}
    for field in ('chat_id', 'session_id'):
        bad = {**response, 'recipient_outcome': {**recipient, field: 'foreign'}}
        with pytest.raises(poll.ClientError):
            poll.reconcile_message_event(values, path, profile, event,
                                         api_call=lambda *_a, **_k: bad)
        assert poll.read_spool_events(path, profile=profile) == [event]
    assert poll.reconcile_message_event(values, path, profile, event,
                                       api_call=lambda *_a, **_k: response)
    assert poll.read_spool_events(path, profile=profile) == []
    assert json.loads(path.read_text())['ack_cursor'] == 0


def test_inbox_help_distinguishes_historical_inspection(capsys):
    from src.agentbus_parser_client import parse_args

    with pytest.raises(SystemExit) as exit_info:
        parse_args(['inbox', '--help'])
    assert exit_info.value.code == 0
    text = capsys.readouterr().out
    assert 'historical' in text.lower()
    assert 'presentation' in text.lower()


def test_attention_is_bounded_context_without_presentation_or_ack(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from src import agentbus_attention_client as attention
    from tests.test_poll_client import profile_at
    path, profile = profile_at(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(attention, 'checked_profile', lambda *_: (profile, {'high_water_cursor': 250}))
    monkeypatch.setattr(attention, 'resolve_identity', lambda identity: identity)
    calls = []

    def api_call(values, route, **kwargs):
        calls.append((route, kwargs))
        return {'messages': [{'cursor': 249, 'audience': 'unrouted', 'sender': 'human',
                              'sender_assurance': 'legacy', 'text': 'context'}]}

    assert attention.attention_command(SimpleNamespace(identity=profile['identity']),
        {'AGENTBUS_URL': profile['service_url']}, api_call=api_call) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['partial'] and result['messages'][0]['sender_assurance'] == 'legacy'
    assert result['presentation_advanced'] is False and result['semantic_ack_advanced'] is False
    assert calls == [('/v1/messages?after=50&limit=200',
        {'session_token': 'x' * 32, 'inbox_id': profile['inbox_id']})]
    assert path.read_bytes() == original


@pytest.mark.parametrize('rows', [[{'cursor': 0}], [{'cursor': 2}, {'cursor': 1}],
    [{'cursor': 1}, {'cursor': 1}], [{'cursor': True}], None])
def test_attention_rejects_invalid_pages(tmp_path, monkeypatch, rows):
    from types import SimpleNamespace

    from src import agentbus_attention_client as attention
    from tests.test_poll_client import profile_at
    path, profile = profile_at(tmp_path)
    original = path.read_bytes()
    monkeypatch.setattr(attention, 'checked_profile', lambda *_: (profile, {'high_water_cursor': 10}))
    monkeypatch.setattr(attention, 'resolve_identity', lambda value: value)
    with pytest.raises(attention.ClientError, match='invalid'):
        attention.attention_command(SimpleNamespace(identity=profile['identity']),
            {'AGENTBUS_URL': profile['service_url']}, api_call=lambda *_a, **_k: {'messages': rows})
    assert path.read_bytes() == original


def test_operator_capability_is_loaded_only_for_declared_operator_commands(tmp_path, monkeypatch):
    from src import agentbus_client as client
    from src import agentbus_operator_client as operator
    monkeypatch.delenv('AGENTBUS_OPERATOR_TOKEN', raising=False)
    values = {'AGENTBUS_API_TOKEN': 'api-secret', 'AGENTBUS_OPERATOR_TOKEN': 'separate-operator-secret'}
    assert operator._operator_token(values) == 'separate-operator-secret'
    with pytest.raises(operator.ClientError):
        operator._operator_token({'AGENTBUS_API_TOKEN': 'same', 'AGENTBUS_OPERATOR_TOKEN': 'same'})
    loaded = []
    monkeypatch.setattr(client, 'configuration', lambda **kwargs: (loaded.append(kwargs) or (tmp_path, {})))
    for command in ('read', 'attention', 'roster'):
        monkeypatch.setitem(client.CLI_OPERATIONS, command, lambda *_: 0)
        assert client.main([command]) == 0
    assert loaded == [{'include_operator': False}, {'include_operator': False}, {'include_operator': True}]
