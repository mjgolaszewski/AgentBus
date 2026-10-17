"""Cross-channel negative controls exercise real service/store boundaries."""
import pytest

from src.agentbus_settings_service import Settings


def test_channel_configuration_rejects_ambiguous_and_unbounded_inputs(monkeypatch, tmp_path):
    for name, value in {'API_TOKEN': 'x' * 32, 'SLACK_BOT_TOKEN': 'xoxb-test',
                        'SLACK_APP_TOKEN': 'xapp-test', 'SLACK_CHANNELS': 'C111,C222'}.items():
        monkeypatch.setenv('AGENTBUS_' + name, value)
    monkeypatch.delenv('AGENTBUS_SLACK_CHANNEL', raising=False)
    settings = Settings.from_env()
    assert settings.channels == ('C111', 'C222')
    monkeypatch.setenv('AGENTBUS_SLACK_CHANNEL', 'C111')
    with pytest.raises(ValueError, match='simultaneous|together|both'):
        Settings.from_env()
    monkeypatch.delenv('AGENTBUS_SLACK_CHANNEL')
    for invalid in ('C111,C111', '', ',C111', 'general', ','.join(f'C{i}' for i in range(17))):
        monkeypatch.setenv('AGENTBUS_SLACK_CHANNELS', invalid)
        with pytest.raises(ValueError):
            Settings.from_env()


def test_channel_public_positions_do_not_reveal_other_channel_activity(tmp_path):
    from src.agentbus_service import MessageStore, SendMessage
    store = MessageStore(tmp_path / 'bus.sqlite3')
    store.channels.configure(('C111', 'C222'))
    first = store.append('C111', '100.1', SendMessage(sender='human', text='one'))
    other = store.append('C222', '100.1', SendMessage(sender='human', text='other'))
    second = store.append('C111', '100.2', SendMessage(sender='human', text='two'))
    assert [store.channels.public_cursor('C111', x.cursor) for x in (first, second)] == [1, 2]
    assert store.channels.public_cursor('C222', other.cursor) == 1
    assert store.channels.internal_cursor('C222', 2) is None
    with pytest.raises(PermissionError):
        store.channels.public_cursor('C222', first.cursor)
    store.close()


def test_enrollment_grant_binds_channel_and_lost_response_to_original_secret(tmp_path):
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path / 'bus.sqlite3')
    store.channels.configure(('C111', 'C222'))
    p = store.participation
    grant = store.channels.issue_grant(channel='C111', repo='agentbus', route='home:agentbus:a', actor='operator')
    args = dict(repo='agentbus', route='home:agentbus:a', display_name='A', session_secret='a' * 32)
    result = store.channels.enroll(grant, **args)
    assert store.channels.enroll(grant, **args) == result
    assert store.channels.channel_for_chat(result[0]) == 'C111'
    with pytest.raises(PermissionError):
        store.channels.enroll(grant, **dict(args, session_secret='b' * 32))
    second = store.channels.issue_grant(channel='C222', repo='agentbus', route='home:agentbus:b', actor='operator')
    with pytest.raises((PermissionError, ValueError)):
        store.channels.enroll(second, **dict(args, route='home:agentbus:b'))
    assert p.session_state(result[1])['state'] == 'joining'
    store.close()


@pytest.fixture
def channel_service(tmp_path):
    """Real API/store composition; Slack is the only replaced boundary."""
    import asyncio
    import json

    import httpx
    from fastapi.testclient import TestClient

    from src.agentbus_service import create_app

    class Socket:
        def __init__(self, settings, receiver):
            self.receiver = receiver
        def connect(self):
            pass
        def close(self):
            pass
        def is_connected(self):
            return True

    posted = []
    def respond(request):
        if request.url.path == '/api/auth.test':
            return httpx.Response(200, json={'ok': True, 'bot_id': 'B123'})
        if request.url.path == '/api/chat.postMessage':
            body = json.loads(request.content)
            posted.append(body)
            return httpx.Response(200, json={'ok': True, 'channel': body['channel'],
                'ts': f'1700000000.{len(posted):06}', 'message': {'text': body['text'], 'blocks': body['blocks']}})
        return httpx.Response(200, json={'ok': True, 'messages': [], 'has_more': False})

    settings = Settings(api_token='service-test', slack_bot_token='xoxb-test', slack_app_token='xapp-test',
                        slack_channel='', slack_channels=('C111', 'C222'), db_path=tmp_path/'service.sqlite3',
                        operator_token='operator-test', local_delivery_version=1)
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings, http_client=http, socket_factory=Socket)
    bearer = {'Authorization': 'Bearer service-test'}
    operator = {'Authorization': 'Bearer operator-test'}
    with TestClient(app) as client:
        def join(channel, name):
            route = 'home:agentbus:' + name
            grant = client.post('/v1/sessions/enrollment-grants', headers=operator,
                json={'channel': channel, 'repo': 'agentbus', 'route': route})
            assert grant.status_code == 201, grant.text
            secret = (name + '-test-secret-').ljust(40, 'x')
            enrolled = client.post('/v1/sessions', headers=bearer, json={'repo': 'agentbus',
                'route': route, 'display_name': name, 'session_secret': secret,
                'enrollment_token': grant.json()['enrollment_token']})
            assert enrolled.status_code == 201, enrolled.text
            receipt = enrolled.json()
            headers = {**bearer, 'X-AgentBus-Session-Token': secret,
                       'X-AgentBus-Inbox-ID': receipt['inbox_id']}
            ack = client.post(f"/v1/sessions/{receipt['session_id']}/policy-ack", headers=headers,
                              json={'revision': receipt['revision']})
            assert ack.status_code == 200, ack.text
            return route, headers, receipt
        a = join('C111', 'a')
        a2 = join('C111', 'a2')
        b = join('C222', 'b')
        yield client, app, posted, a, a2, b, bearer, operator
    asyncio.run(http.aclose())


def test_api_grants_and_all_read_paths_require_session(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    for path in ('/v1/info', '/v1/messages', '/v1/inbox?identity='+a[0]):
        assert client.get(path, headers=bearer).status_code == 403
    assert client.post('/v1/sessions/enrollment-grants', headers=bearer,
        json={'channel':'C222', 'repo':'agentbus', 'route':'home:agentbus:rogue'}).status_code == 401
    assert client.post('/v1/sessions', headers=bearer, json={'repo':'agentbus',
        'route':'home:agentbus:rogue', 'display_name':'rogue', 'session_secret':'z'*40}).status_code in (403, 422)
    assert client.get('/v1/info', headers=a[1]).json()['channel'] == 'C111'
    assert client.get('/v1/info', headers=b[1]).json()['channel'] == 'C222'


@pytest.mark.parametrize('audience', ['direct', 'informational'])
def test_cross_channel_send_and_unknown_route_are_indistinguishable(channel_service, audience):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    before = app.state.store._db.execute('SELECT count(*) FROM messages').fetchone()[0]
    errors = []
    for target in (b[0], 'home:agentbus:unknown'):
        result = client.post('/v1/messages', headers=a[1], json={'sender':a[0], 'recipient':target,
            'audience':audience, 'text':'must not leave scope'})
        errors.append((result.status_code, result.json()))
    assert errors[0] == errors[1]
    assert errors[0][0] == 403
    assert app.state.store._db.execute('SELECT count(*) FROM messages').fetchone()[0] == before


def test_read_reply_claim_and_scope_assertions_cannot_cross_channels(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    from src.agentbus_service import SendMessage
    foreign = app.state.store.append('C222', '100.1', SendMessage(sender='slack:U123', text='foreign', audience='unrouted'))
    own = app.state.store.append('C111', '100.1', SendMessage(sender='slack:U123', text='own', audience='unrouted'))
    for path in ('/v1/messages', '/v1/inbox?identity='+a[0]+'&context=true',
                 '/v1/inbox?identity='+a[0]+'&context=true&thread_ts=100.1'):
        response = client.get(path, headers=a[1])
        assert response.status_code == 200, response.text
        assert 'foreign' not in response.text
    mismatched = {**a[1], 'X-AgentBus-Inbox-ID':b[2]['inbox_id']}
    for method, path, body in [('GET','/v1/info',None),('GET','/v1/messages',None),
        ('POST','/v1/messages/1/claim',{'identity':a[0]}),
        ('POST','/v1/messages',{'sender':a[0], 'recipient':a2[0], 'text':'no'})]:
        result = client.request(method,path,headers=mismatched,json=body)
        assert result.status_code == 403
    result = client.get('/v1/inbox?identity='+b[0], headers=a[1])
    assert result.status_code == 403
    assert app.state.store.channels.public_cursor('C111', own.cursor) == 1
    assert app.state.store.channels.public_cursor('C222', foreign.cursor) == 1


def test_disabled_channel_and_broadcast_are_scoped(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    response = client.post('/v1/messages',headers=a[1],json={'sender':a[0], 'recipient':'all',
        'audience':'broadcast','text':'only-a'})
    assert response.status_code == 200, response.text
    assert posted[-1]['channel'] == 'C111'
    assert 'only-a' not in client.get('/v1/messages',headers=b[1]).text
    app.state.store.channels.configure(('C111',))
    assert client.get('/v1/messages', headers=b[1]).status_code == 403
    assert client.get('/v1/messages', headers=a[1]).status_code == 200


def test_operator_metrics_and_policy_cannot_be_used_as_agent_discovery(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    foreign_chat=b[2]['chat_id']
    assert client.get('/v1/policy/effective?repo=agentbus&chat_id='+foreign_chat,headers=a[1]).status_code == 403
    assert client.get('/v1/sessions/roster',headers=a[1]).status_code == 401
    status=client.get('/v1/status',headers=a[1])
    assert status.status_code == 200
    assert 'database_bytes' not in status.json()
    assert b[0] not in status.text


def test_aliases_and_stopped_sessions_never_change_channel_authority(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    renamed=client.post('/v1/sessions/'+b[2]['session_id']+'/rename',headers=b[1],
        json={'route':'home:agentbus:renamed'})
    assert renamed.status_code == 200,renamed.text
    for route in (b[0],'home:agentbus:renamed'):
        result=client.post('/v1/messages',headers=a[1],json={'sender':a[0],'recipient':route,'text':'blocked'})
        assert result.status_code == 403
    with app.state.store._lock, app.state.store._db:
        app.state.store._db.execute("UPDATE participation_sessions SET state='stopped' WHERE session_id=?",(a[2]['session_id'],))
    for method,path,body in [('GET','/v1/messages',None),('GET','/v1/inbox?identity='+a[0],None),
        ('POST','/v1/messages',{'sender':a[0],'recipient':a2[0],'text':'no'})]:
        assert client.request(method,path,headers=a[1],json=body).status_code == 423
