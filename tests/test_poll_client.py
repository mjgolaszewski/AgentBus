"""The detached polling loop stays silent on empty reads and keeps control probes alive."""

import fcntl
import json

from src import agentbus_poll_client as poll

POLICY = {
    "initial_interval_seconds": 1,
    "backoff_factor": 2,
    "max_interval_seconds": 4,
    "control_check_max_seconds": 1,
    "jitter_fraction": 0,
    "overdue_grace_seconds": 2,
    "presentation_budget_bytes": None,
}


def profile_at(tmp_path):
    path = tmp_path / "agentbus:flower.json"
    profile = {"identity": "agentbus:flower", "ack_cursor": 0,
               "participation": {"session_id": "session-1", "session_secret": "x" * 32,
                                 "policy_values": POLICY, "stopped": False}}
    path.write_text(json.dumps(profile))
    return path, profile


def test_read_only_spool_and_worker_observation_create_no_state(tmp_path):
    path, profile = profile_at(tmp_path)
    assert poll.read_spool_events(path) == []
    assert poll.worker_running(path) is False
    assert list(tmp_path.iterdir()) == [path]
    poll.change_spool(path, profile, lambda state: state["events"].append({"kind": "CONTROL", "id": "one"}))
    before = {item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()}
    assert poll.read_spool_events(path) == [{"kind": "CONTROL", "id": "one"}]
    assert poll.worker_running(path) is False
    assert {item.name: item.read_bytes() for item in tmp_path.iterdir() if item.is_file()} == before
    worker_lock = path.with_name(path.name + ".worker.lock")
    with worker_lock.open("wb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        assert poll.worker_running(path) is True


def test_worker_empty_cycles_are_silent_and_keep_control_checks_running(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    clock = [0.0]
    checks = []
    reads = []
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, _payload=None, **_kwargs):
        if route.endswith("/check-in"):
            checks.append(clock[0])
            if len(checks) == 3:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            return {"revision": "r1", "values": POLICY, "sources": {}, "ack_required": False}
        reads.append(clock[0])
        return {"messages": [], "next_cursor": 0, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {})
    assert checks == [0.0, 1.0, 2.0]
    assert reads == [0.0, 1.0]
    assert poll.peek_event(path, profile) is None


def test_post_restart_healthy_cycle_retires_durable_transport_alarm(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda state: poll._append(state, {
        "kind": "ATTENTION_REQUIRED", "id": "transport", "reason": "Cannot reach AgentBus",
    }))
    clock = [0.0]
    checks = [0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, _payload=None, **_kwargs):
        if route.endswith("/check-in"):
            checks[0] += 1
            if checks[0] == 2:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            return {"revision": "r1", "values": POLICY, "sources": {}, "ack_required": False}
        return {"messages": [], "next_cursor": 0, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {})
    assert checks[0] == 2
    assert poll.peek_event(path, profile) is None


def test_control_precedes_queued_message_and_message_is_retired_independently(tmp_path):
    path, profile = profile_at(tmp_path)

    def record(state):
        poll._append(state, {"kind": "MESSAGE", "id": "2", "message": {"cursor": 2}})
        poll._append(state, {"kind": "MESSAGE", "id": "10", "message": {"cursor": 10}})
        poll._append(state, {"kind": "CONTROL", "id": "stop-1", "control": {"control_id": "stop-1"}})

    poll.change_spool(path, profile, record)
    assert poll.peek_event(path, profile)["kind"] == "CONTROL"
    poll.retire_event(path, profile, "CONTROL", "stop-1")
    assert poll.peek_event(path, profile)["message"]["cursor"] == 2
    poll.retire_event(path, profile, "MESSAGE", "2")
    assert poll.peek_event(path, profile)["message"]["cursor"] == 10


def test_actionable_message_priority_is_independent_of_backlog_cursor(tmp_path):
    path, profile = profile_at(tmp_path)

    def record(state):
        for cursor, kind, audience, reason in (
            (1, "message", "informational", "informational"),
            (2, "message", "broadcast", "explicit broadcast"),
            (3, "message", "unrouted", "claimed by this identity"),
            (4, "request", "direct", "addressed to this identity"),
            (5, "blocker", "direct", "addressed to this identity"),
        ):
            poll._append(state, {"kind": "MESSAGE", "id": str(cursor), "message": {
                "cursor": cursor, "kind": kind, "audience": audience, "action_reason": reason,
                "recipient": profile["identity"] if audience == "direct" else "all",
            }})
        poll._append(state, {"kind": "POLICY_CHANGED", "id": "revision-2",
                             "revision": "revision-2", "values": POLICY})

    poll.change_spool(path, profile, record)
    assert poll.peek_event(path, profile)["kind"] == "POLICY_CHANGED"
    poll.retire_event(path, profile, "POLICY_CHANGED", "revision-2")
    observed = []
    for _ in range(5):
        event = poll.peek_event(path, profile)
        observed.append(event["message"]["cursor"])
        poll.retire_event(path, profile, "MESSAGE", event["id"])
    assert observed == [5, 4, 3, 2, 1]


def test_quiet_discards_only_routine_projection_and_keeps_durable_history(tmp_path):
    path, profile = profile_at(tmp_path)
    def record(state):
        for cursor, audience, kind, reason in (
            (1, "informational", "message", "informational"),
            (2, "broadcast", "request", "explicit broadcast"),
            (3, "direct", "request", "addressed to this identity"),
            (4, "unrouted", "message", "claimed by this identity"),
            (5, "direct", "status", "addressed to this identity"),
            (6, "direct", "status", "addressed to another identity"),
            (7, "informational", "message", "addressed to this identity"),
        ):
            poll._append(state, {"kind": "MESSAGE", "id": str(cursor), "message": {
                "cursor": cursor, "audience": audience, "kind": kind, "action_reason": reason,
                "recipient": "agentbus:someone-else" if cursor == 6 else
                             "all" if cursor in {1, 2, 4} else profile["identity"],
            }})
        poll._append(state, {"kind": "CONTROL", "id": "stop-1", "control": {"control_id": "stop-1"}})
        poll._append(state, {"kind": "POLICY_CHANGED", "id": "r2", "revision": "r2"})
    poll.change_spool(path, profile, record)
    assert poll.peek_event(path, profile, quiet=True)["kind"] == "CONTROL"
    state = json.loads(poll.spool_path(path).read_text())
    assert {event["id"] for event in state["events"]} == {"3", "4", "5", "7", "stop-1", "r2"}
    assert state["cursor"] == 0
    poll.retire_event(path, profile, "CONTROL", "stop-1")
    assert poll.peek_event(path, profile, quiet=True)["kind"] == "POLICY_CHANGED"
    poll.retire_event(path, profile, "POLICY_CHANGED", "r2")
    assert poll.peek_event(path, profile, quiet=True)["message"]["cursor"] == 3


def test_quiet_worker_advances_durable_cursor_without_routine_spool(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    profile["quiet_mode"] = True
    path.write_text(json.dumps(profile))
    clock = [0.0]
    checks = [0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, _payload=None, **_kwargs):
        if route.endswith("/check-in"):
            checks[0] += 1
            if checks[0] == 2:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            return {"revision": "r1", "values": POLICY, "sources": {}, "ack_required": False}
        return {"messages": [
            {"cursor": 1, "audience": "broadcast", "kind": "request", "action_reason": "explicit broadcast"},
            {"cursor": 2, "audience": "direct", "recipient": profile["identity"],
             "kind": "status", "action_reason": "addressed to this identity"},
        ], "next_cursor": 2, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {})
    state = json.loads(poll.spool_path(path).read_text())
    assert state["cursor"] == 2
    assert [event["id"] for event in state["events"]] == ["2"]


def test_worker_keeps_probing_controls_after_message_is_ready_for_agent(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    clock = [0.0]
    checks = [0]
    reads = [0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, _payload=None, **_kwargs):
        if route.endswith("/check-in"):
            checks[0] += 1
            if checks[0] == 2:
                return {"controls": [{"control_id": "stop-1", "kind": "stop_end_turn", "reason": "done"}]}
            if checks[0] == 3:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            return {"revision": "r1", "values": POLICY, "sources": {}, "ack_required": False}
        reads[0] += 1
        if reads[0] == 1:
            return {"messages": [{"cursor": 1, "sender": "agentbus:peer", "recipient": "agentbus:flower",
                                  "text": "work", "audience": "direct"}], "next_cursor": 1, "has_more": False}
        return {"messages": [], "next_cursor": 1, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {})
    assert checks[0] == 3
    assert poll.peek_event(path, profile)["kind"] == "CONTROL"
    poll.retire_event(path, profile, "CONTROL", "stop-1")
    assert poll.peek_event(path, profile)["message"]["text"] == "work"


def test_policy_tightening_accepts_old_report_then_clamps_worker_backoff(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    clock = [0.0]
    reports = []
    tighter = {**POLICY, "max_interval_seconds": 2}
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, payload=None, **_kwargs):
        if route.endswith("/check-in"):
            reports.append(payload["current_backoff_seconds"])
            if len(reports) == 7:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            policy = tighter if len(reports) >= 5 else POLICY
            return {"revision": "r2" if policy is tighter else "r1",
                    "values": policy, "sources": {}, "ack_required": policy is tighter}
        return {"messages": [], "next_cursor": 0, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {})
    assert reports[4] > tighter["max_interval_seconds"]
    assert reports[5:] == [2, 2]
    assert poll.peek_event(path, profile)["kind"] == "POLICY_CHANGED"
