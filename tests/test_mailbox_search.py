"""Retained search stays local and does not mutate custody."""

import pytest

from src.agentbus_search_service import decode_token, search_token
from src.agentbus_service import MessageStore, SendMessage


def test_search_is_literal_channel_scoped_paginated_and_writer_maintained(tmp_path):
    store = MessageStore(tmp_path / "bus.db")
    store.channels.configure(("C111", "C222"))
    a = store.append("C111", "100.1", SendMessage(sender="human", text="hello green world"))
    store.append("C222", "100.1", SendMessage(sender="human", text="hello private world"))
    store.append("C111", "100.2", SendMessage(sender="human", text="hello fresh world"))
    before = store._db.total_changes
    assert [x["position"] for x in store.search.search("C111", "hello", limit=1)] == [1, 2]
    assert [x["position"] for x in store.search.search("C111", "hello", after=1)] == [2]
    assert store.search.search("C111", "private") == []
    assert store.search.search("C111", "hello OR private") == []
    assert store._db.total_changes == before
    with store._lock, store._db:
        store._db.execute("DELETE FROM channel_positions WHERE message_id=?", (a.cursor,))
        store._db.execute("DELETE FROM messages WHERE cursor=?", (a.cursor,))
    assert [x["position"] for x in store.search.search("C111", "hello")] == [2]
    store.close()
    reopened = MessageStore(tmp_path / "bus.db")
    assert [x["position"] for x in reopened.search.search("C111", "fresh")] == [2]
    reopened.close()


def test_search_tokens_bind_exact_session_scope_query_and_public_position():
    scope = ["chat", "session", "C111", "inbox"]
    token = search_token("secret", scope, "green", 7)
    assert decode_token("secret", scope, "green", token) == 7
    for secret, other, query, value in [
        ("wrong", scope, "green", token),
        ("secret", scope, "other", token),
        ("secret", ["foreign"], "green", token),
        ("secret", scope, "green", token + "x"),
    ]:
        with pytest.raises(ValueError):
            decode_token(secret, other, query, value)


def test_lifecycle_activation_keeps_authoritative_writer_guards_and_search(tmp_path):
    store = MessageStore(tmp_path / "lifecycle.db")
    store.channels.configure(("C111",))
    store.lifecycle.activate_fresh()
    store.append("C111", "100.1", SendMessage(sender="human", text="green"))
    assert len(store.search.search("C111", "green")) == 1
    names = {x[0] for x in store._db.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert "lifecycle_writer_messages_INSERT" in names
    assert "lifecycle_writer_messages_DELETE" in names
    store.close()


def test_index_migration_failure_is_atomic_and_preserves_original_history(tmp_path):
    import sqlite3

    from src.agentbus_search_service import SearchStore
    store = MessageStore(tmp_path/'migration.db')
    store.channels.configure(('C111',))
    for name in ('mailbox_search_insert','mailbox_search_delete','mailbox_search_update'):
        store._db.execute('DROP TRIGGER '+name)
    store._db.execute('DROP TABLE mailbox_search')
    store._db.commit()
    store.append('C111','100.1',SendMessage(sender='human',text='retained green'))
    def deny_backfill(action, name, _column, _database, _source):
        return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_INSERT and name=='mailbox_search' else sqlite3.SQLITE_OK
    store._db.set_authorizer(deny_backfill)
    with pytest.raises(sqlite3.DatabaseError):
        SearchStore(store)
    store._db.set_authorizer(None)
    assert store._db.execute("SELECT count(*) FROM sqlite_master WHERE name='mailbox_search'").fetchone()[0]==0
    assert store._db.execute('SELECT count(*) FROM messages').fetchone()[0]==1
    index = SearchStore(store)
    assert len(index.search('C111','green'))==1
    store.close()


def test_unsupported_fts_refuses_before_creating_any_database_state(tmp_path, monkeypatch):
    import sqlite3

    from src.agentbus_service import MessageStore

    original = sqlite3.connect
    def without_fts(*args, **kwargs):
        db = original(*args, **kwargs)
        db.create_function("sqlite_compileoption_used", 1, lambda _option: 0)
        return db
    monkeypatch.setattr(sqlite3, "connect", without_fts)
    destination = tmp_path / "new-store" / "messages.sqlite3"
    with pytest.raises(RuntimeError, match="FTS5"):
        MessageStore(destination)
    assert not destination.parent.exists()
