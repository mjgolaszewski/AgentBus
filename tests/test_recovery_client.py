"""Resume is a scope-bound observation, never acknowledgement or host restart."""
import copy
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src import agentbus_client as client
from src import agentbus_poll_client as poll
from src import agentbus_recovery_client as recovery
from src.agentbus_transport_client import ClientError


@pytest.fixture
def resume(tmp_path, monkeypatch):
    profile = {"identity": "alpha:agentbus:writer", "service_url": "http://127.0.0.1:8766", "channel": "C111",
               "chat_id": str(uuid4()), "inbox_id": str(uuid4()), "ack_cursor": 7,
               "participation": {"session_id": str(uuid4())}}
    presence = {"state": "active_compliant", "work_paused": False}
    controls = {"controls": []}
    values = {"AGENTBUS_URL": profile["service_url"], "AGENTBUS_CONSUMER_STATE_DIR": str(tmp_path)}
    calls = []
    monkeypatch.setattr(client, "resolve_identity", lambda value: value)
    monkeypatch.setattr(client, "control_profile", lambda *_: (profile, presence))
    monkeypatch.setattr(poll, "foreground_check_in", lambda *_: calls.append("check-in") or controls)
    monkeypatch.setattr(recovery, "scoped_api", lambda *args: calls.append(args[1]) or
                        {"messages": [], "next_cursor": 12, "has_more": False})
    yield profile, presence, controls, values, calls


def test_resume_preserves_ack_and_discloses_unknown_gaps(resume, tmp_path, capsys):
    profile, _, _, values, calls = resume
    before = copy.deepcopy(profile)
    ns = SimpleNamespace(identity=profile["identity"])
    assert recovery.cli_resume(ns, tmp_path, values) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["unmonitored_gap"]["from"] is None
    assert first["semantic_ack_cursor"] == 7 and first["read_is_not_acknowledgement"] is True
    assert profile == before and "after=7" in calls[-1]
    assert recovery.cli_resume(ns, tmp_path, values) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["unmonitored_gap"]["from"] == first["unmonitored_gap"]["to"]
    assert not second["unmonitored_gap"]["continuous_monitoring"]
    assert not any("ack" in path or "wake" in path for path in calls)
    assert "session_secret" not in next(tmp_path.glob(".resume-*.json")).read_text()


@pytest.mark.parametrize("hold", ["stopped", "pause_work", "stop_end_turn", "policy_ack"])
def test_resume_safety_controls_precede_mailbox(resume, tmp_path, capsys, hold):
    profile, presence, controls, values, calls = resume
    if hold == "stopped":
        presence["state"] = "stopped"
    elif hold == "policy_ack":
        controls.update({"ack_required": True})
    else:
        controls["controls"] = [{"kind": hold, "control_id": str(uuid4())}]
    assert recovery.cli_resume(SimpleNamespace(identity=profile["identity"]), tmp_path, values) == 0
    assert json.loads(capsys.readouterr().out)["mailbox"] is None
    assert calls == ([] if hold == "stopped" else ["check-in"])


def test_resume_rejects_foreign_checkpoint_before_controls_or_inbox(resume, tmp_path, capsys):
    profile, _, _, values, calls = resume
    ns = SimpleNamespace(identity=profile["identity"])
    recovery.cli_resume(ns, tmp_path, values)
    capsys.readouterr()
    checkpoint = next(tmp_path.glob(".resume-*.json"))
    altered = json.loads(checkpoint.read_text())
    altered["channel"] = "C222"
    checkpoint.write_text(json.dumps(altered))
    calls.clear()
    with pytest.raises(ClientError, match="scope differs"):
        recovery.cli_resume(ns, tmp_path, values)
    assert calls == [] and json.loads(checkpoint.read_text())["channel"] == "C222"
