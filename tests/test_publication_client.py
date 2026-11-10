"""Publication clients preserve scoped immutable retry identity without bus forwarding."""
import io
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src import agentbus_client as client
from src import agentbus_publication_client as publication
from src.agentbus_lifecycle_client import request_stage
from src.agentbus_parser_client import build_parser
from src.agentbus_transport_client import ClientError


@pytest.fixture
def profile(tmp_path):
    return {"identity": "alpha:agentbus:writer", "service_url": "http://127.0.0.1:8766",
            "chat_id": str(uuid4()), "inbox_id": str(uuid4()), "channel": "C111",
            "participation": {"session_id": str(uuid4())}}


def test_publication_staging_is_separate_and_preserves_original_epoch(tmp_path, profile):
    values = {"AGENTBUS_URL": profile["service_url"], "AGENTBUS_STATE_DIR": str(tmp_path)}
    key, payload = str(uuid4()), {"destination": "updates", "text": "Exact"}
    assert request_stage(values, profile, key, payload, "a" * 64, purpose="publication") == "a" * 64
    assert request_stage(values, profile, key, payload, "b" * 64, purpose="publication", existing=True) == "a" * 64
    with pytest.raises(ClientError, match="differs"):
        request_stage(values, profile, key, {**payload, "text": "Changed"}, "b" * 64,
                      purpose="publication", existing=True)
    with pytest.raises(ClientError, match="missing"):
        request_stage(values, profile, key, payload, "a" * 64, existing=True)
    foreign = {**profile, "channel": "C222"}
    with pytest.raises(ClientError, match="missing"):
        request_stage(values, foreign, key, payload, "a" * 64, purpose="publication", existing=True)


def test_update_stages_before_network_and_retries_exact_original(tmp_path, profile, monkeypatch, capsys):
    values = {"AGENTBUS_URL": profile["service_url"], "AGENTBUS_STATE_DIR": str(tmp_path)}
    info = {"features": ["client-publication-v1"], "publication": {"version": 1, "request_epoch": "a" * 64}}
    monkeypatch.setattr(client, "checked_profile", lambda *_: (profile, info))
    monkeypatch.setattr(client, "resolve_identity", lambda value: value)
    calls = []
    def uncertain(values, path, bound, payload, **kwargs):
        calls.append((path, payload, kwargs))
        assert list((tmp_path / "publication-requests").glob("*.json"))
        raise ClientError("HTTP 503")
    monkeypatch.setattr(publication, "scoped_api", uncertain)
    ns = SimpleNamespace(identity=profile["identity"], destination="updates", text="Exact", message_id=None)
    with pytest.raises(ClientError, match="update-status"):
        publication.cli_update(ns, tmp_path, values)
    ns.message_id = calls[0][2]["publication_key"]
    info["publication"]["request_epoch"] = "b" * 64
    with pytest.raises(ClientError):
        publication.cli_update(ns, tmp_path, values)
    assert calls[0] == calls[1]
    ns.text = "Changed"
    with pytest.raises(ClientError, match="differs"):
        publication.cli_update(ns, tmp_path, values)
    assert len(calls) == 2 and not capsys.readouterr().out


def test_unsupported_server_and_oversized_stdin_never_submit(tmp_path, profile, monkeypatch):
    monkeypatch.setattr(client, "checked_profile", lambda *_: (profile, {"features": []}))
    monkeypatch.setattr(client, "resolve_identity", lambda value: value)
    ns = SimpleNamespace(identity=profile["identity"], destination="updates", text="Exact", message_id=None)
    monkeypatch.setattr(publication, "scoped_api", lambda *_a, **_k: pytest.fail("No admission allowed"))
    with pytest.raises(ClientError, match="unsupported"):
        publication.cli_update(ns, tmp_path, {})
    ns.text = "-"
    monkeypatch.setattr(publication.sys, "stdin", io.StringIO("x" * 6001))
    with pytest.raises(ClientError, match="6000"):
        publication.cli_update(ns, tmp_path, {})


def test_publication_parser_has_no_membership_or_operator_selectors():
    parser = build_parser()
    ns = parser.parse_args(["update", "--identity", "writer", "--destination", "updates", "Exact"])
    assert ns.text == "Exact" and ns.message_id is None
    for option in ("--location", "--channel", "--federated", "--recipient", "--approve"):
        with pytest.raises(SystemExit):
            parser.parse_args(["update", "--destination", "updates", option, "elsewhere", "Exact"])


def test_stopped_receipt_cli_skips_only_rejected_info_not_scope_checks(profile, capsys):
    from src.agentbus_lifecycle_client import status_command
    request = str(uuid4())
    ns = SimpleNamespace(message_id=request, cursor=None)
    calls = []
    def api_call(values, path, **kwargs):
        calls.append((path, kwargs))
        if path == "/v1/info":
            raise ClientError("HTTP 423", status=423)
        return {"request_id": request, "known": False}
    assert status_command(ns, {"AGENTBUS_URL": profile["service_url"]}, profile, api_call=api_call) == 0
    assert calls[-1][0] == "/v1/requests/" + request + "/outcome"
    assert calls[-1][1]["inbox_id"] == profile["inbox_id"]
    assert '"known": false' in capsys.readouterr().out


def test_publication_transport_binds_headers_and_checks_original_acceptance(monkeypatch, profile):
    import json

    from src import agentbus_transport_client as transport
    requests = []
    key = str(uuid4())
    class Reply(io.BytesIO):
        status = 202
        headers = {}
    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            return Reply(json.dumps({"request_id": key, "state": "awaiting_approval"}).encode())
    monkeypatch.setattr(transport, "build_opener", lambda *_: Opener())
    values = {"AGENTBUS_URL": profile["service_url"], "AGENTBUS_API_TOKEN": "private-bearer"}
    payload = {"destination": "updates", "text": "Exact"}
    result = transport.api(values, "/v1/publications", payload, session_token="private-session", inbox_id="inbox",
                           publication_key=key, publication_epoch="a" * 64)
    assert result["request_id"] == key
    assert requests[-1].get_header("Idempotency-key") == key
    assert requests[-1].get_header("X-agentbus-request-epoch") == "a" * 64
    for wrong in ("/v1/messages", "/v1/publications/" + key + "/cancel"):
        with pytest.raises(ClientError, match="original scoped"):
            transport.api(values, wrong, payload, session_token="private-session", inbox_id="inbox",
                          publication_key=key, publication_epoch="a" * 64)
    assert len(requests) == 1
    key = str(uuid4())
    # The response belongs to the new closure key while the submitted key stays original.
    with pytest.raises(ClientError, match="uncertain"):
        transport.api(values, "/v1/publications", payload, session_token="private-session", inbox_id="inbox",
                      publication_key=result["request_id"], publication_epoch="a" * 64)
