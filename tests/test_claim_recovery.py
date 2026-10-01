"""Abandoned unrouted claims have one auditable current owner."""

import pytest

from src.agentbus_service import MessageStore, SendMessage

CHANNEL = "C123456"


def test_operator_reassignment_and_release_preserve_one_current_claimant(tmp_path):
    path = tmp_path / "messages.sqlite3"
    store = MessageStore(path)
    try:
        unrouted = store.append(CHANNEL, "1700000000.000001", SendMessage(
            sender="slack:U123", recipient="all", audience="unrouted", text="Please inspect",
        ))
        store.claim(CHANNEL, unrouted.cursor, "agentbus:old")
        changed = store.claim_recovery.reassign(
            channel=CHANNEL, cursor=unrouted.cursor, new_identity="agentbus:new",
            actor="operator", reason="old chat ended",
        )
        assert changed["previous_identity"] == "agentbus:old"
        assert store.claimant(CHANNEL, unrouted.cursor) == "agentbus:new"
        assert store.inbox(CHANNEL, "agentbus:old").messages == []
        assert [item.cursor for item in store.inbox(CHANNEL, "agentbus:new").messages] == [unrouted.cursor]
        assert store.claim_recovery.reassign(
            channel=CHANNEL, cursor=unrouted.cursor, new_identity="agentbus:new",
            actor="operator", reason="retry",
        )["changed"] is False
        released = store.claim_recovery.reassign(
            channel=CHANNEL, cursor=unrouted.cursor, new_identity=None,
            actor="operator", reason="return to unclaimed pool",
        )
        assert released["new_identity"] is None
        assert store.claimant(CHANNEL, unrouted.cursor) is None
        assert store.claim(CHANNEL, unrouted.cursor, "agentbus:later").identity == "agentbus:later"
        history = store.claim_recovery.history(CHANNEL, unrouted.cursor)
        assert [(row["previous_identity"], row["new_identity"]) for row in history] == [
            ("agentbus:old", "agentbus:new"), ("agentbus:new", None),
        ]
        assert all(row["actor"] == "operator" and row["reason"] for row in history)
    finally:
        store.close()
    restarted = MessageStore(path)
    try:
        assert restarted.claimant(CHANNEL, unrouted.cursor) == "agentbus:later"
        assert len(restarted.claim_recovery.history(CHANNEL, unrouted.cursor)) == 2
    finally:
        restarted.close()


def test_recovery_rejects_unclaimed_and_routed_messages(tmp_path):
    store = MessageStore(tmp_path / "messages.sqlite3")
    try:
        routed = store.append(CHANNEL, "1700000000.000001", SendMessage(
            sender="agentbus:a", recipient="agentbus:b", audience="direct", text="direct",
        ))
        with pytest.raises(ValueError, match="unrouted"):
            store.claim_recovery.reassign(channel=CHANNEL, cursor=routed.cursor,
                                          new_identity=None, actor="operator", reason="no")
        unrouted = store.append(CHANNEL, "1700000000.000002", SendMessage(
            sender="slack:U123", recipient="all", audience="unrouted", text="unclaimed",
        ))
        with pytest.raises(ValueError, match="no claim"):
            store.claim_recovery.reassign(channel=CHANNEL, cursor=unrouted.cursor,
                                          new_identity="agentbus:b", actor="operator", reason="no")
    finally:
        store.close()
