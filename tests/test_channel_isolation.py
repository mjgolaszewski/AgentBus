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
        assert result=='Unknown session or identity. Use /agentbus roster.'
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


def test_slack_approval_binds_staged_secret_profile_and_channel_across_restart(tmp_path):
    import uuid

    from src.agentbus_service import MessageStore
    path = tmp_path / 'approval.sqlite3'
    store = MessageStore(path)
    store.channels.configure(('C111', 'C222'))
    store.participation.set_policy(scope='global', scope_key='*', actor='operator', values={
        'initial_interval_seconds': 60, 'backoff_factor': 2, 'max_interval_seconds': 1920,
        'control_check_max_seconds': 60, 'jitter_fraction': 0, 'overdue_grace_seconds': 120,
        'presentation_budget_bytes': None,
    })
    request_id = str(uuid.uuid4())
    chat_id = str(uuid.uuid4())
    args = dict(request_id=request_id, repo='agentbus', route='home:agentbus:approval',
                display_name='Approval', session_secret='s' * 32, chat_id=chat_id)
    assert store.channels.request_enrollment(**args) is None
    assert store.channels.request_enrollment(**args) is None
    assert store._db.execute('SELECT count(*) FROM participation_sessions').fetchone()[0] == 0
    listed = store.channels.pending_enrollments()
    assert len(listed) == 1 and listed[0]['request_id'] == request_id
    assert 'secret' not in str(listed) and 's' * 32 not in str(listed)
    for change in ({'session_secret': 'x' * 32}, {'route': 'home:agentbus:other'}, {'chat_id': str(uuid.uuid4())}):
        with pytest.raises(PermissionError):
            store.channels.request_enrollment(**{**args, **change})
    store.channels.approve_enrollment(request_id, channel='C111', actor='slack:U1')
    store.channels.approve_enrollment(request_id, channel='C111', actor='slack:U1')
    with pytest.raises(PermissionError):
        store.channels.approve_enrollment(request_id, channel='C222', actor='slack:U1')
    store.close()
    store = MessageStore(path)
    store.channels.configure(('C111', 'C222'))
    result = store.channels.request_enrollment(**args)
    assert result[0] == chat_id
    assert store.channels.channel_for_chat(chat_id) == 'C111'
    assert store.channels.request_enrollment(**args) == result
    assert store._db.execute('SELECT count(*) FROM participation_sessions').fetchone()[0] == 1
    assert store._db.execute('SELECT count(*) FROM channel_enrollment_grants').fetchone()[0] == 1
    assert store.participation.session_state(result[1])['state'] == 'joining'
    assert store.channels.pending_enrollments() == []
    store.close()


def test_expired_or_disabled_approval_grants_no_session_authority(tmp_path, monkeypatch):
    import uuid

    from src import agentbus_enrollment_approval_service as approval
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path / 'approval.sqlite3')
    store.channels.configure(('C111', 'C222'))
    now = [1000.0]
    monkeypatch.setattr(approval.time, 'time', lambda: now[0])
    args = dict(request_id=str(uuid.uuid4()), repo='agentbus', route='home:agentbus:approval',
                display_name='Approval', session_secret='s' * 32, chat_id=str(uuid.uuid4()))
    store.channels.request_enrollment(**args)
    store.channels.approve_enrollment(args['request_id'], channel='C111', actor='slack:U1')
    store.channels.configure(('C222',))
    with pytest.raises(PermissionError):
        store.channels.request_enrollment(**args)
    now[0] += 601
    with pytest.raises(PermissionError):
        store.channels.request_enrollment(**args)
    assert store.channels.pending_enrollments() == []
    assert store._db.execute('SELECT count(*) FROM participation_sessions').fetchone()[0] == 0
    store.close()


def test_slack_approval_real_api_has_no_authority_before_human_approval(channel_service):
    import uuid
    from dataclasses import replace

    from src.agentbus_slack_command_service import execute_command
    client, app, posted, a, _a2, b, bearer, _operator = channel_service
    settings = replace(app.state.settings, slack_operator_user_ids=frozenset({'U1'}))
    body = dict(repo='agentbus', route='home:agentbus:approved', display_name='Approved',
                session_secret='approval-secret-with-at-least-32-characters',
                approval_request_id=str(uuid.uuid4()), profile_chat_id=str(uuid.uuid4()))
    assert client.post('/v1/sessions', json=body).status_code == 401
    staged = client.post('/v1/sessions', headers=bearer, json=body)
    assert staged.status_code == 202 and set(staged.json()) == {'status', 'request_id'}
    assert client.post('/v1/sessions', headers=bearer, json={**body, 'channel': 'C111'}).status_code == 422
    assert client.post('/v1/sessions', headers=bearer, json={**body, 'session_secret': 'x' * 32}).status_code == 403
    assert client.get('/v1/inbox?identity='+body['route'], headers=bearer).status_code == 403
    payload = {'command': '/agentbus', 'channel_id': 'C222', 'user_id': 'U1',
               'text': 'approve ' + body['approval_request_id']}
    assert 'not enabled' in execute_command({**payload, 'user_id': 'U2'}, settings, app.state.store, object())
    listing = execute_command({**payload, 'text': 'enrollments'}, settings, app.state.store, object())
    assert body['route'] in listing and body['profile_chat_id'] in listing
    assert listing.splitlines()[-1] == f"`/agentbus approve {body['approval_request_id']}`"
    assert body['session_secret'] not in listing
    assert 'approved in this channel' in execute_command(payload, settings, app.state.store, object())
    assert 'unavailable' in execute_command({**payload, 'channel_id': 'C111'}, settings, app.state.store, object())
    assert body['route'] not in execute_command({**payload, 'channel_id': 'C111', 'text': 'enrollments'}, settings, app.state.store, object())
    joined = client.post('/v1/sessions', headers=bearer, json=body)
    assert joined.status_code == 201, joined.text
    receipt = joined.json()
    assert receipt['chat_id'] == body['profile_chat_id'] and receipt['channel'] == 'C222'
    retry = client.post('/v1/sessions', headers=bearer, json=body)
    assert retry.status_code == 201 and retry.json()['session_id'] == receipt['session_id']
    headers = {**bearer, 'X-AgentBus-Session-Token': body['session_secret'], 'X-AgentBus-Inbox-ID': receipt['inbox_id']}
    assert client.get('/v1/messages', headers=headers).status_code == 403
    assert client.post(f"/v1/sessions/{receipt['session_id']}/policy-ack", headers=headers,
                       json={'revision': receipt['revision']}).status_code == 200
    assert client.post('/v1/messages', headers=headers,
                       json={'sender': body['route'], 'recipient': a[0], 'text': 'foreign'}).status_code == 403
    assert client.get('/v1/inbox?identity='+b[0], headers=b[1]).status_code == 200
    assert posted == []


def test_approval_consumption_failure_rolls_back_grant_session_and_request(channel_service):
    import sqlite3
    import uuid
    _client, app, _posted, _a, _a2, _b, _bearer, _operator = channel_service
    store = app.state.store
    args = dict(request_id=str(uuid.uuid4()), chat_id=str(uuid.uuid4()), repo='agentbus',
                route='home:agentbus:rollback', display_name='Rollback', session_secret='rollback-secret-'.ljust(40, 'x'))
    store.channels.request_enrollment(**args)
    store.channels.approve_enrollment(args['request_id'], channel='C111', actor='slack:U1')
    before = {table: store._db.execute('SELECT count(*) FROM '+table).fetchone()[0]
              for table in ('participation_sessions', 'chat_identities', 'channel_enrollment_grants')}
    store._db.execute("CREATE TRIGGER reject_approval BEFORE UPDATE OF consumed_session ON channel_enrollment_requests BEGIN SELECT RAISE(ABORT,'injected failure'); END")
    store._db.commit()
    with pytest.raises((PermissionError, sqlite3.IntegrityError)):
        store.channels.request_enrollment(**args)
    assert before == {table: store._db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in before}
    assert store._db.execute('SELECT consumed_session FROM channel_enrollment_requests WHERE request_id=?', (args['request_id'],)).fetchone()[0] is None
    store._db.execute('DROP TRIGGER reject_approval')
    store._db.commit()
    result = store.channels.request_enrollment(**args)
    assert store.channels.request_enrollment(**args) == result


def test_concurrent_approval_redemption_retains_one_session_and_no_scope_transfer(channel_service):
    import uuid
    from concurrent.futures import ThreadPoolExecutor
    _client, app, _posted, _a, _a2, _b, _bearer, _operator = channel_service
    store = app.state.store
    args = dict(request_id=str(uuid.uuid4()), chat_id=str(uuid.uuid4()), repo='agentbus',
                route='home:agentbus:concurrent', display_name='Concurrent', session_secret='concurrent-secret-'.ljust(40, 'x'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: store.channels.request_enrollment(**args), range(4))) == [None] * 4
    def approve(channel):
        try:
            store.channels.approve_enrollment(args['request_id'], channel=channel, actor='slack:U1')
            return channel
        except PermissionError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        approvals = list(pool.map(approve, ('C111', 'C222')))
    winners = [value for value in approvals if value is not None]
    assert len(winners) == 1
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.channels.request_enrollment(**args), range(4)))
    assert len(set(results)) == 1
    assert store.channels.channel_for_chat(results[0][0]) == winners[0]
    assert store._db.execute('SELECT count(*) FROM participation_sessions WHERE chat_id=?', (args['chat_id'],)).fetchone()[0] == 1
    with pytest.raises(PermissionError):
        store.channels.approve_enrollment(args['request_id'], channel='C111' if winners[0] == 'C222' else 'C222', actor='slack:U1')
    with pytest.raises(PermissionError):
        store.channels.request_enrollment(**{**args, 'request_id': str(uuid.uuid4())})


def test_approval_capacity_and_expiry_remain_bounded(tmp_path, monkeypatch):
    import uuid

    from src import agentbus_enrollment_approval_service as approval
    from src.agentbus_service import MessageStore
    store = MessageStore(tmp_path / 'capacity.sqlite3')
    store.channels.configure(('C111',))
    monkeypatch.setattr(approval, 'MAX_REQUESTS', 1)
    now = [1000.0]
    monkeypatch.setattr(approval.time, 'time', lambda: now[0])
    first = dict(request_id=str(uuid.uuid4()), chat_id=str(uuid.uuid4()), repo='agentbus',
                 route='home:agentbus:first', display_name='First', session_secret='f' * 32)
    store.channels.request_enrollment(**first)
    second = {**first, 'request_id': str(uuid.uuid4()), 'chat_id': str(uuid.uuid4()),
              'route': 'home:agentbus:second', 'session_secret': 's' * 32}
    with pytest.raises(ValueError, match='capacity'):
        store.channels.request_enrollment(**second)
    now[0] += 601
    with pytest.raises(PermissionError):
        store.channels.approve_enrollment(first['request_id'], channel='C111', actor='slack:U1')
    store.channels.request_enrollment(**second)
    assert [row['request_id'] for row in store.channels.pending_enrollments()] == [second['request_id']]
    assert store._db.execute('SELECT count(*) FROM channel_enrollment_requests').fetchone()[0] == 1
    store.close()


def test_enrollment_expiry_reason_requires_exact_staged_subject(channel_service, monkeypatch):
    import uuid

    from src import agentbus_enrollment_approval_service as approval
    client, app, _posted, _a, _a2, _b, bearer, _operator = channel_service
    instant = [1000.0]
    monkeypatch.setattr(approval.time, 'time', lambda: instant[0])
    body = dict(repo='agentbus', route='home:agentbus:expiry-reason', display_name='Expiry',
                session_secret='staged-expiry-secret-at-least-32-characters',
                approval_request_id=str(uuid.uuid4()), profile_chat_id=str(uuid.uuid4()))
    assert client.post('/v1/sessions', headers=bearer, json=body).status_code == 202
    app.state.store.channels.approve_enrollment(body['approval_request_id'], channel='C111', actor='slack:U1')
    instant[0] += 601
    denied = []
    for change in ({'session_secret': 'wrong-secret-at-least-32-characters'},
                   {'route': 'home:agentbus:foreign'}, {'profile_chat_id': str(uuid.uuid4())}):
        response = client.post('/v1/sessions', headers=bearer, json={**body, **change})
        assert response.status_code == 403
        assert 'X-AgentBus-Recovery-Code' not in response.headers
        denied.append(response.json())
    assert denied[0] == denied[1] == denied[2]
    response = client.post('/v1/sessions', headers=bearer, json=body)
    assert response.status_code == 403
    assert response.headers['X-AgentBus-Recovery-Code'] == 'enrollment_request_expired'
    assert body['session_secret'] not in response.text
    row = app.state.store._db.execute('SELECT consumed_session FROM channel_enrollment_requests WHERE request_id=?',
                                     (body['approval_request_id'],)).fetchone()
    assert row['consumed_session'] is None


def test_consumed_approval_retry_survives_request_expiry(channel_service, monkeypatch):
    import uuid

    from src import agentbus_enrollment_approval_service as approval
    client, app, _posted, _a, _a2, _b, bearer, _operator = channel_service
    instant = [1000.0]
    monkeypatch.setattr(approval.time, 'time', lambda: instant[0])
    body = dict(repo='agentbus', route='home:agentbus:consumed-retry', display_name='Retry',
                session_secret='consumed-retry-secret-at-least-32-characters',
                approval_request_id=str(uuid.uuid4()), profile_chat_id=str(uuid.uuid4()))
    assert client.post('/v1/sessions', headers=bearer, json=body).status_code == 202
    app.state.store.channels.approve_enrollment(body['approval_request_id'], channel='C111', actor='slack:U1')
    first = client.post('/v1/sessions', headers=bearer, json=body)
    assert first.status_code == 201
    instant[0] += 601
    retry = client.post('/v1/sessions', headers=bearer, json=body)
    assert retry.status_code == 201
    assert retry.json()['session_id'] == first.json()['session_id']


def test_authored_request_lookup_is_scoped_nonmutating_and_absence_is_unknown(channel_service):
    from uuid import uuid4

    client, app, posted, a, a2, b, bearer, operator = channel_service
    key = str(uuid4())
    response = client.post('/v1/messages', headers={**a[1], 'X-AgentBus-Delivery-Version':'1', 'Idempotency-Key':key},
                           json={'sender':a[0], 'recipient':a2[0], 'text':'own retained request'})
    assert response.status_code == 202, response.text
    route = f'/v1/requests/{key}/outcome'
    own = client.get(route, headers=a[1])
    assert own.status_code == 200 and own.json()['accepted'] is True
    assert own.json()['cursor'] == response.json()['cursor']
    assert own.json()['execution'] == 'unknown' and own.json()['semantic_acknowledgement'] == 'unknown'
    foreign = [client.get(route, headers=proof) for proof in (a2[1], b[1])]
    missing = client.get('/v1/requests/'+str(uuid4())+'/outcome', headers=a[1])
    for result in [*foreign, missing]:
        assert result.status_code == 200 and result.json()['known'] is False
        assert result.json()['accepted'] is None and 'cursor' not in result.json()
    assert client.get(route, headers=bearer).status_code == 403
    assert client.get(route, headers={**a[1], 'X-AgentBus-Inbox-ID':b[2]['inbox_id']}).status_code == 403
    assert client.get('/v1/requests/not-a-uuid/outcome', headers=a[1]).status_code == 422
    store = app.state.store
    with store._lock:
        before = store._db.total_changes
        principal = store.channels.principal(a[1]['X-AgentBus-Session-Token'], a[0])
        assert store.lifecycle.request_outcome(principal, key)['known'] is True
        assert store._db.total_changes == before


def test_authored_federation_status_requires_retained_exact_frame_not_current_binding(channel_service):
    import hashlib
    from uuid import uuid4

    client, app, posted, a, a2, b, bearer, operator = channel_service
    key = str(uuid4())
    response = client.post('/v1/messages', headers={**a[1], 'X-AgentBus-Delivery-Version':'1', 'Idempotency-Key':key},
                           json={'sender':a[0], 'recipient':a2[0], 'text':'transport fixture'})
    assert response.status_code == 202
    store = app.state.store
    # Retained custody fixture only; signature and receipt admission have separate wire controls.
    digest = hashlib.sha256(b'exact-retained-signed-frame').hexdigest()
    with store._lock, store._db:
        store._db.execute("UPDATE local_delivery_obligations SET federation_frame_hash=? WHERE channel='C111' AND request_id=?", (digest,key))
        store._db.execute("INSERT INTO bridge_obligations (origin_instance_id,origin_message_id,destination_instance_id,state,payload,payload_hash,updated_at,kind) VALUES ('origin',?,'destination','acknowledged','retained',?,'now','message')", (key,'0'*64))
    route = f'/v1/requests/{key}/outcome'
    assert client.get(route, headers=a[1]).json()['destination_custody'] == 'unknown'
    with store._lock, store._db:
        store._db.execute('UPDATE bridge_obligations SET payload_hash=? WHERE origin_message_id=?', (digest,key))
    result = client.get(route, headers=a[1]).json()
    assert result['destination_custody'] == 'authenticated'
    assert result['execution'] == result['semantic_acknowledgement'] == 'unknown'
    assert client.get(route, headers=b[1]).json()['known'] is False
    with store._lock, store._db:
        store._db.execute('DELETE FROM local_delivery_obligations WHERE request_id=?', (key,))
    assert client.get(route, headers=a[1]).json()['accepted'] is None


def test_request_status_same_uuid_in_two_channels_keeps_distinct_custody(channel_service):
    from uuid import uuid4

    client, app, posted, a, a2, b, bearer, operator = channel_service
    key = str(uuid4())
    for identity in (a, b):
        response = client.post('/v1/messages', headers={**identity[1], 'X-AgentBus-Delivery-Version':'1', 'Idempotency-Key':key},
            json={'sender':identity[0], 'recipient':'all', 'audience':'broadcast', 'text':'same UUID, separate scope'})
        assert response.status_code == 202, response.text
    store = app.state.store
    with store._lock, store._db:
        store._db.execute("UPDATE local_delivery_obligations SET state='uncertain' WHERE channel='C111' AND request_id=?", (key,))
        store._db.execute("UPDATE local_delivery_obligations SET state='blocked' WHERE channel='C222' AND request_id=?", (key,))
    assert client.get(f'/v1/requests/{key}/outcome', headers=a[1]).json()['local_transport_state'] == 'uncertain'
    assert client.get(f'/v1/requests/{key}/outcome', headers=b[1]).json()['local_transport_state'] == 'blocked'
