"""Only exact, ready session and idle saved-owner facts permit a live attempt."""

import json

import pytest
from test_codex_wake_client import message, put
from test_codex_wake_client import setup as setup

from src import agentbus_codex_wake_client as wake


@pytest.mark.parametrize("observation", [
    {"id": "other-thread", "ephemeral": False, "status": {"type": "idle"}},
    {"id": "thread-one", "ephemeral": True, "status": {"type": "idle"}},
    {"id": "thread-one", "ephemeral": False, "status": {"type": "notLoaded"}},
    {"id": "thread-one", "ephemeral": False, "status": {"type": "unknown"}},
    {"id": "thread-one", "ephemeral": False, "status": {"type": "active"}},
    {"status": {"type": "idle"}},
    {"id": "thread-one", "ephemeral": False, "status": "idle"},
])
def test_unproved_owner_observation_never_resumes_or_starts(setup, monkeypatch, observation):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.calls.clear()
    monkeypatch.setattr(host, "read_thread", lambda _thread: observation)
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] in {"host_identity_mismatch", "host_not_idle", "thread_active"}
    assert not host.calls and not host.prompts
    state = json.loads(wake._state_path(values, profile["chat_id"]).read_text())
    assert state["last_attempt"] is None and state["wake_epoch"] == 0
    assert profile.get("ack_cursor", 0) == 0


@pytest.mark.parametrize("presence_state", ["joining", "overdue", "stopping", "unknown", None])
def test_unready_session_never_contacts_owner(setup, monkeypatch, presence_state):
    values, identity, profile, path, host = setup
    wake.bind(values, identity, "thread-one", lambda: host)
    wake.set_enabled(values, identity, True)
    put(path, profile, message(1))
    host.calls.clear()
    monkeypatch.setattr(wake, "api", lambda *_args, **_kwargs: {
        "chat_id": profile["chat_id"], "session_id": profile["participation"]["session_id"],
        "route": identity, "state": presence_state,
    })
    result = wake.run_once(values, identity, live=True, host_factory=lambda: host)
    assert result["status"] == "service_session_not_ready"
    assert not host.calls and not host.prompts
