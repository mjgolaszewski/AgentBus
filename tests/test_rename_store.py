"""Routing rename retains old addresses and stable control targeting."""

import pytest

from src.agentbus_codex_wake_client import _candidate
from src.agentbus_service import MessageStore, SendMessage

POLICY = {
    "initial_interval_seconds": 60, "backoff_factor": 2,
    "max_interval_seconds": 1920, "control_check_max_seconds": 60,
    "jitter_fraction": 0, "overdue_grace_seconds": 120,
    "presentation_budget_bytes": None,
}
SECRET = "session-secret-with-at-least-32-characters"
CHANNEL = "C123456"


def test_rename_preserves_old_messages_claims_and_pending_stop(tmp_path) -> None:
    path = tmp_path / "messages.sqlite3"
    store = MessageStore(path)
    store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    chat_id, session_id, revision = store.participation.enroll(
        repo="agentbus", route="agentbus:old-name", display_name="Old Name",
        session_secret=SECRET,
    )
    store.participation.acknowledge_policy(session_id, SECRET, revision)
    direct_input = SendMessage(
        sender="peer:agent", recipient="agentbus:old-name", audience="direct",
        text="Addressed before rename",
    )
    direct_input._sender_assurance = "session"
    direct = store.append(CHANNEL, "1700000000.000001", direct_input)
    unrouted = store.append(CHANNEL, "1700000000.000002", SendMessage(
        sender="slack:U123", recipient="all", audience="unrouted", text="Needs an owner",
    ))
    store.claim(CHANNEL, unrouted.cursor, "agentbus:old-name")
    control_id, targets = store.controls.issue_stop(
        routes=["agentbus:old-name"], reason="end turn", actor="operator",
    )
    assert targets == [session_id]
    assert store.participation.rename(session_id, SECRET, "home:agentbus:new-name") == (
        chat_id, "agentbus:old-name", "home:agentbus:new-name",
    )
    assert set(store.participation.routes_for("home:agentbus:new-name")) == {
        "home:agentbus:new-name", "agentbus:old-name",
    }
    page = store.inbox(CHANNEL, "home:agentbus:new-name")
    assert [item.cursor for item in page.messages] == [direct.cursor, unrouted.cursor]
    assert all(item.actionable for item in page.messages)
    assert page.messages[0].recipient == "agentbus:old-name"
    assert _candidate({"kind": "MESSAGE", "id": str(direct.cursor),
                       "message": page.messages[0].model_dump()}, "home:agentbus:new-name") == (
        3, f"MESSAGE:{direct.cursor}",
    )
    store.validate_reply(CHANNEL, SendMessage(
        sender="home:agentbus:new-name", recipient="peer:agent", audience="direct",
        text="Handled", reply_to_cursor=direct.cursor,
    ))
    assert store.participation.enroll(
        repo="agentbus", route="home:agentbus:new-name", display_name="Imposter",
        session_secret=SECRET,
    ) == (chat_id, session_id, revision)
    with pytest.raises(ValueError, match="reserved"):
        store.participation.enroll(
            repo="agentbus", route="agentbus:old-name", display_name="Imposter",
            session_secret=SECRET,
        )
    assert store.controls.deliver(session_id, SECRET)[0]["control_id"] == control_id
    store.close()
    store = MessageStore(path)
    assert set(store.participation.routes_for("home:agentbus:new-name")) == {
        "home:agentbus:new-name", "agentbus:old-name",
    }
    assert store.controls.acknowledge_stop(control_id, session_id, SECRET)
    assert store.participation.session_state(session_id)["state"] == "stopped"
    store.close()


def test_prefixed_routes_bind_repo_segment_and_reject_reserved_namespace(tmp_path) -> None:
    store = MessageStore(tmp_path / "messages.sqlite3")
    store.participation.set_policy(scope="global", scope_key="*", values=POLICY, actor="operator")
    for route in ("home:other:flower", "slack:agentbus:flower", "home:agentbus:Flower"):
        with pytest.raises(ValueError, match="invalid chat route"):
            store.participation.enroll(
                repo="agentbus", route=route, display_name="Flower", session_secret=SECRET,
            )
    chat_id, session_id, revision = store.participation.enroll(
        repo="agentbus", route="home:agentbus:flower", display_name="Flower",
        session_secret=SECRET,
    )
    store.participation.acknowledge_policy(session_id, SECRET, revision)
    assert store.participation.session_for_secret(SECRET, "home:agentbus:flower")["chat_id"] == chat_id
    with pytest.raises(ValueError, match="same repository"):
        store.participation.rename(session_id, SECRET, "home:other:flower")
    store.close()


def test_real_rename_alias_inbox_survives_quiet_worker_and_peek(tmp_path, monkeypatch):
    import json

    from src import agentbus_poll_client as poll
    store = MessageStore(tmp_path/'alias.sqlite3')
    store.channels.configure((CHANNEL,))
    store.participation.set_policy(scope='global', scope_key='*', values=POLICY, actor='operator')
    old, new = 'agentbus:old-name', 'home:agentbus:new-name'
    grant = store.channels.issue_grant(channel=CHANNEL, repo='agentbus', route=old, actor='operator')
    chat, session, revision = store.channels.enroll(grant, repo='agentbus', route=old, display_name='Alias', session_secret=SECRET)
    store.participation.acknowledge_policy(session, SECRET, revision)
    store.participation.rename(session, SECRET, new)
    body = SendMessage(sender='peer:agent', recipient=old, text='after rename', kind='blocker')
    body._sender_assurance = 'session'
    accepted = store.append(CHANNEL, '123.456', body)
    page = store.inbox(CHANNEL, new)
    assert len(page.messages) == 1 and page.messages[0].actionable is True
    profile = {'identity':new, 'chat_id':chat, 'channel':CHANNEL,
        'inbox_id':store.channels.inbox_id(CHANNEL), 'service_url':'http://127.0.0.1:8766',
        'quiet_mode':True, 'ack_cursor':0, 'participation':{'session_id':session,
            'session_secret':SECRET, 'policy_values':POLICY,'stopped':False}}
    path = tmp_path/'profile.json'
    path.write_text(json.dumps(profile))
    clock, checks = [0.0], [0]
    monkeypatch.setattr(poll.time, 'monotonic', lambda:clock[0])
    monkeypatch.setattr(poll.time, 'sleep', lambda seconds:clock.__setitem__(0, clock[0]+seconds))
    def api(_values, route, _payload=None, **_kwargs):
        if route.endswith('/check-in'):
            checks[0] += 1
            if checks[0] == 2:
                profile['participation']['stopped'] = True
                path.write_text(json.dumps(profile))
            return {'revision':revision,'values':POLICY,'sources':{},'ack_required':False}
        return page.model_dump()
    monkeypatch.setattr(poll, 'api', api)
    try:
        poll._worker(path, {'AGENTBUS_URL':profile['service_url']})
        event = poll.peek_event(path, profile, quiet=True)
        assert event['message']['cursor'] == accepted.cursor
        assert event['message']['recipient'] == old
        assert poll.peek_event(path, profile, quiet=False) == event
        assert json.loads(path.read_text())['ack_cursor'] == 0
        assert store.inbox(CHANNEL, new).messages[0].cursor == accepted.cursor
        assert store.channels.principal(SECRET,new).session_id == session
    finally:
        store.close()
