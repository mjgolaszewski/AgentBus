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


def test_temporary_override_expires_and_reverts_after_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    db, participation, controls = open_stores(path)
    participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    chat_id, session_id = enroll(participation, "one")
    baseline = participation.effective_policy("agentbus", chat_id)
    with pytest.raises(ValueError, match="duration"):
        controls.issue_auxiliary(kind="temporary_policy_override", routes=["agentbus:one"],
                                 reason="focus", actor="operator", override_values={"max_interval_seconds": 120},
                                 duration_seconds=0)
    with pytest.raises(ValueError, match="factor"):
        controls.issue_auxiliary(kind="temporary_policy_override", routes=["agentbus:one"],
                                 reason="focus", actor="operator", override_values={"backoff_factor": 0.5},
                                 duration_seconds=60)
    assert db.execute("SELECT COUNT(*) FROM operator_controls").fetchone()[0] == 0
    control_id, targets = controls.issue_auxiliary(
        kind="temporary_policy_override", routes=["agentbus:one"],
        reason="focus", actor="operator", override_values={"max_interval_seconds": 120},
        duration_seconds=60,
    )
    assert targets == [session_id]
    active = participation.effective_policy_for_session(session_id)
    assert active.values["max_interval_seconds"] == 120
    assert active.sources["max_interval_seconds"] == "temporary"
    assert active.revision != baseline.revision
    with pytest.raises(ValueError, match="active temporary"):
        controls.issue_auxiliary(kind="temporary_policy_override", routes=["agentbus:one"],
                                 reason="overlap", actor="operator", override_values={"max_interval_seconds": 180},
                                 duration_seconds=60)
    delivered = controls.deliver(session_id, SECRET)
    assert delivered[0]["control_id"] == control_id and delivered[0]["expires_at"]
    receipt, kind = controls.acknowledge_control(control_id, session_id, SECRET)
    assert kind == "temporary_policy_override"
    assert controls.acknowledge_control(control_id, session_id, SECRET)[0] == receipt
    assert participation.deliver_policy(session_id, SECRET).revision == active.revision
    with pytest.raises(ValueError, match="unknown or stale"):
        participation.acknowledge_policy(session_id, SECRET, baseline.revision)
    participation.acknowledge_policy(session_id, SECRET, active.revision)
    db.close()

    db, participation, controls = open_stores(path)
    assert participation.effective_policy_for_session(session_id).revision == active.revision
    db.execute("UPDATE operator_controls SET expires_at = ? WHERE control_id = ?",
               ((datetime.now().astimezone() - timedelta(seconds=1)).isoformat(), control_id))
    db.commit()
    assert participation.effective_policy_for_session(session_id).revision == baseline.revision
    assert controls.status(control_id)[0]["override_active"] is False
    reverted = participation.deliver_policy(session_id, SECRET)
    assert reverted.revision == baseline.revision
    with pytest.raises(ValueError, match="unknown or stale"):
        participation.acknowledge_policy(session_id, SECRET, active.revision)
    participation.acknowledge_policy(session_id, SECRET, baseline.revision)
    assert participation.session_state(session_id)["state"] == "active"
    db.close()


def test_existing_control_rows_survive_additive_override_columns(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE operator_controls (control_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
               "reason TEXT NOT NULL, actor TEXT NOT NULL, issued_at TEXT NOT NULL)")
    db.execute("INSERT INTO operator_controls VALUES (?, ?, ?, ?, ?)",
               ("old-control", "nudge", "check in", "operator", "2026-09-30T00:00:00+00:00"))
    db.commit()
    db.close()
    db, participation, controls = open_stores(path)
    assert tuple(db.execute("SELECT kind, reason FROM operator_controls WHERE control_id = 'old-control'").fetchone()) == (
        "nudge", "check in",
    )
    assert {row["name"] for row in db.execute("PRAGMA table_info(operator_controls)")} >= {
        "override_values_json", "expires_at",
    }
    db.close()
