"""Opt-in HTTP enqueue and the real Slack adapter, using an isolated provider."""

import asyncio
import base64
import json

import httpx
import pytest
import test_federation_bridge as bridge_tests
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest
from test_service import CHANNEL

from src.agentbus_federation_bridge_service import ProviderUnavailable
from src.agentbus_federation_runtime_service import load_bridge
from src.agentbus_federation_slack_service import SlackFederationProvider
from src.agentbus_federation_wire_service import Identity, encode_frame, key_generation
from src.agentbus_service import MessageStore, Settings, create_app
from src.agentbus_slack_socket_service import SlackReceiver

AUTH = {'Authorization': 'Bearer local-test-secret'}
pair = bridge_tests.pair


@pytest.mark.parametrize('echo_thread', ['1.000001', None, '9.000001'])
def test_ordinary_thread_port_broadcasts_and_requires_exact_provider_thread(echo_thread):
    from src.agentbus_federation_bridge_service import ProviderCarrierRejected

    async def scenario():
        posts = []
        async def respond(request):
            body = json.loads(request.content)
            posts.append(body)
            return httpx.Response(200, json={'ok': True, 'channel': 'C123', 'ts': '2.000001',
                'message': {'text': body['text'], 'thread_ts': echo_thread}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'C123')
            if echo_thread == '1.000001':
                assert await provider.publish_in_thread('{}', '1.000001') == '2.000001'
            else:
                with pytest.raises(ProviderCarrierRejected):
                    await provider.publish_in_thread('{}', '1.000001')
            assert posts[0]['thread_ts'] == '1.000001' and posts[0]['reply_broadcast'] is True
    asyncio.run(scenario())


class Socket:
    def __init__(self, settings, receiver):
        self.receiver = receiver
        self.connected = False

    def connect(self):
        self.connected = True

    def close(self):
        self.connected = False

    def is_connected(self):
        return self.connected


def settings(tmp_path, *, enabled):
    key = Ed25519PrivateKey.generate()
    peer_key = Ed25519PrivateKey.generate().public_key()
    key_file = tmp_path / 'origin.key'
    key_file.write_bytes(key.private_bytes_raw())
    key_file.chmod(0o600)
    config = tmp_path / 'federation.json'
    config.write_text(json.dumps({'instance_id': 'home', 'route_prefix': 'home',
        'publisher_id': 'A123/B123/C123456', 'private_key_file': str(key_file),
        'key_generation': key_generation(key.public_key()),
        'channel': 'C123456', 'bindings': [{'instance_id': 'remote', 'route_prefix': 'remote',
        'publisher_id': 'A456/B456/C123456',
        'key_generation': key_generation(peer_key),
        'public_key': base64.b64encode(peer_key.public_bytes_raw()).decode()}]}))
    return Settings(api_token='local-test-secret', slack_bot_token='xoxb-test-secret',
        slack_app_token='xapp-test-secret', slack_channel='C123456', db_path=tmp_path / 'inbox.sqlite3',
        federation_config=config if enabled else None)


def test_enqueue_is_disabled_without_explicit_configuration(tmp_path):
    async def scenario():
        async def respond(request):
            return httpx.Response(200, json={'ok': True, 'bot_id': 'B123'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        app = create_app(settings(tmp_path, enabled=False), http_client=client, socket_factory=Socket)
        with TestClient(app) as api:
            body = {'origin_message_id': 'm1', 'destination_instance_id': 'remote', 'message': {}}
            assert api.post('/v1/federation/messages', json=body).status_code == 401
            assert api.post('/v1/federation/messages', headers=AUTH, json=body).status_code == 403
        await client.aclose()
    asyncio.run(scenario())


def test_enqueue_requires_exact_session_and_is_durable_idempotent_and_local_only(tmp_path, monkeypatch):
    posts = []
    async def respond(request):
        if request.url.path.endswith('auth.test'):
            return httpx.Response(200, json={'ok': True, 'bot_id': 'B123'})
        if request.url.path.endswith('conversations.history'):
            return httpx.Response(200, json={'ok': True, 'messages': [], 'has_more': False})
        posts.append(request)
        return httpx.Response(200, json={'ok': True, 'channel': 'C123456', 'ts': '1.000001'})
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings(tmp_path, enabled=True), http_client=client, socket_factory=Socket)
    with TestClient(app) as api:
        participation = app.state.store.participation
        participation.set_policy(scope='global', scope_key='*', values={
            'initial_interval_seconds': 60, 'backoff_factor': 2, 'max_interval_seconds': 1920,
            'control_check_max_seconds': 60, 'jitter_fraction': 0, 'overdue_grace_seconds': 120,
            'presentation_budget_bytes': None}, actor='operator')
        secret = 'joined-session-secret-' * 3
        _, session, revision = participation.enroll(repo='agentbus', route='home:agentbus:sender',
            display_name='Sender', session_secret=secret)
        participation.acknowledge_policy(session, secret, revision)
        body = {'origin_message_id': 'm1', 'destination_instance_id': 'remote', 'message': {
            'sender': 'home:agentbus:sender', 'recipient': 'remote:agentbus:receiver',
            'audience': 'direct', 'text': 'nonce'}}
        assert api.post('/v1/federation/messages', headers=AUTH, json=body).status_code == 403
        joined = dict(AUTH, **{'X-AgentBus-Session-Token': secret, 'X-AgentBus-Inbox-ID': app.state.store.channels.inbox_id(CHANNEL)})
        for _ in range(2):
            response = api.post('/v1/federation/messages', headers=joined, json=body)
            assert response.status_code == 202 and response.json()['accepted'] is True
        row = app.state.store.federation.obligation('home', 'm1', 'remote')
        assert row['state'] == 'origin_accepted' and row['attempts'] == 0
        assert app.state.store._db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 1
        assert posts == []
        body['message']['text'] = 'conflicting retry'
        assert api.post('/v1/federation/messages', headers=joined, json=body).status_code == 422
        body['message']['sender'] = 'home:agentbus:unjoined'
        assert api.post('/v1/federation/messages', headers=AUTH, json=body).status_code == 403
        from src import agentbus_federation_service as persistence
        monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1)
        body['origin_message_id'] = 'm2'
        body['message']['sender'] = 'home:agentbus:sender'
        rejected = api.post('/v1/federation/messages', headers=joined, json=body)
        assert rejected.status_code == 503 and rejected.headers['Retry-After'] == '60'
        assert app.state.store.federation.obligation('home', 'm2', 'remote') is None
        assert posts == []
    asyncio.run(client.aclose())


def test_slack_rate_limit_preserves_retry_after_and_redacts_response():
    async def scenario():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(429, headers={'Retry-After': '1200'}, text='private provider details')))
        provider = SlackFederationProvider(client, 'xoxb-test-secret', 'C123')
        with pytest.raises(ProviderUnavailable) as caught:
            await provider.publish('signed frame')
        assert caught.value.retry_after == 1200
        assert 'private provider details' not in str(caught.value)
        await client.aclose()
    asyncio.run(scenario())


def test_slack_history_requires_complete_bounded_pages_and_orders_exact_timestamps():
    async def scenario():
        replies = [{'ok': True, 'has_more': True, 'messages': [
            {'ts': '1792100000.000002', 'text': 'new', 'app_id': 'A1', 'bot_id': 'B1'}],
            'response_metadata': {'next_cursor': 'page-2'}},
            {'ok': True, 'has_more': False, 'messages': [
            {'ts': '1792100000.000001', 'text': 'old', 'app_id': 'A1', 'bot_id': 'B1'}]}]
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json=replies.pop(0))
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        provider = SlackFederationProvider(client, 'xoxb-test-secret', 'C123')
        events = await provider.history('0', 2)
        assert [event['text'] for event in events] == ['old', 'new']
        assert events[0]['publisher_id'] == 'A1/B1/C123'
        assert calls[1]['cursor'] == 'page-2'
        await client.aclose()
    asyncio.run(scenario())


def test_slack_incomplete_or_oversized_recovery_fails_without_returning_partial_events():
    async def scenario():
        for body in [{'ok': True, 'messages': [], 'has_more': True},
                     {'ok': True, 'messages': [{'ts': '1.000001'}, {'ts': '2.000001'}]}]:
            client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json=body)))
            provider = SlackFederationProvider(client, 'xoxb-test-secret', 'C123')
            with pytest.raises(ProviderUnavailable):
                await provider.history('0', 1)
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize('storage_failure', [False, True, 'capacity'])
def test_existing_socket_acknowledges_signed_frames_only_after_durable_custody(tmp_path, storage_failure, monkeypatch):
    configured = settings(tmp_path, enabled=True)
    peer_key = Ed25519PrivateKey.generate()
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    data['bindings'][0]['public_key'] = base64.b64encode(peer_key.public_key().public_bytes_raw()).decode()
    data['bindings'][0]['key_generation'] = key_generation(peer_key.public_key())
    config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    bridge = load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
    receiver = SlackReceiver(store, configured.slack_channel, 'B123', federation_bridge=bridge)
    acknowledgements = []
    class ProviderSocket:
        def send_socket_mode_response(self, response):
            acknowledgements.append(response.envelope_id)
    frame = encode_frame(Identity('remote', 'remote', 'A456/B456/C123456', peer_key),
        'message', 'socket-1', 'home', json.dumps({'sender': 'remote:agentbus:sender',
        'recipient': 'home:agentbus:receiver', 'audience': 'direct', 'text': 'nonce'}))
    request = SocketModeRequest(type='events_api', envelope_id='signed-event', payload={'event': {
        'type': 'message', 'app_id': 'A456', 'bot_id': 'B456', 'channel': 'C123456',
        'text': frame, 'ts': '1.000001'}})
    if storage_failure == 'capacity':
        from src import agentbus_federation_service as persistence
        monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 0)
        receiver(ProviderSocket(), request)
        assert acknowledgements == []
        monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1024)
    elif storage_failure:
        store._db.execute("""CREATE TRIGGER reject_receipt BEFORE INSERT ON bridge_obligations
            BEGIN SELECT RAISE(ABORT, 'injected receipt failure'); END""")
        receiver(ProviderSocket(), request)
        assert acknowledgements == []
        store._db.execute('DROP TRIGGER reject_receipt')
    receiver(ProviderSocket(), request)
    receiver(ProviderSocket(), request)
    assert acknowledgements == ['signed-event', 'signed-event']
    assert store._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1
    assert store._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1
    assert store._db.execute('SELECT COUNT(*) FROM messages').fetchone()[0] == 0
    assert store._db.execute('SELECT COUNT(*) FROM participation_sessions').fetchone()[0] == 0
    receiver._commands.shutdown()
    store.close()
    asyncio.run(client.aclose())


@pytest.mark.parametrize('which_key', ['local', 'peer'])
def test_key_changes_require_the_matching_operator_approved_generation(tmp_path, which_key):
    configured = settings(tmp_path, enabled=True)
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    replacement = Ed25519PrivateKey.generate()
    if which_key == 'local':
        from pathlib import Path
        Path(data['private_key_file']).write_bytes(replacement.private_bytes_raw())
    else:
        data['bindings'][0]['public_key'] = base64.b64encode(replacement.public_key().public_bytes_raw()).decode()
        config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    with pytest.raises(ValueError, match='approved generation'):
        load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
    store.close()
    asyncio.run(client.aclose())


def test_slack_machine_carrier_has_compact_human_blocks_without_rewriting_bytes():
    async def scenario():
        key = Ed25519PrivateKey.generate()
        frame = encode_frame(Identity("home", "home", "publisher", key),
            "message", "operator-status-1", "remote", '{"text":"structured status"}')
        bodies = []

        def respond(request):
            body = json.loads(request.content)
            bodies.append(body)
            return httpx.Response(200, json={"ok": True, "channel": "C123",
                                           "ts": "1.000001", "message": {"text": body["text"]}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, "xoxb-test-secret", "C123")
            assert await provider.publish(frame) == "1.000001"
        body = bodies[0]
        assert body["text"] == frame
        rendered = json.dumps(body["blocks"], ensure_ascii=False)
        assert "home → remote" in rendered
        assert "operator-status-1" in rendered
        assert "agentbus_federation" not in rendered
        assert "structured status" not in rendered
        assert body["parse"] == "none" and body["mrkdwn"] is False
    asyncio.run(scenario())


@pytest.mark.parametrize('approved', [False, True])
def test_directory_runtime_requires_per_peer_version_and_explicit_chat_allowlist(tmp_path, approved):
    configured = settings(tmp_path, enabled=True)
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    data['directory'] = {'share_chat_ids': [], 'peers': ['remote']}
    if approved:
        data['bindings'][0]['directory_version'] = 2
    config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    try:
        if not approved:
            with pytest.raises(ValueError, match='operator-approved'):
                load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
        else:
            bridge = load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
            assert bridge.directory is not None and bridge.directory.shared == set()
            assert bridge.directory.peers == ('remote',)
            assert store._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 0
    finally:
        store.close()
        asyncio.run(client.aclose())


@pytest.mark.parametrize('echo,warnings', [
    (None, []), ('rewritten', []), ('signed frame', ['message_truncated']),
])
def test_slack_rejects_missing_rewritten_or_truncated_carrier(echo, warnings):
    from src.agentbus_federation_bridge_service import ProviderCarrierRejected

    async def scenario():
        body = {'ok': True, 'channel': 'C123', 'ts': '1.000001',
                'response_metadata': {'warnings': warnings, 'private_detail': 'SECRET'}}
        if echo is not None:
            body['message'] = {'text': echo}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json=body))) as client:
            with pytest.raises(ProviderCarrierRejected) as caught:
                await SlackFederationProvider(client, 'xoxb-test-secret', 'C123').publish('signed frame')
        assert caught.value.provider_ref == '1.000001'
        assert caught.value.reason in {'missing_echo', 'changed_echo', 'truncation_warning'}
        assert 'SECRET' not in str(caught.value) and 'rewritten' not in str(caught.value)
    asyncio.run(scenario())


def test_slack_preserves_long_signed_carrier_with_exact_response_echo():
    async def scenario():
        frame = 'signed-' + 'x' * 5000
        calls = []
        def respond(request):
            body = json.loads(request.content)
            calls.append(body)
            return httpx.Response(200, json={'ok': True, 'channel': 'C123',
                'ts': '1.000001', 'message': {'text': body['text']},
                'response_metadata': {'warnings': ['missing_charset']}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await SlackFederationProvider(client, 'xoxb-test-secret', 'C123').publish(frame) == '1.000001'
        assert len(calls) == 1 and calls[0]['text'] == frame
    asyncio.run(scenario())


@pytest.mark.parametrize('version', [None, 1, True, '1', 2])
def test_inbox_projection_requires_exact_operator_opt_in(tmp_path, version):
    configured = settings(tmp_path, enabled=True)
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    if version is not None:
        data['inbox_projection'] = version
    config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    try:
        if version is None or type(version) is int and version == 1:
            bridge = load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
            assert (bridge.project_message is not None) == (version == 1)
        else:
            with pytest.raises(ValueError, match='projection version'):
                load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
    finally:
        store.close()
        asyncio.run(client.aclose())


@pytest.mark.parametrize('projection', [None, True, '1', 1])
def test_ordinary_peer_requires_explicit_projection_and_installs_versioned_decoder(tmp_path, projection):
    configured = settings(tmp_path, enabled=True)
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    assert data['bindings']
    data['bindings'][0]['ordinary_version'] = 3
    if projection is not None:
        data['inbox_projection'] = projection
    config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    try:
        if type(projection) is int and projection == 1:
            bridge = load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
            assert bridge.project_ordinary is not None and bridge.project_message is not None
        else:
            with pytest.raises(ValueError, match='projection'):
                load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
    finally:
        store.close()
        asyncio.run(client.aclose())


@pytest.mark.parametrize('limit', [None, 1, 100, 1000, True, False, 0, -1, 1001, '200', 200.0])
def test_operator_history_budget_is_explicit_strict_and_bounded(tmp_path, limit):
    configured = settings(tmp_path, enabled=True)
    config = configured.federation_config
    assert config is not None
    data = json.loads(config.read_text())
    if limit is not None:
        data['history_limit'] = limit
    config.write_text(json.dumps(data))
    store = MessageStore(configured.db_path)
    client = httpx.AsyncClient()
    try:
        if limit is None or type(limit) is int and 1 <= limit <= 1000:
            bridge = load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
            assert bridge.history_limit == (100 if limit is None else limit)
        else:
            with pytest.raises(ValueError):
                load_bridge(config, store, client, 'xoxb-test-secret', 'B123')
            assert all(not store.federation.verify_origin(origin_instance_id=row['instance_id'],
                       publisher_id=row['publisher_id']) for row in data['bindings'])
    finally:
        store.close()
        asyncio.run(client.aclose())


@pytest.mark.parametrize('incomplete', [False, True])
def test_maximum_history_budget_requires_all_ten_provider_pages_before_checkpoint(pair, tmp_path, incomplete):
    from dataclasses import replace

    from src.agentbus_federation_bridge_service import FederationBridge

    bridges, stores, _, _ = pair
    bridges['home'].enqueue('bounded-history-root', 'remote', bridge_tests.payload())
    wire = stores['home'].federation.obligation('home', 'bounded-history-root', 'remote')['payload']
    calls = []
    async def respond(request):
        body = json.loads(request.content)
        page = int(body.get('cursor', '0'))
        calls.append(body)
        messages = [{'type': 'message', 'app_id': 'home', 'bot_id': 'publisher',
                     'ts': f'{1000-page*100-i}.000001', 'text': 'unrelated'} for i in range(100)]
        if page == 5:
            messages[0]['text'] = wire
        more = page < 9 or incomplete
        return httpx.Response(200, json={'ok': True, 'messages': messages, 'has_more': more,
            'response_metadata': {'next_cursor': str(page+1) if more else ''}})
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            from types import SimpleNamespace

            old = bridges['remote']
            bindings = dict(old.bindings)
            bindings['home'] = replace(bindings['home'], publisher_id='home/publisher/channel')
            target = MessageStore(tmp_path / 'history-reader.sqlite3')
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            # Characterize the retained complete-history port used by ordinary
            # uncertain local posts, separately from RC6 resumable federation.
            legacy_port = SimpleNamespace(history=provider.history, publish=provider.publish)
            receiver = FederationBridge(target.federation, old.identity, bindings,
                legacy_port, history_limit=1000)
            # Observe only history; submission is separately owned and would post a receipt.
            reader = receiver.identity.instance_id + '/' + receiver.identity.publisher_id
            before = target.federation.checkpoint(reader)
            if incomplete:
                from src.agentbus_federation_bridge_service import ProviderUnavailable
                with pytest.raises(ProviderUnavailable):
                    await receiver.recover_once(limit=receiver.history_limit)
                assert target.federation.checkpoint(reader) == before
                assert target._db.execute('SELECT count(*) FROM federated_messages').fetchone()[0] == 0
            else:
                await receiver.recover_once(limit=receiver.history_limit)
                assert target.federation.checkpoint(reader) == '1000.000001'
                assert target._db.execute('SELECT count(*) FROM federated_messages').fetchone()[0] == 1
            target.close()
    asyncio.run(scenario())
    assert len(calls) == 10 and all(c['limit'] == 100 for c in calls)


class HistorySlack:
    """Slack's documented newest-first, exclusive time pagination."""

    def __init__(self, count):
        self.messages = [{'ts': f'{i}.000001', 'text': f'event-{i}',
                          'app_id': 'home', 'bot_id': 'publisher'} for i in range(1, count + 1)]
        self.calls = []

    def __call__(self, request):
        from decimal import Decimal

        body = json.loads(request.content)
        self.calls.append(body)
        assert request.url.path.endswith('conversations.history')
        selected = sorted([m for m in self.messages
            if Decimal(m['ts']) > Decimal(body['oldest'])
            and ('latest' not in body or Decimal(m['ts']) < Decimal(body['latest']))],
            key=lambda m: Decimal(m['ts']), reverse=True)
        return httpx.Response(200, json={'ok': True, 'messages': selected[:body['limit']],
            'has_more': len(selected) > body['limit']})


def test_resumable_history_drains_more_than_one_thousand_events_without_skipping():
    async def scenario():
        slack = HistorySlack(1307)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            after, observed, cycles = '0', [], 0
            complete = False
            while not complete:
                before = len(slack.calls)
                events, complete = await provider.scan_history(after, 100)
                assert len(slack.calls) - before <= 10
                assert len(events) <= 100
                cycles += 1
                assert cycles < 40
                if events:
                    observed.extend(e['text'] for e in events)
                    after = events[-1]['ts']
                else:
                    assert not complete
            assert observed == [f'event-{i}' for i in range(1, 1308)]
            assert all('cursor' not in call and call['inclusive'] is False for call in slack.calls)
    asyncio.run(scenario())


def test_restart_and_new_arrivals_cannot_skip_oldest_recovery_batch():
    async def scenario():
        slack = HistorySlack(1101)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            assert await provider.scan_history('0', 100) == ([], False)
            slack.messages.append({'ts': '1102.000001', 'text': 'new-arrival'})
            before = len(slack.calls)
            events, complete = await provider.scan_history('0', 100)
            assert events[0]['ts'] == '1.000001' and events[-1]['ts'] == '100.000001'
            assert not complete and 'latest' in slack.calls[before]
            # Lose all volatile scan state: no durable checkpoint was advanced.
            restarted = SlackFederationProvider(client, 'local-fake-token', 'channel')
            before = len(slack.calls)
            assert await restarted.scan_history('0', 100) == ([], False)
            assert 'latest' not in slack.calls[before]
            recovered, complete = await restarted.scan_history('0', 100)
            assert recovered == events and not complete
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', ['foreign', 'repeated', 'unordered', 'empty-more'])
def test_resumable_history_rejects_foreign_or_nonprogressing_page_and_rescans(failure):
    async def scenario():
        slack = HistorySlack(101)
        def respond(request):
            body = json.loads(request.content)
            if len(slack.calls) == 1:
                bad = {'ts': '1.000001', 'text': 'untrusted'}
                if failure == 'foreign':
                    bad['channel'] = 'foreign'
                elif failure == 'repeated':
                    bad['ts'] = body['latest']
                elif failure == 'unordered':
                    return httpx.Response(200, json={'ok': True, 'messages': [bad, {'ts': '1.000002'}]})
                return httpx.Response(200, json={'ok': True,
                    'messages': [] if failure == 'empty-more' else [bad],
                    'has_more': failure == 'empty-more'})
            return slack(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            with pytest.raises(ProviderUnavailable):
                await provider.scan_history('0', 100)
            assert provider._scan is None
            client._transport = httpx.MockTransport(slack)
            before = len(slack.calls)
            events, complete = await provider.scan_history('0', 100)
            assert 'latest' not in slack.calls[before]
            assert events[0]['ts'] == '1.000001' and not complete
    asyncio.run(scenario())


def test_resumable_history_transient_failure_retains_last_valid_time_page():
    async def scenario():
        slack = HistorySlack(1101)
        fail = True
        def respond(request):
            nonlocal fail
            if fail and len(slack.calls) == 3:
                fail = False
                return httpx.Response(429, headers={'Retry-After': '120'})
            return slack(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            with pytest.raises(ProviderUnavailable) as caught:
                await provider.scan_history('0', 100)
            assert caught.value.retry_after == 120
            continuation = provider._scan['latest']
            before = len(slack.calls)
            events, complete = await provider.scan_history('0', 100)
            assert slack.calls[before]['latest'] == continuation
            assert events[0]['ts'] == '1.000001' and not complete
    asyncio.run(scenario())


def test_resumable_history_deadline_retains_progress_without_returning_partial_events(monkeypatch):
    from src import agentbus_federation_slack_service as adapter

    async def scenario():
        slack = HistorySlack(201)
        delayed = False
        async def respond(request):
            nonlocal delayed
            if len(slack.calls) == 1 and not delayed:
                delayed = True
                await asyncio.sleep(1)
            return slack(request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            monkeypatch.setattr(adapter, 'HISTORY_SCAN_SECONDS', 0.01)
            with pytest.raises(ProviderUnavailable):
                await provider.scan_history('0', 100)
            assert provider._scan['latest'] == '102.000001'
            monkeypatch.setattr(adapter, 'HISTORY_SCAN_SECONDS', 15)
            events, complete = await provider.scan_history('0', 100)
            assert events[0]['ts'] == '1.000001' and events[-1]['ts'] == '100.000001'
            assert not complete
    asyncio.run(scenario())


def test_resumable_history_byte_budget_keeps_oldest_prefix(monkeypatch):
    from src import agentbus_federation_slack_service as adapter

    async def scenario():
        slack = HistorySlack(8)
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack)) as client:
            provider = SlackFederationProvider(client, 'local-fake-token', 'channel')
            # Bound retained normalized candidates independently of raw HTTP size.
            monkeypatch.setattr(adapter, 'MAX_PROVIDER_RESPONSE_BYTES', 300)
            request = provider._request
            async def unbounded_raw(method, body):
                monkeypatch.setattr(adapter, 'MAX_PROVIDER_RESPONSE_BYTES', 8_388_608)
                result = await request(method, body)
                monkeypatch.setattr(adapter, 'MAX_PROVIDER_RESPONSE_BYTES', 300)
                return result
            provider._request = unbounded_raw
            events, complete = await provider.scan_history('0', 100)
            assert not complete and 0 < len(events) < 8
            assert [e['text'] for e in events] == [f'event-{i}' for i in range(1, len(events) + 1)]
            assert sum(len(json.dumps(e, ensure_ascii=True).encode()) for e in events) <= 300
    asyncio.run(scenario())


def test_bridge_checkpoint_applies_only_oldest_admitted_prefix_after_full_scan(pair, tmp_path):
    from dataclasses import replace

    from src.agentbus_federation_bridge_service import FederationBridge

    bridges, stores, _, _ = pair
    for name in ('oldest', 'newest'):
        bridges['home'].enqueue(name, 'remote', bridge_tests.payload(name))
    slack = HistorySlack(1101)
    for index, name in ((0, 'oldest'), (1100, 'newest')):
        slack.messages[index]['text'] = stores['home'].federation.obligation('home', name, 'remote')['payload']
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(slack)) as client:
            old = bridges['remote']
            bindings = dict(old.bindings)
            bindings['home'] = replace(bindings['home'], publisher_id='home/publisher/channel')
            target = MessageStore(tmp_path / 'resumable-reader.sqlite3')
            try:
                receiver = FederationBridge(target.federation, old.identity, bindings,
                    SlackFederationProvider(client, 'local-fake-token', 'channel'))
                reader = receiver.identity.instance_id + '/' + receiver.identity.publisher_id
                await receiver.recover_once()
                assert target.federation.checkpoint(reader) == '0'
                assert not receiver._recovery_complete
                await receiver.recover_once()
                assert target.federation.checkpoint(reader) == '100.000001'
                assert target._db.execute('SELECT origin_message_id FROM federated_messages').fetchall()[0][0] == 'oldest'
                for _ in range(30):
                    await receiver.recover_once()
                    if receiver._recovery_complete:
                        break
                assert receiver._recovery_complete
                assert target.federation.checkpoint(reader) == '1101.000001'
                assert {row[0] for row in target._db.execute('SELECT origin_message_id FROM federated_messages')} == {'oldest', 'newest'}
                assert target._db.execute("SELECT count(*) FROM bridge_obligations WHERE kind='receipt'").fetchone()[0] == 2
            finally:
                target.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('version', [1, 3])
@pytest.mark.parametrize('thread', [None, '1.000001'])
def test_federated_message_presentation_preserves_mixed_peer_carrier(pair, version, thread):
    """Old peers still receive the exact text; human blocks carry the message."""
    from dataclasses import replace

    from src.agentbus_federation_ordinary_service import encode_ordinary
    from src.agentbus_federation_wire_service import decode_frame
    from src.agentbus_service import SendMessage

    bridges, _, _, bindings = pair
    origin, destination = bridges['home'].identity, bridges['remote'].identity
    bindings = {key: replace(binding, ordinary_version=3) for key, binding in bindings.items()}
    message = SendMessage(sender='home:agentbus:sender', recipient='remote:agentbus:receiver',
                          text='*Readable* & <@U123> <https://example.invalid|link>', audience='direct')
    payload = message.model_dump_json() if version == 1 else encode_ordinary(message, None)
    frame = (encode_frame(origin, 'message', 'visible-message', destination.instance_id, payload)
             if version == 1 else encode_frame(origin, 'message', 'visible-message', destination.instance_id, payload, version=3))

    async def scenario():
        posts = []
        def respond(request):
            body = json.loads(request.content);posts.append(body)
            echoed = {'text': body['text'], 'blocks': body['blocks']}
            if thread is not None:echoed['thread_ts'] = thread
            return httpx.Response(200, json={'ok': True, 'channel': 'C123', 'ts': '2.000001', 'message': echoed})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = SlackFederationProvider(client, 'isolated-token', 'C123')
            result = await provider.publish(frame) if thread is None else await provider.publish_in_thread(frame, thread)
        assert result == '2.000001' and len(posts) == 1
        posted = posts[0];assert posted['text'] == frame
        # Legacy extraction consumes text directly; no blocks or peer upgrade required.
        recovered = decode_frame(posted['text'], origin.publisher_id, destination.instance_id, bindings)
        assert recovered['payload'] == payload
        visible = json.dumps(posted['blocks'], ensure_ascii=False)
        assert '📤' in visible and message.sender in visible and message.recipient in visible
        assert '*Readable* &amp; &lt;@U123&gt; &lt;https://example.invalid|link&gt;' in visible
        assert frame not in visible and json.loads(frame)['signature'] not in visible
        assert posted['parse'] == 'none' and posted['mrkdwn'] is False
        assert not posted['unfurl_links'] and not posted['unfurl_media']
        if thread is not None:assert posted['thread_ts'] == thread and posted['reply_broadcast'] is True
    asyncio.run(scenario())


def test_federation_presentation_keeps_controls_compact_and_blocks_bounded(pair):
    from src.agentbus_federation_slack_service import carrier_blocks
    from src.agentbus_service import SendMessage

    bridges, _, _, _ = pair
    origin, destination = bridges['home'].identity, bridges['remote'].identity
    receipt = encode_frame(origin, 'receipt', 'receipt-fixture', destination.instance_id,
                           '{"private_control":"not-in-human-view"}')
    rendered = json.dumps(carrier_blocks(receipt))
    assert 'AgentBus receipt:' in rendered and 'not-in-human-view' not in rendered
    message = SendMessage(sender='home:agentbus:sender', text='&' * 6000, audience='broadcast')
    frame = encode_frame(origin, 'message', 'bounded-message', destination.instance_id, message.model_dump_json())
    blocks = carrier_blocks(frame)
    assert len(blocks) <= 50
    for block in blocks:
        if block['type'] == 'section':assert len(block['text']['text']) <= 3000
    assert sum(block.get('text', {}).get('text', '').count('&amp;') for block in blocks) == 6000
    for malformed in ('not-json', '{"kind":"message","agentbus_federation":true}',
                      '{"kind":"message","agentbus_federation":3,"payload":"bad"}'):
        assert '📤' not in json.dumps(carrier_blocks(malformed), ensure_ascii=False)
