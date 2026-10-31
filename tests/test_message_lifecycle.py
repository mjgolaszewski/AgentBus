"""Recipient expiry and replay retirement never manufacture delivery or handling."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from src.agentbus_message_lifecycle_service import LifecyclePolicy, LifecycleRejected
from src.agentbus_service import MessageStore, SendMessage


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr("src.agentbus_message_lifecycle_service.time.time", lambda: 100.0)
    store = MessageStore(tmp_path / "lifecycle.sqlite3")
    store.channels.configure(("C111", "C222"))
    policy = LifecyclePolicy(
        pickup_seconds=10,
        work_lease_seconds=20,
        epoch_seconds=30,
        replay_seconds=60,
        archive_seconds=90,
        retention_seconds=120,
    )
    store.lifecycle.activate_fresh(policy, instant=100)
    store.participation.set_policy(
        scope="global",
        scope_key="*",
        actor="operator",
        values={
            "initial_interval_seconds": 60,
            "backoff_factor": 2,
            "max_interval_seconds": 1920,
            "control_check_max_seconds": 60,
            "jitter_fraction": 0,
            "overdue_grace_seconds": 120,
            "presentation_budget_bytes": None,
        },
    )
    principals = {}
    for name, channel in [("sender", "C111"), ("recipient", "C111"), ("foreign", "C222")]:
        route = "home:agentbus:" + name
        secret = ("lifecycle-private-" + name).ljust(40, "x")
        grant = store.channels.issue_grant(channel=channel, repo="agentbus", route=route, actor="operator")
        chat, session, revision = store.channels.enroll(
            grant, repo="agentbus", route=route, display_name=name, session_secret=secret
        )
        store.participation.acknowledge_policy(session, secret, revision)
        principals[name] = store.channels.principal(secret, route)
    try:
        yield store, principals
    finally:
        store.close()


def message(store, sender, recipient):
    body = SendMessage(sender=sender.route, recipient=recipient.route, text="pickup deadline", kind="request")
    body._sender_assurance = "session"
    return store.append(sender.channel, str(uuid4()), body)


def test_unclaimed_deadline_is_terminal_and_not_a_read_ack(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    assert store.lifecycle.target(p["recipient"], m.cursor, 109)["state"] == "unclaimed"
    assert store.lifecycle.target(p["recipient"], m.cursor, 110)["deadline_elapsed"] is True
    with pytest.raises(LifecycleRejected) as error:
        store.lifecycle.pickup(p["recipient"], m.cursor, str(uuid4()), 110)
    assert error.value.status == 410
    store.lifecycle.settle(110)
    assert store.lifecycle.target(p["recipient"], m.cursor, 110)["pickup_session"] is None
    assert not store.lifecycle.actionable("C111", m.cursor, p["recipient"].chat_id, 110)


def test_dashboard_counts_are_scoped_and_do_not_settle_or_ack(lifecycle):
    store, p = lifecycle
    message(store, p["sender"], p["recipient"])
    before = store._db.total_changes
    status = store.lifecycle.status("C111")
    assert status["recipients"] == {"unclaimed": 1}
    assert status["retained_payloads"] == 1 and status["legacy_unknown_pinned"] == 0
    assert status["archived_payloads"] == 0 and status["archive_bytes"] == 0
    assert store.lifecycle.status("C222")["recipients"] == {}
    assert store._db.total_changes == before


def test_unknown_legacy_uuid_cannot_allocate_after_cutover(lifecycle):
    store, p = lifecycle
    with store._lock, store._db, pytest.raises(LifecycleRejected) as error:
        store.lifecycle.validate_request("C111", str(uuid4()), None, instant=100)
    assert error.value.status == 426


def test_operator_reconcile_preview_scope_and_exact_retry(lifecycle):
    store, p = lifecycle
    message(store, p["sender"], p["recipient"])
    message(store, p["foreign"], p["foreign"])
    before = store._db.total_changes
    preview = store.lifecycle.reconcile("C111", actor="operator", instant=111)
    assert preview["expired_unclaimed"] == 1 and not preview["applied"]
    assert store._db.total_changes == before
    applied = store.lifecycle.reconcile("C111", actor="operator", request_key="settle-1", apply=True, instant=111)
    assert applied["expired_unclaimed"] == 1 and applied["applied"]
    assert store.lifecycle.status("C222")["recipients"] == {"unclaimed": 1}
    assert (
        store.lifecycle.reconcile("C111", actor="operator", request_key="settle-1", apply=True, instant=112) == applied
    )
    assert store._db.execute("SELECT count(*) FROM lifecycle_operations").fetchone()[0] == 1
    with pytest.raises(LifecycleRejected):
        store.lifecycle.reconcile("C222", actor="operator", request_key="settle-1", apply=True, instant=112)


def test_archive_requires_terminal_work_and_preserves_channel_scoped_reads(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    assert store.retention.archive("C111", actor="operator", instant=200)["eligible"] == 0
    store.lifecycle.settle(200)
    before = store._db.total_changes
    assert store.retention.archive("C111", actor="operator", instant=200)["eligible"] == 1
    assert store._db.total_changes == before
    result = store.retention.archive("C111", actor="operator", request_key="archive-1", apply=True, instant=200)
    assert result["eligible"] == 1
    assert store._db.execute("SELECT payload FROM messages WHERE cursor=?", (m.cursor,)).fetchone()[0] == "null"
    assert store.message("C111", m.cursor).model_dump() == m.model_dump()
    assert store.retention.archive("C111", actor="operator", request_key="archive-1", apply=True, instant=201) == result
    with pytest.raises(ValueError):
        store.retention.payload("C222", m.cursor, "null")
    with store._db:
        store._db.execute("UPDATE lifecycle_archive SET sha256='forged' WHERE cursor=?", (m.cursor,))
    with pytest.raises(ValueError, match="integrity"):
        store.message("C111", m.cursor)


def test_archive_failure_rolls_back_hot_payload_and_blocks_live_ancestry(lifecycle):
    import sqlite3

    store, p = lifecycle
    root = message(store, p["sender"], p["recipient"])
    child = SendMessage(
        sender=p["sender"].route,
        recipient=p["recipient"].route,
        text="child",
        kind="request",
        reply_to_cursor=root.cursor,
    )
    child._sender_assurance = "session"
    child = store.append("C111", str(uuid4()), child)
    store.lifecycle.settle(200)
    assert store.retention.archive("C111", actor="operator", instant=200)["eligible"] == 1
    with store._db:
        store._db.execute(
            "CREATE TRIGGER fail_archive BEFORE UPDATE OF payload ON messages "
            "BEGIN SELECT RAISE(ABORT,'archive interrupted'); END"
        )
    with pytest.raises(sqlite3.Error):
        store.retention.archive("C111", actor="operator", request_key="interrupted", apply=True, instant=200)
    assert store._db.execute("SELECT count(*) FROM lifecycle_archive").fetchone()[0] == 0
    assert store.message("C111", child.cursor).text == "child"
    assert store.message("C111", root.cursor).text == "pickup deadline"


def test_purge_reclaims_closed_retry_history_without_cursor_or_ingress_reuse(lifecycle):
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(sender=p["sender"].route, recipient=p["recipient"].route, text="retire safely", kind="request")
    body._sender_assurance = "session"
    key = str(uuid4())
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    accepted = store.local_delivery.accept(
        "C111", key, fingerprint(body.model_dump()), body, "original-session", epoch=epoch
    )
    store.local_delivery.complete("C111", key, "100.000001")
    store.lifecycle.settle(111)
    store.retention.archive("C111", actor="operator", request_key="archive-safe", apply=True, instant=200)
    before = store._db.total_changes
    assert store.retention.purge("C111", actor="operator", instant=221)["eligible"] == 1
    assert store._db.total_changes == before
    result = store.retention.purge("C111", actor="operator", request_key="purge-safe", apply=True, instant=221)
    assert result["eligible"] == 1 and store.message("C111", accepted.cursor) is None
    assert store.channels.high_water("C111") == 1 and store.channels.high_water("C222") == 0
    assert store.local_delivery.capacity()["retained_requests"] == 0
    assert not store.retention.accepts_ingress("C111", "100.000001")
    assert store.retention.accepts_ingress("C222", "100.000001")
    assert store.retention.purge("C111", actor="operator", request_key="purge-safe", apply=True, instant=222) == result
    with pytest.raises(LifecycleRejected) as expired:
        store.local_delivery.accept("C111", key, fingerprint(body.model_dump()), body, "original-session", epoch=epoch)
    assert expired.value.status == 410
    newer = message(store, p["sender"], p["recipient"])
    assert store.channels.public_cursor("C111", newer.cursor) == 2
    assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_purge_refuses_unknown_schema_references_and_rolls_back_failure(lifecycle):
    import sqlite3

    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    store.lifecycle.settle(200)
    store.retention.archive("C111", actor="operator", request_key="archive-extra-ref", apply=True, instant=200)
    with store._db:
        store._db.execute("CREATE TABLE future_custody (message_id INTEGER REFERENCES messages(cursor))")
        store._db.execute("INSERT INTO future_custody VALUES (?)", (m.cursor,))
    assert store.retention.purge("C111", actor="operator", instant=221)["eligible"] == 0
    with store._db:
        store._db.execute("DELETE FROM future_custody")
        store._db.execute(
            "CREATE TRIGGER interrupted_purge BEFORE DELETE ON messages BEGIN SELECT RAISE(ABORT,'interrupted'); END"
        )
    with pytest.raises(sqlite3.Error):
        store.retention.purge("C111", actor="operator", request_key="failed-purge", apply=True, instant=221)
    assert store.message("C111", m.cursor).text == m.text
    assert store._db.execute("SELECT count(*) FROM lifecycle_archive").fetchone()[0] == 1
    assert store.channels.public_cursor("C111", m.cursor) == 1


@pytest.mark.parametrize("already_attempted", [False, True])
def test_expired_targets_never_post_and_unknown_submission_stays_unresolved(lifecycle, already_attempted):
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(
        sender=p["sender"].route, recipient=p["recipient"].route, text="deadline before provider", kind="request"
    )
    body._sender_assurance = "session"
    key = str(uuid4())
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    accepted = store.local_delivery.accept(
        "C111", key, fingerprint(body.model_dump()), body, "exact-session", epoch=epoch
    )
    if already_attempted:
        assert store.local_delivery.reserve("C111", key, now=100)
        store.local_delivery.uncertain("C111", key)
    assert not store.local_delivery.reserve("C111", key, now=200)
    row = store._db.execute(
        "SELECT state,attempts FROM local_delivery_obligations WHERE request_id=?", (key,)
    ).fetchone()
    assert row["state"] == ("expired_uncertain" if already_attempted else "expired_unsubmitted")
    assert row["attempts"] == int(already_attempted)
    assert store.local_delivery.capacity()["outstanding"] == int(already_attempted)
    assert store.lifecycle.target(p["recipient"], accepted.cursor, 200)["state"] == "expired_unclaimed"
    assert bool(store.local_delivery.recovery_pending("C111", now=200)) == already_attempted


def test_storage_pressure_rejects_new_custody_but_preserves_exact_retry_and_receipt_recovery(lifecycle):
    from dataclasses import replace

    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(
        sender=p["sender"].route, recipient=p["recipient"].route, text="stored before pressure", kind="request"
    )
    body._sender_assurance = "session"
    key = str(uuid4())
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    accepted = store.local_delivery.accept("C111", key, fingerprint(body.model_dump()), body, "session", epoch=epoch)
    with store._db:
        store._db.execute("CREATE TABLE unrelated_footprint (data BLOB)")
        store._db.execute("INSERT INTO unrelated_footprint VALUES (zeroblob(2097152))")
    store.lifecycle.policy = replace(store.lifecycle.policy, storage_bytes=1048576)
    assert store.retention.storage()["accounted_bytes"] > 1048576
    with pytest.raises(LifecycleRejected) as pressure:
        store.local_delivery.accept("C111", str(uuid4()), fingerprint(body.model_dump()), body, "session", epoch=epoch)
    assert pressure.value.status == 503
    assert (
        store.local_delivery.accept("C111", key, fingerprint(body.model_dump()), body, "session", epoch=epoch).cursor
        == accepted.cursor
    )
    token = str(uuid4())
    store.lifecycle.pickup(p["recipient"], accepted.cursor, token, 105)
    assert store.lifecycle.finish(p["recipient"], accepted.cursor, token, 106)["state"] == "handled"
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations").fetchone()[0] == 1


def test_claim_has_original_deadline_and_recovery_cannot_transfer_owned_work(lifecycle):
    store, p = lifecycle
    body = SendMessage(sender="slack:U123", recipient="all", text="unrouted question", audience="unrouted")
    body._sender_assurance = "slack-human"
    m = store.append("C111", "100.000002", body)
    store.claim("C111", m.cursor, p["recipient"].route)
    target = store.lifecycle.target(p["recipient"], m.cursor)
    assert target["state"] == "unclaimed" and target["deadline"] == 110
    store.lifecycle.pickup(p["recipient"], m.cursor, str(uuid4()), 105)
    with pytest.raises(LifecycleRejected):
        store.claim_recovery.reassign(
            channel="C111", cursor=m.cursor, new_identity=p["sender"].route, actor="operator", reason="unsafe transfer"
        )
    assert store.claimant("C111", m.cursor) == p["recipient"].route
    store.lifecycle.settle(126)
    assert store.inbox("C111", p["recipient"].route).messages[0].actionable is False


def test_expired_unrouted_claim_is_not_resurrected(lifecycle):
    store, p = lifecycle
    body = SendMessage(sender="slack:U123", recipient="all", text="old question", audience="unrouted")
    body._sender_assurance = "slack-human"
    m = store.append("C111", "100.000003", body)
    store.lifecycle.settle(111)
    with pytest.raises(LifecycleRejected):
        store.claim("C111", m.cursor, p["recipient"].route)
    assert store.claimant("C111", m.cursor) is None


def test_handling_batch_is_atomic_and_never_relabels_expiry_as_ack(lifecycle):
    store, p = lifecycle
    own = message(store, p["sender"], p["recipient"])
    foreign = message(store, p["foreign"], p["foreign"])
    token = str(uuid4())
    with pytest.raises(LifecycleRejected):
        store.lifecycle.handle_batch(p["recipient"], [(own.cursor, token), (foreign.cursor, str(uuid4()))])
    assert store.lifecycle.target(p["recipient"], own.cursor)["state"] == "unclaimed"
    store.lifecycle.settle(111)
    result = store.lifecycle.handle_batch(p["recipient"], [(own.cursor, token)])
    assert result == {"handled": 0, "already_handled": 0, "expired_unclaimed": 1}
    assert store.lifecycle.target(p["recipient"], own.cursor)["pickup_token"] is None


def test_stopped_batch_can_finish_owned_work_but_not_pick_up_more(lifecycle):
    store, p = lifecycle
    first = message(store, p["sender"], p["recipient"])
    token = str(uuid4())
    store.lifecycle.pickup(p["recipient"], first.cursor, token)
    second = message(store, p["sender"], p["recipient"])
    with pytest.raises(LifecycleRejected) as denied:
        store.lifecycle.handle_batch(p["recipient"], [(second.cursor, str(uuid4()))], allow_pickup=False)
    assert denied.value.status == 423
    assert store.lifecycle.handle_batch(p["recipient"], [(first.cursor, token)], allow_pickup=False)["handled"] == 1


def test_pickup_is_exact_session_owned_and_retries_never_extend_lease(lifecycle):
    from dataclasses import replace

    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    token = str(uuid4())
    first = store.lifecycle.pickup(p["recipient"], m.cursor, token, 105)
    assert first["state"] == "picked_up" and first["lease_until"] == 125
    assert store.lifecycle.pickup(p["recipient"], m.cursor, token, 108)["lease_until"] == 125
    with pytest.raises(LifecycleRejected):
        store.lifecycle.pickup(replace(p["recipient"], session_id=str(uuid4())), m.cursor, token, 108)
    store.lifecycle.settle(126)
    assert store.lifecycle.target(p["recipient"], m.cursor, 126)["state"] == "recovery_required"
    assert store.lifecycle.pickup(p["recipient"], m.cursor, token, 126)["lease_until"] == 125
    done = store.lifecycle.finish(p["recipient"], m.cursor, token, 127)
    assert done["state"] == "handled"
    assert store.lifecycle.finish(p["recipient"], m.cursor, token, 130)["outcome_at"] == done["outcome_at"]


def test_pickup_and_expiry_race_cannot_both_win(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])

    def take():
        try:
            return store.lifecycle.pickup(p["recipient"], m.cursor, str(uuid4()), 109)["state"]
        except LifecycleRejected:
            return "denied"

    def expire():
        store.lifecycle.settle(110)

    with ThreadPoolExecutor(2) as pool:
        take_future = pool.submit(take)
        expiry_future = pool.submit(expire)
        picked = take_future.result()
        expiry_future.result()
    result = store.lifecycle.target(p["recipient"], m.cursor, 110)
    assert result["state"] in {"picked_up", "expired_unclaimed"}
    assert (picked == "picked_up") == (result["state"] == "picked_up")
    if result["state"] == "expired_unclaimed":
        assert result["pickup_session"] is None


def test_two_pickups_have_one_owner_and_foreign_scope_cannot_observe_target(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])

    def take(token):
        try:
            return store.lifecycle.pickup(p["recipient"], m.cursor, token, 105)["pickup_token"]
        except LifecycleRejected:
            return None

    tokens = [str(uuid4()), str(uuid4())]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, tokens))
    assert len([x for x in results if x]) == 1
    for principal in [p["foreign"], p["sender"]]:
        with pytest.raises(LifecycleRejected) as error:
            store.lifecycle.target(principal, m.cursor, 106)
        assert error.value.status == 404


def test_epoch_is_channel_bound_and_closed_epoch_never_reallocates(lifecycle):
    store, p = lifecycle
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    with store._lock, store._db:
        store.lifecycle.validate_request("C111", str(uuid4()), epoch, instant=100)
        with pytest.raises(LifecycleRejected) as foreign:
            store.lifecycle.validate_request("C222", str(uuid4()), epoch, instant=100)
        assert foreign.value.status == 410
    with store._lock, store._db, pytest.raises(LifecycleRejected) as expired:
        store.lifecycle.validate_request("C111", str(uuid4()), epoch, instant=130)
    assert expired.value.status == 410


def test_original_epoch_replay_and_expiry_are_durable(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    key = str(uuid4())
    with store._lock, store._db:
        store.lifecycle.bind_request("C111", key, epoch, m.cursor, 100)
        store.lifecycle.validate_request("C111", key, epoch, existing=True, instant=150)
    with store._lock, store._db, pytest.raises(LifecycleRejected) as altered:
        store.lifecycle.validate_request("C111", key, "forged", existing=True, instant=150)
    assert altered.value.status == 409
    with store._lock, store._db, pytest.raises(LifecycleRejected) as expired:
        store.lifecycle.validate_request("C111", key, epoch, existing=True, instant=160)
    assert expired.value.status == 410


def test_clock_backwards_never_reopens_expired_epoch_or_target(lifecycle):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    store.lifecycle.settle(140)
    assert store.lifecycle.epoch("C111", 99) is None
    assert store.lifecycle.target(p["recipient"], m.cursor, 99)["state"] == "expired_unclaimed"
    with pytest.raises(LifecycleRejected):
        store.lifecycle.pickup(p["recipient"], m.cursor, str(uuid4()), 99)


def test_restart_preserves_policy_pickup_and_original_epoch(lifecycle):
    from pathlib import Path

    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    token = str(uuid4())
    first = store.lifecycle.pickup(p["recipient"], m.cursor, token, 105)
    epoch = store.lifecycle.epoch("C111", 105)["epoch"]
    path = Path(store._db.execute("PRAGMA database_list").fetchone()[2])
    store.close()
    reopened = MessageStore(path)
    try:
        reopened.channels.configure(("C111", "C222"))
        assert reopened.lifecycle.enabled and reopened.lifecycle.policy.pickup_seconds == 10
        assert reopened.lifecycle.epoch("C111", 105)["epoch"] == epoch
        assert reopened.lifecycle.pickup(p["recipient"], m.cursor, token, 108)["lease_until"] == first["lease_until"]
    finally:
        reopened.close()


@pytest.mark.parametrize("token", [None, "", "forged", "A" * 36])
def test_pickup_requires_a_canonical_receipt_identity(lifecycle, token):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    with pytest.raises(LifecycleRejected) as error:
        store.lifecycle.pickup(p["recipient"], m.cursor, token, 105)
    assert error.value.status == 422
    assert store.lifecycle.target(p["recipient"], m.cursor, 105)["state"] == "unclaimed"


def test_old_connection_cannot_start_writing_migrated_channel_registry(lifecycle):
    import sqlite3

    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    path = store._db.execute("PRAGMA database_list").fetchone()[2]
    # Old released startup only recognizes a table; its configure path also
    # cannot write through the view without the new connection capability.
    with sqlite3.connect(path) as old:
        assert old.execute("SELECT type FROM sqlite_master WHERE name='bus_channels'").fetchone()[0] == "view"
        with pytest.raises(sqlite3.OperationalError, match="agentbus_message_lifecycle_v1"):
            old.execute("INSERT OR IGNORE INTO bus_channels VALUES ('C111','ignored',1)")
        with pytest.raises(sqlite3.OperationalError, match="agentbus_message_lifecycle_v1"):
            old.execute("UPDATE messages SET payload=payload WHERE cursor=?", (m.cursor,))
        with pytest.raises(sqlite3.OperationalError, match="agentbus_message_lifecycle_v1"):
            old.execute("DELETE FROM metadata WHERE key='message_lifecycle_version'")


def test_offline_migration_dry_run_and_apply_preserve_custody_and_legacy_retry(tmp_path):
    import hashlib

    from src.agentbus_lifecycle_migration_service import migrate_lifecycle
    from src.agentbus_local_delivery_service import fingerprint

    path = tmp_path / "old.sqlite3"
    store = MessageStore(path)
    store.channels.configure(("C111", "C222"))
    body = SendMessage(sender="home:agentbus:old", recipient="all", text="legacy retained")
    key = str(uuid4())
    old = store.local_delivery.accept("C111", key, fingerprint(body.model_dump()), body, "principal")
    store.local_delivery.complete("C111", key, "100.000001")
    inbox = store.channels.inbox_id("C111")
    store.close()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    preview = migrate_lifecycle(path)
    assert not preview["applied"] and preview["messages"] == 1
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    backup = tmp_path / "backup.sqlite3"
    report = migrate_lifecycle(path, apply=True, backup_path=backup, instant=100)
    assert report["applied"] and report["messages"] == 1 and backup.stat().st_mode & 0o777 == 0o600
    reopened = MessageStore(path)
    try:
        reopened.channels.configure(("C111", "C222"))
        assert reopened.channels.inbox_id("C111") == inbox
        assert reopened.message("C111", old.cursor).text == "legacy retained"
        assert reopened._db.execute("SELECT legacy_unknown FROM lifecycle_payloads").fetchone()[0] == 1
        with reopened._lock, reopened._db:
            reopened.lifecycle.validate_request("C111", key, None, existing=True, instant=101)
        assert reopened.lifecycle.enabled
    finally:
        reopened.close()


def test_migration_failure_rolls_back_registry_and_metadata_with_backup_retained(tmp_path):
    import sqlite3

    from src.agentbus_lifecycle_migration_service import migrate_lifecycle

    path = tmp_path / "rollback.sqlite3"
    store = MessageStore(path)
    store.channels.configure(("C111",))
    with store._db:
        store._db.execute(
            "CREATE TRIGGER fail_lifecycle BEFORE INSERT ON metadata WHEN NEW.key='message_lifecycle_version' BEGIN SELECT RAISE(ABORT,'injected'); END"
        )
    store.close()
    backup = tmp_path / "backup.sqlite3"
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        migrate_lifecycle(path, apply=True, backup_path=backup)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT type FROM sqlite_master WHERE name='bus_channels'").fetchone()[0] == "table"
        assert not db.execute("SELECT value FROM metadata WHERE key='message_lifecycle_version'").fetchone()
    assert backup.is_file()


def test_migration_refuses_live_database_lease_and_existing_backup(tmp_path):
    from src.agentbus_channel_migration_service import database_lease
    from src.agentbus_lifecycle_migration_service import migrate_lifecycle

    path = tmp_path / "lease.sqlite3"
    store = MessageStore(path)
    store.channels.configure(("C111",))
    store.close()
    with database_lease(path), pytest.raises(ValueError, match="Service must be stopped"):
        migrate_lifecycle(path)
    backup = tmp_path / "occupied.sqlite3"
    backup.write_text("do not overwrite")
    with pytest.raises(FileExistsError):
        migrate_lifecycle(path, apply=True, backup_path=backup)
    assert backup.read_text() == "do not overwrite"


def test_idempotent_migration_keeps_policy_and_epoch_without_new_backup(lifecycle):
    from pathlib import Path

    from src.agentbus_lifecycle_migration_service import migrate_lifecycle

    store, _ = lifecycle
    path = Path(store._db.execute("PRAGMA database_list").fetchone()[2])
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    store.close()
    result = migrate_lifecycle(path, apply=True)
    assert result["applied"] and result["policy"]["pickup_seconds"] == 10
    reopened = MessageStore(path)
    try:
        assert reopened.lifecycle.epoch("C111", 100)["epoch"] == epoch
    finally:
        reopened.close()


def test_shared_poll_expiry_keeps_fixed_membership_and_never_fakes_ack(lifecycle):
    store, p = lifecycle
    secret = ("lifecycle-private-recipient").ljust(40, "x")
    control, targets = store.controls.issue_auxiliary(
        kind="poll",
        routes=None,
        session_ids=[p["sender"].session_id, p["recipient"].session_id],
        reason="Which option?",
        actor="operator",
        request_key="fixed-poll",
    )
    assert len(targets) == 2
    assert store._db.execute("SELECT count(*) FROM operator_controls").fetchone()[0] == 1
    assert store.controls.deliver(p["recipient"].session_id, secret)[0]["lifecycle_state"] == "unread"
    store.controls.acknowledge_control(control, p["recipient"].session_id, secret, answer="Option A")
    store.lifecycle.settle(111)
    rows = {row["session_id"]: row for row in store.controls.status(control, channel="C111")}
    assert rows[p["sender"].session_id]["lifecycle_state"] == "expired_unread"
    assert rows[p["sender"].session_id]["receipt"] is None
    assert rows[p["recipient"].session_id]["lifecycle_state"] == "read_acknowledged"
    assert rows[p["recipient"].session_id]["state"] == "effective"
    assert store.controls.status(control, channel="C222") == []
    sender_secret = ("lifecycle-private-sender").ljust(40, "x")
    assert store.controls.deliver(p["sender"].session_id, sender_secret) == []
    with pytest.raises(ValueError, match="expired"):
        store.controls.acknowledge_control(control, p["sender"].session_id, sender_secret, answer="Late answer")
    assert control not in {
        row["control_id"] for row in store.participation.presence(p["sender"].session_id)["outstanding_controls"]
    }
    stop, _ = store.controls.issue_stop(routes=[p["sender"].route], reason="Safety remains mandatory", actor="operator")
    store.lifecycle.settle(1000)
    assert [row["control_id"] for row in store.controls.deliver(p["sender"].session_id, sender_secret)] == [stop]


def test_committed_poll_ack_recovers_after_lost_effect_without_expiry(lifecycle, monkeypatch):
    store, p = lifecycle
    secret = ("lifecycle-private-recipient").ljust(40, "x")
    control, _ = store.controls.issue_auxiliary(
        kind="poll", routes=None, session_ids=[p["recipient"].session_id], reason="Recovery answer", actor="operator"
    )
    store.controls.deliver(p["recipient"].session_id, secret)
    original = store.controls._apply_aux_effect

    def interrupted(*_):
        raise RuntimeError("interrupted after committed ACK")

    monkeypatch.setattr(store.controls, "_apply_aux_effect", interrupted)
    with pytest.raises(RuntimeError):
        store.controls.acknowledge_control(control, p["recipient"].session_id, secret, answer="Durable answer")
    store.lifecycle.settle(111)
    assert store.controls.status(control)[0]["lifecycle_state"] == "unread"
    monkeypatch.setattr(store.controls, "_apply_aux_effect", original)
    store.controls.acknowledge_control(control, p["recipient"].session_id, secret, answer="Durable answer")
    row = store.controls.status(control)[0]
    assert row["state"] == "effective" and row["lifecycle_state"] == "read_acknowledged"
    assert row["answer"] == "Durable answer"


def test_repeated_archive_retire_cycles_reuse_pages_and_checkpoint_without_vacuum(lifecycle):
    from dataclasses import replace

    store, p = lifecycle
    store.retention.checkpoint()
    for cycle in range(3):
        start = 100 + cycle * 200
        with store._db:
            store.lifecycle.now(start, persist=True)
        for index in range(8):
            body = SendMessage(
                sender=p["sender"].route,
                recipient=p["recipient"].route,
                text=("payload-%s-%s-" % (cycle, index)) + "x" * 4800,
                kind="request",
            )
            body._sender_assurance = "session"
            store.append("C111", f"{start}.{index + 1:06d}", body)
        store.lifecycle.settle(start + 11)
        assert (
            store.retention.archive(
                "C111", actor="operator", request_key=f"archive-cycle-{cycle}", apply=True, instant=start + 100
            )["eligible"]
            == 8
        )
        assert (
            store.retention.purge(
                "C111", actor="operator", request_key=f"purge-cycle-{cycle}", apply=True, instant=start + 121
            )["eligible"]
            == 8
        )
        assert store.retention.checkpoint()["status"] == "completed"
        assert store._db.execute("PRAGMA foreign_key_check").fetchall() == []
    # Allocated file bytes can remain large while its pages are reusable.
    with store._db:
        store._db.execute("CREATE TABLE freed_pages (data BLOB)")
        store._db.execute("INSERT INTO freed_pages VALUES (zeroblob(2097152))")
        store._db.execute("DROP TABLE freed_pages")
    store.retention.checkpoint()
    store.lifecycle.policy = replace(store.lifecycle.policy, storage_bytes=1048576)
    status = store.retention.storage()
    assert status["physical_bytes"] > status["limit_bytes"]
    assert status["accounted_bytes"] < status["limit_bytes"] and status["reusable_bytes"] > 0
    assert message(store, p["sender"], p["recipient"]).cursor > 24
    assert store.channels.high_water("C111") == 25


def test_busy_checkpoint_defers_and_never_commits_an_existing_writer(lifecycle):
    import sqlite3

    from src.agentbus_transaction_service import atomic

    store, p = lifecycle
    store.retention.checkpoint()
    path = store._db.execute("PRAGMA database_list").fetchone()[2]
    reader = sqlite3.connect(path)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT count(*) FROM messages").fetchone()
        m = message(store, p["sender"], p["recipient"])
        assert store.retention.checkpoint()["status"] == "deferred"
        assert store.message("C111", m.cursor).text == m.text
    finally:
        reader.close()
    assert store.retention.checkpoint()["status"] == "completed"
    with pytest.raises(ValueError, match="writer transaction"):
        with atomic(store._db):
            store._db.execute("INSERT INTO metadata VALUES ('checkpoint-must-not-commit','probe')")
            store.retention.checkpoint()
    assert store._db.execute("SELECT 1 FROM metadata WHERE key='checkpoint-must-not-commit'").fetchone() is None


def test_lifecycle_default_policy_matches_the_governed_contract():
    from pathlib import Path

    import yaml

    contract = yaml.safe_load(
        (Path(__file__).parents[1] / "contracts/messages/v1/message-lifecycle.contract.yml").read_text()
    )
    assert vars(LifecyclePolicy()) == contract["defaults"]["policy"]


def test_archive_keeps_sender_metrics_and_retirement_keeps_channel_high_water(lifecycle):
    from src.agentbus_metrics_service import session_metrics

    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    before = session_metrics(store, "C111", p["sender"].session_id)
    assert before["sent_messages"] == 1 and before["channel_high_water_cursor"] == 1
    store.lifecycle.settle(200)
    store.retention.archive("C111", actor="operator", request_key="metrics-archive", apply=True, instant=200)
    archived = session_metrics(store, "C111", p["sender"].session_id)
    assert archived["sent_messages"] == 1 and archived["sent_high_water_cursor"] == 1
    store.retention.purge("C111", actor="operator", request_key="metrics-purge", apply=True, instant=221)
    retired = session_metrics(store, "C111", p["sender"].session_id)
    assert retired["sent_messages"] == 0 and retired["count_scope"] == "retained_history"
    assert retired["channel_high_water_cursor"] == 1
    assert store.channels.internal_cursor("C111", 1) is None and store.message("C111", m.cursor) is None


def test_new_reply_cannot_reactivate_expired_parent_but_exact_accepted_retry_survives(lifecycle):
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    parent = message(store, p["sender"], p["recipient"])
    reply = SendMessage(
        sender=p["recipient"].route,
        recipient=p["sender"].route,
        text="already accepted reply",
        kind="reply",
        reply_to_cursor=parent.cursor,
    )
    reply._sender_assurance = "session"
    epoch = store.lifecycle.epoch("C111", 100)["epoch"]
    key = str(uuid4())
    original = store.local_delivery.accept(
        "C111", key, fingerprint(reply.model_dump()), reply, "recipient-session", epoch=epoch
    )
    store.lifecycle.settle(111)
    assert (
        store.local_delivery.accept(
            "C111", key, fingerprint(reply.model_dump()), reply, "recipient-session", epoch=epoch
        ).cursor
        == original.cursor
    )
    with pytest.raises(LifecycleRejected) as expired:
        store.local_delivery.accept(
            "C111", str(uuid4()), fingerprint(reply.model_dump()), reply, "recipient-session", epoch=epoch
        )
    assert expired.value.status == 410
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations").fetchone()[0] == 1


def test_retired_history_is_explicit_and_channel_scoped_at_http_boundary(lifecycle):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from src.agentbus_service import Settings, create_app

    store, p = lifecycle
    message(store, p["sender"], p["recipient"])
    store.lifecycle.settle(200)
    store.retention.archive("C111", actor="operator", request_key="http-archive", apply=True, instant=200)
    store.retention.purge("C111", actor="operator", request_key="http-purge", apply=True, instant=221)
    settings = Settings(
        api_token="retired-history-test-bearer",
        slack_bot_token="xoxb-test",
        slack_app_token="xapp-test",
        slack_channel="",
        db_path=Path(store._db.execute('PRAGMA database_list').fetchone()[2]),
        slack_channels=("C111", "C222"),
    )
    app = create_app(settings)
    app.state.store = store
    client = TestClient(app)  # No lifespan: never start a socket/provider from the fixture store.

    def headers(name):
        return {
            "Authorization": "Bearer retired-history-test-bearer",
            "X-AgentBus-Session-Token": ("lifecycle-private-" + name).ljust(40, "x"),
            "X-AgentBus-Inbox-ID": p[name].inbox_id,
            "X-AgentBus-Lifecycle-Version": "1",
        }

    try:
        page = client.get("/v1/messages", headers=headers("sender"))
        assert page.status_code == 200 and page.json()["retired_history"] is True
        assert page.json()["next_cursor"] == 1 and page.json()["messages"] == []
        assert client.get("/v1/messages/1/outcome", headers=headers("sender")).status_code == 410
        assert client.get("/v1/messages/1/outcome", headers=headers("foreign")).status_code == 404
        assert client.get("/v1/messages?after=1", headers=headers("sender")).json()["retired_history"] is False
    finally:
        client.close()


def test_archive_recovers_exhausted_admission_budget_with_reader_pinned_wal(lifecycle):
    import sqlite3
    from dataclasses import replace

    store, p = lifecycle
    for index in range(180):
        body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                           kind='request', text=f'{index}:' + 'x' * 5900)
        body._sender_assurance = 'session'
        store.append('C111', str(uuid4()), body)
    store.lifecycle.settle(200)
    store.retention.checkpoint()
    budget = store.retention.storage()['accounted_bytes'] - 256 * 1024
    assert budget >= 1048576
    store.lifecycle.policy = replace(store.lifecycle.policy, storage_bytes=budget)
    assert store.retention.storage()['accounted_bytes'] > budget
    reader = sqlite3.connect(store._db.execute('PRAGMA database_list').fetchone()[2])
    try:
        reader.execute('BEGIN')
        reader.execute('SELECT count(*) FROM messages').fetchone()
        with pytest.raises(LifecycleRejected):
            store.retention.reserve_storage(100)
        result = store.retention.archive('C111', actor='operator', request_key='full-archive',
                                         apply=True, instant=200)
        assert result['eligible'] == 100
        assert store.retention.checkpoint()['status'] == 'deferred'
        assert store._db.execute('SELECT count(*) FROM lifecycle_archive').fetchone()[0] == 100
        with pytest.raises(LifecycleRejected):
            store.retention.reserve_storage(100)
        assert store.message('C111', 1).text.startswith('0:')
    finally:
        reader.close()
    assert store.retention.checkpoint()['status'] == 'completed'
    assert store.retention.storage()['accounted_bytes'] < budget
    store.retention.reserve_storage(100)


def test_archive_maintenance_bound_refuses_without_partial_retirement(lifecycle):
    from dataclasses import replace

    store, p = lifecycle
    m = message(store, p['sender'], p['recipient'])
    store.lifecycle.settle(200)
    store.lifecycle.policy = replace(store.lifecycle.policy, storage_bytes=1048576)
    with store._db:
        store._db.execute('CREATE TABLE maintenance_pressure(payload BLOB)')
        store._db.execute('INSERT INTO maintenance_pressure VALUES (zeroblob(?))', (35 * 1024 * 1024,))
    store.retention.checkpoint()
    preview = store.retention.archive('C111', actor='operator', instant=200)
    status = preview['storage_before']
    assert status['archive_maintenance_bytes'] == 32 * 1024 * 1024
    assert status['accounted_bytes'] > status['archive_limit_bytes']
    with pytest.raises(LifecycleRejected) as full:
        store.retention.archive('C111', actor='operator', request_key='bounded-maintenance',
                                apply=True, instant=200)
    assert full.value.status == 503
    assert store.message('C111', m.cursor).text == m.text
    assert store._db.execute('SELECT count(*) FROM lifecycle_archive').fetchone()[0] == 0
    assert store._db.execute("SELECT 1 FROM lifecycle_operations WHERE request_key='bounded-maintenance'").fetchone() is None


@pytest.mark.parametrize('provider_refs', [('105.000001',), ('105.000001', '106.500001')])
def test_purge_waits_until_all_delayed_provider_refs_are_covered(lifecycle, provider_refs):
    from src.agentbus_channel_ingress_service import append_scoped
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                       text='delayed provider retirement', kind='request')
    body._sender_assurance = 'session'
    key = str(uuid4())
    epoch = store.lifecycle.epoch('C111', 100)['epoch']
    accepted = store.local_delivery.accept('C111', key, fingerprint(body.model_dump()), body,
                                          'original-session', epoch=epoch)
    for reference in provider_refs:
        store.local_delivery.complete('C111', key, reference)
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='delayed-archive', apply=True, instant=200)
    assert store.retention.purge('C111', actor='operator', instant=221)['eligible'] == 0
    early = store.retention.purge('C111', actor='operator', request_key='deferred-retirement',
                                  apply=True, instant=221)
    assert early['eligible'] == 0 and store.message('C111', accepted.cursor) is not None
    assert store.local_delivery.capacity()['retained_requests'] == 1
    retired = store.retention.purge('C111', actor='operator', request_key='covered-retirement',
                                    apply=True, instant=228)
    assert retired['eligible'] == 1 and store.message('C111', accepted.cursor) is None
    for reference in provider_refs:
        assert not store.retention.accepts_ingress('C111', reference)
        assert store.retention.accepts_ingress('C222', reference)
        assert append_scoped(store, 'C111', reference, body) is None
    assert store.channels.high_water('C111') == 1 and store.channels.high_water('C222') == 0
    assert store.retention.purge('C111', actor='operator', request_key='deferred-retirement',
                                 apply=True, instant=228) == early
    assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []


@pytest.mark.parametrize('restart', [False, True])
def test_retired_delivery_locator_cannot_reallocate_late_unknown_alias(lifecycle, restart):
    from pathlib import Path
    from types import SimpleNamespace

    from slack_sdk.socket_mode.request import SocketModeRequest

    from src.agentbus_local_delivery_service import LOCATOR_PREFIX, fingerprint
    from src.agentbus_service import SlackReceiver, encode_envelope

    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                       text='late alias of retired request', kind='request')
    body._sender_assurance = 'session'
    key = str(uuid4())
    epoch = store.lifecycle.epoch('C111', 100)['epoch']
    store.local_delivery.accept('C111', key, fingerprint(body.model_dump()), body,
                                'original-session', epoch=epoch)
    store.local_delivery.complete('C111', key, '105.000001')
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='socket-archive', apply=True, instant=200)
    assert store.retention.purge('C111', actor='operator', request_key='socket-purge',
                                  apply=True, instant=228)['eligible'] == 1
    floor = store._db.execute('SELECT provider_floor FROM lifecycle_ingress WHERE channel=?', ('C111',)).fetchone()[0]
    if restart:
        path = store._db.execute('PRAGMA database_list').fetchone()[2]
        store.close()
        store = MessageStore(Path(path))
        store.channels.configure(('C111', 'C222'))
    blocks = [{'type':'context', 'block_id':LOCATOR_PREFIX + key, 'elements':[]}]
    assert store.local_delivery.accept_event_echo('C111', '120.000001', encode_envelope(body), blocks) is False
    responses = []
    receiver = SlackReceiver(store, ('C111','C222'), 'B123')
    try:
        receiver(SimpleNamespace(send_socket_mode_response=responses.append), SocketModeRequest(
            type='events_api', envelope_id='retired-alias', payload={'event':{
                'type':'message', 'bot_id':'B123', 'channel':'C111', 'ts':'120.000001',
                'text':encode_envelope(body), 'blocks':blocks}}))
        assert len(responses) == 1  # Transport acknowledgement is not semantic delivery or handling.
        assert store._db.execute('SELECT count(*) FROM messages').fetchone()[0] == 0
        assert store.channels.high_water('C111') == 1
        assert store.local_delivery.capacity()['retained_requests'] == 0
        assert store._db.execute('SELECT provider_floor FROM lifecycle_ingress WHERE channel=?', ('C111',)).fetchone()[0] == floor
    finally:
        receiver._commands.shutdown(wait=True)
        if restart:
            store.close()


def test_new_provider_alias_after_preview_repins_purge(lifecycle):
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                       text='recompute provider alias eligibility', kind='request')
    body._sender_assurance = 'session'
    key = str(uuid4())
    epoch = store.lifecycle.epoch('C111', 100)['epoch']
    accepted = store.local_delivery.accept('C111', key, fingerprint(body.model_dump()), body,
                                          'original-session', epoch=epoch)
    store.local_delivery.complete('C111', key, '105.000001')
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='repin-archive', apply=True, instant=200)
    assert store.retention.purge('C111', actor='operator', instant=226)['eligible'] == 1
    store.local_delivery.complete('C111', key, '110.000001')
    assert store.retention.purge('C111', actor='operator', request_key='repinned-purge',
                                  apply=True, instant=226)['eligible'] == 0
    assert store.message('C111', accepted.cursor).text == body.text
    assert store.retention.purge('C111', actor='operator', request_key='repin-now-covered',
                                  apply=True, instant=231)['eligible'] == 1
    assert not store.retention.accepts_ingress('C111', '105.000001')
    assert not store.retention.accepts_ingress('C111', '110.000001')


def test_provider_alias_and_purge_serialize_without_reallocating_custody(lifecycle):
    from threading import Barrier

    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                       text='provider alias purge race', kind='request')
    body._sender_assurance = 'session'
    key = str(uuid4())
    epoch = store.lifecycle.epoch('C111', 100)['epoch']
    store.local_delivery.accept('C111', key, fingerprint(body.model_dump()), body,
                                'original-session', epoch=epoch)
    store.local_delivery.complete('C111', key, '105.000001')
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='race-archive', apply=True, instant=200)
    barrier = Barrier(2)
    def observe():
        barrier.wait()
        try:
            store.local_delivery.complete('C111', key, '110.000001')
            return 'stored'
        except ValueError as exc:
            assert str(exc) == 'unknown local delivery'
            return 'already_retired'
    def retire():
        barrier.wait()
        return store.retention.purge('C111', actor='operator', request_key='racing-purge',
                                     apply=True, instant=226)['eligible']
    with ThreadPoolExecutor(max_workers=2) as pool:
        observed = pool.submit(observe)
        retired = pool.submit(retire)
        outcome = observed.result(), retired.result()
    assert outcome in {('stored', 0), ('already_retired', 1)}
    assert store.local_delivery.capacity()['retained_requests'] == (1 if outcome[0] == 'stored' else 0)
    assert store.channels.high_water('C111') == 1
    assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_recent_aliases_do_not_hide_later_reclaimable_rows(lifecycle):
    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
                       text='bounded purge progress', kind='request')
    body._sender_assurance = 'session'
    for index in range(100):
        store.append('C111', f'110.{index:06d}', body)
    last = store.append('C111', '105.500001', body)
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='scan-archive-1', apply=True, instant=200)
    store.retention.archive('C111', actor='operator', request_key='scan-archive-2', apply=True, instant=200)
    result = store.retention.purge('C111', actor='operator', request_key='scan-purge', apply=True, instant=226)
    assert result['eligible'] == 1, 'First 100 pinned rows hide a later reclaimable row'
    assert store.channels.high_water('C111') == 101
    assert store.retention.accepts_ingress('C111', '110.000000')
    assert not store.retention.accepts_ingress('C111', '105.500001')
    assert store.message('C111', last.cursor) is None
    assert store._db.execute('SELECT count(*) FROM messages').fetchone()[0] == 100


def test_provider_retirement_SQL_comparison_keeps_subfloat_precision(lifecycle):
    store, _ = lifecycle
    cutoff = '1700000000.0'
    for reference, covered in [('1700000000.00000000000000000001', 0),
                               ('1700000000.00000000000000000000', 1),
                               ('1699999999.99999999999999999999', 1)]:
        assert store._db.execute('SELECT agentbus_provider_ref_covered_v1(?,?)',
                                 (reference, cutoff)).fetchone()[0] == covered


def test_blocked_ancestry_cannot_starve_independent_delivery_or_expiry(lifecycle, monkeypatch):
    import asyncio

    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    # Build a backlog over successive rate windows; retain the real rate check.
    reserve = store.send_rate._reserve
    rate_clock = [100.0]
    monkeypatch.setattr(store.send_rate, '_reserve', lambda principal: reserve(principal, now=rate_clock[0]))
    epoch = store.lifecycle.epoch('C111', 100)['epoch']
    parent_body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route, text='uncertain parent', kind='request')
    parent_body._sender_assurance = 'session'
    parent_key = str(uuid4())
    parent = store.local_delivery.accept('C111', parent_key, fingerprint(parent_body.model_dump()), parent_body, 'exact-session', epoch=epoch)
    assert store.local_delivery.reserve('C111', parent_key, now=100)
    store.local_delivery.uncertain('C111', parent_key)
    keys = []
    for index in range(101):
        rate_clock[0] += 61
        body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route,
            text=str(index), kind='request', reply_to_cursor=parent.cursor if index < 100 else None)
        body._sender_assurance = 'session'
        key = str(uuid4())
        keys.append(key)
        store.local_delivery.accept('C111', key, fingerprint(body.model_dump()), body, 'exact-session', epoch=epoch)
    class Poster:
        def __init__(self): self.keys = []
        async def post(self, message, *, message_id):
            self.keys.append(message_id)
            return '123.456'
    poster = Poster()
    worker = LocalDeliveryWorker(store, poster, 'C111')
    for _ in range(3):
        asyncio.run(worker.submit_once())
    assert poster.keys == [keys[-1]]
    assert store._db.execute("SELECT count(*) FROM local_delivery_obligations WHERE state='pending'").fetchone()[0] == 100
    # Expiry must settle without ever reserving a blocked child.
    store.lifecycle.now(200, persist=True)
    asyncio.run(LocalDeliveryWorker(store, poster, 'C111').submit_once())
    rows = store._db.execute('SELECT state,attempts FROM local_delivery_obligations ORDER BY local_cursor').fetchall()
    assert all(row['state'] == 'expired_unsubmitted' and row['attempts'] == 0 for row in rows[1:101])
    assert rows[0]['state'] == 'expired_uncertain' and rows[0]['attempts'] == 1
    assert rows[-1]['state'] == 'submitted'
    assert store._db.execute("SELECT count(*) FROM messages WHERE channel='C111'").fetchone()[0] == 102
    assert store.local_delivery.capacity()['outstanding'] == 1


def test_parent_recovery_unblocks_child_after_restart_without_cross_channel_expiry(lifecycle):
    import asyncio

    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_local_delivery_service import fingerprint
    store, p = lifecycle
    def accept(channel, sender, recipient, parent=None):
        body = SendMessage(sender=sender, recipient=recipient, text='retained edge', kind='request', reply_to_cursor=parent)
        body._sender_assurance = 'session'
        key = str(uuid4())
        epoch = store.lifecycle.epoch(channel,100)['epoch']
        return key, store.local_delivery.accept(channel,key,fingerprint(body.model_dump()),body,'exact-session',epoch=epoch)
    parent_key, parent = accept('C111',p['sender'].route,p['recipient'].route)
    child_key, child = accept('C111',p['sender'].route,p['recipient'].route,parent.cursor)
    foreign_key, _ = accept('C222',p['foreign'].route,p['foreign'].route)
    assert store.local_delivery.reserve('C111',parent_key,now=100)
    store.local_delivery.blocked('C111',parent_key,'missing_echo','123.400')
    assert store.local_delivery.pending(channel='C111',now=100) == []
    assert store.retention.purge('C111',actor='operator',instant=105)['eligible'] == 0
    database = store._db.execute('PRAGMA database_list').fetchone()['file']
    store.close()
    restored = MessageStore(__import__('pathlib').Path(database))
    restored.channels.configure(('C111','C222'))
    class Poster:
        async def post(self, message, *, message_id):
            assert message_id == child_key and message.thread_ts == '123.400'
            return '123.500'
    try:
        assert asyncio.run(LocalDeliveryWorker(restored,Poster(),'C111').submit_once()) == 0
        restored.local_delivery.complete('C111',parent_key,'123.400')
        assert asyncio.run(LocalDeliveryWorker(restored,Poster(),'C111').submit_once()) == 1
        restored.local_delivery.settle_expired('C111',now=200)
        assert restored._db.execute('SELECT state FROM local_delivery_obligations WHERE request_id=?',(foreign_key,)).fetchone()[0] == 'pending'
        assert restored.message('C111',child.cursor).slack_ts == '123.500'
        assert restored._db.execute('PRAGMA foreign_key_check').fetchall() == []
    finally:
        restored.close()


def test_expiry_settlement_rolls_back_targets_and_obligations_together(lifecycle):
    import sqlite3

    from src.agentbus_local_delivery_service import fingerprint
    from src.agentbus_transaction_service import atomic

    store, p = lifecycle
    body = SendMessage(sender=p['sender'].route, recipient=p['recipient'].route, text='atomic expiry', kind='request')
    body._sender_assurance = 'session'
    key = str(uuid4())
    epoch = store.lifecycle.epoch('C111',100)['epoch']
    accepted = store.local_delivery.accept('C111',key,fingerprint(body.model_dump()),body,'exact-session',epoch=epoch)
    with store._db:
        store._db.execute("CREATE TRIGGER expiry_failure BEFORE UPDATE ON local_delivery_obligations "
            "WHEN NEW.state='expired_unsubmitted' BEGIN SELECT RAISE(ABORT,'expiry interruption'); END")
    with pytest.raises(sqlite3.DatabaseError,match='expiry interruption'):
        store.local_delivery.settle_expired('C111',now=200)
    assert store._db.execute('SELECT state FROM lifecycle_targets WHERE cursor=?',(accepted.cursor,)).fetchone()[0] == 'unclaimed'
    assert store._db.execute('SELECT state FROM local_delivery_obligations WHERE request_id=?',(key,)).fetchone()[0] == 'pending'
    with store._db:
        store._db.execute('DROP TRIGGER expiry_failure')
    with pytest.raises(RuntimeError,match='caller rollback'):
        with atomic(store._db):
            store._db.execute("INSERT INTO metadata(key,value) VALUES ('outer-command-probe','uncommitted')")
            store.local_delivery.settle_expired('C111',now=200)
            raise RuntimeError('caller rollback')
    assert store._db.execute("SELECT 1 FROM metadata WHERE key='outer-command-probe'").fetchone() is None
    assert store._db.execute('SELECT state FROM lifecycle_targets WHERE cursor=?',(accepted.cursor,)).fetchone()[0] == 'unclaimed'
    store.local_delivery.settle_expired('C111',now=200)
    assert store._db.execute('SELECT state FROM lifecycle_targets WHERE cursor=?',(accepted.cursor,)).fetchone()[0] == 'expired_unclaimed'
    assert store._db.execute('SELECT state FROM local_delivery_obligations WHERE request_id=?',(key,)).fetchone()[0] == 'expired_unsubmitted'


def test_newly_ready_reply_chain_progresses_only_within_remaining_cycle_budget(lifecycle):
    import asyncio

    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    epoch = store.lifecycle.epoch('C111',100)['epoch']
    keys, parent = [], None
    for index in range(4):
        body = SendMessage(sender=p['sender'].route,recipient=p['recipient'].route,text=str(index),kind='request',reply_to_cursor=parent)
        body._sender_assurance = 'session'
        key = str(uuid4())
        accepted = store.local_delivery.accept('C111',key,fingerprint(body.model_dump()),body,'exact-session',epoch=epoch)
        keys.append(key)
        parent = accepted.cursor
    class Poster:
        def __init__(self):self.calls=[]
        async def post(self,message,*,message_id):
            self.calls.append((message_id,message.thread_ts))
            return f'200.{len(self.calls):06d}'
    poster=Poster()
    worker=LocalDeliveryWorker(store,poster,'C111')
    assert asyncio.run(worker.submit_once(budget=3)) == 3
    assert worker.cycle_attempts == 3
    assert poster.calls == [(keys[0],None),(keys[1],'200.000001'),(keys[2],'200.000001')]
    assert asyncio.run(worker.submit_once(budget=1)) == 1
    assert worker.cycle_attempts == 1 and poster.calls[-1] == (keys[3],'200.000001')


def test_eligibility_refresh_cannot_project_foreign_payload_through_shared_request_key(lifecycle):
    import asyncio

    from src.agentbus_delivery_runtime_service import LocalDeliveryWorker
    from src.agentbus_local_delivery_service import fingerprint

    store, p = lifecycle
    def accept(sender, recipient, text, *, key=None, parent=None):
        body = SendMessage(sender=sender.route,recipient=recipient.route,text=text,kind='request',reply_to_cursor=parent)
        body._sender_assurance = 'session'
        key = key or str(uuid4())
        epoch = store.lifecycle.epoch(sender.channel,100)['epoch']
        return key,store.local_delivery.accept(sender.channel,key,fingerprint(body.model_dump()),body,sender.session_id,epoch=epoch)
    root_key, root = accept(p['sender'],p['recipient'],'unknown parent')
    assert store.local_delivery.reserve('C111',root_key,now=100)
    store.local_delivery.uncertain('C111',root_key)
    shared, child = accept(p['sender'],p['recipient'],'blocked local',parent=root.cursor)
    ready, _ = accept(p['sender'],p['recipient'],'safe independent')
    accept(p['foreign'],p['foreign'],'foreign private payload',key=shared)
    class Poster:
        def __init__(self):self.calls=[]
        async def post(self,message,*,message_id):
            self.calls.append((message_id,message.text))
            return f'200.{len(self.calls):06d}'
    poster=Poster()
    try:
        assert asyncio.run(LocalDeliveryWorker(store,poster,'C111').submit_once()) == 1
        assert poster.calls == [(ready,'safe independent')]
        assert store.message('C111',child.cursor).slack_ts == ''
        assert store._db.execute('SELECT attempts FROM local_delivery_obligations WHERE channel=? AND request_id=?',('C111',shared)).fetchone()[0] == 0
    finally:
        assert store._db.execute('PRAGMA foreign_key_check').fetchall() == []


def test_check_in_snapshot_reconciles_expired_announcements_without_ack(lifecycle, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from src.agentbus_service import Settings, create_app

    store, p = lifecycle
    recipient = p['recipient']
    secret = 'lifecycle-private-recipient'.ljust(40, 'x')
    control, _ = store.controls.issue_auxiliary(kind='broadcast', routes=None,
        session_ids=[recipient.session_id], reason='Read this', actor='operator')
    app = create_app(Settings(api_token='snapshot-test-bearer', slack_bot_token='xoxb-test',
        slack_app_token='xapp-test', slack_channel='', slack_channels=('C111', 'C222'),
        db_path=Path(store._db.execute('PRAGMA database_list').fetchone()[2])))
    app.state.store = store
    headers = {'Authorization': 'Bearer snapshot-test-bearer',
        'X-AgentBus-Session-Token': secret, 'X-AgentBus-Inbox-ID': recipient.inbox_id}
    client = TestClient(app)  # Never enter lifespan: no Slack connection.
    path = f'/v1/sessions/{recipient.session_id}/check-in'
    try:
        response = client.post(path, headers=headers, json={})
        assert response.status_code == 200
        assert response.json()['pending_announcement_ids'] == [control]
        foreign = {**headers, 'X-AgentBus-Inbox-ID': p['foreign'].inbox_id}
        assert client.post(path, headers=foreign, json={}).status_code == 403
        monkeypatch.setattr('src.agentbus_message_lifecycle_service.time.time', lambda: 111.0)
        response = client.post(path, headers=headers, json={})
        assert response.status_code == 200 and response.json()['pending_announcement_ids'] == []
        ack = client.post(f'/v1/controls/{control}/targets/{recipient.session_id}/ack',
                          headers=headers, json={})
        assert ack.status_code == 409
        assert 'expired' in ack.json()['detail']
        outcome = store.controls.status(control, channel='C111')[0]
        assert outcome['lifecycle_state'] == 'unread' and outcome['receipt'] is None
        store.lifecycle.settle(111)
        outcome = store.controls.status(control, channel='C111')[0]
        assert outcome['lifecycle_state'] == 'expired_unread' and outcome['receipt'] is None
        stop, _ = store.controls.issue_stop(routes=None, session_ids=[recipient.session_id],
                                          reason='Stop now', actor='operator')
        response = client.post(path, headers=headers, json={})
        assert response.json()['pending_announcement_ids'] == []
        assert stop in [item['control_id'] for item in response.json()['controls']]
    finally:
        client.close()


@pytest.mark.parametrize('query', ['read', 'inbox', 'message', 'api_read', 'api_inbox'])
def test_retirement_cannot_split_payload_and_public_cursor_snapshot(lifecycle, monkeypatch, query):
    from threading import Event
    from types import SimpleNamespace

    from src import agentbus_channel_api_service as channel_api

    store, p = lifecycle
    original = message(store, p['sender'], p['recipient'])
    store.lifecycle.settle(111)
    store.retention.archive('C111', actor='operator', request_key='snapshot-archive', apply=True, instant=200)
    selected, attempted = Event(), Event()
    def retire():
        assert selected.wait(3), 'reader never reached snapshot boundary'
        acquired = store._lock.acquire(blocking=False)
        if not acquired:
            attempted.set()
        try:
            return store.retention.purge('C111', actor='operator', request_key='snapshot-purge',
                                         apply=True, instant=221)
        finally:
            if acquired:
                store._lock.release()
                attempted.set()
    hook = 'serialize_page' if query.startswith('api_') else '_message'
    owner = channel_api if query.startswith('api_') else store
    target = getattr(owner, hook)
    def paused(*args, **kwargs):
        selected.set()
        assert attempted.wait(3), 'retirement did not attempt the writer lock'
        return target(*args, **kwargs)
    monkeypatch.setattr(owner, hook, paused)
    with ThreadPoolExecutor(max_workers=1) as pool:
        retired = pool.submit(retire)
        if query == 'read':
            result = store.read('C111').messages[0]
        elif query == 'inbox':
            result = store.inbox('C111', p['recipient'].route).messages[0]
        elif query == 'message':
            result = store.message('C111', original.cursor)
        else:
            monkeypatch.setattr(channel_api, 'resolve_principal', lambda *_args, **_kw: p['recipient'])
            request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=store)))
            if query == 'api_read':
                page = channel_api.api_read(request, after=0, recipient=None, limit=100, thread_ts=None)
            else:
                page = channel_api.api_inbox(request, p['recipient'].route, after=0, limit=100,
                                            thread_ts=None, context=False, x_agentbus_session_token=None)
            result = page['messages'][0]
        assert (result['text'] if isinstance(result, dict) else result.text) == original.text
        assert (result['cursor'] if isinstance(result, dict) else result.cursor) == original.cursor
        assert retired.result(timeout=3)['eligible'] == 1
    monkeypatch.setattr(owner, hook, target)
    assert store.message('C111', original.cursor) is None
    assert store.channels.high_water('C111') == 1
