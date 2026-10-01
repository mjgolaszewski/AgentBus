"""A first join adopts the service UUID and survives an uncertain response."""

from argparse import Namespace
from pathlib import Path

import pytest

from src import agentbus_client as client
from src import agentbus_codex_wake_client as wake
from src.agentbus_transport_client import ClientError


def test_first_join_stages_credential_then_adopts_service_identity(monkeypatch, tmp_path, capsys):
    profile = {"identity": "agentbus:new-chat", "repo": "agentbus",
               "display_name": "New Chat", "chat_id": "old-local-uuid"}
    values = {"AGENTBUS_URL": "http://example.invalid", "AGENTBUS_API_TOKEN": "test"}
    attempts = []
    monkeypatch.setattr(client, "checked_profile", lambda *_: (profile, {}))
    monkeypatch.setattr(client, "save_profile", lambda *_: None)
    monkeypatch.setattr(wake, "enroll_current", lambda *_: {"status": "workspace_disabled"})

    def enroll(_values, path, body, **_kwargs):
        assert path == "/v1/sessions"
        attempts.append(body.copy())
        if len(attempts) == 1:
            raise ClientError("response lost")
        return {"chat_id": "server-issued-uuid", "session_id": "session-one",
                "revision": "revision-one", "values": {}, "sources": {}, "ack_required": True}

    monkeypatch.setattr(client, "api", enroll)
    ns = Namespace(command="join", identity="agentbus:new-chat", handoff_file=None)
    with pytest.raises(ClientError, match="response lost"):
        client._execute_command(ns, Path(tmp_path), values)
    assert profile["chat_id"] == "old-local-uuid"
    assert "handoff_token" not in profile["participation_pending"]
    assert client._execute_command(ns, Path(tmp_path), values) == 0
    assert attempts[0]["session_secret"] == attempts[1]["session_secret"]
    assert profile["chat_id"] == "server-issued-uuid"
    assert profile["participation"]["session_id"] == "session-one"
    assert "session_secret" not in capsys.readouterr().out
