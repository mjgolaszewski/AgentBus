"""Signed bridge custody, replay, outage, and recovery behavior."""

import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.agentbus_federation_bridge_service import FederationBridge, ProviderUnavailable
from src.agentbus_federation_wire_service import (
    Binding,
    Identity,
    canonical,
    decode_frame,
    encode_frame,
    key_generation,
)
from src.agentbus_service import MessageStore


class Provider:
    def __init__(self, publisher):
        self.publisher = publisher
        self.events = []
        self.fail = False

    async def publish(self, text):
        if self.fail:
            raise TimeoutError('uncertain provider outcome')
        stamp = f'{len(self.events) + 1}.000001'
        self.events.append({'publisher_id': self.publisher, 'text': text, 'ts': stamp})
        return stamp

    async def history(self, after, limit):
        result = [item for item in self.events if float(item['ts']) > float(after)]
        if len(result) > limit:
            raise ValueError('history exceeds recovery budget')
        return result


@pytest.fixture
def pair(tmp_path):
    keys = {name: Ed25519PrivateKey.generate() for name in ['home', 'remote']}
    bindings = {name: Binding(name, name, name + '-publisher', key.public_key())
                for name, key in keys.items()}
    stores = {name: MessageStore(tmp_path / f'{name}.sqlite3') for name in keys}
    providers = {name: Provider(name + '-publisher') for name in keys}
    bridges = {name: FederationBridge(stores[name].federation,
                Identity(name, name, name + '-publisher', key), bindings, providers[name],
                now=lambda: 100.0) for name, key in keys.items()}
    yield bridges, stores, providers, bindings
    for store in stores.values():
        store.close()


def payload(text='nonce'):
    return json.dumps({'sender': 'home:agentbus:sender', 'recipient': 'remote:agentbus:receiver',
                       'audience': 'direct', 'text': text, 'kind': 'status'}, sort_keys=True)


def test_outage_keeps_durable_message_and_uncertain_obligation(pair):
    bridges, stores, providers, _ = pair
    origin = bridges['home']
    origin.enqueue('m1', 'remote', payload())
    providers['home'].fail = True
    asyncio.run(origin.submit_once())
    row = stores['home'].federation.obligation('home', 'm1', 'remote')
    assert row['state'] == 'uncertain' and row['attempts'] == 1
    assert stores['home']._db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 1
    asyncio.run(origin.submit_once())
    assert stores['home'].federation.obligation('home', 'm1', 'remote')['attempts'] == 1


def test_submission_never_fabricates_destination_acceptance(pair):
    bridges, stores, _, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    assert stores['home'].federation.obligation('home', 'm1', 'remote')['state'] == 'submitted'


@pytest.mark.parametrize('receipt_race', [False, True])
def test_carrier_failure_blocks_unchanged_retries_and_survives_restart(pair, receipt_race):
    from src.agentbus_federation_bridge_service import ProviderCarrierRejected
    from src.agentbus_federation_service import FederationStore

    bridges, stores, providers, _ = pair
    bridge = bridges['home']
    bridge.enqueue('carrier-1', 'remote', payload())
    calls = []

    async def rejected(text):
        calls.append(text)
        if receipt_race:
            bridges['remote'].receive({'publisher_id': 'home-publisher', 'text': text})
            await bridges['remote'].submit_once()
            bridge.receive(providers['remote'].events[0])
        raise ProviderCarrierRejected('changed_echo', '1.000001')

    providers['home'].publish = rejected
    asyncio.run(bridge.submit_once())
    row = stores['home'].federation.obligation('home', 'carrier-1', 'remote')
    assert row['state'] == ('destination_accepted' if receipt_race else 'provider_blocked')
    assert row['attempts'] == 1
    if not receipt_race:
        assert row['provider_failure'] == 'changed_echo' and row['provider_ref'] == '1.000001'
    reopened = FederationStore(stores['home']._db, stores['home']._lock)
    assert reopened.pending(now=100000, max_attempts=8) == []
    bridge.store, bridge.now = reopened, lambda: 100000.0
    asyncio.run(bridge.submit_once())
    assert len(calls) == 1
    if not receipt_race:
        # A later independently verified destination receipt can resolve uncertainty.
        bridges['remote'].receive({'publisher_id': 'home-publisher', 'text': calls[0]})
        asyncio.run(bridges['remote'].submit_once())
        bridge.receive(providers['remote'].events[0])
        assert reopened.obligation('home', 'carrier-1', 'remote')['state'] == 'destination_accepted'


def test_outbound_capacity_preserves_replay_and_rejects_without_orphan_origin(pair, monkeypatch):
    from src import agentbus_federation_service as persistence

    monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1)
    bridges, stores, _, _ = pair
    bridge, db = bridges['home'], stores['home']._db
    bridge.enqueue('m1', 'remote', payload())
    before = db.total_changes
    bridge.enqueue('m1', 'remote', payload())
    assert db.total_changes == before
    with pytest.raises(persistence.CustodyCapacityError):
        bridge.enqueue('m2', 'remote', payload())
    assert db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 1
    assert db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1
    monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 2)
    row = stores['home'].federation.obligation('home', 'm1', 'remote')
    local = db.execute('SELECT payload FROM federation_origin_messages').fetchone()[0]
    stores['home'].federation.accept(origin_instance_id='home', origin_message_id='m1',
        destination_instance_id='third', payload=row['payload'], local_payload=local)
    with pytest.raises(persistence.CustodyCapacityError):
        bridge.enqueue('m2', 'remote', payload())
    assert db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 1
    monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1)
    bridge.enqueue('m1', 'remote', payload())
    asyncio.run(bridge.submit_once())
    assert stores['home'].federation.obligation('home', 'm1', 'remote')['state'] == 'submitted'


def test_inbound_capacity_withholds_receipt_and_recovery_checkpoint(pair, monkeypatch):
    from src import agentbus_federation_service as persistence

    bridges, stores, providers, _ = pair
    origin, destination = bridges['home'], bridges['remote']
    origin.enqueue('m1', 'remote', payload())
    origin.enqueue('m2', 'remote', payload())
    asyncio.run(origin.submit_once())
    providers['remote'].events = providers['home'].events
    monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1)
    with pytest.raises(persistence.CustodyCapacityError):
        asyncio.run(destination.recover_once())
    assert stores['remote'].federation.checkpoint('remote/remote-publisher') == '0'
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1
    # Existing admitted custody can be replayed at capacity without another receipt.
    destination.receive(providers['home'].events[0])
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1


def test_custody_capacity_serializes_independent_sqlite_writers(tmp_path, monkeypatch):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor

    from src import agentbus_federation_service as persistence

    monkeypatch.setattr(persistence, 'MAX_CUSTODY_ROWS', 1)
    capacity = persistence.FederationStore._custody_capacity
    def slow_capacity(self, table):
        capacity(self, table)
        time.sleep(0.02)  # Expose the count/insert race without the SQLite writer reservation.
    monkeypatch.setattr(persistence.FederationStore, '_custody_capacity', slow_capacity)
    stores = [MessageStore(tmp_path / 'shared.sqlite3') for _ in range(2)]
    barrier = threading.Barrier(2)
    def allocate(index):
        barrier.wait(timeout=5)
        try:
            stores[index].federation.accept(origin_instance_id='home', origin_message_id=f'm{index}',
                destination_instance_id='remote', payload='message')
        except persistence.CustodyCapacityError:
            return 'capacity'
        return 'accepted'
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(allocate, range(2))) == ['accepted', 'capacity']
        assert stores[0]._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1
        assert stores[0]._db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 1
    finally:
        for store in stores:
            store.close()


def test_local_custody_payload_bound_cannot_bypass_frame_limit(pair):
    _, stores, _, _ = pair
    with pytest.raises(ValueError, match='local payload exceeds'):
        stores['home'].federation.accept(origin_instance_id='home', origin_message_id='oversized',
            destination_instance_id='remote', payload='frame', local_payload='x' * 48001)
    assert stores['home']._db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 0
    assert stores['home']._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 0


def test_authenticated_destination_dedup_and_signed_receipt(pair):
    bridges, stores, providers, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    event = providers['home'].events[0]
    bridges['remote'].receive(event)
    bridges['remote'].receive(event)
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM participation_sessions').fetchone()[0] == 0
    asyncio.run(bridges['remote'].submit_once())
    bridges['home'].receive(providers['remote'].events[0])
    bridges['home'].receive(providers['remote'].events[0])
    row = stores['home'].federation.obligation('home', 'm1', 'remote')
    assert row['state'] == 'destination_accepted'


def test_receipt_obligation_is_not_republished_after_success(pair):
    bridges, stores, providers, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    bridges['remote'].receive(providers['home'].events[0])
    receipt = stores['remote'].federation.obligation(
        'remote', next(row['origin_message_id'] for row in stores['remote']._db.execute(
            "SELECT origin_message_id FROM bridge_obligations WHERE kind='receipt'")), 'home')
    assert receipt['kind'] == 'receipt'
    asyncio.run(bridges['remote'].submit_once())
    assert len(providers['remote'].events) == 1
    assert stores['remote'].federation.obligation(
        'remote', receipt['origin_message_id'], 'home')['state'] == 'submitted'
    asyncio.run(bridges['remote'].submit_once())
    assert len(providers['remote'].events) == 1


def test_uncertain_receipt_retries_once_provider_recovers(pair):
    bridges, stores, providers, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    bridges['remote'].receive(providers['home'].events[0])
    receipt_id = next(row['origin_message_id'] for row in stores['remote']._db.execute(
        "SELECT origin_message_id FROM bridge_obligations WHERE kind='receipt'"))
    providers['remote'].fail = True
    asyncio.run(bridges['remote'].submit_once())
    row = stores['remote'].federation.obligation('remote', receipt_id, 'home')
    assert row['state'] == 'uncertain' and row['attempts'] == 1
    providers['remote'].fail = False
    bridges['remote'].now = lambda: 1000.0
    asyncio.run(bridges['remote'].submit_once())
    assert stores['remote'].federation.obligation('remote', receipt_id, 'home')['state'] == 'submitted'
    assert len(providers['remote'].events) == 1


def test_forged_publisher_signature_destination_and_conflicting_replay_fail_closed(pair):
    bridges, stores, providers, bindings = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    event = providers['home'].events[0]
    with pytest.raises(ValueError):
        bridges['remote'].receive(dict(event, publisher_id='foreign'))
    with pytest.raises(ValueError):
        decode_frame(event['text'], 'foreign', 'remote', bindings)
    frame = json.loads(event['text'])
    frame['payload'] = payload('changed')
    with pytest.raises(ValueError):
        bridges['remote'].receive(dict(event, text=json.dumps(frame)))
    with pytest.raises(ValueError):
        decode_frame(event['text'], 'home-publisher', 'other', bindings)
    bridges['remote'].receive(event)
    conflict = encode_frame(bridges['home'].identity, 'message', 'm1', 'remote', payload('changed'))
    with pytest.raises(ValueError, match='conflict'):
        bridges['remote'].receive(dict(event, text=conflict))
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1


def test_recovery_replays_without_skipping_or_semantic_ack(pair):
    bridges, stores, providers, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    bridges['remote'].provider = providers['home']
    asyncio.run(bridges['remote'].recover_once())
    asyncio.run(bridges['remote'].recover_once())
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1
    assert stores['home'].federation.obligation('home', 'm1', 'remote')['state'] == 'submitted'


def test_origin_nonce_conflict_and_authority_route_fail_before_publication(pair):
    bridges, _, providers, _ = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    with pytest.raises(ValueError, match='conflict'):
        bridges['home'].enqueue('m1', 'remote', payload('changed'))
    wrong = json.loads(payload())
    wrong['sender'] = 'remote:agentbus:spoof'
    with pytest.raises(ValueError):
        bridges['home'].enqueue('m2', 'remote', json.dumps(wrong))
    wrong['sender'] = 'home:ambiguous'
    with pytest.raises(ValueError):
        bridges['home'].enqueue('m2', 'remote', json.dumps(wrong))
    wrong = json.loads(payload())
    wrong['wake'] = True
    with pytest.raises(ValueError):
        bridges['home'].enqueue('m2', 'remote', json.dumps(wrong))
    assert providers['home'].events == []


def test_receipt_race_cannot_be_regressed_by_late_provider_response(pair):
    bridges, stores, providers, _ = pair
    publish = providers['home'].publish
    async def immediate_delivery(text):
        reference = await publish(text)
        bridges['remote'].receive(providers['home'].events[-1])
        await bridges['remote'].submit_once()
        bridges['home'].receive(providers['remote'].events[-1])
        return reference
    providers['home'].publish = immediate_delivery
    bridges['home'].enqueue('race', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    assert stores['home'].federation.obligation('home', 'race', 'remote')['state'] == 'destination_accepted'


def test_retry_budget_survives_restart_without_unbounded_submission(pair):
    bridges, stores, providers, bindings = pair
    bridge = bridges['home']
    bridge.max_attempts = 2
    bridge.enqueue('retry', 'remote', payload())
    providers['home'].fail = True
    asyncio.run(bridge.submit_once())
    bridge.now = lambda: 1000.0
    asyncio.run(bridge.submit_once())
    reopened = FederationBridge(stores['home'].federation, bridge.identity, bindings, providers['home'],
                                now=lambda: 10000.0, max_attempts=2)
    asyncio.run(reopened.submit_once())
    assert stores['home'].federation.obligation('home', 'retry', 'remote')['attempts'] == 2


def test_crash_before_submission_retains_custody_on_disk(pair):
    bridges, stores, providers, bindings = pair
    bridge = bridges['home']
    bridge.enqueue('crash', 'remote', payload())
    path = stores['home']._db.execute('PRAGMA database_list').fetchone()['file']
    stores['home'].close()
    from pathlib import Path
    stores['home'] = MessageStore(Path(path))
    resumed = FederationBridge(stores['home'].federation, bridge.identity, bindings, providers['home'])
    asyncio.run(resumed.submit_once())
    assert len(providers['home'].events) == 1
    assert stores['home'].federation.obligation('home', 'crash', 'remote')['state'] == 'submitted'


def test_origin_message_and_obligation_rollback_together_on_storage_failure(pair):
    bridges, stores, providers, _ = pair
    stores['home']._db.execute("""CREATE TRIGGER reject_bridge BEFORE INSERT ON bridge_obligations
        BEGIN SELECT RAISE(ABORT, 'injected custody failure'); END""")
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        bridges['home'].enqueue('failed', 'remote', payload())
    assert stores['home']._db.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 0
    assert providers['home'].events == []


def test_conflicting_legacy_obligation_cannot_commit_an_origin_message_backfill(pair):
    bridges, stores, _, _ = pair
    bridges['home'].enqueue('legacy', 'remote', payload())
    database = stores['home']._db
    database.execute('DELETE FROM federation_origin_messages')
    database.commit()
    original = stores['home'].federation.obligation('home', 'legacy', 'remote')['payload']
    with pytest.raises(ValueError, match='conflicting federation payload for obligation'):
        stores['home'].federation.accept(origin_instance_id='home', origin_message_id='legacy',
            destination_instance_id='remote', payload='conflicting wire', local_payload='different message')
    assert database.execute('SELECT COUNT(*) FROM federation_origin_messages').fetchone()[0] == 0
    assert stores['home'].federation.obligation('home', 'legacy', 'remote')['payload'] == original


def test_recovery_checkpoint_belongs_to_the_bound_provider_stream(pair):
    bridges, stores, providers, bindings = pair
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    remote = bridges['remote']
    remote.provider = providers['home']
    asyncio.run(remote.recover_once())
    reader = 'remote/remote-publisher'
    assert stores['remote'].federation.checkpoint(reader) == '1.000001'
    changed = FederationBridge(stores['remote'].federation,
        Identity('remote', 'remote', 'remote-new-channel', remote.identity.private_key),
        {'home': bindings['home']}, providers['home'])
    asyncio.run(changed.recover_once())
    assert stores['remote'].federation.checkpoint('remote/remote-new-channel') == '1.000001'
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1


def test_origin_cannot_occupy_another_origins_operator_namespace(pair):
    bridges, stores, providers, bindings = pair
    forged = Binding('remote', 'home', 'remote-publisher', bindings['remote'].public_key)
    with pytest.raises(ValueError, match='distinct publisher and route'):
        FederationBridge(stores['home'].federation, bridges['home'].identity,
                         {'remote': forged}, providers['home'])


def test_recovery_rate_limit_controls_the_worker_retry_deadline(pair, monkeypatch):
    bridges, _, providers, _ = pair
    delays = []
    async def unavailable(after, limit):
        raise ProviderUnavailable(1200)
    async def stop_after_sleep(delay):
        delays.append(delay)
        raise asyncio.CancelledError
    providers['home'].history = unavailable
    monkeypatch.setattr(asyncio, 'sleep', stop_after_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bridges['home'].run())
    assert delays == [1200]


def test_key_rotation_rejects_retired_and_false_generations_even_with_a_valid_signature(pair):
    bridges, _, _, bindings = pair
    old = bridges['home'].identity
    retired = encode_frame(old, 'message', 'm1', 'remote', payload())
    current_key = Ed25519PrivateKey.generate()
    current = Identity('home', 'home', 'home-publisher', current_key)
    bindings['home'] = Binding('home', 'home', 'home-publisher', current_key.public_key())
    with pytest.raises(ValueError, match='key generation'):
        decode_frame(retired, 'home-publisher', 'remote', bindings)
    admitted = encode_frame(current, 'message', 'm2', 'remote', payload())
    assert decode_frame(admitted, 'home-publisher', 'remote', bindings)['key_generation'] == key_generation(current_key.public_key())
    frame = json.loads(admitted)
    frame['key_generation'] = key_generation(old.private_key.public_key())
    del frame['signature']
    frame['signature'] = base64.b64encode(current_key.sign(canonical(frame).encode())).decode()
    with pytest.raises(ValueError, match='key generation'):
        decode_frame(canonical(frame), 'home-publisher', 'remote', bindings)


def test_operator_history_budget_recovers_complete_backlog_without_checkpoint_reset(pair):
    bridges, stores, providers, _ = pair
    bridges['home'].enqueue('after-offline-gap', 'remote', payload('after offline gap'))
    for i in range(101):
        providers['home'].events.append({'publisher_id': 'unrelated', 'text': 'not a federation frame', 'ts': f'{i+1}.000001'})
    asyncio.run(bridges['home'].submit_once())
    providers['remote'].events = list(providers['home'].events)
    receiver = bridges['remote']
    reader = receiver.identity.instance_id + '/' + receiver.identity.publisher_id
    before = stores['remote'].federation.checkpoint(reader)
    asyncio.run(receiver.cycle())
    assert stores['remote'].federation.checkpoint(reader) == before
    assert stores['remote']._db.execute('SELECT count(*) FROM federated_messages').fetchone()[0] == 0
    receiver.history_limit = 200
    asyncio.run(receiver.cycle())
    assert stores['remote'].federation.checkpoint(reader) == '102.000001'
    assert stores['remote']._db.execute('SELECT count(*) FROM federated_messages').fetchone()[0] == 1
    assert stores['remote']._db.execute("SELECT count(*) FROM bridge_obligations WHERE kind='receipt'").fetchone()[0] == 1


@pytest.mark.parametrize('limit', [True, False, 0, -1, 1001, '200', 200.0])
def test_recovery_rejects_noncanonical_budget_before_provider_or_checkpoint_work(pair, limit):
    bridges, stores, providers, _ = pair
    bridge = bridges['remote']
    calls = []
    async def forbidden(after, budget):
        calls.append((after, budget))
        return []
    providers['remote'].history = forbidden
    reader = bridge.identity.instance_id + '/' + bridge.identity.publisher_id
    before = stores['remote'].federation.checkpoint(reader)
    with pytest.raises(ValueError):
        asyncio.run(bridge.recover_once(limit=limit))
    assert not calls and stores['remote'].federation.checkpoint(reader) == before
