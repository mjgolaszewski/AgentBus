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
