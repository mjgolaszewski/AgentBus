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
    values = {"AGENTBUS_CONSUMER_STATE_DIR": str(state), "AGENTBUS_URL": "http://127.0.0.1:8766",
              "AGENTBUS_LOCAL_PREFIX": "home"}
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
                        {"chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123", "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]})
    attempts = []

    def fake_rename(_values, path, payload, **kwargs):
        attempts.append((path, payload, kwargs))
        if len(attempts) == 1:
            raise ClientError("transport uncertain")
        return {"chat_id": "chat-1", "new_route": "home:agentbus:night-flower"}

    monkeypatch.setattr(rename, "api", fake_rename)
    workers = []
    monkeypatch.setattr(rename, "ensure_worker", lambda path, _values: workers.append(path))
    command = ["rename", "--identity", "agentbus:flower", "--name", "night-flower"]
    assert client.main(command) == 1
    assert json.loads(old_path.read_text())["rename_pending"] == "home:agentbus:night-flower"
    assert client.main(command) == 0
    new_path = old_path.with_name("home:agentbus:night-flower.json")
    migrated = json.loads(new_path.read_text())
    assert not old_path.exists()
    assert migrated["chat_id"] == "chat-1" and migrated["identity"] == "home:agentbus:night-flower"
    assert migrated["display_name"] == "The Flower" and migrated["ack_cursor"] == 7
    assert "rename_pending" not in migrated
    assert poll.peek_event(new_path, migrated)["control"]["control_id"] == "stop-1"
    assert not poll.spool_path(old_path).exists()
    assert workers == [new_path]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert capsys.readouterr().out.endswith("ROUTE agentbus:flower -> home:agentbus:night-flower\n")


def test_rename_rejects_local_collision_before_remote_mutation(tmp_path, monkeypatch):
    values, _, old_path = setup_profile(tmp_path)
    old_path.with_name("home:agentbus:taken.json").write_text(json.dumps({"chat_id": "other"}))
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123", "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]})
    monkeypatch.setattr(rename, "api", lambda *_args, **_kwargs:
                        pytest.fail("remote rename must not execute after local collision"))
    with pytest.raises(ClientError, match="another chat"):
        rename.rename_chat(client.argparse.Namespace(identity="agentbus:flower", name="taken"), values)
    assert "rename_pending" not in json.loads(old_path.read_text())


def test_rename_without_new_name_moves_joined_chat_to_configured_prefix(tmp_path, monkeypatch):
    values, profile, old_path = setup_profile(tmp_path)
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123", "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]})
    monkeypatch.setattr(rename, "api", lambda *_args, **_kwargs:
                        {"chat_id": "chat-1", "new_route": "home:agentbus:flower"})
    monkeypatch.setattr(rename, "ensure_worker", lambda *_args: None)
    assert rename.rename_chat(client.argparse.Namespace(identity=profile["identity"], name=None), values) == 0
    new_path = old_path.with_name("home:agentbus:flower.json")
    moved = json.loads(new_path.read_text())
    assert not old_path.exists()
    assert moved["chat_id"] == profile["chat_id"]
    assert moved["participation"] == profile["participation"]
    assert moved["ack_cursor"] == profile["ack_cursor"]


def test_rename_rejects_invalid_configured_prefix_before_service_mutation(tmp_path, monkeypatch):
    values, profile, old_path = setup_profile(tmp_path)
    values["AGENTBUS_LOCAL_PREFIX"] = "Home!"
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs:
                        {"chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123", "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]})
    monkeypatch.setattr(rename, "api", lambda *_args, **_kwargs:
                        pytest.fail("invalid prefix reached the service"))
    with pytest.raises(ClientError, match="AGENTBUS_LOCAL_PREFIX"):
        rename.rename_chat(client.argparse.Namespace(identity=profile["identity"], name=None), values)
    assert old_path.exists()
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
                        {"chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123", "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"]})
    workers = []
    monkeypatch.setattr(rename, "ensure_worker", lambda path, _values: workers.append(path))
    recovered, _ = client.checked_profile(values, "agentbus:night-flower")
    assert not old_path.exists() and not poll.spool_path(old_path).exists()
    assert "rename_source" not in recovered
    assert json.loads(new_path.read_text()) == recovered
    assert poll.peek_event(new_path, recovered)["kind"] == "CONTROL"
    assert workers == [new_path]


@pytest.mark.parametrize("change", ["secret", "rotation", "stopped", "metadata"])
def test_rename_recovery_retains_both_profiles_on_newer_source_state(tmp_path, monkeypatch, change):
    values, profile, old_path = setup_profile(tmp_path)
    new_path = old_path.with_name("agentbus:night-flower.json")
    target = {**profile, "identity": "agentbus:night-flower", "name": "night-flower",
              "rename_source": profile["identity"]}
    new_path.write_text(json.dumps(target))
    target = json.loads(new_path.read_text())
    if change == "secret":
        profile["participation"]["session_secret"] = "y" * 32
    elif change == "rotation":
        profile["participation_rotation_pending"] = {"request_id": "retained-request"}
    elif change == "stopped":
        profile["participation"]["stopped"] = True
    else:
        profile["display_name"] = "Fresh display name"
    old_path.write_text(json.dumps(profile))
    before = (old_path.read_bytes(), new_path.read_bytes())
    monkeypatch.setattr(rename, "ensure_worker", lambda *_args: pytest.fail("conflict started worker"))
    with pytest.raises(ClientError, match="state changed"):
        rename.recover_rename_state(values, target)
    assert (old_path.read_bytes(), new_path.read_bytes()) == before


def test_rename_recovery_rejects_self_source_without_deleting_profile(tmp_path):
    values, profile, path = setup_profile(tmp_path)
    profile["rename_source"] = profile["identity"]
    path.write_text(json.dumps(profile))
    before = path.read_bytes()
    with pytest.raises(ClientError, match="Invalid rename source"):
        rename.recover_rename_state(values, profile)
    assert path.read_bytes() == before


def test_atomic_profile_directory_sync_failure_retains_replaced_file(tmp_path, monkeypatch):
    import os
    import stat

    path = tmp_path / "profile.json"
    path.write_text('{"old": true}')
    actual_fsync = os.fsync
    calls = []

    def fsync(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            calls.append("directory")
            raise OSError("injected directory sync failure")
        calls.append("file")
        return actual_fsync(fd)

    monkeypatch.setattr(client.os, "fsync", fsync)
    with pytest.raises(OSError, match="directory sync"):
        client.atomic_json(path, {"new": True})
    assert calls == ["file", "directory"]
    assert json.loads(path.read_text()) == {"new": True}
    assert list(tmp_path.iterdir()) == [path]


def test_remote_rename_receipt_cannot_overwrite_concurrent_local_rotation(tmp_path, monkeypatch):
    values, profile, old_path = setup_profile(tmp_path)
    monkeypatch.setattr(client, "api", lambda *_args, **_kwargs: {
        "chat_id": "chat-1", "session_id": "session-1", "inbox_id": "inbox-1", "channel": "C123",
        "features": ["session-required", "channel-isolation-v1", "channel-local-cursors-v1"],
    })

    def receipt(*_args, **_kwargs):
        fresh = client.load_profile(values, profile["identity"], save_migration=False)
        fresh["participation"]["session_secret"] = "new-session-secret" * 2
        client.save_profile(values, fresh)
        return {"chat_id": "chat-1", "new_route": "home:agentbus:night-flower"}

    monkeypatch.setattr(rename, "api", receipt)
    with pytest.raises(ClientError, match="authority changed"):
        rename.rename_chat(client.argparse.Namespace(identity=profile["identity"], name="night-flower"), values)
    retained = json.loads(old_path.read_text())
    assert retained["participation"]["session_secret"] == "new-session-secret" * 2
    assert retained["rename_pending"] == "home:agentbus:night-flower"
    assert not old_path.with_name("home:agentbus:night-flower.json").exists()
