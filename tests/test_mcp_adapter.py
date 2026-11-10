"""The stdio boundary exposes one session and no operator tools."""

import io
import json

import pytest

from src.agentbus_mcp_client import PROTOCOL, serve, tool_argv, tool_list


def test_mcp_handshake_tool_execution_and_notifications_are_protocol_only():
    calls = []
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": PROTOCOL}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "send", "arguments": {"recipient": "local:agentbus:other", "text": "--literal"}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "send", "arguments": {"identity": "foreign", "recipient": "x", "text": "bad"}},
        },
    ]
    incoming = io.StringIO("".join(json.dumps(r) + "\n" for r in requests))
    outgoing = io.StringIO()

    def invoke(argv):
        calls.append(argv)
        return '{"delivery_receipt":{"state":"accepted"}}'

    assert serve("local:agentbus:mine", invoke, incoming, outgoing) == 0
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert len(responses) == 4
    assert responses[0]["result"]["protocolVersion"] == PROTOCOL
    assert calls == [
        ["send", "--identity", "local:agentbus:mine", "--recipient=local:agentbus:other", "--", "--literal"]
    ]
    assert responses[-1]["error"]["code"] == -32602
    assert all("operator" not in t["name"] for t in tool_list())


def test_tools_reject_authority_switches_and_ack_is_explicit():
    for key in ["identity", "channel", "service_url", "operator_token"]:
        with pytest.raises(ValueError):
            tool_argv("mine", "inbox", {key: "other"})
    assert "ack" not in [t["name"] for t in tool_list() if t["annotations"]["readOnlyHint"]]
    with pytest.raises(ValueError):
        tool_argv("mine", "inbox", {"limit": True})


def test_failed_tool_preserves_uuid_but_redacts_other_exception_details():
    from src.agentbus_transport_client import ClientError
    request_id='11111111-1111-4111-8111-111111111111'
    requests=[{'jsonrpc':'2.0','id':1,'method':'initialize'},
        {'jsonrpc':'2.0','method':'notifications/initialized'},
        {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'send','arguments':{'recipient':'local:agentbus:other','text':'hello'}}}]
    def fail(_argv):
        raise ClientError('private diagnostic secret; preserve original UUID '+request_id)
    output=io.StringIO()
    serve('local:agentbus:mine',fail,io.StringIO(''.join(json.dumps(r)+'\n' for r in requests)),output)
    result=json.loads(output.getvalue().splitlines()[-1])['result']
    assert result['isError']
    detail=json.loads(result['content'][0]['text'])
    assert detail['request_id']==request_id and detail['acceptance']=='unknown'
    assert 'private diagnostic secret' not in output.getvalue()


@pytest.mark.parametrize("envelope", [
    [], {"jsonrpc": "1.0", "id": 1, "method": "ping"},
    {"jsonrpc": "2.0", "id": True, "method": "ping"},
    {"jsonrpc": "2.0", "id": None, "method": "ping"},
    {"jsonrpc": "2.0", "id": 1, "method": []},
])
def test_malformed_envelope_has_no_reflected_authority_and_stream_recovers(envelope):
    calls = []
    output = io.StringIO()
    incoming = io.StringIO(json.dumps(envelope) + '\n' + json.dumps(
        {"jsonrpc": "2.0", "id": "recovery", "method": "ping"}) + '\n')
    serve("local:agentbus:mine", calls.append, incoming, output)
    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0] == {"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "Invalid request"}}
    assert responses[1] == {"jsonrpc": "2.0", "id": "recovery", "result": {}}
    assert calls == []


def test_real_stdio_client_uses_only_its_bound_authenticated_mailbox(tmp_path):
    import os
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from src.agentbus_client import atomic_json

    observed = []

    class Mailbox(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append((self.command, self.path, self.headers.get("Authorization"),
                             self.headers.get("X-AgentBus-Session-Token"),
                             self.headers.get("X-AgentBus-Inbox-ID")))
            body = json.dumps({"messages": [], "has_more": False, "after": None}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            observed.append((self.command, self.path))
            self.send_error(405)

        def log_message(self, *_args):
            pass

    root = Path(__file__).resolve().parents[1]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Mailbox)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    url = "http://127.0.0.1:" + str(server.server_port)
    identity = "isolated:agentbus:stdio-client"
    profiles = tmp_path / "profiles"
    profile = {"schema_version": 2, "identity": identity, "repo": "agentbus",
               "chat_id": "isolated-chat", "service_url": url, "channel": "C123",
               "inbox_id": "isolated-inbox", "participation": {
                   "session_id": "isolated-session", "session_secret": "isolated-test-secret"}}
    atomic_json(profiles / (identity + ".json"), profile)
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "isolated-acceptance-client", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "scoped text", "limit": 20}}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "search", "arguments": {"query": "foreign", "identity": "other"}}},
    ]
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("AGENTBUS_")}
    environment.update(AGENTBUS_CONFIG=str(tmp_path / "absent.env"),
                       AGENTBUS_PROJECT_DIR=str(root), AGENTBUS_URL=url,
                       AGENTBUS_API_TOKEN="isolated-test-bearer",
                       AGENTBUS_CONSUMER_STATE_DIR=str(profiles))
    try:
        result = subprocess.run([sys.executable, str(root / "agentbus"), "mcp", "--identity", identity],
                                input="".join(json.dumps(r) + "\n" for r in requests),
                                capture_output=True, text=True, timeout=20, env=environment)
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3]
    assert responses[1]["result"]["isError"] is False
    assert responses[2]["error"]["code"] == -32602
    assert observed == [("GET", "/v1/search?query=scoped+text&limit=20", "Bearer isolated-test-bearer",
                         "isolated-test-secret", "isolated-inbox")]
    assert json.loads((profiles / (identity + ".json")).read_text()) == profile
