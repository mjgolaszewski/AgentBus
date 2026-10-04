"""The optional adapter may wake a bound host thread, never AgentBus itself."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from src import agentbus_codex_wake_client as wake
from src import agentbus_wake_inbox_client as wake_inbox
from src.agentbus_codex_host_client import HostSelection
from src.agentbus_codex_rpc_client import CodexHostRejected, TurnCompletionUncertain, TurnStartUncertain
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
        self.terminal_status = "completed"

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

    def await_turn_terminal(self, thread_id: str, turn_id: str, *, max_seconds: float = 600) -> str:
        self.calls.append(("terminal", turn_id))
        return self.terminal_status

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
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(tmp_path / "consumers"),
              "AGENTBUS_CODEX_HOST_MODE": "standalone"}
    wake.set_workspace_enabled(values, True)
    monkeypatch.setattr(wake, "checked_profile", lambda _values, _identity: (profile, {}))
    monkeypatch.setattr(wake, "identity_path", lambda _values, _identity: profile_path)
    monkeypatch.setattr(wake, "api", lambda _values, _path, **_kwargs: {
        "session_id": profile["participation"]["session_id"],
        "chat_id": profile["chat_id"], "route": identity,
        "state": "active_compliant", "work_paused": False,
    })
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


def service_message(cursor: int, **overrides: object) -> dict:
    result = dict(message(cursor)["message"])
    result.update(overrides)
    return result


def fake_inbox(messages: list[dict]):
    def respond(_values: dict, path: str, **_kwargs: object) -> dict:
        assert path.startswith("/v1/inbox?")
        query = parse_qs(urlparse(path).query)
        after = int(query["after"][0])
        page = [item for item in messages if item["cursor"] > after][:100]
        remaining = any(item["cursor"] > page[-1]["cursor"] for item in messages) if page else False
        return {"messages": page, "next_cursor": page[-1]["cursor"] if page else after,
                "has_more": remaining}
    return respond


def test_saturated_spool_cannot_block_service_backed_direct_wake(setup, monkeypatch):
    values, identity, profile, path, host = setup
    values.update(AGENTBUS_URL="http://127.0.0.1:8766", AGENTBUS_API_TOKEN="shared")
    profile["participation"]["session_secret"] = "session-proof"
    profile["ack_cursor"] = 0
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, *(message(i, audience="broadcast") for i in range(1, 102)))
    original_spool = path.with_name(path.name + ".poll.json").read_bytes()
    messages = [service_message(i, audience="broadcast") for i in range(1, 151)]
    messages.append(service_message(151, kind="information"))
    monkeypatch.setattr(wake_inbox, "api", fake_inbox(messages))
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completed"
    assert result["eligible"] == ["MESSAGE:151"]
    assert "MESSAGE:151" in host.prompts[0]
    assert path.with_name(path.name + ".poll.json").read_bytes() == original_spool
    assert profile["ack_cursor"] == 0
    assert wake.status(values, identity)["eligible"] == ["MESSAGE:151"]
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "wake_deferred"
    assert [name for name, _ in host.calls].count("start") == 1


def test_wake_scan_replays_after_page_failure_without_skipping_or_ack(setup, monkeypatch):
    values, identity, profile, _path, _host = setup
    profile["participation"]["session_secret"] = "session-proof"
    profile["ack_cursor"] = 0
    messages = [service_message(i, audience="broadcast") for i in range(1, 101)]
    messages.extend([service_message(101, sender_assurance="legacy"),
                     service_message(102, recipient="agentbus:other"),
                     service_message(103, audience="broadcast"),
                     service_message(104, sender_assurance="slack-human", kind="reply")])
    respond = fake_inbox(messages)
    calls = 0
    def fail_once(v: dict, p: str, **kwargs: object) -> dict:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ClientError("temporary inbox failure")
        return respond(v, p, **kwargs)
    monkeypatch.setattr(wake_inbox, "api", fail_once)
    with pytest.raises(ClientError, match="temporary inbox failure"):
        wake_inbox.scan(values, profile, 1, wake._candidate)
    state = wake_inbox.read_state(values, profile, 1)
    assert state["cursor"] == 100 and state["pending"] == []
    resumed = wake_inbox.scan(values, profile, 1, wake._candidate)
    assert resumed["cursor"] == 104 and resumed["caught_up"] is True
    assert wake_inbox.candidates(resumed, 0) == [(3, "MESSAGE:104")]
    assert profile["ack_cursor"] == 0
    assert wake_inbox.candidates(wake_inbox.scan(values, profile, 1, wake._candidate), 0) == [(3, "MESSAGE:104")]


def test_wake_scan_compacts_only_local_candidate_projection(setup, monkeypatch):
    values, identity, profile, _path, _host = setup
    profile["participation"]["session_secret"] = "session-proof"
    profile["ack_cursor"] = 0
    messages = [service_message(i) for i in range(1, 132)]
    monkeypatch.setattr(wake_inbox, "api", fake_inbox(messages))
    state = wake_inbox.scan(values, profile, 1, wake._candidate)
    assert state["cursor"] == 131 and state["compacted"] is True
    assert len(state["pending"]) == wake_inbox.MAX_CANDIDATES
    assert wake_inbox.candidates(state, 0)[-1] == (3, "MESSAGE:131")
    profile["ack_cursor"] = 131
    assert wake_inbox.candidates(wake_inbox.scan(values, profile, 1, wake._candidate), 131) == []


def test_wake_scan_rejects_out_of_order_page_without_skipping_direct_message(setup, monkeypatch):
    values, _identity, profile, _path, _host = setup
    profile["participation"]["session_secret"] = "session-proof"
    profile["ack_cursor"] = 0
    monkeypatch.setattr(wake_inbox, "api", lambda *_args, **_kwargs: {
        "messages": [service_message(2), service_message(1)],
        "next_cursor": 2, "has_more": False,
    })
    with pytest.raises(ClientError, match="cursor is invalid"):
        wake_inbox.scan(values, profile, 1, wake._candidate)
    state = wake_inbox.read_state(values, profile, 1)
    assert state["cursor"] == 0 and state["pending"] == []


def test_wake_scan_replays_from_ack_after_binding_generation_changes(setup, monkeypatch):
    values, _identity, profile, _path, _host = setup
    profile["participation"]["session_secret"] = "session-proof"
    profile["ack_cursor"] = 4
    monkeypatch.setattr(wake_inbox, "api", fake_inbox([service_message(5)]))
    assert wake_inbox.scan(values, profile, 1, wake._candidate)["cursor"] == 5
    fresh = wake_inbox.read_state(values, profile, 2)
    assert fresh["cursor"] == 4 and fresh["pending"] == []
    assert wake_inbox.candidates(wake_inbox.scan(values, profile, 2, wake._candidate), 4) == [
        (3, "MESSAGE:5")]


def test_legacy_message_cannot_wake_bound_chat(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    put(path, profile, message(1, assurance="legacy"))
    assert wake.status(values, identity)["eligible"] == []
    assert wake.run_once(values, identity, host_factory=lambda: host)["status"] == "no_action"
    assert all(name != "start" for name, _ in host.calls)


def test_operator_pause_keeps_addressed_messages_wakable(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    host.calls.clear()
    put(path, profile, message(1))
    monkeypatch.setattr(wake, "api", lambda _values, _path, **_kwargs: {
        "session_id": profile["participation"]["session_id"],
        "chat_id": profile["chat_id"], "route": identity,
        "state": "active_compliant", "work_paused": True,
    })
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completed"
    assert result["eligible"] == ["MESSAGE:1"]
    assert ("start", "thread-one") in host.calls
    assert "Operator work hold is active" in host.prompts[0]


def test_resume_control_wakes_held_conversation(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    host.calls.clear()
    put(path, profile, {"kind": "CONTROL", "id": "resume-1",
                        "control": {"kind": "resume_work"}})
    monkeypatch.setattr(wake, "api", lambda _values, _path, **_kwargs: {
        "session_id": profile["participation"]["session_id"],
        "chat_id": profile["chat_id"], "route": identity,
        "state": "control_pending", "work_paused": False,
    })
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completed"
    assert result["eligible"] == ["CONTROL:resume-1"]
    assert ("start", "thread-one") in host.calls
    assert "Operator work hold is active" not in host.prompts[0]


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


def test_watch_all_long_turn_for_one_chat_does_not_delay_another(monkeypatch, tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"agentbus:{name}.json").write_text(json.dumps({
            "identity": f"agentbus:{name}", "chat_id": str(uuid.uuid4()),
            "participation": {"session_id": name, "stopped": False},
        }))
    binding = tmp_path / "binding.json"
    binding.write_text("{}")
    a_started = threading.Event()
    a_release = threading.Event()
    b_completed = threading.Event()
    stop = threading.Event()
    monkeypatch.setattr(wake, "workspace_status", lambda _values: {"enabled": not stop.is_set()})
    monkeypatch.setattr(wake, "consumer_state_dir", lambda _values: tmp_path)
    monkeypatch.setattr(wake, "_state_path", lambda *_args: binding)
    monkeypatch.setattr(wake, "ensure_worker", lambda *_args: None)

    def observe(_values, identity, **_kwargs):
        if identity == "agentbus:a":
            a_started.set()
            assert a_release.wait(2)
        else:
            assert a_started.wait(2)
            b_completed.set()
            stop.set()
        return {"status": "no_action"}

    monkeypatch.setattr(wake, "run_once", observe)
    supervisor = threading.Thread(target=wake.watch_all, args=({},), kwargs={"interval": 1}, daemon=True)
    supervisor.start()
    try:
        assert b_completed.wait(2), "B must be scanned while A's turn is still running"
        assert not a_release.is_set()
    finally:
        stop.set()
        a_release.set()
        supervisor.join(3)
    assert not supervisor.is_alive()


def test_workspace_disable_drains_accepted_turn_before_supervisor_exits(tmp_path):
    """A supervisor SIGTERM must not tear down its live app-server transport."""
    consumers = tmp_path / "consumers"
    consumers.mkdir()
    identity = "agentbus:flower"
    profile = {"identity": identity, "chat_id": str(uuid.uuid4()),
               "participation": {"session_id": "s1", "stopped": False}}
    (consumers / f"{identity}.json").write_text(json.dumps(profile))
    (tmp_path / "binding.json").write_text("{}")
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(consumers)}
    wake.set_workspace_enabled(values, True)

    fake_host = tmp_path / "fake_host.py"
    fake_host.write_text('''
import json, os, pathlib, sys, time
root = pathlib.Path(os.environ["WAKE_TEST_ROOT"])
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialized":
        continue
    if method == "thread/read":
        result = {"thread": {"id": request["params"]["threadId"], "status": {"type": "idle"}}}
    elif method == "thread/resume":
        result = {"thread": {"id": request["params"]["threadId"]}}
    elif method == "turn/start":
        result = {"turn": {"id": "turn-one", "status": "inProgress"}}
    else:
        result = {}
    print(json.dumps({"id": request["id"], "result": result}), flush=True)
    if method == "turn/start":
        (root / "accepted").write_text("accepted")
        until = time.monotonic() + 5
        while not (root / "release").exists() and time.monotonic() < until:
            time.sleep(0.01)
        (root / "host_terminal").write_text("completed")
        print(json.dumps({"method": "turn/completed", "params": {"turn": {
            "id": "turn-one", "status": "completed"}}}), flush=True)
''')
    supervisor_script = tmp_path / "supervisor.py"
    supervisor_script.write_text('''
import os, pathlib, sys
sys.path.insert(0, os.environ["WAKE_TEST_REPO"])
from src import agentbus_codex_wake_client as wake
from src.agentbus_codex_rpc_client import CodexAppServer
root = pathlib.Path(os.environ["WAKE_TEST_ROOT"])
values = {"AGENTBUS_CONSUMER_STATE_DIR": str(root / "consumers")}
wake._state_path = lambda *_args: root / "binding.json"
wake.ensure_worker = lambda *_args: None
def run_once(_values, _identity, *, live):
    with CodexAppServer((sys.executable, str(root / "fake_host.py")), timeout=1) as host:
        host.read_thread("thread-one")
        host.resume("thread-one")
        turn_id = host.start_turn("thread-one", "wake")
        terminal = host.await_turn_terminal("thread-one", turn_id, max_seconds=5)
    (root / "observed_terminal").write_text(terminal)
    return {"status": "turn_" + terminal}
wake.run_once = run_once
wake.watch_all(values, interval=1)
''')
    env = dict(os.environ, WAKE_TEST_ROOT=str(tmp_path),
               WAKE_TEST_REPO=str(Path(__file__).resolve().parents[1]))
    proc = subprocess.Popen([sys.executable, str(supervisor_script)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    stopped: list[dict | Exception] = []
    try:
        deadline = time.monotonic() + 3
        while not (tmp_path / "accepted").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "accepted").exists(), "turn never reached accepted state"
        from src.agentbus_client import process_identity
        worker_path = wake._worker_path(values)
        wake.atomic_json(worker_path, {"pid": proc.pid,
                                       "start_time": process_identity(proc.pid)})

        def disable() -> None:
            try:
                wake.set_workspace_enabled(values, False)
                stopped.append(wake.stop_worker(values))
            except Exception as exc:
                stopped.append(exc)

        stopper = threading.Thread(target=disable)
        stopper.start()
        time.sleep(0.15)
        assert stopper.is_alive(), "disable returned before the accepted turn became terminal"
        assert proc.poll() is None, "supervisor exited while the turn was active"
        (tmp_path / "release").write_text("go")
        stopper.join(4)
        assert not stopper.is_alive(), "disable did not drain the active turn"
        assert stopped == [{"worker_stopped": True}]
        assert proc.wait(timeout=3) == 0
        assert (tmp_path / "host_terminal").read_text() == "completed"
        assert (tmp_path / "observed_terminal").read_text() == "completed"
    finally:
        (tmp_path / "release").write_text("go")
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=3)


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
    assert result == {"status": "dry_run", "eligible": ["MESSAGE:8", "MESSAGE:6", "MESSAGE:5", "MESSAGE:4"], "enabled": False}
    assert host.calls == []
    assert wake.status(values, identity)["eligible"] == ["MESSAGE:8", "MESSAGE:6", "MESSAGE:5", "MESSAGE:4"]


def test_live_wake_is_opt_in_compact_and_not_repeated(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    put(path, profile, message(1, text="DO NOT PUT THIS IN THE PROMPT"), message(2, kind="blocker"))
    with pytest.raises(ClientError, match="disabled"):
        wake.run_once(values, identity, live=True, host_factory=lambda: host)
    wake.set_enabled(values, identity, True)
    host.calls.clear()
    accepted = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert accepted["status"] == "turn_completed" and accepted["turn_id"] == "turn-one"
    assert [name for name, _ in host.calls] == ["read", "resume", "start", "terminal"]
    assert "MESSAGE:1" in host.prompts[0] and "MESSAGE:2" in host.prompts[0]
    assert "DO NOT PUT THIS" not in host.prompts[0]
    assert "AGENTBUS_WAKE_ATTEMPT=" in host.prompts[0]
    again = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert again["status"] == "wake_deferred"
    assert [name for name, _ in host.calls].count("start") == 1


def test_direct_status_alone_wakes_joined_chat(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1, kind="status"))
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completed"
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


def test_unshared_stdio_defaults_to_notification_only_before_host_launch(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    values.pop("AGENTBUS_CODEX_HOST_MODE")
    own_status = wake.status(values, identity)
    assert own_status["wake_readiness"] == "ineligible"
    assert own_status["reason"] == "owning_host_endpoint_unavailable"
    assert own_status["host_mode"] == "notification_only"
    result = wake.run_once(values, identity, live=True)
    assert result["status"] == "notification_only"
    assert result["eligible"] == ["MESSAGE:1"]
    assert not host.prompts


def test_experimental_proxy_mode_uses_only_validated_host_factory(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    values["AGENTBUS_CODEX_HOST_MODE"] = "experimental_vs_code_proxy"
    monkeypatch.setattr(wake, "select_host", lambda _values: HostSelection(
        "experimental_vs_code_proxy", None, lambda: host))
    own_status = wake.status(values, identity)
    assert own_status["host_mode"] == "experimental_vs_code_proxy"
    assert wake.run_once(values, identity, live=True)["status"] == "turn_completed"
    assert host.calls[-1] == ("terminal", "turn-one")


def test_experimental_enrollment_uses_the_same_validated_proxy(setup, monkeypatch):
    values, identity, _profile, _path, host = setup
    values["AGENTBUS_CODEX_HOST_MODE"] = "experimental_vs_code_proxy"
    monkeypatch.setattr(wake, "select_host", lambda _values: HostSelection(
        "experimental_vs_code_proxy", None, lambda: host))
    assert wake.bind(values, identity, "thread-one", auto_enable=True)["enabled"] is True
    assert host.calls == [("read", "thread-one")]


def test_experimental_proxy_without_valid_endpoint_never_launches(setup, monkeypatch):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    values["AGENTBUS_CODEX_HOST_MODE"] = "experimental_vs_code_proxy"
    monkeypatch.setattr(wake, "select_host", lambda _values: HostSelection(
        "experimental_vs_code_proxy", "experimental_proxy_socket_unsafe", None))
    assert wake.status(values, identity)["reason"] == "experimental_proxy_socket_unsafe"
    result = wake.run_once(values, identity, live=True)
    assert result["status"] == "notification_only"
    assert result["reason"] == "experimental_proxy_socket_unsafe"
    assert not host.prompts


def test_experimental_enrollment_rejects_unsafe_endpoint_before_host_launch(setup, monkeypatch):
    values, identity, _profile, _path, host = setup
    values["AGENTBUS_CODEX_HOST_MODE"] = "experimental_vs_code_proxy"
    monkeypatch.setattr(wake, "select_host", lambda _values: HostSelection(
        "experimental_vs_code_proxy", "experimental_proxy_socket_unsafe", None))
    with pytest.raises(ClientError, match="experimental_proxy_socket_unsafe"):
        wake.bind(values, identity, "thread-one", auto_enable=True)
    assert not host.calls


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


@pytest.mark.parametrize("terminal", ["completed", "failed", "interrupted"])
def test_terminal_truth_preserves_pending_bus_work_and_reports_outcome(setup, terminal):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.terminal_status = terminal
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == f"turn_{terminal}"
    assert result["host_turn_status"] == terminal
    status = wake.status(values, identity)
    assert status["last_attempt"]["state"] == terminal
    assert status["last_terminal_status"] == terminal
    assert status["eligible"] == ["MESSAGE:1"]
    assert profile.get("ack_cursor", 0) == 0


def test_accepted_start_consumes_rate_budget_even_if_terminal_is_unobserved(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    def lose_terminal(*_args, **_kwargs):
        raise TurnCompletionUncertain("terminal not observed")
    host.await_turn_terminal = lose_terminal
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completion_unobserved"
    assert wake.status(values, identity)["accepted_wakes_last_hour"] == 1
    assert wake.status(values, identity)["last_attempt"]["turn_id"] == "turn-one"


def test_interrupted_turn_gets_one_cautious_recovery_only_when_host_idle(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.terminal_status = "interrupted"
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_interrupted"
    state_path = wake._state_path(values, profile["chat_id"])
    state = wake._load(state_path)
    state["wake_history"] = [(datetime.now(timezone.utc) - timedelta(seconds=21)).isoformat()]
    wake._save(state_path, state)
    host.resume_failure = CodexHostRejected("thread already has an active writer")
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "thread_owned_by_host"
    assert len(host.prompts) == 1
    assert wake.status(values, identity)["last_attempt"]["turn_status"] == "interrupted"
    host.resume_failure = None
    host.terminal_status = "completed"
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_completed"
    assert "do not repeat them blindly" in host.prompts[-1]
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "pending_after_followup"


def test_completed_turn_is_rate_deferred_before_its_bounded_followup(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    wake.run_once(values, identity, live=True, host_factory=lambda: host)
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "wake_deferred"
    assert len(host.prompts) == 1


def test_newest_equal_priority_message_is_visible_in_compact_prompt(setup):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, *(message(cursor) for cursor in range(1, 12)))
    refs = wake.eligible_events(path, profile)
    assert refs[:8] == [f"MESSAGE:{cursor}" for cursor in range(11, 3, -1)]
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "turn_completed"
    assert "MESSAGE:11" in host.prompts[0]
    assert "MESSAGE:4 and 3 more" in host.prompts[0]
    put(path, profile, {"kind": "CONTROL", "id": "stop-1", "control": {"kind": "stop_end_turn"}})
    assert wake.eligible_events(path, profile) == ["CONTROL:stop-1"]


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
    assert wake.run_once(values, identity, live=True, host_factory=lambda: host)["status"] == "turn_completed"
    assert len(host.prompts) == 1
