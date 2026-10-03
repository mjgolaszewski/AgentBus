"""The optional adapter may wake a bound host thread, never AgentBus itself."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src import agentbus_codex_wake_client as wake
from src.agentbus_codex_rpc_client import CodexHostRejected, TurnStartUncertain
from src.agentbus_poll_client import change_spool
from src.agentbus_transport_client import ClientError


class FakeHost:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.state = "idle"
        self.failure: Exception | None = None
        self.found: tuple[str, str] | None = None
        self.prompts: list[str] = []
        self.resume_failure: Exception | None = None

    def __enter__(self) -> FakeHost:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read_thread(self, thread_id: str) -> dict:
        self.calls.append(("read", thread_id))
        return {"id": thread_id, "ephemeral": False, "status": {"type": self.state}}

    def resume(self, thread_id: str) -> None:
        self.calls.append(("resume", thread_id))
        if self.resume_failure:
            raise self.resume_failure

    def start_turn(self, thread_id: str, prompt: str) -> str:
        self.calls.append(("start", thread_id))
        self.prompts.append(prompt)
        if self.failure:
            raise self.failure
        return "turn-one"

    def find_attempt(self, thread_id: str, attempt_id: str) -> tuple[str, str] | None:
        self.calls.append(("find", thread_id))
        return self.found


@pytest.fixture
def setup(monkeypatch, tmp_path: Path):
    identity = "agentbus:signal-gardener"
    profile = {
        "identity": identity, "chat_id": str(uuid.uuid4()),
        "participation": {"session_id": "session-one", "stopped": False},
    }
    profile_path = tmp_path / "profile.json"
    profile_path.write_text(json.dumps(profile))
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(tmp_path / "consumers")}
    wake.set_workspace_enabled(values, True)
    monkeypatch.setattr(wake, "checked_profile", lambda _values, _identity: (profile, {}))
    monkeypatch.setattr(wake, "identity_path", lambda _values, _identity: profile_path)
    host = FakeHost()
    return values, identity, profile, profile_path, host


def put(profile_path: Path, profile: dict, *events: dict) -> None:
    change_spool(profile_path, profile, lambda state: state["events"].extend(events))


def message(cursor: int, *, recipient: str = "agentbus:signal-gardener",
            audience: str = "direct", kind: str = "request", text: str = "private peer text",
            action_reason: str | None = None, assurance: str = "session") -> dict:
    return {"kind": "MESSAGE", "id": str(cursor), "message": {
        "cursor": cursor, "recipient": recipient, "audience": audience,
        "kind": kind, "text": text, "action_reason": action_reason,
        "sender_assurance": assurance,
    }}


def test_legacy_message_cannot_wake_bound_chat(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    put(path, profile, message(1, assurance="legacy"))
    assert wake.status(values, identity)["eligible"] == []
    assert wake.run_once(values, identity, host_factory=lambda: host)["status"] == "no_action"
    assert all(name != "start" for name, _ in host.calls)


def test_watch_all_supervises_poll_worker_before_wake_projection(monkeypatch, tmp_path):
    profile_path = tmp_path / "agentbus:flower.json"
    profile_path.write_text(json.dumps({
        "identity": "agentbus:flower", "chat_id": str(uuid.uuid4()),
        "participation": {"session_id": "s1", "stopped": False},
    }))
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    checks = iter((True, False))
    calls = []
    monkeypatch.setattr(wake, "workspace_status", lambda _values: {"enabled": next(checks)})
    monkeypatch.setattr(wake, "consumer_state_dir", lambda _values: tmp_path)
    monkeypatch.setattr(wake, "_state_path", lambda *_args: binding)
    monkeypatch.setattr(wake, "ensure_worker", lambda path, _values: calls.append(("poll", path)))
    def observe(_values, identity, **_kwargs):
        calls.append(("wake", identity))
        return {"status": "no_action"}
    monkeypatch.setattr(wake, "run_once", observe)
    monkeypatch.setattr(wake.time, "sleep", lambda _interval: None)
    wake.watch_all({}, interval=2)
    assert calls == [("poll", profile_path), ("wake", "agentbus:flower")]


def test_dry_run_filters_noise_and_does_not_call_host(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    host.calls.clear()
    put(path, profile, message(1, audience="broadcast"), message(2, kind="status", audience="broadcast"),
        message(3, recipient="agentbus:someone-else"), message(4),
        message(5, kind="status"), message(6, kind="reply"),
        message(7, kind="message", assurance="legacy"),
        message(8, audience="informational", kind="message"))
    result = wake.run_once(values, identity, host_factory=lambda: host)
    assert result == {"status": "dry_run", "eligible": ["MESSAGE:4", "MESSAGE:5", "MESSAGE:6", "MESSAGE:8"], "enabled": False}
    assert host.calls == []
    assert wake.status(values, identity)["eligible"] == ["MESSAGE:4", "MESSAGE:5", "MESSAGE:6", "MESSAGE:8"]


def test_live_wake_is_opt_in_compact_and_not_repeated(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    put(path, profile, message(1, text="DO NOT PUT THIS IN THE PROMPT"), message(2, kind="blocker"))
    with pytest.raises(ClientError, match="disabled"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    wake.set_enabled(values, identity, True)
    host.calls.clear()
    accepted = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert accepted["status"] == "turn_accepted" and accepted["turn_id"] == "turn-one"
    assert [name for name, _ in host.calls] == ["read", "resume", "start"]
    assert "MESSAGE:1" in host.prompts[0] and "MESSAGE:2" in host.prompts[0]
    assert "DO NOT PUT THIS" not in host.prompts[0]
    assert "AGENTBUS_WAKE_ATTEMPT=" in host.prompts[0]
    again = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert again["status"] == "already_woken"
    assert [name for name, _ in host.calls].count("start") == 1


def test_direct_status_alone_wakes_joined_chat(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1, kind="status"))
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_accepted"
    assert result["eligible"] == ["MESSAGE:1"]
    assert len(host.prompts) == 1


def test_control_priority_claim_and_active_host(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1, audience="unrouted", action_reason="claimed by this identity"),
        message(2), {"kind": "POLICY_CHANGED", "id": "rev-2", "revision": "rev-2"},
        {"kind": "CONTROL", "id": "control-1", "control": {"kind": "stop_end_turn"}})
    assert wake.status(values, identity)["eligible"] == ["CONTROL:control-1"]
    host.state = "active"
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "thread_active"
    assert not host.prompts
    profile["participation"]["stopped"] = True
    assert wake.status(values, identity)["session_stopped"] is True
    with pytest.raises(ClientError, match="Stopped"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)


def test_uncertain_start_requires_host_reconciliation(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.failure = TurnStartUncertain("lost response")
    first = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert first["status"] == "uncertain"
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    assert [name for name, _ in host.calls].count("start") == 1
    host.found = ("turn-recovered", "completed")
    recovered = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert recovered["status"] == "already_woken"
    assert recovered["turn_id"] == "turn-recovered"
    assert [name for name, _ in host.calls].count("start") == 1


def test_binding_stays_with_chat_id_on_rename_and_rejects_session_change(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    linked = wake.local_binding_projection(values, profile["chat_id"], "session-one")
    assert linked and linked["thread_id"] == "thread-one" and linked["session_matches"]
    profile["identity"] = "agentbus:new-name"
    assert wake.status(values, profile["identity"])["thread_id"] == "thread-one"
    profile["participation"]["session_id"] = "different-session"
    assert wake.local_binding_projection(values, profile["chat_id"], "different-session")["session_matches"] is False
    stale = wake.status(values, profile["identity"])
    assert stale["wake_readiness"] == "ineligible" and stale["reason"] == "binding_missing_or_stale"


def test_self_status_combines_proved_service_and_local_facts_without_side_effects(setup, monkeypatch):
    values, identity, profile, path, host = setup
    values.update({"AGENTBUS_URL": "http://127.0.0.1:8766", "AGENTBUS_API_TOKEN": "shared"})
    profile["participation"]["session_secret"] = "exact-session-secret"
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1, assurance="legacy"), message(2, kind="status"))
    presence = {"chat_id": profile["chat_id"], "session_id": "session-one",
                "route": identity, "state": "active_compliant",
                "last_client_contact_at": "2026-10-03T00:00:00+00:00"}
    calls = []

    def service_api(_values, route, **kwargs):
        calls.append((route, kwargs))
        return dict(presence)

    monkeypatch.setattr(wake, "api", service_api)
    monkeypatch.setattr(wake, "_worker_pid", lambda _path: 1234)
    monkeypatch.setattr(wake, "worker_running", lambda _path: True)
    before = {file.relative_to(path.parent): file.read_bytes() for file in path.parent.rglob("*") if file.is_file()}
    host_calls = list(host.calls)
    result = wake.status(values, identity)
    after = {file.relative_to(path.parent): file.read_bytes() for file in path.parent.rglob("*") if file.is_file()}
    assert before == after and host.calls == host_calls
    assert calls == [("/v1/sessions/session-one/self-presence", {"session_token": "exact-session-secret"})]
    assert result["wake_readiness"] == "eligible" and result["reason"] == "ready_to_attempt"
    assert result["host_turn_start"] == "unobserved"
    assert result["registration"] == "joined" and result["service_state"] == "active_compliant"
    assert result["route_matches"] is True
    assert result["last_check_in_at"] == presence["last_client_contact_at"]
    assert result["eligible"] == ["MESSAGE:2"]
    assert result["service_presence"]["last_client_contact_at"] == presence["last_client_contact_at"]
    monkeypatch.setattr(wake, "_worker_pid", lambda _path: None)
    assert wake.status(values, identity)["reason"] == "local_worker_not_running"
    monkeypatch.setattr(wake, "_worker_pid", lambda _path: 1234)
    binding_path = wake._state_path(values, profile["chat_id"])
    binding = wake._load(binding_path)
    binding["wake_history"] = [wake._now()] * wake.MAX_WAKES_PER_HOUR
    wake._save(binding_path, binding)
    assert wake.status(values, identity)["reason"] == "wake_rate_limited"
    binding["wake_history"] = []
    wake._save(binding_path, binding)
    presence["route"] = "agentbus:other"
    mismatch = wake.status(values, identity)
    assert mismatch["reason"] == "service_identity_mismatch" and mismatch["route_matches"] is False
    presence["route"] = identity
    presence["state"] = "stopped"
    assert wake.status(values, identity)["reason"] == "session_stopped"
    presence["state"] = "active_compliant"
    host.failure = TurnStartUncertain("lost turn-start response")
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "uncertain"
    unresolved = wake.status(values, identity)
    assert unresolved["wake_readiness"] == "deferred"
    assert unresolved["reason"] == "wake_attempt_unresolved"


def test_self_status_reports_opt_out_missing_binding_and_unobserved_service(setup, monkeypatch):
    values, identity, profile, path, host = setup
    assert wake.status(values, identity)["reason"] == "binding_missing_or_stale"
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, False)
    assert wake.status(values, identity)["reason"] == "chat_opted_out"
    wake.set_enabled(values, identity, True)
    assert wake.status(values, identity)["wake_readiness"] == "unknown"
    assert wake.status(values, identity)["reason"] == "service_presence_unobserved"
    wake.set_workspace_enabled(values, False)
    assert wake.status(values, identity)["reason"] == "workspace_disabled"
    profile.pop("participation")
    assert wake.status(values, identity)["registration"] == "not_joined"


def test_workspace_opt_in_enrolls_exact_current_thread_and_controls_live_wake(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.set_workspace_enabled(values, False)
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-one")
    assert wake.enroll_current(values, identity, lambda: host) == {"status": "workspace_disabled"}
    assert host.calls == []
    wake.set_workspace_enabled(values, True)
    first = wake.enroll_current(values, identity, lambda: host)
    assert first["status"] == "bound" and first["enabled"] is True
    again = wake.enroll_current(values, identity, lambda: host)
    assert again["binding_generation"] == first["binding_generation"]
    put(path, profile, message(1))
    wake.set_workspace_enabled(values, False)
    with pytest.raises(ClientError, match="workspace is disabled"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert not host.prompts


def test_other_host_writer_defers_without_a_launch_attempt(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.resume_failure = CodexHostRejected("thread already has an active writer")
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "thread_owned_by_host"
    assert wake.status(values, identity)["last_attempt"] is None
    assert not host.prompts


def test_completed_turn_gets_one_bounded_followup_for_unacknowledged_work(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    first = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert first["wake_epoch"] == 1
    state_path = wake._state_path(values, profile["chat_id"])
    state = wake._load(state_path)
    state["wake_history"] = [(datetime.now(timezone.utc) - timedelta(seconds=21)).isoformat()]
    wake._save(state_path, state)
    host.found = ("turn-one", "completed")
    second = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert second["wake_epoch"] == 2
    assert second["eligible"] == first["eligible"]
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "pending_after_followup"
    assert len(host.prompts) == 2


def test_ordinary_rate_budget_never_blocks_required_control(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    state_path = wake._state_path(values, profile["chat_id"])
    state = wake._load(state_path)
    past = (datetime.now(timezone.utc) - timedelta(seconds=21)).isoformat()
    state["wake_history"] = [past] * wake.MAX_WAKES_PER_HOUR
    wake._save(state_path, state)
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "wake_rate_limited"
    assert not host.prompts
    put(path, profile, {"kind": "CONTROL", "id": "stop-1", "control": {"kind": "stop_end_turn"}})
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_accepted"
    assert len(host.prompts) == 1
