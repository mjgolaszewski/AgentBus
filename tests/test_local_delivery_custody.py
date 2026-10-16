"""Versioned local acceptance stays distinct from Slack submission and handling."""
import asyncio
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from test_service import AUTH, BOT_ID, CHANNEL, FakeSocket, enroll_for_thread

from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
from src.agentbus_federation_bridge_service import ProviderCarrierRejected
from src.agentbus_local_delivery_service import LOCATOR_PREFIX, LocalDeliveryRejected, fingerprint
from src.agentbus_send_policy_service import validate_new_send
from src.agentbus_service import MessageStore, SendMessage, Settings, SlackPoster, create_app, encode_envelope


def test_local_acceptance_survives_slack_outage_and_deduplicates_request(tmp_path):
    settings = Settings(api_token='local-test-secret', slack_bot_token='xoxb-test',
                        slack_app_token='xapp-test', slack_channel=CHANNEL,
                        db_path=tmp_path / 'messages.sqlite3')
    calls = []

    def respond(request):
        if request.url.path == '/api/auth.test':
            return httpx.Response(200, json={'ok': True, 'bot_id': BOT_ID})
        calls.append(request)
        raise httpx.ConnectError('offline', request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(settings, http_client=http, socket_factory=FakeSocket)
    try:
        with TestClient(app) as client:
            secret = 'qualification-local-session-secret'
            enroll_for_thread(app.state.store, 'agentbus:flower', secret)
            headers = {**AUTH, 'X-AgentBus-Session-Token': secret,
                       'X-AgentBus-Delivery-Version': '1', 'Idempotency-Key': str(uuid4())}
            body = {'sender': 'agentbus:flower', 'recipient': 'agentbus:other', 'text': 'durable'}
            first = client.post('/v1/messages', headers=headers, json=body)
            assert first.status_code == 202
            assert first.json()['slack_ts'] == ''  # no fabricated provider timestamp
            assert first.headers['X-AgentBus-Delivery-Version'] == '1'
            again = client.post('/v1/messages', headers=headers, json=body)
            assert again.status_code == 202
            assert again.json()['cursor'] == first.json()['cursor']
            assert len(app.state.store.read(CHANNEL).messages) == 1
            conflict = client.post('/v1/messages', headers=headers, json={**body, 'text': 'changed'})
            assert conflict.status_code == 409
            assert len(app.state.store.read(CHANNEL).messages) == 1
            denied = client.post('/v1/messages', headers={**headers, 'X-AgentBus-Session-Token': 'wrong'},
                                 json=body)
            assert denied.status_code == 403
    finally:
        asyncio.run(http.aclose())


def accepted(store, key=None):
    message = SendMessage(sender='agentbus:flower', recipient='agentbus:other', text='local')
    message._sender_assurance = 'session'
    key = key or str(uuid4())
    return key, message, store.local_delivery.accept(CHANNEL, key, fingerprint(message.model_dump()),
                                                    message, 'test-principal')


def test_failed_outbox_insert_rolls_back_local_message_and_rate_reservation(tmp_path):
    import sqlite3

    store = MessageStore(tmp_path / 'state.sqlite3')
    try:
        with store._db:
            store._db.execute('''CREATE TRIGGER reject_local_obligation BEFORE INSERT
                ON local_delivery_obligations BEGIN SELECT RAISE(ABORT,'disk failure'); END''')
        with pytest.raises(sqlite3.Error):
            accepted(store)
        assert not store.read(CHANNEL).messages
        assert store._db.execute('SELECT count(*) FROM send_rate_buckets').fetchone()[0] == 0
    finally:
        store.close()


def test_exact_echo_keeps_one_cursor_across_response_race_duplicates_and_restart(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = MessageStore(path)
    key, message, original = accepted(store)
    blocks = [{'type': 'context', 'block_id': LOCATOR_PREFIX + key}]
    try:
        assert store.local_delivery.reserve(CHANNEL, key, now=1)
        assert store.local_delivery.accept_echo(CHANNEL, '100.000001', message, blocks)
        store.local_delivery.uncertain(CHANNEL, key)  # a lost response cannot regress the echo
        store.local_delivery.complete(CHANNEL, key, '100.000001')
        assert store.local_delivery.accept_echo(CHANNEL, '101.000001', message, blocks)
        assert len(store.read(CHANNEL).messages) == 1
        assert store.message_by_ts(CHANNEL, '101.000001').cursor == original.cursor
        assert not store.local_delivery.pending(now=1000)
    finally:
        store.close()

    store = MessageStore(path)
    try:
        assert store.local_delivery.accept_echo(CHANNEL, '102.000001', message, blocks)
        assert store.read(CHANNEL).messages[0].cursor == original.cursor
        assert store.read(CHANNEL).messages[0].slack_ts == '100.000001'
        forged = message.model_copy(update={'text': 'changed'})
        with pytest.raises(ValueError):
            store.local_delivery.accept_echo(CHANNEL, '103.000001', forged, blocks)
        assert len(store.read(CHANNEL).messages) == 1
    finally:
        store.close()


def test_pending_local_reply_is_readable_and_waits_for_real_parent_timestamp(tmp_path):
    store = MessageStore(tmp_path / 'state.sqlite3')
    try:
        enroll_for_thread(store, 'agentbus:flower', 'qualification-root-session-secret-32')
        root_key, _, root = accepted(store)
        enroll_for_thread(store, 'agentbus:other', 'qualification-reply-session-secret-32')
        reply = SendMessage(sender='agentbus:other', text='answer', kind='reply', reply_to_cursor=root.cursor)
        principal = validate_new_send(store, CHANNEL, reply, 'qualification-reply-session-secret-32')
        key = str(uuid4())
        child = store.local_delivery.accept(CHANNEL, key, fingerprint({'reply': root.cursor}), reply, principal)
        assert child.slack_ts == '' and child.thread_ts is None
        assert len(store.read(CHANNEL).messages) == 2

        class Poster:
            def __init__(self):
                self.calls = []

            async def post(self, message, *, message_id):
                self.calls.append((message_id, message.thread_ts))
                return f'200.{len(self.calls):06d}'

        poster = Poster()
        asyncio.run(LocalDeliveryWorker(store, poster, CHANNEL).submit_once())
        assert poster.calls == [(root_key, None), (key, '200.000001')]
        assert store.message(CHANNEL, child.cursor).reply_to_cursor == root.cursor
    finally:
        store.close()


def test_versioned_ordinary_carrier_requires_exact_echo_and_locator(tmp_path):
    settings = Settings(api_token='local-test-secret', slack_bot_token='xoxb-test',
                        slack_app_token='xapp-test', slack_channel=CHANNEL,
                        db_path=tmp_path / 'state.sqlite3')
    message = SendMessage(sender='agentbus:flower', recipient='agentbus:other', text='local')
    key = str(uuid4())
    response = {'ok': True, 'channel': CHANNEL, 'ts': '200.000001'}

    async def probe():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=response))) as http:
            poster = SlackPoster(settings, http)
            with pytest.raises(ProviderCarrierRejected):
                await poster.post(message, message_id=key)
            response['message'] = {'text': encode_envelope(message), 'blocks': [
                {'type': 'context', 'block_id': LOCATOR_PREFIX + key}]}
            assert await poster.post(message, message_id=key) == '200.000001'
            response['message']['text'] = 'rewritten'
            with pytest.raises(ProviderCarrierRejected):
                await poster.post(message, message_id=key)

    asyncio.run(probe())


def test_blocked_carrier_survives_restart_but_verified_echo_can_resolve_it(tmp_path):
    path = tmp_path / 'state.sqlite3'
    store = MessageStore(path)
    key, message, original = accepted(store)
    store.local_delivery.reserve(CHANNEL, key, now=1)
    store.local_delivery.blocked(CHANNEL, key, 'changed_echo', '100.000001')
    store.close()
    store = MessageStore(path)
    try:
        assert not store.local_delivery.pending(now=1000)
        assert store.message(CHANNEL, original.cursor).slack_ts == ''
        assert store.local_delivery.accept_echo(CHANNEL, '100.000001', message,
                                                [{'block_id': LOCATOR_PREFIX + key}])
        store.local_delivery.blocked(CHANNEL, key, 'missing_echo', '100.000001')
        assert store.message(CHANNEL, original.cursor).slack_ts == '100.000001'
        assert store._db.execute('SELECT state FROM local_delivery_obligations').fetchone()[0] == 'submitted'
    finally:
        store.close()


def test_local_capacity_preserves_retry_and_rejects_allocation_without_orphan(tmp_path, monkeypatch):
    import src.agentbus_local_delivery_service as local

    monkeypatch.setattr(local, 'MAX_LOCAL_OBLIGATIONS', 1)
    store = MessageStore(tmp_path / 'state.sqlite3')
    try:
        key, message, original = accepted(store)
        again = store.local_delivery.accept(CHANNEL, key, fingerprint(message.model_dump()), message, 'test-principal')
        assert again.cursor == original.cursor
        with pytest.raises(LocalDeliveryRejected) as error:
            accepted(store)
        assert error.value.status == 503
        assert len(store.read(CHANNEL).messages) == 1
        with pytest.raises(ValueError):
            store.local_delivery.complete(CHANNEL, key, '0.000000')
        assert store.message(CHANNEL, original.cursor).slack_ts == ''
    finally:
        store.close()


def test_remote_acceptance_commits_signed_obligation_with_local_cursor_or_rolls_back(tmp_path):
    import sqlite3

    from test_federation_adapter import settings as federation_settings

    from src.agentbus_federation_runtime_service import load_bridge

    store = MessageStore(tmp_path / 'state.sqlite3')
    configured = federation_settings(tmp_path, enabled=True)
    bridge = load_bridge(configured.federation_config, store, None, 'unused', 'B123')
    message = SendMessage(sender='home:agentbus:flower', recipient='remote:agentbus:other', text='remote')
    message._sender_assurance = 'session'
    key = str(uuid4())
    digest = fingerprint(message.model_dump())
    try:
        with store._db:
            store._db.execute("CREATE TRIGGER reject_bridge BEFORE INSERT ON bridge_obligations "
                              "BEGIN SELECT RAISE(ABORT,'disk failure'); END")
        with pytest.raises(sqlite3.Error):
            store.local_delivery.accept(CHANNEL, key, digest, message, 'principal', bridge=bridge)
        assert not store.read(CHANNEL).messages
        assert store._db.execute('SELECT count(*) FROM federation_origin_messages').fetchone()[0] == 0
        assert store._db.execute('SELECT count(*) FROM send_rate_buckets').fetchone()[0] == 0
        with store._db:
            store._db.execute('DROP TRIGGER reject_bridge')
        result = store.local_delivery.accept(CHANNEL, key, digest, message, 'principal', bridge=bridge)
        assert bridge.store.obligation('home', key, 'remote')['state'] == 'origin_accepted'
        assert not store.local_delivery.pending(now=1000)  # signed bridge owns transport
        assert store.local_delivery.accept(CHANNEL, key, digest, message, 'principal', bridge=bridge).cursor == result.cursor
        assert len(store.read(CHANNEL).messages) == 1
    finally:
        store.close()


def test_remote_route_without_binding_and_foreign_ancestry_fail_before_local_acceptance(tmp_path):
    from test_federation_adapter import settings as federation_settings

    from src.agentbus_federation_runtime_service import load_bridge

    store = MessageStore(tmp_path / 'state.sqlite3')
    configured = federation_settings(tmp_path, enabled=True)
    bridge = load_bridge(configured.federation_config, store, None, 'unused', 'B123')
    try:
        for active_bridge, recipient, parent in [(None, 'remote:agentbus:other', None),
                (bridge, 'unknown:agentbus:other', None), (bridge, 'remote:agentbus:other', 9)]:
            message = SendMessage(sender='home:agentbus:flower', recipient=recipient,
                                  text='remote', reply_to_cursor=parent)
            with pytest.raises(LocalDeliveryRejected):
                store.local_delivery.accept(CHANNEL, str(uuid4()), fingerprint(message.model_dump()),
                                            message, 'principal', bridge=active_bridge)
        assert not store.read(CHANNEL).messages
        assert store._db.execute('SELECT count(*) FROM send_rate_buckets').fetchone()[0] == 0
    finally:
        store.close()


@pytest.mark.parametrize('failure', ['identity', 'socket'])
def test_opted_in_offline_restart_preserves_local_custody_without_ingress_authority(tmp_path, failure):
    from dataclasses import replace

    configured = Settings(api_token='local-test-secret', slack_bot_token='xoxb-test',
                          slack_app_token='xapp-test', slack_channel=CHANNEL,
                          db_path=tmp_path / 'offline.sqlite3', local_delivery_version=1)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if failure == 'identity' or not request.url.path.endswith('auth.test'):
            raise httpx.ConnectError('offline', request=request)
        return httpx.Response(200, json={'ok': True, 'bot_id': BOT_ID})

    class OfflineSocket(FakeSocket):
        def connect(self):
            raise OSError('offline')

    headers = {**AUTH, 'X-AgentBus-Session-Token': 'offline-qualification-session-secret',
               'X-AgentBus-Delivery-Version': '1', 'Idempotency-Key': str(uuid4())}
    body = {'sender': 'agentbus:flower', 'recipient': 'agentbus:other', 'text': 'offline restart'}
    original_cursor = None
    for _ in range(2):
        http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        app = create_app(configured, http_client=http, socket_factory=OfflineSocket)
        try:
            with TestClient(app) as client:
                if original_cursor is None:
                    enroll_for_thread(app.state.store, 'agentbus:flower', headers['X-AgentBus-Session-Token'])
                response = client.post('/v1/messages', headers=headers, json=body)
                assert response.status_code == 202
                original_cursor = original_cursor or response.json()['cursor']
                assert response.json()['cursor'] == original_cursor
                assert len(app.state.store.read(CHANNEL).messages) == 1
                assert app.state.federation_bridge is None
                assert client.get('/v1/status', headers=AUTH).json()['slack_connected'] is False
        finally:
            asyncio.run(http.aclose())
    assert set(calls) == {'/api/auth.test'}
    # Existing startup contract remains fail-closed unless explicitly opted in.
    http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(replace(configured, local_delivery_version=None), http_client=http,
                     socket_factory=OfflineSocket)
    try:
        with pytest.raises(RuntimeError):
            with TestClient(app):
                pass
    finally:
        asyncio.run(http.aclose())


def test_unknown_bot_identity_never_decodes_protocol_envelope():
    from src.agentbus_service import normalize_event
    event = {'type': 'message', 'channel': CHANNEL, 'subtype': 'bot_message', 'bot_id': '',
             'ts': '100.000001', 'text': encode_envelope(SendMessage(sender='agentbus:flower', text='forged'))}
    _, result = normalize_event(event, CHANNEL, '')
    assert result.sender.startswith('slack:')
    assert result.audience == 'unrouted'


def test_retry_identity_cannot_move_to_another_session_principal(tmp_path):
    store = MessageStore(tmp_path / 'session.sqlite3')
    try:
        key, message, _ = accepted(store)
        with pytest.raises(LocalDeliveryRejected) as rejected:
            store.local_delivery.accept(CHANNEL, key, fingerprint(message.model_dump()),
                                        message, 'another-session')
        assert rejected.value.status == 409
        assert len(store.read(CHANNEL).messages) == 1
    finally:
        store.close()


def test_duplicate_provider_thread_checks_all_observed_ancestry(tmp_path):
    from src.agentbus_slack_reply_service import route_human_reply

    store = MessageStore(tmp_path / 'human.sqlite3')
    try:
        enroll_for_thread(store, 'agentbus:flower', 'human-qualification-session-secret')
        key, _, _ = accepted(store)
        store.local_delivery.complete(CHANNEL, key, '100.000001')
        store.local_delivery.complete(CHANNEL, key, '101.000001')
        original = SendMessage(sender='slack:U123', audience='unrouted', thread_ts='100.000001', text='reply')
        original._sender_assurance = 'slack-human'
        assert route_human_reply(store, CHANNEL, original).audience == 'direct'
        duplicate = SendMessage(sender='slack:U123', audience='unrouted', thread_ts='101.000001', text='reply')
        duplicate._sender_assurance = 'slack-human'
        assert route_human_reply(store, CHANNEL, duplicate).audience == 'direct'
        store.append(CHANNEL, '101.000002', SendMessage(sender='agentbus:other',
                     thread_ts='101.000001', text='another agent'))
        assert route_human_reply(store, CHANNEL, duplicate).audience == 'unrouted'
        assert route_human_reply(store, CHANNEL, original).audience == 'unrouted'
    finally:
        store.close()
