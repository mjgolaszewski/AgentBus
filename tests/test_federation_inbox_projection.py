"""Opt-in authenticated custody projection without local authority promotion."""
import asyncio
import json
import sqlite3
from functools import partial
from pathlib import Path

import pytest
from test_federation_bridge import pair as pair
from test_federation_bridge import payload

from src.agentbus_federation_bridge_service import FederationBridge
from src.agentbus_federation_inbox_service import InboxProjectionError, project_message
from src.agentbus_service import MessageStore, SendMessage


def attach(bridges, stores):
    for name, bridge in bridges.items():
        bridge.project_message = partial(project_message, stores[name], 'C123')


def test_projection_deduplicates_provider_retry_and_service_restart(pair):
    bridges, stores, providers, _ = pair
    attach(bridges, stores)
    original = json.loads(payload())
    original.update(thread_ts='99.000001', reply_to_cursor=123)
    bridges['home'].enqueue('m1', 'remote', json.dumps(original))
    asyncio.run(bridges['home'].submit_once())
    event = providers['home'].events[0]
    bridges['remote'].receive(event)
    first = stores['remote'].read('C123').messages[0]
    assert first.text == 'nonce' and first.sender == 'home:agentbus:sender'
    assert first.sender_assurance == 'legacy' and first.slack_ts == event['ts']
    assert first.thread_ts is None and first.reply_to_cursor is None
    from src.agentbus_codex_wake_events_client import _candidate
    assert _candidate({'kind': 'MESSAGE', 'id': str(first.cursor), 'message': {
        **first.model_dump(), 'actionable': True, 'action_reason': 'addressed to this identity'}},
        'remote:agentbus:receiver') is None
    assert stores['remote'].participation.roster() == []
    stored = stores['remote']._db.execute('SELECT payload FROM federated_messages').fetchone()[0]
    assert json.loads(stored)['reply_to_cursor'] == 123
    bridges['remote'].receive(dict(event, ts='2.000001'))
    assert len(stores['remote'].read('C123').messages) == 1
    path = stores['remote']._db.execute('PRAGMA database_list').fetchone()['file']
    stores['remote'].close()
    reopened = MessageStore(Path(path))
    stores['remote'] = reopened
    bridge = FederationBridge(reopened.federation, bridges['remote'].identity,
        bridges['remote'].bindings, providers['remote'])
    bridge.project_message = partial(project_message, reopened, 'C123')
    bridge.receive(dict(event, ts='3.000001'))
    assert reopened.read('C123').messages[0].cursor == first.cursor
    assert len(reopened.read('C123').messages) == 1
    assert reopened._db.execute('SELECT COUNT(*) FROM federation_inbox_projection').fetchone()[0] == 1
    asyncio.run(bridge.submit_once())
    bridges['home'].receive(providers['remote'].events[0])
    assert stores['home'].read('C123').messages == []
    assert stores['home'].federation.obligation('home', 'm1', 'remote')['state'] == 'destination_accepted'


def test_projection_failure_rolls_back_inbox_and_withholds_receipt(pair):
    bridges, stores, providers, _ = pair
    attach(bridges, stores)
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    db = stores['remote']._db
    db.execute('''CREATE TRIGGER reject_projection BEFORE INSERT ON federation_inbox_projection
        BEGIN SELECT RAISE(ABORT, 'injected projection failure'); END''')
    with pytest.raises(sqlite3.Error):
        bridges['remote'].receive(providers['home'].events[0])
    for table in ('messages', 'federation_inbox_projection', 'bridge_obligations'):
        assert db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 0
    assert db.execute('SELECT COUNT(*) FROM federated_messages').fetchone()[0] == 1
    db.execute('DROP TRIGGER reject_projection')
    bridges['remote'].receive(providers['home'].events[0])
    assert len(stores['remote'].read('C123').messages) == 1
    assert db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 1


def test_projection_never_reuses_an_occupied_local_provider_timestamp(pair):
    bridges, stores, providers, _ = pair
    attach(bridges, stores)
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    event = providers['home'].events[0]
    original = stores['remote'].append('C123', event['ts'],
        SendMessage(sender='local', recipient='reader', audience='direct', text='existing'))
    with pytest.raises(InboxProjectionError):
        bridges['remote'].receive(event)
    assert stores['remote'].read('C123').messages == [original]
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 0


@pytest.mark.parametrize('stamp', [None, '', 'NaN', '-1.0', '0.000000', 'invented-reference'])
def test_projection_requires_actual_provider_timestamp(pair, stamp):
    bridges, stores, providers, _ = pair
    attach(bridges, stores)
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    with pytest.raises(InboxProjectionError):
        bridges['remote'].receive(dict(providers['home'].events[0], ts=stamp))
    assert stores['remote'].read('C123').messages == []
    assert stores['remote']._db.execute('SELECT COUNT(*) FROM bridge_obligations').fetchone()[0] == 0


def test_projection_cannot_be_created_from_unadmitted_labels(pair):
    _, stores, _, _ = pair
    with pytest.raises(InboxProjectionError, match='authenticated custody'):
        project_message(stores['remote'], 'C123', 'home', 'invented', 'remote', payload(), '1.000001')
    assert stores['remote'].read('C123').messages == []


def test_projection_duplicate_race_across_independent_connections(pair):
    from concurrent.futures import ThreadPoolExecutor

    bridges, stores, providers, _ = pair
    attach(bridges, stores)
    bridges['home'].enqueue('m1', 'remote', payload())
    asyncio.run(bridges['home'].submit_once())
    path = Path(stores['remote']._db.execute('PRAGMA database_list').fetchone()['file'])
    second = MessageStore(path)
    other = FederationBridge(second.federation, bridges['remote'].identity,
        bridges['remote'].bindings, providers['remote'])
    other.project_message = partial(project_message, second, 'C123')
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda bridge: bridge.receive(providers['home'].events[0]),
                          [bridges['remote'], other]))
        for table in ('messages', 'federation_inbox_projection', 'bridge_obligations'):
            assert second._db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] == 1
    finally:
        second.close()
