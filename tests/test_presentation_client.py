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
        {"kind": "POLICY_CHANGED", "revision": "revision-2", "values": {
            "initial_interval_seconds": 60, "backoff_factor": 2, "max_interval_seconds": 1920,
        }},
        message_event(kind="blocker", audience="direct", reason="addressed to this identity"),
        message_event(kind="status", audience="direct", reason="addressed to this identity"),
        message_event(kind="message", audience="informational", reason="addressed to this identity"),
        message_event(reason="claimed by this identity"),
    ):
        rendered = render_event(event, json_mode=False, budget_bytes=1)
        assert len(rendered.encode("utf-8")) > 1
        assert not rendered.endswith("~")


def test_work_hold_controls_state_the_pause_and_resume_boundary():
    pause = {"kind": "CONTROL", "control": {"control_id": "hold-1",
             "kind": "pause_work", "reason": "Pause this task"}}
    resume = {"kind": "CONTROL", "control": {"control_id": "hold-2",
              "kind": "resume_work", "reason": "Continue this task"}}
    assert "remain reachable" in render_event(pause, json_mode=False, budget_bytes=1)
    assert "resume the prior task" in render_event(resume, json_mode=False, budget_bytes=1)


def test_broadcast_can_compact_and_full_record_is_unchanged():
    event = message_event(kind="handoff", audience="broadcast", reason="explicit broadcast")
    before = json.dumps(event, sort_keys=True)
    assert len(render_event(event, json_mode=False, budget_bytes=24).encode()) <= 24
    assert json.dumps(event, sort_keys=True) == before


def test_untrusted_directional_and_control_characters_are_visible_escapes():
    event = message_event()
    event["message"]["text"] = "trusted\u202eabc\u200b\x1b[31m"
    for mode in (False, True):
        rendered = render_event(event, json_mode=mode, budget_bytes=None)
        assert "\u202e" not in rendered and "\u200b" not in rendered and "\x1b" not in rendered
        assert "\\u202e" in rendered and "\\u200b" in rendered and "\\u001b" in rendered
    assert event["message"]["text"] == "trusted\u202eabc\u200b\x1b[31m"
