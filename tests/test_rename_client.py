"""Routing rename keeps local identity and undelivered poll state through retry."""

import json
from pathlib import Path

import pytest

from src import agentbus_client as client
from src import agentbus_poll_client as poll
from src import agentbus_rename_client as rename
from src.agentbus_transport_client import ClientError


def setup_profile(tmp_path):
    state = tmp_path / "profiles"
    state.mkdir()
    old_route = "agentbus:flower"
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766"}
    profile = {"schema_version": 2, "identity": old_route, "chat_id": "chat-1",
               "repo": "agentbus", "name": "flower", "display_name": "The Flower",
               "inbox_id": "inbox-1", "channel": "C123", "service_url": values["AGENTBUS_URL"],
               "ack_cursor": 7, "observed_cursor": 8,
               "participation": {"session_id": "session-1", "session_secret": "x" * 32,
                                 "acknowledged_policy_revision": "revision-1"}}
    path = state / f"{old_route}.json"
    path.write_text(json.dumps(profile))
    return values, profile, path


def test_rename_recovers_uncertain_response_and_moves_undelivered_events(
    tmp_path, monkeypatch, capsys,
):
    values, profile, old_path = setup_profile(tmp_path)
    poll.change_spool(old_path, profile, lambda spool: poll._append(spool, {
        "kind": "CONTROL", "id": "stop-1", "control": {"control_id": "stop-1"},
    }))
    monkeypatch.setattr(client, "configuration", lambda **_kwargs: (Path("/unused"), values))
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"inbox_id": "inbox-1", "channel": "C123"})
    attempts = []

    def fake_rename(_values, path, payload, **kwargs):
        attempts.append((path, payload, kwargs))
        if len(attempts) == 1:
            raise ClientError("transport uncertain")
        return {"chat_id": "chat-1", "new_route": "agentbus:night-flower"}

    monkeypatch.setattr(rename, "api", fake_rename)
    workers = []
    monkeypatch.setattr(rename, "ensure_worker", lambda path, _values: workers.append(path))
    command = ["rename", "--identity", "agentbus:flower", "--name", "night-flower"]
    assert client.main(command) == 1
    assert json.loads(old_path.read_text())["rename_pending"] == "agentbus:night-flower"
    assert client.main(command) == 0
    new_path = old_path.with_name("agentbus:night-flower.json")
    migrated = json.loads(new_path.read_text())
    assert not old_path.exists()
    assert migrated["chat_id"] == "chat-1" and migrated["identity"] == "agentbus:night-flower"
    assert migrated["display_name"] == "The Flower" and migrated["ack_cursor"] == 7
    assert "rename_pending" not in migrated
    assert poll.peek_event(new_path, migrated)["control"]["control_id"] == "stop-1"
    assert not poll.spool_path(old_path).exists()
    assert workers == [new_path]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert capsys.readouterr().out.endswith("ROUTE agentbus:flower -> agentbus:night-flower\n")


def test_rename_rejects_local_collision_before_remote_mutation(tmp_path, monkeypatch):
    values, _, old_path = setup_profile(tmp_path)
    old_path.with_name("agentbus:taken.json").write_text(json.dumps({"chat_id": "other"}))
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"inbox_id": "inbox-1", "channel": "C123"})
    monkeypatch.setattr(rename, "api", lambda *_args, **_kwargs:
                        pytest.fail("remote rename must not execute after local collision"))
    with pytest.raises(ClientError, match="another chat"):
        rename.rename_chat(client.argparse.Namespace(identity="agentbus:flower", name="taken"), values)
    assert "rename_pending" not in json.loads(old_path.read_text())


def test_first_new_route_read_recovers_crash_after_service_receipt(tmp_path, monkeypatch):
    values, profile, old_path = setup_profile(tmp_path)
    poll.change_spool(old_path, profile, lambda spool: poll._append(spool, {
        "kind": "CONTROL", "id": "stop-1", "control": {"control_id": "stop-1"},
    }))
    new_path = old_path.with_name("agentbus:night-flower.json")
    new_profile = {**profile, "identity": "agentbus:night-flower", "name": "night-flower",
                   "rename_source": "agentbus:flower"}
    new_path.write_text(json.dumps(new_profile))
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"inbox_id": "inbox-1", "channel": "C123"})
    workers = []
    monkeypatch.setattr(rename, "ensure_worker", lambda path, _values: workers.append(path))
    recovered, _ = client.checked_profile(values, "agentbus:night-flower")
    assert not old_path.exists() and not poll.spool_path(old_path).exists()
    assert "rename_source" not in recovered
    assert json.loads(new_path.read_text()) == recovered
    assert poll.peek_event(new_path, recovered)["kind"] == "CONTROL"
    assert workers == [new_path]
