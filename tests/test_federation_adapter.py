"""Opt-in HTTP enqueue and the real Slack adapter, using an isolated provider."""

import asyncio
import base64
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from slack_sdk.socket_mode.request import SocketModeRequest

from src.agentbus_federation_bridge_service import ProviderUnavailable
from src.agentbus_federation_runtime_service import load_bridge
from src.agentbus_federation_slack_service import SlackFederationProvider
from src.agentbus_federation_wire_service import Identity, encode_frame, key_generation
from src.agentbus_service import MessageStore, Settings, create_app
from src.agentbus_slack_socket_service import SlackReceiver

AUTH = {'Authorization': 'Bearer local-test-secret'}


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
            assert api.post('/v1/federation/messages', headers=AUTH, json=body).status_code == 503
        await client.aclose()
    asyncio.run(scenario())


def test_enqueue_requires_exact_session_and_is_durable_idempotent_and_local_only(tmp_path):
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
        joined = dict(AUTH, **{'X-AgentBus-Session-Token': secret})
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


@pytest.mark.parametrize('storage_failure', [False, True])
def test_existing_socket_acknowledges_signed_frames_only_after_durable_custody(tmp_path, storage_failure):
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
    if storage_failure:
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
