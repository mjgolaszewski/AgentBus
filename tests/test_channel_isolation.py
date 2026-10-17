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
    p.set_policy(scope='global',scope_key='*',values={'initial_interval_seconds':60,'backoff_factor':2,'max_interval_seconds':1920,'control_check_max_seconds':60,'jitter_fraction':0,'overdue_grace_seconds':120,'presentation_budget_bytes':None},actor='operator')
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
        def send_socket_mode_response(self, response):
            pass

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
        app.state.store.participation.set_policy(scope='global',scope_key='*',values={'initial_interval_seconds':60,'backoff_factor':2,'max_interval_seconds':1920,'control_check_max_seconds':60,'jitter_fraction':0,'overdue_grace_seconds':120,'presentation_budget_bytes':None},actor='operator')
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
        assert response.json()['messages'][0]['cursor'] == 1
        assert response.json()['next_cursor'] == 1
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
    assert response.status_code == 201, response.text
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


def test_slack_commands_resolve_only_invoking_channel_members(channel_service):
    from dataclasses import replace

    from src.agentbus_slack_command_service import execute_command
    client,app,posted,a,a2,b,bearer,operator=channel_service
    settings=replace(app.state.settings,slack_operator_user_ids=frozenset({'U123'}))
    base={'command':'/agentbus','channel_id':'C111','user_id':'U123'}
    roster=execute_command({**base,'text':'roster'},settings,app.state.store,None)
    assert a[0] in roster and a2[0] in roster and b[0] not in roster
    for action in ('status','metrics','pause','resume','checkpoint','send','wake'):
        text=action+' '+b[2]['session_id']+(' no' if action in ('send','wake') else '')
        result=execute_command({**base,'text':text},settings,app.state.store,None)
        assert result=='Unknown session ID. Use /agentbus roster.'
    assert b[2]['session_id'] not in execute_command({**base,'text':'status '+b[2]['session_id'][-6:]},settings,app.state.store,None)


def test_trusted_slack_frames_cannot_forge_local_channel_membership(channel_service):
    from slack_sdk.socket_mode.request import SocketModeRequest

    from src.agentbus_service import SendMessage, encode_envelope
    client,app,posted,a,a2,b,bearer,operator=channel_service
    socket=app.state.socket
    for channel,sender,recipient in [('C111',b[0],a[0]),('C111',a[0],b[0]),('C999',a[0],a2[0]),('C111','legacy-agent',b[0])]:
        frame=SendMessage(sender=sender,recipient=recipient,text='foreign frame')
        socket.receiver(socket,SocketModeRequest(type='events_api',envelope_id='scope-control',payload={
            'event':{'type':'message','channel':channel,'bot_id':'B123','subtype':'bot_message',
                     'ts':'1700000000.111111','text':encode_envelope(frame)}}))
    assert app.state.store._db.execute('SELECT count(*) FROM messages').fetchone()[0]==0


def test_explicit_federation_endpoint_rejects_additional_channel_sessions(channel_service):
    from types import SimpleNamespace
    client,app,posted,a,a2,b,bearer,operator=channel_service
    enqueued=[]
    app.state.federation_bridge=SimpleNamespace(channel='C111',identity=SimpleNamespace(instance_id='home'),
        bindings={},enqueue=lambda *args:enqueued.append(args))
    for agent,expected in [(a,202),(b,403)]:
        result=client.post('/v1/federation/messages',headers=agent[1],json={
            'origin_message_id':'scope-test','destination_instance_id':'remote',
            'message':{'sender':agent[0],'recipient':agent[0],'text':'scope only'}})
        assert result.status_code==expected,result.text
    assert len(enqueued)==1


def test_rotation_cannot_reuse_another_channel_credential(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    _, first_headers, first = a
    _, second_headers, second = b
    grant = client.post(f"/v1/sessions/{first['session_id']}/rotation-grants",
        headers={'Authorization': 'Bearer operator-test'}, json={'chat_id': first['chat_id']})
    assert grant.status_code == 201, grant.text
    response = client.post(f"/v1/sessions/{first['session_id']}/rotate-secret",
        headers={'Authorization': 'Bearer service-test'},
        json={'rotation_token': grant.json()['rotation_token'],
              'new_session_secret': second_headers['X-AgentBus-Session-Token']})
    assert response.status_code == 409
    assert client.get('/v1/info', headers=first_headers).status_code == 200
    assert client.get('/v1/info', headers=second_headers).status_code == 200


def test_expired_grant_and_concurrent_consumption_fail_without_extra_identity(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from src import agentbus_channel_grants_service as grants
    from src.agentbus_service import MessageStore
    path = tmp_path/'grant-races.sqlite3'
    first = MessageStore(path)
    first.channels.configure(('C111', 'C222'))
    first.participation.set_policy(scope='global', scope_key='*', values={
        'initial_interval_seconds':60,'backoff_factor':2,'max_interval_seconds':1920,
        'control_check_max_seconds':60,'jitter_fraction':0,'overdue_grace_seconds':120,
        'presentation_budget_bytes':None}, actor='operator')
    clock = [1000.0]
    monkeypatch.setattr(grants.time, 'time', lambda: clock[0])
    expired = first.channels.issue_grant(channel='C111', repo='agentbus', route='home:agentbus:expired', actor='operator')
    clock[0] += 601
    with pytest.raises(PermissionError):
        first.channels.enroll(expired, repo='agentbus', route='home:agentbus:expired', display_name='Expired', session_secret='e'*32)
    token = first.channels.issue_grant(channel='C222', repo='agentbus', route='home:agentbus:race', actor='operator')
    second = MessageStore(path)
    second.channels.configure(('C111', 'C222'))
    args = dict(repo='agentbus', route='home:agentbus:race', display_name='Race', session_secret='r'*32)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(store.channels.enroll, token, **args) for store in (first, second)]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert first._db.execute('SELECT count(*) FROM chat_identities').fetchone()[0] == 1
    assert first.channels.channel_for_chat(results[0][0]) == 'C222'
    assert token not in str(first._db.execute('SELECT * FROM channel_enrollment_grants').fetchall())
    first.close()
    second.close()


def test_federation_directory_and_projection_cannot_export_or_consume_other_channel(channel_service):
    import json
    from types import SimpleNamespace

    from src.agentbus_federation_directory_service import DirectorySynchronizer
    from src.agentbus_federation_inbox_service import InboxProjectionError, project_message
    client, app, posted, a, a2, b, bearer, operator = channel_service
    store = app.state.store
    bridge = SimpleNamespace(channel='C111', identity=SimpleNamespace(instance_id='home', route_prefix='home'),
                             bindings={}, store=store.federation)
    own = DirectorySynchronizer(bridge, store.participation, share_chat_ids=[a[2]['chat_id']], peers=[])
    assert [entry['route'] for entry in own._snapshot()['entries']] == [a[0]]
    foreign = DirectorySynchronizer(bridge, store.participation, share_chat_ids=[b[2]['chat_id']], peers=[])
    with pytest.raises(ValueError, match='another channel'):
        foreign._snapshot()
    payload = json.dumps({'sender':'remote:agentbus:sender','recipient':b[0], 'audience':'direct','text':'foreign'})
    store.federation.bind_origin(origin_instance_id='remote', publisher_id='test-publisher')
    store.federation.accept_destination(origin_instance_id='remote', publisher_id='test-publisher',
        origin_message_id='foreign-projection', destination_instance_id='home', payload=payload)
    before = store._db.execute('SELECT count(*) FROM messages').fetchone()[0]
    with pytest.raises(InboxProjectionError, match='another channel'):
        project_message(store, 'C111', 'remote', 'foreign-projection', 'home', payload, '1700000099.000001')
    assert store._db.execute('SELECT count(*) FROM messages').fetchone()[0] == before
    assert store._db.execute('SELECT count(*) FROM federation_inbox_projection').fetchone()[0] == 0


def test_rename_requires_bound_inbox_before_route_mutation(channel_service):
    client, app, posted, a, a2, b, bearer, operator = channel_service
    route, headers, receipt = a
    forged = {**headers, 'X-AgentBus-Inbox-ID': b[2]['inbox_id']}
    response = client.post(f"/v1/sessions/{receipt['session_id']}/rename", headers=forged,
                           json={'route':'home:agentbus:renamed'})
    assert response.status_code == 403
    assert app.state.store.participation.current_route_for(route) == route


def test_federation_invalid_message_retains_validation_status(channel_service):
    from types import SimpleNamespace
    client, app, posted, a, a2, b, bearer, operator = channel_service
    app.state.federation_bridge = SimpleNamespace(channel='C111')
    response = client.post('/v1/federation/messages', headers=a[1], json={
        'origin_message_id':'invalid-message', 'destination_instance_id':'remote',
        'message':{'sender':a[0], 'text':''}})
    assert response.status_code == 422
    assert app.state.store._db.execute('SELECT count(*) FROM federation_origin_messages').fetchone()[0] == 0
