"""Stop controls keep per-target truth across delivery, retry, and restart."""

import sqlite3
import threading
from datetime import datetime, timedelta

import pytest

from src.agentbus_control_store_service import ControlStore
from src.agentbus_participation_store_service import ParticipationStore

POLICY = {
    "initial_interval_seconds": 60, "backoff_factor": 2,
    "max_interval_seconds": 1920, "control_check_max_seconds": 60,
    "jitter_fraction": 0, "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}
SECRET = "session-secret-with-at-least-32-characters"


def open_stores(path):
    db = sqlite3.connect(path)
    lock = threading.RLock()
    participation = ParticipationStore(db, lock)
    controls = ControlStore(db, lock, participation)
    return db, participation, controls


def enroll(participation, name):
    chat_id, session_id, revision = participation.enroll(
        repo="agentbus", route=f"agentbus:{name}", display_name=name,
        session_secret=SECRET,
    )
    participation.acknowledge_policy(session_id, SECRET, revision)
    return chat_id, session_id


def test_stop_all_snapshots_overdue_targets_and_acks_are_session_bound(tmp_path) -> None:
    db, participation, controls = open_stores(tmp_path / "state.sqlite3")
    participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, first = enroll(participation, "one")
    _, second = enroll(participation, "two")
    db.execute("UPDATE participation_sessions SET state = 'overdue' WHERE session_id = ?", (second,))
    db.commit()
    control_id, targets = controls.issue_stop(routes=None, reason="operator stand-down", actor="operator")
    assert set(targets) == {first, second}
    _, later = enroll(participation, "later")
    assert later not in targets
    assert {row["state"] for row in controls.status(control_id)} == {"issued"}
    with pytest.raises(ValueError, match="delivery is pending"):
        controls.acknowledge_stop(control_id, first, SECRET)
    assert controls.deliver(first, SECRET)[0]["control_id"] == control_id
    with pytest.raises(PermissionError, match="session authority"):
        controls.acknowledge_stop(control_id, second, "wrong")
    receipt = controls.acknowledge_stop(control_id, first, SECRET)
    assert controls.acknowledge_stop(control_id, first, SECRET) == receipt
    assert participation.session_state(first)["state"] == "stopped"
    assert participation.session_state(second)["state"] == "overdue"
    assert controls.status(control_id)[0]["state"] in {"issued", "effective"}
    db.close()


def test_presence_is_read_only_and_reports_control_deadline_and_pending_stop(tmp_path) -> None:
    db, participation, controls = open_stores(tmp_path / "state.sqlite3")
    participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, session_id = enroll(participation, "one")
    before = participation.session_state(session_id)["last_client_contact_at"]
    observed = datetime.fromisoformat(before) + timedelta(minutes=4)
    overdue = participation.presence(session_id, observed_at=observed)
    assert overdue["state"] == "overdue"
    assert overdue["overdue_reason"] == "control check missed"
    assert participation.session_state(session_id)["last_client_contact_at"] == before
    control_id, _ = controls.issue_stop(routes=["agentbus:one"], reason="end turn", actor="operator")
    pending = participation.presence(session_id, observed_at=datetime.fromisoformat(before))
    assert pending["state"] == "control_pending"
    assert pending["outstanding_controls"] == [{"control_id": control_id, "state": "issued"}]
    assert participation.session_state(session_id)["last_client_contact_at"] == before
    db.close()


def test_restart_finishes_durable_ack_without_reactivating_stopped_session(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    db, participation, controls = open_stores(path)
    participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, session_id = enroll(participation, "one")
    control_id, _ = controls.issue_stop(routes=["agentbus:one"], reason="end turn", actor="operator")
    controls.deliver(session_id, SECRET)
    # Model a crash after the ack commit and before applying bus stop effect.
    receipt = "persisted-ack-receipt"
    db.execute(
        "UPDATE control_targets SET state = 'acknowledged', acknowledged_at = '2026-09-30T00:00:00Z', receipt = ? WHERE control_id = ? AND session_id = ?",
        (receipt, control_id, session_id),
    )
    db.commit()
    db.close()
    db, participation, controls = open_stores(path)
    assert participation.session_state(session_id)["state"] == "stopped"
    assert controls.status(control_id)[0]["state"] == "effective"
    assert controls.acknowledge_stop(control_id, session_id, SECRET) == receipt
    with pytest.raises(ValueError, match="stopped session"):
        participation.acknowledge_policy(session_id, SECRET, participation.session_state(session_id)["delivered_policy_revision"])
    db.close()


def test_nudge_and_checkpoint_receipts_do_not_stop_participation(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    db, participation, controls = open_stores(path)
    participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    _, session_id = enroll(participation, "one")
    nudge_id, _ = controls.issue_auxiliary(kind="nudge", routes=["agentbus:one"],
                                            reason="check in", actor="operator")
    checkpoint_id, _ = controls.issue_auxiliary(kind="checkpoint_request", routes=["agentbus:one"],
                                                 reason="report status", actor="operator")
    assert {item["kind"] for item in controls.deliver(session_id, SECRET)} == {
        "nudge", "checkpoint_request",
    }
    nudge_receipt, kind = controls.acknowledge_control(nudge_id, session_id, SECRET)
    assert kind == "nudge"
    assert controls.acknowledge_control(nudge_id, session_id, SECRET)[0] == nudge_receipt
    with pytest.raises(ValueError, match="different kind"):
        controls.acknowledge_stop(nudge_id, session_id, SECRET)
    with pytest.raises(ValueError, match="report is required"):
        controls.acknowledge_control(checkpoint_id, session_id, SECRET)
    report = {"activity": "inspecting CI", "blockers": ["provider queue"],
              "waiting_on": [], "work_refs": ["PR-42"]}
    checkpoint_receipt, kind = controls.acknowledge_control(
        checkpoint_id, session_id, SECRET, report=report,
    )
    assert kind == "checkpoint_request"
    assert controls.acknowledge_control(checkpoint_id, session_id, SECRET,
                                        report=report)[0] == checkpoint_receipt
    with pytest.raises(ValueError, match="differs"):
        controls.acknowledge_control(checkpoint_id, session_id, SECRET,
                                     report={**report, "activity": "different"})
    assert participation.session_state(session_id)["state"] == "active"
    assert {item["state"] for item in controls.status(checkpoint_id)} == {"effective"}
    db.close()
    db, participation, controls = open_stores(path)
    assert participation.session_state(session_id)["state"] == "active"
    assert controls.status(checkpoint_id)[0]["report"] == report
    db.close()
