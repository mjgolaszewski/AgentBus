"""Old disposable queues reconcile against explicit semantic acknowledgements."""

import json
from urllib.parse import parse_qs, urlparse

from test_poll_client import POLICY, profile_at

from src import agentbus_poll_client as poll
from src.agentbus_codex_wake_events_client import eligible_events
from src.agentbus_scope_client import scope


def write_legacy_queue(path, events, cursor=400):
    # ACK retirement is characterized within already-migrated channel scope.
    # Legacy scope adoption itself requires the explicit migration command.
    profile = json.loads(path.read_text())
    poll.spool_path(path).write_text(json.dumps({**scope(profile), "cursor": cursor, "events": events}))


def msg(cursor):
    return {"kind": "MESSAGE", "id": str(cursor), "message": {
        "cursor": cursor, "audience": "direct", "recipient": "agentbus:flower",
        "kind": "request", "sender_assurance": "session", "actionable": True,
    }}


def test_old_acknowledged_queue_cannot_replay_after_restart(tmp_path):
    path, profile = profile_at(tmp_path)
    profile["ack_cursor"] = 344
    path.write_text(json.dumps(profile))
    write_legacy_queue(path, [msg(245), msg(345)])
    restarted_profile = json.loads(path.read_text())
    assert poll.peek_event(path, restarted_profile)["id"] == "345"
    state = json.loads(poll.spool_path(path).read_text())
    assert [event["id"] for event in state["events"]] == ["345"]
    assert state["cursor"] == 400 and restarted_profile["ack_cursor"] == 344


def test_capacity_queue_recovery_keeps_safety_events_and_new_work(tmp_path):
    path, profile = profile_at(tmp_path)
    profile["ack_cursor"] = 344
    events = [msg(cursor) for cursor in range(245, 345)] + [
        {"kind": "CONTROL", "id": "stop"},
        {"kind": "POLICY_CHANGED", "id": "policy"},
        {"kind": "ATTENTION_REQUIRED", "id": "transport"}, msg(345),
    ]
    write_legacy_queue(path, events)
    assert poll.peek_event(path, profile)["kind"] == "CONTROL"
    state = json.loads(poll.spool_path(path).read_text())
    assert {event["id"] for event in state["events"]} == {"stop", "policy", "transport", "345"}
    assert state["cursor"] == 400


def test_wake_projection_filters_stale_queue_without_writing(tmp_path):
    path, profile = profile_at(tmp_path)
    profile["ack_cursor"] = 344
    write_legacy_queue(path, [msg(245), msg(345)])
    before = poll.spool_path(path).read_bytes()
    assert eligible_events(path, profile) == ["MESSAGE:345"]
    assert poll.spool_path(path).read_bytes() == before


def test_ack_hook_retains_retirement_count_when_profile_already_advanced(tmp_path):
    path, profile = profile_at(tmp_path)
    write_legacy_queue(path, [msg(245), msg(345)])
    profile["ack_cursor"] = 344
    assert poll.retire_acknowledged_messages(path, profile, 344) == 1
    assert poll.retire_acknowledged_messages(path, profile, 344) == 0


def test_already_running_reader_uses_newer_profile_acknowledgement(tmp_path):
    path, old_profile = profile_at(tmp_path)
    current = {**old_profile, "ack_cursor": 344}
    path.write_text(json.dumps(current))
    write_legacy_queue(path, [msg(245), msg(345)])
    assert eligible_events(path, old_profile) == ["MESSAGE:345"]
    assert poll.peek_event(path, old_profile)["id"] == "345"
    assert old_profile["ack_cursor"] == 0


def test_late_worker_append_cannot_reintroduce_acknowledged_message(tmp_path):
    path, old_profile = profile_at(tmp_path)
    path.write_text(json.dumps({**old_profile, "ack_cursor": 344}))
    write_legacy_queue(path, [])
    poll.change_spool(path, old_profile, lambda state: poll._append(state, msg(245)))
    assert poll.peek_event(path, old_profile) is None
    assert json.loads(poll.spool_path(path).read_text())["cursor"] == 400


def test_worker_fetch_uses_ack_floor_and_reconciles_inflight_ack(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    profile["ack_cursor"] = 344
    path.write_text(json.dumps(profile))
    write_legacy_queue(path, [msg(245)], cursor=10)
    afters = []

    def api(_values, target, *_args, **_kwargs):
        if target.endswith("check-in"):
            return {"values": POLICY, "revision": "r1", "sources": {},
                    "ack_required": False, "controls": []}
        afters.append(int(parse_qs(urlparse(target).query)["after"][0]))
        current = json.loads(path.read_text())
        current["ack_cursor"] = 345
        current["participation"]["stopped"] = True
        path.write_text(json.dumps(current))
        return {"messages": [msg(345)["message"], msg(346)["message"]],
                "next_cursor": 346, "has_more": False}

    monkeypatch.setattr(poll, "api", api)
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
    state = json.loads(poll.spool_path(path).read_text())
    assert afters == [344]
    assert [event["id"] for event in state["events"]] == ["346"]
    assert state["cursor"] == 346
