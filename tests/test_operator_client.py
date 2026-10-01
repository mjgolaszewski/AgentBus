"""Operator CLI commands keep their capability separate from agent messaging."""

import json

import pytest

from src import agentbus_operator_client as operator
from src.agentbus_parser_client import parse_args
from src.agentbus_transport_client import ClientError

VALUES = {"AGENTBUS_API_TOKEN": "ordinary-agent-bearer"}


def test_roster_requires_operator_and_marks_host_binding_local(monkeypatch, capsys):
    from src import agentbus_codex_wake_client as wake

    calls = []
    monkeypatch.setattr(operator, "api", lambda _values, path, **kwargs:
                        calls.append((path, kwargs)) or {"sessions": [{
                            "chat_id": "chat-one", "session_id": "session-one",
                            "route": "agentbus:one", "state": "active_compliant",
                        }]})
    monkeypatch.setattr(wake, "local_binding_projection", lambda *_: {
        "scope": "this_host", "thread_id": "thread-one", "enabled": True,
        "binding_generation": 1, "session_matches": True,
    })
    monkeypatch.delenv("AGENTBUS_OPERATOR_TOKEN", raising=False)
    with pytest.raises(ClientError, match="separate AGENTBUS_OPERATOR_TOKEN"):
        operator.roster(parse_args(["roster"]), VALUES)
    assert not calls
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "operator-only-capability")
    assert operator.roster(parse_args(["roster", "--json", "--all"]), VALUES) == 0
    assert calls == [("/v1/sessions/roster?include_stopped=true",
                      {"bearer_token": "operator-only-capability"})]
    row = json.loads(capsys.readouterr().out)["sessions"][0]
    assert row["local_codex_binding"]["thread_id"] == "thread-one"
    assert row["local_codex_binding"]["scope"] == "this_host"


def test_policy_set_requires_separate_operator_capability_and_reads_json_file(
    tmp_path, monkeypatch, capsys,
):
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps({"max_interval_seconds": 240}))
    ns = parse_args(["policy-set", "--scope", "repo", "--scope-key", "agentbus",
                     "--values-file", str(policy)])
    calls = []
    monkeypatch.setattr(operator, "api", lambda values, path, payload, **kwargs:
                        calls.append((path, payload, kwargs)) or {"revision_id": 7})
    monkeypatch.delenv("AGENTBUS_OPERATOR_TOKEN", raising=False)
    with pytest.raises(ClientError, match="separate AGENTBUS_OPERATOR_TOKEN"):
        operator.policy_set(ns, VALUES)
    assert calls == []
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "ordinary-agent-bearer")
    with pytest.raises(ClientError, match="separate AGENTBUS_OPERATOR_TOKEN"):
        operator.policy_set(ns, VALUES)
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "operator-only-capability")
    assert operator.policy_set(ns, VALUES) == 0
    assert calls == [("/v1/policy/revisions", {
        "scope": "repo", "scope_key": "agentbus", "values": {"max_interval_seconds": 240},
    }, {"bearer_token": "operator-only-capability"})]
    assert capsys.readouterr().out == "POLICY REVISION 7\n"


def test_stop_targets_and_operator_queries_use_existing_api(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "operator-only-capability")
    calls = []

    def fake_api(_values, path, payload=None, **kwargs):
        calls.append((path, payload, kwargs.get("bearer_token")))
        return {"control_id": "control-1", "target_session_ids": ["session-1"]}

    monkeypatch.setattr(operator, "api", fake_api)
    stop = parse_args(["control-stop", "--to", "agentbus:one", "--to", "agentbus:two",
                       "--reason", "stand down"])
    assert operator.control_stop(stop, VALUES) == 0
    all_current = parse_args(["control-stop", "--all", "--reason", "stand down"])
    assert operator.control_stop(all_current, VALUES) == 0
    assert calls[:2] == [
        ("/v1/controls/stop", {"routes": ["agentbus:one", "agentbus:two"],
                               "all_current": False, "reason": "stand down"}, "operator-only-capability"),
        ("/v1/controls/stop", {"routes": None, "all_current": True,
                               "reason": "stand down"}, "operator-only-capability"),
    ]
    issue = parse_args(["control-issue", "--kind", "nudge", "--to", "agentbus:one",
                        "--reason", "please check in"])
    assert operator.control_issue(issue, VALUES) == 0
    assert calls[2] == ("/v1/controls", {
        "kind": "nudge", "routes": ["agentbus:one"], "all_current": False,
        "reason": "please check in",
    }, "operator-only-capability")
    assert operator.control_status(parse_args(["control-status", "control-1"]), VALUES) == 0
    assert operator.session_presence(parse_args(["session-presence", "session-1"]), VALUES) == 0
    assert calls[3:] == [
        ("/v1/controls/control-1", None, "operator-only-capability"),
        ("/v1/sessions/session-1/presence", None, "operator-only-capability"),
    ]
    assert "control-1" in capsys.readouterr().out


def test_policy_show_reads_effective_values_and_sources_without_operator_token(monkeypatch, capsys):
    monkeypatch.delenv("AGENTBUS_OPERATOR_TOKEN", raising=False)
    calls = []

    def fake_api(_values, path):
        calls.append(path)
        return {"revision": "revision-1", "values": {"max_interval_seconds": 240},
                "sources": {"max_interval_seconds": "repo"}}

    monkeypatch.setattr(operator, "api", fake_api)
    assert operator.policy_show(parse_args([
        "policy-show", "--repo", "agentbus", "--chat-id", "chat-1",
    ]), VALUES) == 0
    assert calls == ["/v1/policy/effective?repo=agentbus&chat_id=chat-1"]
    assert json.loads(capsys.readouterr().out)["sources"]["max_interval_seconds"] == "repo"


def test_claim_recovery_cli_can_reassign_or_release(monkeypatch, capsys):
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "operator-only-capability")
    calls = []

    def fake_api(_values, path, payload, **kwargs):
        calls.append((path, payload, kwargs["bearer_token"]))
        return {"cursor": 42, "new_identity": payload["new_identity"]}

    monkeypatch.setattr(operator, "api", fake_api)
    for args in (["claim-recovery", "--cursor", "42", "--to", "agentbus:new",
                  "--reason", "old chat ended"],
                 ["claim-recovery", "--cursor", "42", "--reason", "release"]):
        assert operator.claim_recovery(parse_args(args), VALUES) == 0
    assert calls == [
        ("/v1/messages/42/claim-recovery", {"new_identity": "agentbus:new",
                                           "reason": "old chat ended"}, "operator-only-capability"),
        ("/v1/messages/42/claim-recovery", {"new_identity": None,
                                           "reason": "release"}, "operator-only-capability"),
    ]
    assert len(capsys.readouterr().out.split('"cursor"')) == 3


def test_temporary_override_cli_and_session_policy_explanation(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENTBUS_OPERATOR_TOKEN", "operator-only-capability")
    overlay = tmp_path / "override.json"
    overlay.write_text(json.dumps({"max_interval_seconds": 120}))
    calls = []
    def fake_api(_values, path, payload=None, **kwargs):
        calls.append((path, payload, kwargs.get("bearer_token")))
        return {"control_id": "control-1"} if path == "/v1/controls" else {"revision": "r2"}
    monkeypatch.setattr(operator, "api", fake_api)
    issue = parse_args(["control-issue", "--kind", "temporary_policy_override",
                        "--to", "agentbus:one", "--reason", "focus", "--values-file",
                        str(overlay), "--duration-seconds", "60"])
    assert operator.control_issue(issue, VALUES) == 0
    assert calls[0] == ("/v1/controls", {
        "kind": "temporary_policy_override", "routes": ["agentbus:one"],
        "all_current": False, "reason": "focus", "values": {"max_interval_seconds": 120},
        "duration_seconds": 60,
    }, "operator-only-capability")
    assert operator.policy_show(parse_args(["policy-show", "--session-id", "session-1"]), VALUES) == 0
    assert calls[1] == ("/v1/sessions/session-1/policy", None, "operator-only-capability")
    assert "r2" in capsys.readouterr().out
