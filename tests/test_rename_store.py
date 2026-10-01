"""Routing rename retains old addresses and stable control targeting."""

import pytest

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
    direct = store.append(CHANNEL, "1700000000.000001", SendMessage(
        sender="peer:agent", recipient="agentbus:old-name", audience="direct",
        text="Addressed before rename",
    ))
    unrouted = store.append(CHANNEL, "1700000000.000002", SendMessage(
        sender="slack:U123", recipient="all", audience="unrouted", text="Needs an owner",
    ))
    store.claim(CHANNEL, unrouted.cursor, "agentbus:old-name")
    control_id, targets = store.controls.issue_stop(
        routes=["agentbus:old-name"], reason="end turn", actor="operator",
    )
    assert targets == [session_id]
    assert store.participation.rename(session_id, SECRET, "agentbus:new-name") == (
        chat_id, "agentbus:old-name", "agentbus:new-name",
    )
    assert store.participation.routes_for("agentbus:new-name") == (
        "agentbus:new-name", "agentbus:old-name",
    )
    page = store.inbox(CHANNEL, "agentbus:new-name")
    assert [item.cursor for item in page.messages] == [direct.cursor, unrouted.cursor]
    assert all(item.actionable for item in page.messages)
    assert page.messages[0].recipient == "agentbus:old-name"
    store.validate_reply(CHANNEL, SendMessage(
        sender="agentbus:new-name", recipient="peer:agent", audience="direct",
        text="Handled", reply_to_cursor=direct.cursor,
    ))
    assert store.participation.enroll(
        repo="agentbus", route="agentbus:new-name", display_name="Imposter",
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
    assert store.participation.routes_for("agentbus:new-name") == (
        "agentbus:new-name", "agentbus:old-name",
    )
    assert store.controls.acknowledge_stop(control_id, session_id, SECRET)
    assert store.participation.session_state(session_id)["state"] == "stopped"
    store.close()
