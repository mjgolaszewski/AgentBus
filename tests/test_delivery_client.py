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


def test_lifecycle_send_stages_before_network_and_reuses_original_epoch(tmp_path):
    import json

    from src.agentbus_delivery_client import send_command
    values = {'_AGENTBUS_DELIVERY_VERSION':'2', 'AGENTBUS_STATE_DIR':str(tmp_path),
              'AGENTBUS_URL':'http://localhost:8766'}
    profile = {'repo':'agentbus', 'inbox_id':'inbox-one', 'channel':'C111', 'chat_id':'chat-one',
               'service_url':values['AGENTBUS_URL'],
               'participation':{'session_id':'session-one', 'session_secret':'private-session'}}
    info = {'features':['message-lifecycle-v1'], 'lifecycle':{'version':1, 'request_epoch':'a' * 64}}
    calls = []
    def post(_values, path, payload, **kwargs):
        files = list((tmp_path / 'send-requests').glob('*.json'))
        assert len(files) == 1
        staged = json.loads(files[0].read_text())
        assert staged['epoch'] == kwargs['delivery_epoch']
        assert staged['payload'] == payload
        assert 'private-session' not in files[0].read_text()
        calls.append(kwargs)
        raise ClientError('lost response')
    ns = parse_args(['send', '--identity','agentbus:flower','--to','agentbus:other','hello'])
    with pytest.raises(ClientError, match='--message-id'):
        send_command(ns, values, lambda *_:(profile,info), lambda value:value, post)
    key = calls[0]['delivery_key']
    info['lifecycle']['request_epoch'] = 'b' * 64
    ns = parse_args(['send','--identity','agentbus:flower','--to','agentbus:other','--message-id',key,'hello'])
    with pytest.raises(ClientError):
        send_command(ns, values, lambda *_:(profile,info), lambda value:value, post)
    assert calls[0] == calls[1] and calls[1]['delivery_epoch'] == 'a' * 64
    ns.text = 'changed'
    with pytest.raises(ClientError, match='evidence retained'):
        send_command(ns, values, lambda *_:(profile,info), lambda value:value, post)
    assert len(calls) == 2


def test_lifecycle_stage_missing_or_symlink_fails_before_submission(tmp_path):
    from src.agentbus_lifecycle_client import request_stage
    values = {'AGENTBUS_STATE_DIR':str(tmp_path),'AGENTBUS_URL':'http://localhost:8766'}
    profile = {'service_url':values['AGENTBUS_URL'],'inbox_id':'one','channel':'C111',
               'chat_id':'chat','participation':{'session_id':'session'}}
    with pytest.raises(ClientError, match='missing'):
        request_stage(values, profile, 'old-id', {}, 'a' * 64, existing=True)
    directory = tmp_path / 'send-requests'
    directory.rmdir()
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ClientError, match='symbolic links'):
        request_stage(values, profile, 'new-id', {}, 'a' * 64)


def test_work_receipt_is_stable_across_retry_and_bound_to_session():
    from copy import deepcopy

    from src.agentbus_lifecycle_client import message_command
    values = {'AGENTBUS_URL':'http://localhost:8766'}
    profile = {'service_url':values['AGENTBUS_URL'],'inbox_id':'one','channel':'C111',
               'chat_id':'chat','participation':{'session_id':'session','session_secret':'private'}}
    calls = []
    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return {'state':'picked_up'}
    ns = parse_args(['pickup','--identity','agentbus:flower','--cursor','7'])
    message_command(ns, values, profile, api_call=post)
    message_command(ns, values, profile, api_call=post)
    assert calls[0] == calls[1]
    token = calls[0][0][2]['receipt_id']
    ns.command = 'complete'
    message_command(ns, values, profile, api_call=post)
    assert calls[2][0][2]['receipt_id'] == token
    replacement = deepcopy(profile)
    replacement['participation']['session_id'] = 'replacement'
    message_command(ns, values, replacement, api_call=post)
    assert calls[3][0][2]['receipt_id'] != token


def test_cached_wake_refs_require_current_recipient_outcomes_and_do_not_ack():
    from src.agentbus_lifecycle_client import filter_wake_refs
    values = {'AGENTBUS_URL':'http://localhost:8766'}
    profile = {'service_url':values['AGENTBUS_URL'],'inbox_id':'one','channel':'C111',
               'chat_id':'chat','participation':{'session_id':'session','session_secret':'private'},
               'message_lifecycle_version':1, 'ack_cursor':0}
    requests = []
    states = {1:'expired_unclaimed', 2:'unclaimed', 3:'handled', 4:'recovery_required'}
    def read(_values, path, **kwargs):
        requests.append(path)
        cursor = int(path.split('/')[3])
        return {'cursor':cursor,'scope':'recipient','state':states[cursor],
                'deadline_elapsed':False,'owned_by_this_session':False}
    projection = {}
    refs = ['MESSAGE:1','MESSAGE:2','MESSAGE:3','MESSAGE:4']
    assert filter_wake_refs(values, profile, refs, projection, api_call=read) == ['MESSAGE:2']
    assert projection['lifecycle_terminal_cursors'] == [1,3]
    assert profile['ack_cursor'] == 0
    requests.clear()
    assert filter_wake_refs(values, profile, refs, projection, api_call=read) == ['MESSAGE:2']
    assert len(requests) == 2  # Definitive terminal projections do not repeatedly query or acknowledge.


def test_semantic_receipts_precede_local_ack_and_survive_lost_response():
    from src.agentbus_lifecycle_client import acknowledge_handled
    values = {'AGENTBUS_URL':'http://localhost:8766'}
    profile = {'service_url':values['AGENTBUS_URL'],'inbox_id':'one','channel':'C111', 'identity':'agentbus:flower',
               'chat_id':'chat','participation':{'session_id':'session','session_secret':'private'},
               'message_lifecycle_version':1, 'ack_cursor':0}
    requests = []
    failed = [True]
    def call(_values, path, payload=None, **kwargs):
        if payload is None:
            return {'messages':[{'cursor':1,'lifecycle':{'state':'unclaimed'}},
                                {'cursor':2,'lifecycle':{'state':'legacy_unknown'}}],
                    'next_cursor':2, 'has_more':False}
        requests.append((path, payload))
        if failed[0]:
            raise ClientError('response lost')
        return {'session_id':'session','handled':0,'already_handled':1,'expired_unclaimed':0}
    with pytest.raises(ClientError):
        acknowledge_handled(values, profile, 2, api_call=call)
    assert profile['ack_cursor'] == 0
    failed[0] = False
    result = acknowledge_handled(values, profile, 2, api_call=call)
    assert result['through'] == 2 and result['outcomes']['legacy_unknown'] == 1
    assert requests[0] == requests[1] and profile['ack_cursor'] == 0


def test_claimed_inactive_projection_cannot_be_wake_candidate():
    from src.agentbus_codex_wake_events_client import _candidate
    event = {'kind':'MESSAGE', 'id':'1', 'message':{'sender_assurance':'session', 'audience':'unrouted',
             'action_reason':'claimed by this identity', 'actionable':False}}
    assert _candidate(event, 'agentbus:flower') is None
    event['message']['actionable'] = True
    assert _candidate(event, 'agentbus:flower') == (4, 'MESSAGE:1')
    event['message']['lifecycle'] = {'state':'handled'}
    assert _candidate(event, 'agentbus:flower') is None


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


def test_loopback_transport_does_not_initialize_tls_or_follow_redirects(monkeypatch):
    import ssl
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from src import agentbus_transport_client as transport

    paths = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            paths.append(self.path)
            if self.path == '/redirect':
                self.send_response(302)
                self.send_header('Location', '/unexpected')
                self.end_headers()
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"status":"ok"}')
        def log_message(self, *_args):
            pass
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Plain loopback HTTP must not load TLS trust')
    monkeypatch.setattr(ssl, 'create_default_context', forbidden)
    monkeypatch.setattr(ssl, '_create_default_https_context', forbidden)
    server = HTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever)
    worker.start()
    values = {'AGENTBUS_URL': f'http://127.0.0.1:{server.server_port}', 'AGENTBUS_API_TOKEN': 'test'}
    try:
        for _ in range(3):
            assert transport.api(values, '/healthz') == {'status':'ok'}
        with pytest.raises(ClientError, match='HTTP 302'):
            transport.api(values, '/redirect')
        with pytest.raises(ClientError, match='Use HTTPS'):
            transport.api({**values, 'AGENTBUS_URL':'http://remote.invalid'}, '/healthz')
        assert paths == ['/healthz'] * 3 + ['/redirect']
    finally:
        server.shutdown()
        worker.join(2)
        server.server_close()


def test_https_transport_defers_but_preserves_fresh_standard_verified_tls(monkeypatch):
    import ssl
    from urllib.request import Request

    from src import agentbus_transport_client as transport

    original = transport.HTTPSHandler
    handlers = []
    def standard(**kwargs):
        handler = original(**kwargs)
        assert handler._context.verify_mode == ssl.CERT_REQUIRED
        assert handler._context.check_hostname is True
        handlers.append(handler)
        handler.https_open = lambda request: request.full_url
        return handler
    monkeypatch.setattr(transport, 'HTTPSHandler', standard)
    deferred = transport.DeferredHTTPSHandler()
    assert handlers == []
    for _ in range(2):
        assert deferred.https_open(Request('https://service.invalid/healthz')) == 'https://service.invalid/healthz'
    assert len(handlers) == 2 and handlers[0]._context is not handlers[1]._context


# Follow-up regression controls use no Slack or live service.
def _text_bound_context(tmp_path):
    values = {'_AGENTBUS_DELIVERY_VERSION': '2', 'AGENTBUS_STATE_DIR': str(tmp_path),
              'AGENTBUS_URL': 'http://localhost:8766'}
    profile = {'repo': 'agentbus', 'inbox_id': 'inbox-one', 'channel': 'C111', 'chat_id': 'chat-one',
               'service_url': values['AGENTBUS_URL'],
               'participation': {'session_id': 'session-one', 'session_secret': 'private-session'}}
    info = {'features': ['message-lifecycle-v1'],
            'lifecycle': {'version': 1, 'request_epoch': 'a' * 64}}
    return values, profile, info


def _text_bound_command(operation, text):
    if operation == 'reply':
        return parse_args(['reply', '--identity', 'agentbus:flower', '--to-cursor', '1', text])
    return parse_args(['send', '--identity', 'agentbus:flower', '--to', 'agentbus:other',
                       '-' if operation == 'stdin' else text])


@pytest.mark.parametrize('operation', ['send', 'reply', 'stdin'])
@pytest.mark.parametrize('text', ['', 'x' * 6001, '\u00e9' * 6001])
def test_local_text_rejection_never_stages_or_submits(tmp_path, monkeypatch, operation, text):
    import io

    from src.agentbus_delivery_client import send_command

    values, profile, info = _text_bound_context(tmp_path)
    calls = []
    monkeypatch.setattr('sys.stdin', io.StringIO(text))
    ns = _text_bound_command(operation, text)
    with pytest.raises(ClientError, match='1..6000 characters; no message submitted'):
        send_command(ns, values, lambda *_: (profile, info), lambda value: value,
                     lambda *args, **kwargs: calls.append((args, kwargs)))
    assert calls == []
    assert not (tmp_path / 'send-requests').exists()


@pytest.mark.parametrize('operation', ['send', 'reply', 'stdin'])
@pytest.mark.parametrize('character', ['x', '\u00e9'])
def test_text_boundary_counts_characters_and_preserves_custody(tmp_path, monkeypatch, operation, character):
    import io
    import json

    from src.agentbus_delivery_client import send_command
    from src.agentbus_service import MAX_TEXT

    values, profile, info = _text_bound_context(tmp_path)
    text = character * MAX_TEXT
    monkeypatch.setattr('sys.stdin', io.StringIO(text))
    calls = []
    def post(_values, path, payload, **kwargs):
        calls.append((path, payload, kwargs))
        return {'cursor': 1}
    ns = _text_bound_command(operation, text)
    assert send_command(ns, values, lambda *_: (profile, info), lambda value: value, post) == {'cursor': 1}
    assert len(calls) == 1 and calls[0][1]['text'] == text
    stages = list((tmp_path / 'send-requests').glob('*.json'))
    assert len(stages) == 1
    assert json.loads(stages[0].read_text())['payload']['text'] == text


@pytest.mark.parametrize('status', [400, 401, 403, 404, 413, 422])
def test_known_http_rejection_preserves_status_and_stage_without_blind_retry(tmp_path, status):
    import json

    from src.agentbus_delivery_client import send_command

    values, profile, info = _text_bound_context(tmp_path)
    calls = []
    def post(*args, **kwargs):
        calls.append(kwargs)
        raise ClientError(f'AgentBus returned HTTP {status}.', status=status)
    ns = _text_bound_command('send', 'hello')
    with pytest.raises(ClientError, match='Inspect validation, authorization and destination') as failed:
        send_command(ns, values, lambda *_: (profile, info), lambda value: value, post)
    assert failed.value.status == status
    assert 'acceptance may be uncertain' not in str(failed.value)
    assert 'does not establish custody' in str(failed.value)
    assert 'private-session' not in str(failed.value)
    stages = list((tmp_path / 'send-requests').glob('*.json'))
    assert len(stages) == 1
    retained = json.loads(stages[0].read_text())
    assert retained['request_id'] == calls[0]['delivery_key']
    assert retained['epoch'] == 'a' * 64
    assert retained['request_id'] in str(failed.value)


def test_missing_explicit_stage_explains_first_send_without_submission(tmp_path):
    from src.agentbus_delivery_client import send_command

    values, profile, info = _text_bound_context(tmp_path)
    ns = _text_bound_command('send', 'hello')
    ns.message_id = '7d76697d-ed3f-4abf-afc9-45e20312f42a'
    calls = []
    with pytest.raises(ClientError, match='omit --message-id for a new first send'):
        send_command(ns, values, lambda *_: (profile, info), lambda value: value,
                     lambda *args, **kwargs: calls.append((args, kwargs)))
    assert calls == []
    assert list((tmp_path / 'send-requests').glob('*.json')) == []


@pytest.mark.parametrize('path,code,expected', [
    ('/v1/sessions', 'enrollment_request_expired', 'enrollment_request_expired'),
    ('/v1/sessions', 'private-untrusted-value', None),
    ('/v1/messages', 'enrollment_request_expired', None),
])
def test_transport_enrollment_recovery_header_is_allowlisted(monkeypatch, path, code, expected):
    import io
    from urllib.error import HTTPError

    from src import agentbus_transport_client as transport
    class Opener:
        def open(self, *_args, **_kwargs):
            raise HTTPError('http://localhost', 403, 'denied',
                            {'X-AgentBus-Recovery-Code': code}, io.BytesIO(b'private-response-body'))
    monkeypatch.setattr(transport, 'build_opener', lambda *_: Opener())
    with pytest.raises(ClientError) as failed:
        transport.api({'AGENTBUS_URL': 'http://localhost', 'AGENTBUS_API_TOKEN': 'private-bearer'},
                      path, {'approval_request_id': 'request-one'})
    assert failed.value.recovery_code == expected
    assert 'private-' not in str(failed.value)
