"""Durable identity and policy-ack authority across service restarts."""

import sqlite3
import threading
import uuid

import pytest

from src.agentbus_participation_store_service import ParticipationStore

BASE_POLICY = {
    "initial_interval_seconds": 60,
    "backoff_factor": 2,
    "max_interval_seconds": 1920,
    "control_check_max_seconds": 60,
    "jitter_fraction": 0,
    "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}


def open_store(path):
    db = sqlite3.connect(path)
    return db, ParticipationStore(db, threading.RLock())


def test_join_requires_exact_session_policy_ack_and_survives_restart(tmp_path) -> None:
    path = tmp_path / "messages.sqlite3"
    secret = "session-secret-with-at-least-32-characters"
    db, store = open_store(path)
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    chat_id, session_id, revision = store.enroll(
        repo="agentbus", route="agentbus:signal-gardener",
        display_name="Signal Gardener", session_secret=secret,
    )
    assert revision == store.effective_policy("agentbus", chat_id).revision
    assert store.session_state(session_id)["state"] == "joining"
    with pytest.raises(PermissionError, match="session authority"):
        store.acknowledge_policy(session_id, "wrong", revision)
    with pytest.raises(ValueError, match="unknown or stale"):
        store.acknowledge_policy(session_id, secret, "revision-2")
    receipt = store.acknowledge_policy(session_id, secret, revision)
    assert store.acknowledge_policy(session_id, secret, revision) == receipt
    assert store.session_state(session_id)["state"] == "active"
    db.close()

    db, store = open_store(path)
    assert store.session_state(session_id)["acknowledged_policy_revision"] == revision
    assert store.acknowledge_policy(session_id, secret, revision) == receipt
    assert store.enroll(
        repo="agentbus", route="agentbus:signal-gardener",
        display_name="Signal Gardener", session_secret=secret,
    ) == (chat_id, session_id, revision)
    with pytest.raises(ValueError, match="already enrolled"):
        store.enroll(repo="agentbus", route="agentbus:signal-gardener",
                     display_name="Imposter", session_secret="x" * 32)
    assert store.contact(session_id, secret)
    store.set_policy(scope="repo", scope_key="agentbus", values={"max_interval_seconds": 600}, actor="operator")
    pending = store.session_state(session_id)
    assert pending["effective_policy_revision"] != revision
    assert pending["delivered_policy_revision"] == revision
    assert pending["acknowledged_policy_revision"] == revision
    with pytest.raises(ValueError, match="unknown or stale"):
        store.acknowledge_policy(session_id, secret, revision)
    delivered = store.deliver_policy(session_id, secret)
    assert delivered.revision == pending["effective_policy_revision"]
    assert store.session_state(session_id)["acknowledged_policy_revision"] == revision
    new_receipt = store.acknowledge_policy(session_id, secret, delivered.revision)
    assert new_receipt != receipt
    assert store.acknowledge_policy(session_id, secret, delivered.revision) == new_receipt
    db.close()


def test_policy_revisions_persist_and_resolve_global_repo_chat(tmp_path) -> None:
    path = tmp_path / "messages.sqlite3"
    db, store = open_store(path)
    with pytest.raises(ValueError, match="global policy"):
        store.set_policy(scope="repo", scope_key="agentbus", values={"max_interval_seconds": 600}, actor="operator")
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    store.set_policy(scope="repo", scope_key="agentbus", values={"max_interval_seconds": 600}, actor="operator")
    chat_id, _, _ = store.enroll(
        repo="agentbus", route="agentbus:signal-gardener",
        display_name="Signal Gardener", session_secret="session-secret-with-at-least-32-characters",
    )
    store.set_policy(scope="chat", scope_key=chat_id, values={"max_interval_seconds": 240}, actor="operator")
    effective = store.effective_policy("agentbus", chat_id)
    assert effective.max_interval_seconds == 240
    assert effective.sources["max_interval_seconds"] == "chat"
    assert effective.sources["control_check_max_seconds"] == "global"
    db.close()

    db, store = open_store(path)
    assert store.effective_policy("agentbus", chat_id).revision == effective.revision
    db.close()


def test_operator_handoff_preserves_legacy_chat_id_and_cannot_be_replayed(tmp_path) -> None:
    db, store = open_store(tmp_path / "messages.sqlite3")
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    old_chat_id = str(uuid.uuid4())
    with pytest.raises(ValueError, match="UUID"):
        store.issue_profile_handoff(chat_id="agentbus:old", repo="agentbus",
                                    route="agentbus:old", actor="operator")
    token = store.issue_profile_handoff(chat_id=old_chat_id, repo="agentbus",
                                        route="agentbus:old", actor="operator")
    db.close()
    db, store = open_store(tmp_path / "messages.sqlite3")
    with pytest.raises(ValueError, match="invalid"):
        store.enroll(repo="agentbus", route="agentbus:wrong", display_name="Old",
                     session_secret="a" * 32, handoff_token=token)
    chat_id, session_id, revision = store.enroll(
        repo="agentbus", route="agentbus:old", display_name="Old",
        session_secret="a" * 32, handoff_token=token,
    )
    assert chat_id == old_chat_id
    db.close()
    db, store = open_store(tmp_path / "messages.sqlite3")
    assert store.enroll(repo="agentbus", route="agentbus:old", display_name="Old",
                        session_secret="a" * 32, handoff_token=token) == (chat_id, session_id, revision)
    assert store.acknowledge_policy(session_id, "a" * 32, revision)
    with pytest.raises(ValueError, match="already used"):
        store.enroll(repo="agentbus", route="agentbus:old", display_name="Old",
                     session_secret="b" * 32, handoff_token=token)
    audit = db.execute("SELECT actor, action, chat_id FROM participation_audit WHERE action = 'handoff_issued'").fetchone()
    assert tuple(audit) == ("operator", "handoff_issued", old_chat_id)
    db.close()
