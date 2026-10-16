"""Own signed carriers resolve accepted cursors without inventing remote custody."""
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import test_federation_bridge as bridge_tests

from src.agentbus_local_delivery_service import fingerprint
from src.agentbus_service import SendMessage

CHANNEL = 'CQUAL'
pair = bridge_tests.pair


def accept_remote(bridges, stores):
    message = SendMessage(sender='home:agentbus:sender', recipient='remote:agentbus:receiver', text='ordinary')
    message._sender_assurance = 'session'
    key = str(uuid4())
    accepted = stores['home'].local_delivery.accept(CHANNEL, key, fingerprint(message.model_dump()),
                                                  message, 'principal', bridge=bridges['home'])
    return key, accepted


def test_signed_submission_materializes_existing_cursor_without_destination_receipt(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store = bridges['home'], stores['home']
    assert bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    asyncio.run(bridge.submit_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == '1.000001'
    assert len(store.read(CHANNEL).messages) == 1
    assert store.federation.obligation('home', key, 'remote')['state'] == 'submitted'
    assert len(providers['home'].events) == 1


def test_lost_signed_response_is_observed_from_history_after_restart(pair):
    from src.agentbus_federation_bridge_service import FederationBridge
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    from src.agentbus_service import MessageStore
    bridges, stores, providers, _ = pair
    old, store, provider = bridges['home'], stores['home'], providers['home']
    bind_ordinary_delivery(old, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    publish = provider.publish
    async def lost(text):
        await publish(text)
        raise TimeoutError('response lost after commit')
    provider.publish = lost
    asyncio.run(old.submit_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''
    # The fixture's store path is recoverable from SQLite without manufacturing identities.
    path = store._db.execute('PRAGMA database_list').fetchone()[2]
    store.close()
    stores['home'] = store = MessageStore(Path(path))
    bridge = FederationBridge(store.federation, old.identity, old.bindings, provider, now=old.now)
    bridges['home'] = bridge
    bind_ordinary_delivery(bridge, store, CHANNEL)
    asyncio.run(bridge.recover_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == '1.000001'
    assert store.federation.obligation('home', key, 'remote')['state'] == 'submitted'
    assert store.federation.obligation('home', key, 'remote')['attempts'] == 1
    assert len(provider.events) == 1 and len(store.read(CHANNEL).messages) == 1


def test_own_observation_rejects_wrong_publisher_or_changed_frame(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store, provider = bridges['home'], stores['home'], providers['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    frame = store.federation.obligation('home', key, 'remote')['payload']
    for event in [{'publisher_id': 'forged-publisher', 'text': frame, 'ts': '1.000001'},
                  {'publisher_id': provider.publisher, 'text': frame.replace('ordinary', 'changed'), 'ts': '1.000001'}]:
        with pytest.raises(ValueError):
            bridge.receive(event)
        assert store.message(CHANNEL, accepted.cursor).slack_ts == ''
    bridge.receive({'publisher_id': provider.publisher, 'text': frame, 'ts': '1.000001'})
    assert store.message(CHANNEL, accepted.cursor).slack_ts == '1.000001'
    assert store.federation.obligation('home', key, 'remote')['state'] == 'submitted'


def test_frame_binding_failure_rolls_back_message_rate_and_bridge_custody(pair):
    import sqlite3

    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, _, _ = pair
    store = stores['home']
    bind_ordinary_delivery(bridges['home'], store, CHANNEL)
    with store._db:
        store._db.execute("""CREATE TRIGGER reject_frame_binding BEFORE UPDATE OF federation_frame_hash
            ON local_delivery_obligations BEGIN SELECT RAISE(ABORT,'frame binding failed'); END""")
    with pytest.raises(sqlite3.Error):
        accept_remote(bridges, stores)
    assert not store.read(CHANNEL).messages
    for table in ('bridge_obligations', 'federation_origin_messages', 'send_rate_buckets'):
        assert store._db.execute('SELECT count(*) FROM ' + table).fetchone()[0] == 0


def test_legacy_unbound_federated_rows_are_not_backfilled_from_matching_text(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store = bridges['home'], stores['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    frame = store.federation.obligation('home', key, 'remote')['payload']
    with store._db:
        store._db.execute('UPDATE local_delivery_obligations SET federation_frame_hash=NULL WHERE request_id=?', (key,))
    bridge.receive({'publisher_id': providers['home'].publisher, 'text': frame, 'ts': '1.000001'})
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''
    assert len(store.read(CHANNEL).messages) == 1


def test_ordinary_submission_projection_does_not_relabel_other_channel(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, _, _ = pair
    bridge, store = bridges['home'], stores['home']
    bridge.channel = 'COTHER'
    assert bind_ordinary_delivery(bridge, store, CHANNEL) is False
    _, accepted = accept_remote(bridges, stores)
    asyncio.run(bridge.submit_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''


def test_store_owned_frame_column_migration_preserves_legacy_rows_and_unknown_columns(tmp_path):
    from src.agentbus_service import MessageStore
    path = tmp_path / 'legacy.sqlite3'
    store = MessageStore(path)
    message = SendMessage(sender='agentbus:sender', recipient='agentbus:receiver', text='legacy queued')
    key = str(uuid4())
    accepted = store.local_delivery.accept(CHANNEL, key, fingerprint(message.model_dump()), message, 'principal')
    columns = ['channel','request_id','request_hash','local_cursor','state','attempts','next_attempt',
               'provider_ref','provider_failure','failure_ref']
    row = store._db.execute('SELECT * FROM local_delivery_obligations').fetchone()
    original = {name: row[name] for name in columns}
    with store._db:
        store._db.execute('DROP TABLE local_delivery_obligations')
        store._db.execute("""CREATE TABLE local_delivery_obligations (
            channel TEXT NOT NULL,request_id TEXT NOT NULL,request_hash TEXT NOT NULL,
            local_cursor INTEGER NOT NULL UNIQUE REFERENCES messages(cursor),state TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,provider_ref TEXT,
            provider_failure TEXT,failure_ref TEXT,retained_marker TEXT DEFAULT 'legacy-preserve',
            PRIMARY KEY(channel,request_id))""")
        store._db.execute('INSERT INTO local_delivery_obligations (' + ','.join(columns) + ') VALUES (' +
                          ','.join('?' for _ in columns) + ')', tuple(original[name] for name in columns))
    store.close()
    store = MessageStore(path)
    try:
        migrated = store._db.execute('SELECT * FROM local_delivery_obligations').fetchone()
        assert {name: migrated[name] for name in columns} == original
        assert migrated['federation_frame_hash'] is None
        assert migrated['retained_marker'] == 'legacy-preserve'
        assert store.message(CHANNEL, accepted.cursor).text == 'legacy queued'
    finally:
        store.close()


def test_malformed_deep_own_carrier_fails_as_invalid_input_without_state_change(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store = bridges['home'], stores['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    text = '{"agentbus_federation":1,"payload":' + '[' * 1200 + '0' + ']' * 1200 + '}'
    previous_limit = sys.getrecursionlimit()
    try:
        sys.setrecursionlimit(1000)
        with pytest.raises(ValueError):
            bridge.receive({'publisher_id': providers['home'].publisher, 'text': text, 'ts': '1.000001'})
    finally:
        sys.setrecursionlimit(previous_limit)
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''
    assert store.federation.obligation('home', key, 'remote')['state'] == 'origin_accepted'


def test_owned_carrier_recovery_does_not_depend_on_shared_checkpoint(pair):
    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store, provider = bridges['home'], stores['home'], providers['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    with store._db:
        store._db.execute("UPDATE messages SET received_at='1970-01-01T00:00:00+00:00' WHERE cursor=?", (accepted.cursor,))
    publish = provider.publish
    async def lost(text):
        await publish(text)
        raise TimeoutError('response lost')
    provider.publish = lost
    asyncio.run(bridge.submit_once())
    reader = bridge.identity.instance_id + '/' + bridge.identity.publisher_id
    store.federation.advance_checkpoint(reader, '9.000001')
    asyncio.run(bridge.recover_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''
    class Poster:
        async def observed_messages(self, after, limit):
            return await provider.history(after, limit)
        async def post(self, message, *, message_id):
            raise AssertionError('federated observation must not use ordinary posting')
    worker = LocalDeliveryWorker(store, Poster(), CHANNEL)
    worker.trusted_bot_id = 'BQUAL'
    worker.federation = bridge
    asyncio.run(worker.submit_once())
    assert store.message(CHANNEL, accepted.cursor).slack_ts == '1.000001'
    assert store.federation.checkpoint(reader) == '9.000001'
    assert store.federation.obligation('home', key, 'remote')['attempts'] == 1
    assert len(provider.events) == 1


def test_federated_history_failure_defers_read_only_recovery_without_retry_burn(pair):
    import time

    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, _, _ = pair
    bridge, store = bridges['home'], stores['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    calls = []
    class Poster:
        async def observed_messages(self, after, limit):
            calls.append((after, limit))
            raise OSError('provider offline')
        async def post(self, message, *, message_id):
            raise AssertionError('bridge owns submission')
    worker = LocalDeliveryWorker(store, Poster(), CHANNEL)
    worker.trusted_bot_id, worker.federation = 'BQUAL', bridge
    asyncio.run(worker.submit_once())
    assert not calls  # Unattempted frames need no lost-response recovery.
    assert store.federation.reserve_submission('home', key, 'remote', now=0, delay=60, max_attempts=8)
    before = time.time()
    asyncio.run(worker.submit_once())
    asyncio.run(worker.submit_once())
    row = store._db.execute('SELECT * FROM local_delivery_obligations WHERE request_id=?', (key,)).fetchone()
    assert len(calls) == 1 and row['next_attempt'] >= before + 60
    assert store.federation.obligation('home', key, 'remote')['attempts'] == 1
    assert store.message(CHANNEL, accepted.cursor).slack_ts == ''


def test_duplicate_owned_carrier_observations_preserve_cursor_and_destination_ack(pair):
    from src.agentbus_federation_inbox_service import bind_ordinary_delivery
    bridges, stores, providers, _ = pair
    bridge, store, provider = bridges['home'], stores['home'], providers['home']
    bind_ordinary_delivery(bridge, store, CHANNEL)
    key, accepted = accept_remote(bridges, stores)
    asyncio.run(bridge.submit_once())
    with store._db:
        store._db.execute("UPDATE bridge_obligations SET state='acknowledged' WHERE origin_message_id=?", (key,))
    event = dict(provider.events[0], ts='2.000001')
    bridge.receive(event)
    bridge.receive(event)
    assert store.message(CHANNEL, accepted.cursor).slack_ts == '1.000001'
    assert store.local_delivery.observation_cursor(CHANNEL, '2.000001') == accepted.cursor
    assert len(store.read(CHANNEL).messages) == 1
    assert store.federation.obligation('home', key, 'remote')['state'] == 'acknowledged'
