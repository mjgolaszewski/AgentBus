"""Elapsed outcome facts survive settlement without reviving work or acknowledging it."""

from uuid import uuid4

import pytest
from test_message_lifecycle import lifecycle as lifecycle
from test_message_lifecycle import message

from src.agentbus_message_lifecycle_service import LifecycleRejected
from src.agentbus_service import MessageStore, SendMessage


@pytest.mark.parametrize(
    "picked_up,deadline,field,initial,settled",
    [(False, 110, "deadline_elapsed", "unclaimed", "expired_unclaimed"),
     (True, 120, "lease_elapsed", "picked_up", "recovery_required")],
    ids=["expired", "recovery"],
)
def test_elapsed_outcomes_survive_settlement_and_restart_without_query_writes(
    lifecycle, monkeypatch, tmp_path, picked_up, deadline, field, initial, settled
):
    store, p = lifecycle
    clock = [100]
    monkeypatch.setattr("src.agentbus_message_lifecycle_service.time.time", lambda: clock[0])
    m = message(store, p["sender"], p["recipient"])
    token = str(uuid4())
    if picked_up:
        store.lifecycle.pickup(p["recipient"], m.cursor, token, 100)
    clock[0] = deadline - 1
    before = store._db.total_changes
    assert store.lifecycle.outcome(p["recipient"], m.cursor)[field] is False
    clock[0] = deadline
    due = store.lifecycle.outcome(p["recipient"], m.cursor)
    assert due["state"] == initial and due[field] is True
    assert store._db.total_changes == before
    store.lifecycle.settle(deadline)
    before = store._db.total_changes
    outcome = store.lifecycle.outcome(p["recipient"], m.cursor)
    assert outcome["state"] == settled and outcome[field] is True
    assert outcome["owned_by_this_session"] is picked_up
    assert store.lifecycle.target(p["recipient"], m.cursor)["pickup_token"] == (token if picked_up else None)
    assert store._db.total_changes == before
    restarted = MessageStore(tmp_path / "lifecycle.sqlite3")
    try:
        before = restarted._db.total_changes
        assert restarted.lifecycle.outcome(p["recipient"], m.cursor) == outcome
        assert restarted._db.total_changes == before
        with pytest.raises(LifecycleRejected) as error:
            restarted.lifecycle.outcome(p["foreign"], m.cursor)
        assert error.value.status == 404
    finally:
        restarted.close()


def test_late_completion_does_not_turn_pickup_deadline_into_expiry(lifecycle, monkeypatch):
    store, p = lifecycle
    m = message(store, p["sender"], p["recipient"])
    token = str(uuid4())
    store.lifecycle.pickup(p["recipient"], m.cursor, token, 100)
    monkeypatch.setattr("src.agentbus_message_lifecycle_service.time.time", lambda: 115)
    active = store.lifecycle.outcome(p["recipient"], m.cursor)
    assert active["state"] == "picked_up"
    assert active["deadline_elapsed"] is False and active["lease_elapsed"] is False
    store.lifecycle.settle(120)
    monkeypatch.setattr("src.agentbus_message_lifecycle_service.time.time", lambda: 125)
    assert store.lifecycle.outcome(p["recipient"], m.cursor)["lease_elapsed"] is True
    store.lifecycle.finish(p["recipient"], m.cursor, token, 125)
    before = store._db.total_changes
    handled = store.lifecycle.outcome(p["recipient"], m.cursor)
    assert handled["state"] == "handled"
    assert handled["deadline_elapsed"] is False and handled["lease_elapsed"] is False
    assert store._db.total_changes == before


def test_elapsed_expired_parent_stays_denied_while_new_report_is_allowed(lifecycle, monkeypatch):
    store, p = lifecycle
    old = message(store, p["sender"], p["recipient"])
    store.lifecycle.settle(110)
    monkeypatch.setattr("src.agentbus_message_lifecycle_service.time.time", lambda: 115)
    before = store._db.total_changes
    with pytest.raises(LifecycleRejected) as error:
        store.lifecycle.require_live_parent(p["recipient"].channel, old.cursor, p["recipient"].route)
    assert error.value.status == 410 and store._db.total_changes == before
    body = SendMessage(sender=p["recipient"].route, recipient="all", audience="broadcast",
                       text="A genuinely new milestone", kind="status")
    body._sender_assurance = "session"
    fresh = store.append(p["recipient"].channel, str(uuid4()), body)
    assert fresh.cursor != old.cursor
    assert store.lifecycle.outcome(p["recipient"], old.cursor)["state"] == "expired_unclaimed"
    assert store.lifecycle.outcome(p["sender"], fresh.cursor)["state"] == "unclaimed"
