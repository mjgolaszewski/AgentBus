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
               "chat_id": "chat-one", "channel": "C123", "inbox_id": "inbox-one", "service_url": "http://127.0.0.1:8766",
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


def test_worker_empty_cycles_are_silent_and_keep_control_checks_running(tmp_path, monkeypatch, capsys):
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
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
    assert checks == [0.0, 1.0, 2.0]
    assert reads == [0.0, 1.0]
    assert capsys.readouterr() == ('', '')
    assert poll.peek_event(path, profile) is None


def test_post_restart_healthy_cycle_retires_durable_transport_alarm(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    def saturate(state):
        state["cursor"] = 100
        for cursor in range(1, 101):
            poll._append(state, {"kind": "MESSAGE", "id": str(cursor),
                                 "message": {"cursor": cursor}})
        poll._append(state, {"kind": "ATTENTION_REQUIRED", "id": "transport",
                             "reason": "Cannot reach AgentBus"})
    poll.change_spool(path, profile, saturate)
    clock = [0.0]
    checks = [0]
    reads = [0]
    monkeypatch.setattr(poll.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(poll.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))

    def fake_api(_values, route, _payload=None, **_kwargs):
        if route.endswith("/check-in"):
            checks[0] += 1
            if checks[0] == 2:
                profile["participation"]["stopped"] = True
                path.write_text(json.dumps(profile))
            return {"revision": "r1", "values": POLICY, "sources": {}, "ack_required": False}
        reads[0] += 1
        return {"messages": [], "next_cursor": 0, "has_more": False}

    monkeypatch.setattr(poll, "api", fake_api)
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
    assert checks[0] == 2
    assert reads[0] == 0
    state = json.loads(poll.spool_path(path).read_text())
    assert state["cursor"] == 100
    assert len(state["events"]) == 100
    assert {event["id"] for event in state["events"]} == {str(i) for i in range(1, 101)}


def test_failed_check_in_keeps_transport_alarm_when_presentation_is_full(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    def saturate(state):
        state["cursor"] = 100
        for cursor in range(1, 101):
            poll._append(state, {"kind": "MESSAGE", "id": str(cursor),
                                 "message": {"cursor": cursor}})
        poll._append(state, {"kind": "ATTENTION_REQUIRED", "id": "transport",
                             "reason": "Cannot reach AgentBus"})
    poll.change_spool(path, profile, saturate)
    checks = [0]

    def failed_api(_values, route, _payload=None, **_kwargs):
        assert route.endswith("/check-in")
        checks[0] += 1
        profile["participation"]["stopped"] = True
        path.write_text(json.dumps(profile))
        raise poll.ClientError("Cannot reach AgentBus")

    monkeypatch.setattr(poll, "api", failed_api)
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
    assert checks[0] == 1
    state = json.loads(poll.spool_path(path).read_text())
    assert state["cursor"] == 100
    assert len(state["events"]) == 101
    assert any(event["kind"] == "ATTENTION_REQUIRED" for event in state["events"])


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


def test_acknowledged_messages_are_retired_without_touching_controls(tmp_path):
    path, profile = profile_at(tmp_path)

    def record(state):
        poll._append(state, {"kind": "MESSAGE", "id": "2", "message": {"cursor": 2}})
        poll._append(state, {"kind": "MESSAGE", "id": "4", "message": {"cursor": 4}})
        poll._append(state, {"kind": "CONTROL", "id": "stop-1",
                             "control": {"control_id": "stop-1"}})

    poll.change_spool(path, profile, record)
    assert poll.retire_acknowledged_messages(path, profile, 2) == 1
    state = json.loads(poll.spool_path(path).read_text())
    assert [event["id"] for event in state["events"]] == ["4", "stop-1"]


def test_acknowledged_message_cleanup_is_idempotent(tmp_path):
    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda state: poll._append(
        state, {"kind": "MESSAGE", "id": "2", "message": {"cursor": 2}}
    ))
    assert poll.retire_acknowledged_messages(path, profile, 2) == 1
    assert poll.retire_acknowledged_messages(path, profile, 2) == 0


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
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
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
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
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
    poll._worker(path, {"AGENTBUS_URL": profile["service_url"]})
    assert reports[4] > tighter["max_interval_seconds"]
    assert reports[5:] == [2, 2]
    assert poll.peek_event(path, profile)["kind"] == "POLICY_CHANGED"


def test_quiet_keeps_service_authorized_alias_at_direct_priority(tmp_path):
    path, profile = profile_at(tmp_path)
    old_route = 'agentbus:old-name'
    event = {'kind':'MESSAGE','id':'7','message':{'cursor':7,'recipient':old_route,
        'audience':'direct','kind':'blocker','actionable':True,
        'action_reason':'addressed to this identity','sender_assurance':'session'}}
    poll.change_spool(path, profile, lambda state: state['events'].extend([
        {'kind':'MESSAGE','id':'8','message':{'cursor':8,'recipient':'all','audience':'broadcast'}}, event]))
    assert poll.peek_event(path, profile, quiet=True) == event
    assert poll.peek_event(path, profile, quiet=False) == event
    assert json.loads(path.read_text())['identity'] == profile['identity']
    assert json.loads(poll.spool_path(path).read_text())['events'] == [event]


def test_client_cadence_projection_matches_domain_without_service_import():
    from src.agentbus_participation_service import next_inbox_interval_values
    for initial, maximum in [(1, 32), (60, 1920), (5, 17), (2.5, 90)]:
        values = {'initial_interval_seconds':initial, 'max_interval_seconds':maximum}
        for previous in [None, 0, initial, 3*initial, maximum, maximum+1]:
            for delta in [True, False]:
                assert poll.next_inbox_interval_values(values, previous, semantic_delta=delta) == next_inbox_interval_values(values, previous, semantic_delta=delta)


def test_client_cadence_rejects_every_domain_invalid_policy_value():
    import pytest

    from src.agentbus_participation_service import next_inbox_interval_values
    for field in ('initial_interval_seconds', 'max_interval_seconds'):
        for invalid in (True, False, None, '60', 0, -1, float('nan'), float('inf')):
            values = {'initial_interval_seconds':60, 'max_interval_seconds':1920, field:invalid}
            for calculate in (next_inbox_interval_values, poll.next_inbox_interval_values):
                with pytest.raises(ValueError):
                    calculate(values, None, semantic_delta=False)


def test_fresh_snapshot_retires_only_observed_ordinary_controls(tmp_path):
    path, profile = profile_at(tmp_path)

    def event(name, kind):
        return {"kind": "CONTROL", "id": name,
                "control": {"control_id": name, "kind": kind, "read_deadline": 1}}
    events = [event("expired", "broadcast"), event("active", "poll"),
              event("stop", "stop_end_turn"), event("hold", "pause_work"),
              event("checkpoint", "checkpoint_request"), event("new", "broadcast"),
              {"kind": "CONTROL", "id": "unknown"}]
    poll.change_spool(path, profile, lambda state: state["events"].extend(events))
    original_profile = path.read_bytes()
    poll.reconcile_announcement_snapshot(path, profile,
        {"pending_announcement_ids": ["active"]}, {"expired", "active", "stop", "hold", "checkpoint", "unknown"})
    assert [event["id"] for event in poll.read_spool_events(path, profile=profile)] == [
        "active", "stop", "hold", "checkpoint", "new", "unknown"]
    assert path.read_bytes() == original_profile  # No semantic cursor or session changes.
    restarted = json.loads(path.read_text())
    assert poll.peek_event(path, restarted)["kind"] == "CONTROL"


def test_legacy_and_malformed_snapshots_preserve_poll_evidence(tmp_path):
    import pytest

    from src.agentbus_transport_client import ClientError

    path, profile = profile_at(tmp_path)
    event = {"kind": "CONTROL", "id": "expired", "control": {"kind": "poll", "read_deadline": 1}}
    poll.change_spool(path, profile, lambda state: state["events"].append(event))
    before = poll.spool_path(path).read_bytes()
    poll.reconcile_announcement_snapshot(path, profile, {}, {"expired"})
    for invalid in [None, "expired", [None], ["x", "x"], ["x" * 81]]:
        with pytest.raises(ClientError, match="snapshot"):
            poll.reconcile_announcement_snapshot(path, profile,
                {"pending_announcement_ids": invalid}, {"expired"})
        assert poll.spool_path(path).read_bytes() == before
    assert poll.peek_event(path, profile) == event  # Local clock cannot retire it.


def test_foreign_snapshot_projection_is_rejected_before_retirement(tmp_path):
    import pytest

    from src.agentbus_transport_client import ClientError

    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda state: state["events"].append(
        {"kind": "CONTROL", "id": "expired", "control": {"kind": "broadcast"}}))
    before = poll.spool_path(path).read_bytes()
    with pytest.raises(ClientError, match="scope"):
        poll.reconcile_announcement_snapshot(path, {**profile, "channel": "foreign"},
            {"pending_announcement_ids": []}, {"expired"})
    assert poll.spool_path(path).read_bytes() == before


def test_foreground_check_in_projects_fresh_control_before_cached_message(tmp_path, monkeypatch, capsys):
    from src import agentbus_client as client
    from src.agentbus_parser_client import parse_args
    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda state: poll._append(state,
        {'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7}}))
    values = {'AGENTBUS_URL': profile['service_url'], 'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path)}
    monkeypatch.setattr(client, 'control_profile', lambda *_: (profile, {}))
    control = {'control_id': 'fresh-stop', 'kind': 'stop_end_turn', 'reason': 'stop'}
    monkeypatch.setattr(client, 'api', lambda *_args, **_kwargs: {'controls': [control]})
    ns = parse_args(['check-in', '--identity', profile['identity']])
    assert client._execute_command(ns, tmp_path, values) == 0
    assert poll.peek_event(path, profile)['control'] == control
    assert json.loads(path.read_text())['ack_cursor'] == 0
    assert 'fresh-stop' in capsys.readouterr().out


def test_complete_retires_cached_notice_without_semantic_ack(tmp_path, monkeypatch, capsys):
    from src import agentbus_client as client
    from src import agentbus_lifecycle_client as lifecycle
    from src.agentbus_parser_client import parse_args
    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda state: poll._append(state,
        {'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7}}))
    values = {'AGENTBUS_URL': profile['service_url'], 'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path)}
    monkeypatch.setattr(client, 'load_profile', lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(lifecycle, 'message_command', lambda *_args, **_kwargs:
                        {'cursor': 7, 'state': 'handled', 'receipt_id': 'original'})
    ns = parse_args(['complete', '--identity', profile['identity'], '--cursor', '7'])
    assert client.cli_lifecycle_message(ns, tmp_path, values) == 0
    assert poll.read_spool_events(path, profile=profile) == []
    assert json.loads(path.read_text())['ack_cursor'] == 0
    assert 'original' in capsys.readouterr().out


def test_cached_message_retirement_requires_fresh_exact_terminal_outcome(tmp_path):
    import pytest

    path, profile = profile_at(tmp_path)
    profile['message_lifecycle_version'] = 1
    event = {'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7, 'lifecycle': {'state': 'unclaimed'}}}
    values = {'AGENTBUS_URL': profile['service_url']}
    for state in ('unclaimed', 'picked_up', 'recovery_required', 'unknown', 'handled', 'expired_unclaimed'):
        poll.change_spool(path, profile, lambda spool: poll._append(spool, event))
        def read(*_args, **_kwargs):
            return {'cursor': 7, 'scope': 'recipient', 'state': state}
        retired = poll.reconcile_message_event(values, path, profile, event, api_call=read)
        assert retired == (state in {'handled', 'expired_unclaimed'})
        assert bool(poll.read_spool_events(path, profile=profile)) != retired
    poll.change_spool(path, profile, lambda spool: poll._append(spool, event))
    for response in ({'cursor': 8, 'scope': 'recipient', 'state': 'handled'},
                     {'cursor': 7, 'scope': 'sender', 'state': 'handled'}):
        with pytest.raises(poll.ClientError, match='scope differs'):
            poll.reconcile_message_event(values, path, profile, event, api_call=lambda *_a, **_k: response)
        assert poll.read_spool_events(path, profile=profile) == [event]
    assert json.loads(path.read_text())['ack_cursor'] == 0


def test_worker_skips_terminal_presentation_and_advances_only_private_scan(tmp_path, monkeypatch):
    path, profile = profile_at(tmp_path)
    profile['message_lifecycle_version'] = 1
    path.write_text(json.dumps(profile))
    def read(_values, route, *_args, **_kwargs):
        if route.endswith('/check-in'):
            profile['participation']['stopped'] = True
            path.write_text(json.dumps(profile))
            return {'revision': 'r1', 'values': POLICY, 'sources': {}, 'ack_required': False}
        return {'messages': [{'cursor': i, 'lifecycle': {'state': state}} for i, state in
                             enumerate(('handled', 'expired_unclaimed', 'recovery_required'), 1)],
                'next_cursor': 3, 'has_more': False}
    monkeypatch.setattr(poll, 'api', read)
    poll._worker(path, {'AGENTBUS_URL': profile['service_url']})
    state = json.loads(poll.spool_path(path).read_text())
    assert state['cursor'] == 3
    assert [event['id'] for event in state['events']] == ['3']
    assert json.loads(path.read_text())['ack_cursor'] == 0


def test_wake_prompt_uses_observing_inbox_instead_of_stateless_history():
    from src.agentbus_codex_wake_events_client import _prompt
    prompt = _prompt('example:project:agent', 'attempt', ['MESSAGE:7'], ack_cursor=3)
    assert 'agentbus inbox --identity example:project:agent`' in prompt
    assert '--after' not in prompt
    assert 'only after handling' in prompt


def test_mismatched_completion_keeps_presentation_evidence(tmp_path, monkeypatch):
    import pytest

    from src import agentbus_client as client
    from src import agentbus_lifecycle_client as lifecycle
    from src.agentbus_parser_client import parse_args
    path, profile = profile_at(tmp_path)
    event = {'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7}}
    poll.change_spool(path, profile, lambda state: poll._append(state, event))
    values = {'AGENTBUS_URL': profile['service_url'], 'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path)}
    monkeypatch.setattr(client, 'load_profile', lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(lifecycle, 'message_command', lambda *_args, **_kwargs:
                        {'cursor': 8, 'state': 'handled'})
    ns = parse_args(['complete', '--identity', profile['identity'], '--cursor', '7'])
    with pytest.raises(client.ClientError, match='scope differs'):
        client.cli_lifecycle_message(ns, tmp_path, values)
    assert poll.read_spool_events(path, profile=profile) == [event]
    assert json.loads(path.read_text())['ack_cursor'] == 0


def test_stop_precedes_unanswered_poll_without_retiring_other_controls(tmp_path):
    path, profile = profile_at(tmp_path)
    events = [{'kind': 'CONTROL', 'id': 'a-poll', 'control': {'kind': 'poll'}},
              {'kind': 'CONTROL', 'id': 'b-broadcast', 'control': {'kind': 'broadcast'}},
              {'kind': 'CONTROL', 'id': 'z-stop', 'control': {'kind': 'stop_end_turn'}}]
    poll.change_spool(path, profile, lambda state: state['events'].extend(events))
    before = poll.spool_path(path).read_bytes()
    for _ in range(3):
        assert poll.peek_event(path, profile, quiet=True)['id'] == 'z-stop'
    assert poll.spool_path(path).read_bytes() == before
    assert json.loads(path.read_text())['ack_cursor'] == 0
    poll.retire_event(path, profile, 'CONTROL', 'z-stop')
    assert poll.peek_event(path, profile)['id'] == 'a-poll'


def test_foreground_poll_checks_fresh_controls_before_presenting_cached_work(tmp_path, monkeypatch, capsys):
    from src import agentbus_client as client
    from src.agentbus_parser_client import parse_args

    path, profile = profile_at(tmp_path)
    event = {'kind': 'MESSAGE', 'id': '7', 'message': {
        'cursor': 7, 'sender': 'agentbus:peer', 'recipient': profile['identity'],
        'audience': 'direct', 'kind': 'request', 'text': 'retained unfinished work',
        'actionable': True, 'lifecycle': {'state': 'picked_up'},
    }}
    poll.change_spool(path, profile, lambda spool: poll._append(spool, event))
    values = {'AGENTBUS_URL': profile['service_url'], 'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path)}
    monkeypatch.setattr(client, 'checked_profile', lambda *_args: (profile, {}))
    monkeypatch.setattr(poll, 'ensure_worker', lambda *_args: None)
    calls = []
    control = {'control_id': 'fresh-owner-stop', 'kind': 'stop_end_turn', 'reason': 'stop now'}

    def service(_values, route, body=None, **kwargs):
        assert route == '/v1/sessions/session-1/check-in'
        assert kwargs['session_token'] == profile['participation']['session_secret']
        assert kwargs['inbox_id'] == profile['inbox_id']
        calls.append(route)
        return {'controls': [control]}

    monkeypatch.setattr(client, 'api', service)
    ns = parse_args(['poll', '--identity', profile['identity'], '--check', '--json'])
    assert client._execute_command(ns, tmp_path, values) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['kind'] == 'CONTROL' and result['control'] == control
    assert calls == ['/v1/sessions/session-1/check-in']
    assert event in poll.read_spool_events(path, profile=profile)
    assert json.loads(path.read_text())['ack_cursor'] == 0
    assert json.loads(path.read_text()).get('observed_cursor', 0) == 0


def test_failed_foreground_poll_control_check_preserves_cached_evidence(tmp_path, monkeypatch, capsys):
    import pytest

    from src import agentbus_client as client
    from src.agentbus_parser_client import parse_args
    from src.agentbus_transport_client import ClientError

    path, profile = profile_at(tmp_path)
    poll.change_spool(path, profile, lambda spool: poll._append(spool, {
        'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7, 'sender': 'agentbus:peer',
            'recipient': profile['identity'], 'kind': 'request', 'text': 'retain this',
            'audience': 'direct', 'actionable': True},
    }))
    before = poll.spool_path(path).read_bytes()
    values = {'AGENTBUS_URL': profile['service_url'], 'AGENTBUS_CONSUMER_STATE_DIR': str(tmp_path)}
    monkeypatch.setattr(client, 'checked_profile', lambda *_args: (profile, {}))
    monkeypatch.setattr(poll, 'ensure_worker', lambda *_args: None)

    def unavailable(*_args, **_kwargs):
        raise ClientError('control service unavailable', status=503)

    monkeypatch.setattr(client, 'api', unavailable)
    ns = parse_args(['poll', '--identity', profile['identity'], '--check', '--json'])
    with pytest.raises(ClientError, match='control service unavailable'):
        client._execute_command(ns, tmp_path, values)
    assert poll.spool_path(path).read_bytes() == before
    assert capsys.readouterr().out == ''
    assert json.loads(path.read_text())['ack_cursor'] == 0



def test_cached_nonterminal_outcome_refreshes_flags_without_retirement_or_ack(tmp_path):
    path, profile = profile_at(tmp_path)
    profile['message_lifecycle_version'] = 1
    event = {'kind': 'MESSAGE', 'id': '7', 'message': {'cursor': 7,
        'text': 'retain original work', 'lifecycle': {'state': 'picked_up'}}}
    poll.change_spool(path, profile, lambda spool: poll._append(spool, event))
    def fresh(*_args, **_kwargs):
        return {'cursor': 7, 'scope': 'recipient', 'state': 'recovery_required',
                'lease_elapsed': True, 'owned_by_this_session': False}
    assert poll.reconcile_message_event({'AGENTBUS_URL': profile['service_url']},
        path, profile, event, api_call=fresh) is False
    retained = poll.read_spool_events(path, profile=profile)
    assert len(retained) == 1 and retained[0]['message']['text'] == 'retain original work'
    assert retained[0]['message']['lifecycle']['state'] == 'recovery_required'
    assert retained[0]['message']['lifecycle']['lease_elapsed'] is True
    assert event['message']['lifecycle']['owned_by_this_session'] is False
    assert json.loads(path.read_text())['ack_cursor'] == 0
