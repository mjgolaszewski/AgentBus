"""Persist host admission facts without promoting them to execution or acknowledgement."""

import json

import pytest
from test_codex_wake_client import message, put
from test_codex_wake_client import setup as setup

from src import agentbus_codex_wake_client as wake
from src.agentbus_codex_rpc_client import TurnStartUncertain
from src.agentbus_codex_wake_intent_client import STATES, observe_host, record_intent
from src.agentbus_poll_client import retire_acknowledged_messages
from src.agentbus_scope_client import FIELDS, scope


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


def legacy_intent(setup, phase="blocked"):
    values, identity, profile, _, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    path = wake._state_path(values, profile["chat_id"])
    state = json.loads(path.read_text())
    record_intent(state, phase, ["MESSAGE:1"], reason="retained observation", turn_id="retained-turn")
    del state["wake_intent"]["chat_id"]
    path.write_text(json.dumps(state))
    host.calls.clear()
    return path, state


@pytest.mark.parametrize("phase", sorted(STATES))
def test_legacy_intent_read_completes_only_chat_id_without_disk_or_host_effects(setup, phase):
    path, before = legacy_intent(setup, phase)
    raw = path.read_bytes()
    restored = wake._load(path)
    expected = json.loads(raw)
    expected["wake_intent"]["chat_id"] = expected["chat_id"]
    assert restored == expected
    assert restored.legacy_scope_recovered is True
    assert restored["wake_intent"]["id"] == before["wake_intent"]["id"]
    assert restored["wake_intent"]["state"] == phase
    assert path.read_bytes() == raw
    assert not setup[-1].calls


@pytest.mark.parametrize("field", ["service_url", "inbox_id", "channel", "session_id",
                                 "thread_id", "binding_generation", "chat_id"])
def test_legacy_recovery_rejects_conflicts_without_rewriting_or_host_contact(setup, field):
    values, identity, _, _, host = setup
    path, state = legacy_intent(setup)
    state["wake_intent"][field] = 999 if field == "binding_generation" else "foreign"
    path.write_text(json.dumps(state))
    raw = path.read_bytes()
    with pytest.raises(wake.ClientError, match="invalid binding"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert path.read_bytes() == raw
    assert not host.calls


@pytest.mark.parametrize("malformation", ["second_missing_field", "invalid_id", "invalid_state",
                                        "empty_candidates", "invalid_transition", "explicit_null_chat"])
def test_legacy_recovery_refuses_unrecognized_or_malformed_state(setup, malformation):
    path, state = legacy_intent(setup)
    intent = state["wake_intent"]
    if malformation == "second_missing_field":
        del intent["service_url"]
    elif malformation == "invalid_id":
        intent["id"] = "not-a-retained-digest"
    elif malformation == "invalid_state":
        intent["state"] = "invented"
    elif malformation == "empty_candidates":
        intent["candidate_ids"] = []
    elif malformation == "invalid_transition":
        intent["transitions"] = [{"state":"invented"}]
    else:
        intent["chat_id"] = None
    path.write_text(json.dumps(state))
    raw = path.read_bytes()
    with pytest.raises(wake.ClientError):
        wake._load(path)
    assert path.read_bytes() == raw


def test_live_legacy_recovery_is_persisted_before_normal_processing_and_restart(setup, monkeypatch):
    values, identity, profile, _, host = setup
    path, before = legacy_intent(setup)
    saves = []
    original_save = wake._save
    def save(target, state):
        saves.append(json.loads(json.dumps(state)))
        original_save(target, state)
    monkeypatch.setattr(wake, "_save", save)
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "no_action"
    expected = {**before, "wake_intent":{**before["wake_intent"], "chat_id":profile["chat_id"]}}
    assert saves == [expected]
    assert json.loads(path.read_text()) == expected
    assert wake._load(path).legacy_scope_recovered is False
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "no_action"
    assert saves == [expected]
    assert not host.calls
    assert profile.get("ack_cursor", 0) == 0


@pytest.mark.parametrize("conflict", ["profile", "attempt"])
def test_migration_cannot_persist_before_exact_profile_and_attempt_validation(setup, conflict):
    values, identity, profile, _, host = setup
    path, state = legacy_intent(setup)
    if conflict == "profile":
        profile["channel"] = "CFOREIGN"
    else:
        state["last_attempt"] = {"session_id":"foreign"}
        path.write_text(json.dumps(state))
    raw = path.read_bytes()
    with pytest.raises(wake.ClientError, match="scope|binding|channel"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert path.read_bytes() == raw
    assert not host.calls


def test_legacy_uncertain_intent_recovers_without_resubmission_or_identity_reset(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.failure = TurnStartUncertain("lost response")
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    state_path = wake._state_path(values, profile["chat_id"])
    before = json.loads(state_path.read_text())
    del before["wake_intent"]["chat_id"]
    state_path.write_text(json.dumps(before))
    put(path, profile, message(2))
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    after = json.loads(state_path.read_text())
    assert after["wake_intent"]["id"] == before["wake_intent"]["id"]
    assert after["wake_intent"]["state"] == "uncertain"
    assert after["wake_intent"]["candidate_ids"] == ["MESSAGE:1"]
    assert after["wake_intent"]["transitions"] == before["wake_intent"]["transitions"]
    assert after["last_attempt"] == before["last_attempt"]
    assert [name for name, _ in host.calls].count("start") == 1
    assert profile.get("ack_cursor", 0) == 0


@pytest.mark.parametrize("attempt_state", [None, "accepted", "starting", "uncertain"])
def test_pre_channel_binding_recovers_only_from_exact_profile_without_changing_facts(setup, attempt_state):
    _, _, profile, _, host = setup
    path, before = legacy_intent(setup, "uncertain" if attempt_state else "blocked")
    for field in ("service_url", "inbox_id", "channel"):
        del before[field]
        del before["wake_intent"][field]
    if attempt_state:
        before["last_attempt"] = {
            "attempt_id":"33d47067-a5a7-4fc9-a0f2-ad7977a166f6", "fingerprint":"a" * 64,
            "candidate_ids":["MESSAGE:1"], "state":attempt_state,
            "wake_epoch":7, "observed_at":"2026-01-01T00:00:00+00:00", "turn_id":"retained-turn",
        }
    path.write_text(json.dumps(before))
    raw = path.read_bytes()
    loaded = wake._load(path, profile=profile)
    expected = {**before, **scope(profile), "wake_intent":{**before["wake_intent"], **scope(profile)}}
    if attempt_state:
        expected["last_attempt"] = {**before["last_attempt"], **scope(profile)}
    assert loaded == expected
    assert loaded.legacy_scope_recovered
    assert path.read_bytes() == raw
    assert not host.calls
    assert "legacy_scope_recovered" not in loaded


@pytest.mark.parametrize("field", FIELDS)
def test_pre_channel_recovery_rejects_conflicting_authenticated_profile(setup, field):
    values, identity, profile, _, host = setup
    path, before = legacy_intent(setup)
    for missing in ("service_url", "inbox_id", "channel"):
        del before[missing]
        del before["wake_intent"][missing]
    # A retained explicit value always wins over guesses: it must agree.
    before[field] = "foreign"
    path.write_text(json.dumps(before))
    raw = path.read_bytes()
    with pytest.raises(wake.ClientError, match="scope|binding"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert path.read_bytes() == raw
    assert not host.calls


def test_pre_channel_status_is_read_only_and_live_recovery_persists_once(setup):
    values, identity, profile, _, host = setup
    path, before = legacy_intent(setup)
    for field in ("service_url", "inbox_id", "channel"):
        del before[field]
        del before["wake_intent"][field]
    path.write_text(json.dumps(before))
    raw = path.read_bytes()
    wake.status(values, identity)
    assert path.read_bytes() == raw
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "no_action"
    expected = {**before, **scope(profile), "wake_intent":{**before["wake_intent"], **scope(profile)}}
    assert json.loads(path.read_text()) == expected
    after = path.read_bytes()
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "no_action"
    assert path.read_bytes() == after
    assert not host.calls
    assert profile.get("ack_cursor", 0) == 0
