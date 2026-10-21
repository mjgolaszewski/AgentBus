"""A first join adopts the service UUID and survives an uncertain response."""

from argparse import Namespace
from pathlib import Path

import pytest

from src import agentbus_client as client
from src import agentbus_codex_wake_client as wake
from src.agentbus_transport_client import ClientError


def test_first_join_stages_credential_then_adopts_service_identity(monkeypatch, tmp_path, capsys):
    values = {"AGENTBUS_URL": "http://example.invalid", "AGENTBUS_API_TOKEN": "test"}
    profile = {"identity": "agentbus:new-chat", "repo": "agentbus",
               "display_name": "New Chat", "chat_id": "old-local-uuid",
               "service_url": values["AGENTBUS_URL"]}
    attempts = []
    monkeypatch.setattr(client, "load_profile", lambda *_args, **_kwargs: profile)
    monkeypatch.setattr(client, "save_profile", lambda *_: None)
    monkeypatch.setattr(wake, "enroll_current", lambda *_: {"status": "workspace_disabled"})

    def enroll(_values, path, body, **_kwargs):
        assert path == "/v1/sessions"
        attempts.append(body.copy())
        if len(attempts) == 1:
            raise ClientError("response lost")
        return {"chat_id": "server-issued-uuid", "session_id": "session-one",
                "channel": "C123", "inbox_id": "inbox-one", "high_water_cursor": 0,
                "revision": "revision-one", "values": {
                    "initial_interval_seconds": 60, "backoff_factor": 2,
                    "max_interval_seconds": 1920,
                }, "sources": {}, "ack_required": True}

    monkeypatch.setattr(client, "api", enroll)
    grant = tmp_path / "enrollment-grant"
    grant.write_text("g" * 40)
    grant.chmod(0o600)
    ns = Namespace(command="join", identity="agentbus:new-chat", handoff_file=None, enrollment_file=str(grant))
    with pytest.raises(ClientError, match="response lost"):
        client._execute_command(ns, Path(tmp_path), values)
    assert profile["chat_id"] == "old-local-uuid"
    assert "handoff_token" not in profile["participation_pending"]
    assert client._execute_command(ns, Path(tmp_path), values) == 0
    assert attempts[0]["session_secret"] == attempts[1]["session_secret"]
    assert profile["chat_id"] == "server-issued-uuid"
    assert profile["participation"]["session_id"] == "session-one"
    assert "session_secret" not in capsys.readouterr().out


@pytest.mark.parametrize('service_url', [None, 'http://foreign.invalid'])
def test_file_grant_join_rejects_missing_or_foreign_service_before_staging(monkeypatch, tmp_path, service_url):
    profile = {'identity': 'agentbus:new-chat', 'repo': 'agentbus',
               'display_name': 'New Chat', 'chat_id': 'local-profile-uuid'}
    if service_url is not None:
        profile['service_url'] = service_url
    before = dict(profile)
    values = {'AGENTBUS_URL': 'http://example.invalid', 'AGENTBUS_API_TOKEN': 'test'}
    monkeypatch.setattr(client, 'load_profile', lambda *_args, **_kwargs: profile)
    effects = []
    monkeypatch.setattr(client, 'save_profile', lambda *_args, **_kwargs: effects.append('save'))
    monkeypatch.setattr(client, 'api', lambda *_args, **_kwargs: effects.append('network'))
    grant = tmp_path / 'enrollment-grant'
    grant.write_text('g' * 40)
    grant.chmod(0o600)
    ns = Namespace(command='join', identity='agentbus:new-chat', handoff_file=None, enrollment_file=str(grant))
    with pytest.raises(ClientError, match='Enrollment service differs'):
        client._execute_command(ns, Path(tmp_path), values)
    assert profile == before and effects == []
    assert 'participation_pending' not in profile
