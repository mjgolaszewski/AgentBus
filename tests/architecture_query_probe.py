"""Every registered HTTP query runs against a write-denying SQLite authority."""
import asyncio
import sqlite3
import tempfile
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from test_rename_store import POLICY
from test_service import AUTH, CHANNEL, FakeSocket

from src.agentbus_service import API_OPERATIONS, MessageStore, SendMessage, Settings, create_app


def assert_query_nonmutation(operations, *, inject=None):
    queries = {o['population_key'] for o in operations
               if o['population'] == 'api' and o['semantic_kind'] == 'query'}
    with tempfile.TemporaryDirectory(prefix='agentbus-query-probe-') as directory:
        settings = Settings(api_token='local-test-secret', operator_token='operator-test-secret',
            slack_bot_token='xoxb-test', slack_app_token='xapp-test', slack_channel=CHANNEL,
            db_path=Path(directory)/'state.sqlite3')
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={'ok':True,'bot_id':'B123'})))
        app = create_app(settings, http_client=http, socket_factory=FakeSocket)
        # No lifespan: no background writer can confound query attribution.
        store = MessageStore(settings.db_path)
        store.channels.configure(settings.channels)
        app.state.store = store
        app.state.socket = app.state.federation_bridge = None
        app.state.runtime_started = 0.0
        secret = 'query-probe-secret-with-at-least-32-characters'
        store.participation.set_policy(scope='global', scope_key='*', values=POLICY, actor='operator')
        grant = store.channels.issue_grant(channel=CHANNEL, repo='agentbus', route='agentbus:query-probe', actor='operator')
        chat, session, revision = store.channels.enroll(grant, repo='agentbus', route='agentbus:query-probe', display_name='probe', session_secret=secret)
        store.participation.acknowledge_policy(session, secret, revision)
        control, _ = store.controls.issue_stop(routes=['agentbus:query-probe'], actor='operator', reason='probe')
        message = SendMessage(sender='agentbus:query-probe', recipient='agentbus:query-probe', text='query fixture')
        message._sender_assurance = 'session'
        store.lifecycle.activate_fresh()
        message = store.append(CHANNEL, '123.456', message)
        principal = store.channels.principal(secret, 'agentbus:query-probe')
        cursor = store.channels.public_cursor(CHANNEL, message.cursor)
        fixtures = {'session_id':session,'control_id':control,'cursor':cursor}
        query_params = {'repo':'agentbus','chat_id':chat,'identity':principal.route}
        headers = {**AUTH, 'X-AgentBus-Session-Token':secret,
                   'X-AgentBus-Inbox-ID':principal.inbox_id, 'X-AgentBus-Lifecycle-Version':'1'}
        operator = {'Authorization':'Bearer operator-test-secret',**{k:v for k,v in headers.items() if k!='Authorization'}}
        denied = []
        forbidden = {getattr(sqlite3, name) for name in ('SQLITE_INSERT','SQLITE_UPDATE','SQLITE_DELETE',
            'SQLITE_CREATE_TABLE','SQLITE_DROP_TABLE','SQLITE_ALTER_TABLE','SQLITE_CREATE_INDEX',
            'SQLITE_DROP_INDEX','SQLITE_CREATE_TRIGGER','SQLITE_DROP_TRIGGER','SQLITE_ATTACH','SQLITE_DETACH')}
        def authorize(action, first, second, database, source):
            if action in forbidden:
                denied.append((action, first, second))
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK
        covered = set()
        client = TestClient(app, raise_server_exceptions=True)
        store._db.set_authorizer(authorize)
        changes = store._db.total_changes
        try:
            for key in sorted(queries):
                endpoint = API_OPERATIONS[key]
                routes = [route for route in app.routes if getattr(route, 'endpoint', None) is endpoint]
                assert len(routes) == 1, key
                route = routes[0]
                assert route.methods == {'GET'}, key
                url = route.path.format(**fixtures)
                responses = [client.get(url, params=query_params, headers=proof) for proof in (headers, operator)]
                assert any(response.status_code == 200 for response in responses), (key, [(r.status_code,r.text) for r in responses])
                covered.add(key)
            if inject is not None:
                inject(store)
            assert not denied, denied
            assert store._db.total_changes == changes
            assert covered == queries
        finally:
            store._db.set_authorizer(None)
            client.close()
            store.close()
            asyncio.run(http.aclose())


def assert_cli_query_nonmutation(operations):
    import contextlib
    import io

    import pytest

    from src import agentbus_client as client
    from src import agentbus_operator_client as operator
    from src import agentbus_transport_client as transport
    from src.agentbus_parser_client import parse_args
    cases = {'status':[], 'read':[], 'agents':[], 'roster':['--json'],
        'policy-show':['--session-id','session-one'], 'control-status':['control-one'],
        'session-presence':['session-one'], 'message-status':['--cursor','1']}
    queries = {o['population_key'] for o in operations
               if o['population'] == 'cli' and o['semantic_kind'] == 'query'}
    assert set(cases) == queries
    with tempfile.TemporaryDirectory(prefix='agentbus-cli-query-') as directory, pytest.MonkeyPatch.context() as patch:
        root = Path(directory)
        patch.setenv('AGENTBUS_OPERATOR_TOKEN', 'operator-test')
        values = {'AGENTBUS_URL':'http://127.0.0.1:8766','AGENTBUS_OPERATOR_TOKEN':'operator-test',
            'AGENTBUS_STATE_DIR':directory,'AGENTBUS_CONSUMER_STATE_DIR':str(root/'consumers')}
        profile = {'identity':'agentbus:probe','channel':CHANNEL,'inbox_id':'inbox-one',
            'service_url':values['AGENTBUS_URL'],'chat_id':'chat-one','ack_cursor':0,
            'participation':{'session_id':'session-one','session_secret':'x'*32}}
        calls = []
        def read_only(_values, path, payload=None, **kwargs):
            assert payload is None and kwargs.get('method', 'GET') == 'GET', (path,payload,kwargs)
            calls.append(path)
            return {'messages':[],'sessions':[],'revision':'r1','values':POLICY,'sources':{},'status':'ok'}
        for module in (client,operator,transport):
            patch.setattr(module,'api',read_only)
        patch.setattr(client,'checked_profile',lambda *_args,**_kwargs:(dict(profile),{}))
        patch.setattr(client,'load_profile',lambda *_args,**_kwargs:dict(profile))
        patch.setattr(client,'resolve_identity',lambda *_args:profile['identity'])
        def forbidden(*_args, **_kwargs): raise AssertionError('query invoked a mutation port')
        for name in ('save_profile','atomic_json','ensure_state_dir','serve'):
            if hasattr(client,name):
                patch.setattr(client,name,forbidden)
        patch.setattr(client.subprocess,'Popen',forbidden)
        with contextlib.redirect_stdout(io.StringIO()):
            for command, args in cases.items():
                assert client.CLI_OPERATIONS[command](parse_args([command,*args]),root,values) == 0
        assert calls
