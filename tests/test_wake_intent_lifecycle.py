"""Persist host admission facts without promoting them to execution or acknowledgement."""

import json

import pytest
from test_codex_wake_client import message, put
from test_codex_wake_client import setup as setup

from src import agentbus_codex_wake_client as wake
from src.agentbus_codex_rpc_client import TurnStartUncertain
from src.agentbus_codex_wake_intent_client import observe_host
from src.agentbus_poll_client import retire_acknowledged_messages


@pytest.mark.parametrize("host_status,expected", [("unknown", "admitted"), ("queued", "queued"), ("inProgress", "running")])
def test_host_acceptance_records_only_observed_lifecycle(setup, monkeypatch, host_status, expected):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    monkeypatch.setattr(host, "last_turn_status", host_status, raising=False)

    def terminal(_thread, _turn, **_kwargs):
        state = json.loads(wake._state_path(values, profile["chat_id"]).read_text())
        assert state["wake_intent"]["state"] == expected
        assert profile.get("ack_cursor", 0) == 0
        return "completed"

    monkeypatch.setattr(host, "await_turn_terminal", terminal)
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_completed"
    state_path = wake._state_path(values, profile["chat_id"])
    state = json.loads(state_path.read_text())
    assert state["wake_intent"]["state"] == "completed"
    phases = [item["state"] for item in state["wake_intent"]["transitions"]]
    assert phases[:4] == ["pending", "eligible", "submitting", "admitted"]
    assert "acknowledged" not in phases
    profile["ack_cursor"] = 1
    retire_acknowledged_messages(path, profile, 1)
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "no_action"
    assert json.loads(state_path.read_text())["wake_intent"]["state"] == "acknowledged"


def test_lifecycle_callback_is_bound_to_exact_thread_and_turn(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    callbacks = []
    monkeypatch.setattr(host, "set_turn_observer", callbacks.append, raising=False)

    def terminal(_thread, _turn, **_kwargs):
        state_path = wake._state_path(values, profile["chat_id"])
        callbacks[0]("foreign-thread", "turn-one", "inProgress")
        callbacks[0]("thread-one", "foreign-turn", "inProgress")
        assert json.loads(state_path.read_text())["wake_intent"]["state"] == "admitted"
        callbacks[0]("thread-one", "turn-one", "inProgress")
        assert json.loads(state_path.read_text())["wake_intent"]["state"] == "running"
        return "completed"

    monkeypatch.setattr(host, "await_turn_terminal", terminal)
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_completed"


def test_uncertain_intent_keeps_original_batch_when_new_work_arrives(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.failure = TurnStartUncertain("lost response")
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    put(path, profile, message(2))
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    state = json.loads(wake._state_path(values, profile["chat_id"]).read_text())
    assert state["wake_intent"]["state"] == "uncertain"
    assert state["wake_intent"]["candidate_ids"] == ["MESSAGE:1"]
    assert [name for name, _ in host.calls].count("start") == 1
    assert profile.get("ack_cursor", 0) == 0


def test_late_host_facts_cannot_regress_terminal_intent(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    wake.run_once(values, identity, live=True, host_factory=lambda: host)
    state = wake._load(wake._state_path(values, profile["chat_id"]))
    before = json.dumps(state)
    assert not observe_host(state, "thread-one", "turn-one", "inProgress")
    assert not observe_host(state, "thread-one", "turn-one", "queued")
    assert json.dumps(state) == before


def test_foreign_binding_intent_fails_closed_before_host_contact(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    wake.run_once(values, identity, live=True, host_factory=lambda: host)
    state_path = wake._state_path(values, profile["chat_id"])
    state = json.loads(state_path.read_text())
    state["wake_intent"]["binding_generation"] += 1
    state_path.write_text(json.dumps(state))
    host.calls.clear()
    with pytest.raises(wake.ClientError, match="invalid binding"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert not host.calls
