"""Acknowledged outbound archival preserves replay identity and blocks unsafe rows."""
import hashlib
import json

import pytest

from src.agentbus_federation_retirement_service import FederationRetirement
from src.agentbus_service import MessageStore


@pytest.fixture
def custody(tmp_path):
    store = MessageStore(tmp_path / 'custody.sqlite3')
    yield store
    store.close()


def settled(store, key='old', state='acknowledged'):
    store.federation.accept(origin_instance_id='home', origin_message_id=key,
        destination_instance_id='peer', payload='{"retained":"wire"}', local_payload='{"retained":"origin"}')
    store._db.execute('UPDATE bridge_obligations SET state=?,updated_at=? WHERE origin_message_id=?',
                      (state, '2020-01-01T00:00:00+00:00', key))
    store._db.commit()


def test_archive_frees_active_rows_but_preserves_exact_retry_and_conflict(custody):
    settled(custody)
    owner = FederationRetirement(custody._db)
    before = custody._db.total_changes
    plan = owner.preview(now=1_700_000_000)
    assert custody._db.total_changes == before and len(plan['eligible']) == 1
    result = owner.apply(plan)
    assert result['archived'] == 1
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 0
    retry = custody.federation.accept(origin_instance_id='home', origin_message_id='old',
        destination_instance_id='peer', payload='{"retained":"wire"}', local_payload='{"retained":"origin"}')
    assert retry['state'] == 'acknowledged' and retry['retired'] is True
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 0
    with pytest.raises(ValueError, match='conflicting'):
        custody.federation.accept(origin_instance_id='home', origin_message_id='old',
            destination_instance_id='peer', payload='changed', local_payload='{"retained":"origin"}')


def test_unsettled_recent_and_changed_preview_stay_pinned(custody):
    settled(custody, 'unknown', 'uncertain')
    settled(custody, 'destination', 'destination_accepted')
    settled(custody)
    owner = FederationRetirement(custody._db)
    plan = owner.preview(now=1_700_000_000)
    assert len(plan['eligible']) == 1 and plan['pinned']['unsettled'] == 2
    custody._db.execute("UPDATE bridge_obligations SET updated_at='2026-01-01' WHERE origin_message_id='old'")
    custody._db.commit()
    with pytest.raises(ValueError, match='preview'):
        owner.apply(plan)
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 3


def test_corrupt_archive_never_becomes_absence(custody):
    settled(custody)
    owner = FederationRetirement(custody._db)
    owner.apply(owner.preview(now=1_700_000_000))
    custody._db.execute("UPDATE federation_retired_obligations SET payload=x'00'")
    custody._db.commit()
    with pytest.raises(ValueError, match='archive'):
        custody.federation.obligation('home', 'old', 'peer')


def test_preview_scope_and_digest_cannot_be_substituted(custody):
    settled(custody)
    owner = FederationRetirement(custody._db)
    plan = owner.preview(now=1_700_000_000)
    forged = {**plan, 'scope': 'foreign'}
    forged['digest'] = hashlib.sha256(json.dumps({k:v for k,v in forged.items() if k != 'digest'},
        sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    with pytest.raises(ValueError, match='scope'):
        owner.apply(forged)
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 1


def test_automatic_compaction_is_bounded_and_keeps_uncertain_custody(custody):
    from src.agentbus_federation_retirement_service import AutomaticFederationCompaction
    settled(custody, 'unknown', 'uncertain')
    settled(custody)
    clock = [1_700_000_000]
    worker = AutomaticFederationCompaction(custody, now=lambda: clock[0])
    report = worker.tick()
    assert report['archived'] == 1
    assert custody.federation.obligation('home', 'unknown', 'peer')['state'] == 'uncertain'
    assert custody.federation.obligation('home', 'old', 'peer')['retired']
    before = custody._db.total_changes
    assert worker.tick() is None
    assert custody._db.total_changes == before
    clock[0] += 3600
    assert worker.tick()['archived'] == 0


def test_automatic_compaction_preserves_recent_rows(custody):
    from datetime import datetime, timezone

    from src.agentbus_federation_retirement_service import AutomaticFederationCompaction
    settled(custody)
    custody._db.execute('UPDATE bridge_obligations SET updated_at=?',
                        (datetime.now(timezone.utc).isoformat(),))
    custody._db.commit()
    assert AutomaticFederationCompaction(custody).tick()['archived'] == 0
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 1


def test_compaction_batch_does_not_skip_eligible_rows(custody):
    from src.agentbus_federation_retirement_service import AutomaticFederationCompaction
    for index in range(105):
        settled(custody, f'old-{index:03}')
    clock = [1_700_000_000]
    worker = AutomaticFederationCompaction(custody, now=lambda: clock[0])
    assert worker.tick()['archived'] == 100
    clock[0] += 3600
    assert worker.tick()['archived'] == 5
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 0


def test_compaction_transaction_failure_preserves_all_originals(custody):
    import sqlite3
    settled(custody, 'a')
    settled(custody, 'b')
    custody._db.execute("""CREATE TRIGGER deny_second_archive BEFORE INSERT ON federation_retired_obligations
        WHEN NEW.origin_message_id='b' BEGIN SELECT RAISE(ABORT,'injected storage failure'); END""")
    custody._db.commit()
    owner = FederationRetirement(custody._db)
    with pytest.raises(sqlite3.IntegrityError, match='storage failure'):
        owner.apply(owner.preview(now=1_700_000_000))
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 2
    assert custody._db.execute('SELECT count(*) FROM federation_retired_obligations').fetchone()[0] == 0


def test_archive_capacity_exhaustion_preserves_original_custody(custody, monkeypatch):
    from src import agentbus_federation_retirement_service as retirement
    settled(custody)
    monkeypatch.setattr(retirement, 'MAX_ROWS', 0)
    owner = FederationRetirement(custody._db)
    with pytest.raises(ValueError, match='capacity exhausted'):
        owner.apply(owner.preview(now=1_700_000_000))
    assert custody.federation.capacity()['rows']['bridge_obligations'] == 1
    assert custody._db.execute('SELECT count(*) FROM federation_retired_obligations').fetchone()[0] == 0


def test_offline_preview_apply_and_database_lease(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    from src.agentbus_channel_migration_service import database_lease
    path = tmp_path / 'offline.sqlite3'
    store = MessageStore(path)
    settled(store)
    store.close()
    plan = tmp_path / 'plan.json'
    command = [sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/federation_retention.py'),
               '--database', str(path), '--plan', str(plan)]
    with database_lease(path):
        blocked = subprocess.run(command, capture_output=True, text=True)
        assert blocked.returncode != 0 and not plan.exists()
    preview = subprocess.run(command, capture_output=True, text=True)
    assert preview.returncode == 0, preview.stderr
    assert plan.stat().st_mode & 0o777 == 0o600
    result = subprocess.run([*command, '--apply'], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['archived'] == 1
    store = MessageStore(path)
    try:
        assert store.federation.obligation('home', 'old', 'peer')['retired'] is True
    finally:
        store.close()
