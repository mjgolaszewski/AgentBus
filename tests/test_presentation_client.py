"""Presentation budgets never alter evidence or hide mandatory agent work."""

import json

from src.agentbus_presentation_client import render_event


def message_event(*, kind="message", audience="informational", reason="informational"):
    return {"kind": "MESSAGE", "id": "42", "message": {
        "cursor": 42, "sender": "agentbus:peer", "recipient": "agentbus:flower",
        "kind": kind, "audience": audience, "action_reason": reason,
        "text": "A long note about the next release 🌼" * 8,
    }}


def test_replaceable_message_is_bounded_by_utf8_bytes_but_json_remains_complete():
    event = message_event()
    compact = render_event(event, json_mode=False, budget_bytes=32)
    assert len(compact.encode("utf-8")) <= 32
    assert compact.endswith("~")
    assert render_event(event, json_mode=False, budget_bytes=1) == "~"
    assert json.loads(render_event(event, json_mode=True, budget_bytes=1)) == event
    assert event["message"]["text"].endswith("🌼")


def test_control_policy_direct_request_and_claimed_work_ignore_tiny_budget():
    for event in (
        {"kind": "CONTROL", "control": {"control_id": "stop-1", "kind": "stop_end_turn",
                                        "reason": "Stand down"}},
        {"kind": "POLICY_CHANGED", "revision": "revision-2"},
        message_event(kind="blocker", audience="direct", reason="addressed to this identity"),
        message_event(reason="claimed by this identity"),
    ):
        rendered = render_event(event, json_mode=False, budget_bytes=1)
        assert len(rendered.encode("utf-8")) > 1
        assert not rendered.endswith("~")


def test_broadcast_can_compact_and_full_record_is_unchanged():
    event = message_event(kind="handoff", audience="broadcast", reason="explicit broadcast")
    before = json.dumps(event, sort_keys=True)
    assert len(render_event(event, json_mode=False, budget_bytes=24).encode()) <= 24
    assert json.dumps(event, sort_keys=True) == before
