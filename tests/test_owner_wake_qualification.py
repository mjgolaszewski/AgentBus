"""Characterize released adapter boundaries; these are not AHP provider proofs."""

import json

import pytest
from test_codex_wake_client import message, put
from test_codex_wake_client import setup as setup

from src import agentbus_codex_wake_client as wake
from src.agentbus_codex_rpc_client import CodexHostRejected, TurnStartUncertain


def test_idle_observation_does_not_supply_atomic_admission(setup, monkeypatch):
    """A racing owner can accept input: the adapter has no idle epoch argument."""
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    original_resume = host.resume

    def intervening_user_turn(thread_id):
        original_resume(thread_id)
        host.state = "active"

    monkeypatch.setattr(host, "resume", intervening_user_turn)
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert host.state == "active" and len(host.prompts) == 1
    assert result["status"] == "turn_completed"
    assert profile.get("ack_cursor", 0) == 0


@pytest.mark.parametrize("reason", ["turn is finishing", "permissions changed on resume"])
def test_owner_rejection_never_promotes_input_to_acknowledgement(setup, reason):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.failure = CodexHostRejected(reason)
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_rejected"
    assert not any(name == "terminal" for name, _ in host.calls)
    state = json.loads(wake._state_path(values, profile["chat_id"]).read_text())
    assert state["last_attempt"]["state"] == "rejected"
    assert profile.get("ack_cursor", 0) == 0


def test_restart_after_lost_start_response_preserves_original_attempt(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.failure = TurnStartUncertain("managed server restarted after possible acceptance")
    first = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    state_path = wake._state_path(values, profile["chat_id"])
    original = json.loads(state_path.read_text())["last_attempt"]
    replacement = type(host)()
    replacement.found = None
    second = wake.run_once(values, identity, live=True, host_factory=lambda: replacement)
    assert first["status"] == second["status"] == "uncertain"
    assert json.loads(state_path.read_text())["last_attempt"] == original
    assert not replacement.prompts
    assert not any(name in {"resume", "start"} for name, _ in replacement.calls)
    assert profile.get("ack_cursor", 0) == 0
