"""Delivery negotiation preserves old servers and provides a retry identity on uncertainty."""
from uuid import UUID

import pytest

from src.agentbus_parser_client import parse_args
from src.agentbus_transport_client import ClientError


def test_joined_send_negotiates_durable_custody_and_reports_retry_identity(monkeypatch):
    from src.agentbus_delivery_client import send_command

    values = {'_AGENTBUS_DELIVERY_VERSION': '1'}
    profile = {'inbox_id': 'inbox-one', 'repo': 'agentbus', 'participation': {'session_secret': 'private-session'}}
    calls = []
    def post(_values, path, payload, **kwargs):
        calls.append((path, payload, kwargs))
        raise ClientError('response lost')
    ns = parse_args(['send', '--identity', 'agentbus:flower', '--to', 'agentbus:other', 'hello'])
    with pytest.raises(ClientError, match='--message-id') as failed:
        send_command(ns, values, lambda *_: (profile, {}), lambda value: value, post)
    key = calls[0][2]['delivery_key']
    assert str(UUID(key)) == key and key in str(failed.value)
    assert 'private-session' not in str(failed.value)
    ns = parse_args(['send', '--identity', 'agentbus:flower', '--to', 'agentbus:other',
                     '--message-id', key, 'hello'])
    with pytest.raises(ClientError):
        send_command(ns, values, lambda *_: (profile, {}), lambda value: value, post)
    assert calls[1] == calls[0]


def test_immediate_transport_retains_session_scope_and_rejects_unnegotiated_durable_identity():
    from src.agentbus_delivery_client import send_command

    calls = []
    profile = {'inbox_id': 'inbox-one', 'repo': 'agentbus', 'participation': {'session_secret': 'session'}}
    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return {'cursor': 1}
    ns = parse_args(['reply', '--identity', 'agentbus:flower', '--to-cursor', '1', 'hello'])
    assert send_command(ns, {}, lambda *_: (profile, {}), lambda value: value, post) == {'cursor': 1}
    assert calls[0][1] == {'session_token': 'session', 'inbox_id': 'inbox-one'}
    ns = parse_args(['reply', '--identity', 'agentbus:flower', '--to-cursor', '1',
                     '--message-id', '7d76697d-ed3f-4abf-afc9-45e20312f42a', 'hello'])
    with pytest.raises(ClientError, match='advertise'):
        send_command(ns, {}, lambda *_: (profile, {}), lambda value: value, post)
    assert len(calls) == 1


@pytest.mark.parametrize('confirmed', [True, False])
def test_transport_negotiation_checks_exact_acceptance_headers(monkeypatch, confirmed):
    import io
    import json

    from src import agentbus_transport_client as transport

    key = '7d76697d-ed3f-4abf-afc9-45e20312f42a'
    requests = []
    class Response(io.BytesIO):
        status = 202
        headers = {'X-AgentBus-Delivery-Version': '1',
                   'Idempotency-Key': key if confirmed else 'changed'}
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            return Response(json.dumps({'cursor': 1}).encode())
    monkeypatch.setattr(transport, 'build_opener', lambda *_: Opener())
    values = {'AGENTBUS_API_TOKEN': 'ordinary', 'AGENTBUS_URL': 'http://localhost:8766'}
    transport.api(values, '/v1/info')
    assert values['_AGENTBUS_DELIVERY_VERSION'] == '1'
    if confirmed:
        assert transport.api(values, '/v1/messages', {}, session_token='session',
                             delivery_key=key) == {'cursor': 1}
    else:
        with pytest.raises(ClientError, match='did not confirm'):
            transport.api(values, '/v1/messages', {}, session_token='session', delivery_key=key)
    assert requests[-1].get_header('Idempotency-key') == key
    assert requests[-1].get_header('X-agentbus-delivery-version') == '1'
    assert requests[-1].get_header('X-agentbus-session-token') == 'session'
