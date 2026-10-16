"""Capacity rejection preserves enrollment retries and reserved route history."""

import pytest
from test_participation_store import BASE_POLICY, open_store

from src import agentbus_participation_store_service as participation

SECRET = "session-secret-with-at-least-32-characters"


def enroll(store, name, secret=SECRET):
    return store.enroll(repo="agentbus", route=f"agentbus:{name}", display_name=name,
                        session_secret=secret)


def test_enrollment_capacity_preserves_idempotency_and_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(participation, "MAX_ENROLLED_CHATS", 2, raising=False)
    path = tmp_path / "messages.sqlite3"
    db, store = open_store(path)
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    first = enroll(store, "first")
    enroll(store, "second", "second-session-secret-with-at-least-32-characters")
    assert enroll(store, "first") == first
    with pytest.raises(ValueError, match="enrollment capacity"):
        enroll(store, "third")
    assert db.execute("SELECT count(*) FROM chat_identities").fetchone()[0] == 2
    db.close()
    db, store = open_store(path)
    assert enroll(store, "first") == first
    with pytest.raises(ValueError, match="enrollment capacity"):
        enroll(store, "third")
    assert db.execute("SELECT count(*) FROM participation_sessions").fetchone()[0] == 2
    db.close()


def test_alias_capacity_keeps_existing_addresses_and_allows_alias_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(participation, "MAX_ALIASES_PER_CHAT", 2, raising=False)
    db, store = open_store(tmp_path / "messages.sqlite3")
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    chat, session, revision = enroll(store, "first")
    store.acknowledge_policy(session, SECRET, revision)
    store.rename(session, SECRET, "agentbus:second")
    store.rename(session, SECRET, "agentbus:third")
    with pytest.raises(ValueError, match="alias capacity"):
        store.rename(session, SECRET, "agentbus:fourth")
    assert store.current_route_for("agentbus:first") == "agentbus:third"
    store.rename(session, SECRET, "agentbus:first")
    assert store.current_route_for("agentbus:third") == "agentbus:first"
    assert db.execute("SELECT count(*) FROM routing_aliases WHERE chat_id=?", (chat,)).fetchone()[0] == 2
    db.close()


def test_oversized_session_secret_is_rejected_before_enrollment(tmp_path):
    db, store = open_store(tmp_path / "messages.sqlite3")
    store.set_policy(scope="global", scope_key="*", values=BASE_POLICY, actor="operator")
    with pytest.raises(ValueError, match="maximum length"):
        enroll(store, "first", "x" * 513)
    assert db.execute("SELECT count(*) FROM chat_identities").fetchone()[0] == 0
    db.close()
