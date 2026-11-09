"""Optional stdio MCP projection over one existing session-bound CLI."""

from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from src.agentbus_transport_client import ClientError

PROTOCOL = "2025-11-25"
# Tools never accept identity, channel, service URL or operator authority.
TOOLS = {
    "inbox": ("inbox", {"after": "integer", "limit": "integer", "context": "boolean"}, []),
    "search": ("search", {"query": "string", "limit": "integer", "after": "string"}, ["query"]),
    "thread": ("inbox", {"thread_ts": "string", "limit": "integer"}, ["thread_ts"]),
    "check_in": ("check-in", {}, []),
    "send": ("send", {"recipient": "string", "text": "string", "message_id": "string"}, ["recipient", "text"]),
    "reply": ("reply", {"to_cursor": "integer", "text": "string", "message_id": "string"}, ["to_cursor", "text"]),
    "status": ("message-status", {"cursor": "integer", "message_id": "string"}, []),
    "pickup": ("pickup", {"cursor": "integer", "receipt_id": "string"}, ["cursor"]),
    "complete": ("complete", {"cursor": "integer", "receipt_id": "string"}, ["cursor"]),
    "ack": ("ack", {"through": "integer"}, ["through"]),
}
READ_ONLY = {"search", "status"}


def tool_list():
    return [
        {
            "name": name,
            "description": "Session-scoped AgentBus " + name + "; custody is not recipient execution.",
            "inputSchema": {
                "type": "object",
                "properties": {key: {"type": typ} for key, typ in fields.items()},
                "required": required,
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": name in READ_ONLY},
        }
        for name, (_command, fields, required) in TOOLS.items()
    ]


def tool_argv(identity, name, arguments):
    if name not in TOOLS or not isinstance(arguments, dict):
        raise ValueError("Unknown tool or invalid arguments")
    command, fields, required = TOOLS[name]
    if set(arguments) - set(fields) or any(key not in arguments for key in required):
        raise ValueError("Arguments do not match the session-bound tool schema")
    argv = [command, "--identity", identity]
    if name == "thread":
        argv.append("--context")
    for key, value in arguments.items():
        expected = fields[key]
        if (
            (expected == "string" and (not isinstance(value, str) or len(value) > 6000))
            or (expected == "integer" and type(value) is not int)
            or (expected == "boolean" and type(value) is not bool)
        ):
            raise ValueError("Invalid bounded tool argument")
        if expected == "integer":
            if key == "limit" and not 1 <= value <= (100 if name == "search" else 200):
                raise ValueError("Invalid page limit")
            if key in {"cursor", "to_cursor", "through"} and value < 1:
                raise ValueError("A positive public cursor is required")
            if key == "after" and value < 0:
                raise ValueError("Invalid public cursor")
        if key == "text":
            continue
        flag = "--" + key.replace("_", "-")
        if expected == "boolean":
            if value:
                argv.append(flag)
        else:
            argv.append(flag + "=" + str(value))
    if "text" in arguments:
        argv.extend(["--", arguments["text"]])
    return argv


def serve(identity, invoke, incoming, outgoing):
    initialized = False
    ready = False
    while True:
        raw = incoming.readline(1048577)
        if not raw:
            return 0
        if len(raw.encode()) > 1048576:
            return 1
        request_id = None
        try:
            request = json.loads(raw)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC request")
            request_id = request.get("id")
            method = request.get("method")
            if request_id is None:
                if method == "notifications/initialized" and initialized:
                    ready = True
                continue
            if type(request_id) not in (str, int):
                raise ValueError("Invalid request identity")
            params = request.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("Invalid parameters")
            result: dict[str, Any]
            if method == "initialize":
                if initialized:
                    raise ValueError("Session already initialized")
                initialized = True
                result = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "agentbus",
                        "version": tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())[
                            "project"
                        ]["version"],
                    },
                }
            elif method == "ping":
                result = {}
            elif not ready:
                raise ValueError("Initialize the MCP session before calling tools")
            elif method == "tools/list":
                result = {"tools": tool_list()}
            elif method == "tools/call":
                argv = tool_argv(identity, params.get("name"), params.get("arguments", {}))
                try:
                    result = {"content": [{"type": "text", "text": invoke(argv)}], "isError": False}
                except (ClientError, ValueError, OSError) as exc:
                    result = {
                        "content": [
                            {
                                "type": "text",
                                "text": json.dumps(
                                    {
                                        "error": "AgentBus operation failed; preserve the original request before retrying.",
                                        "request_id": next(
                                            iter(re.findall(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", str(exc))),
                                            None,
                                        )
                                        if isinstance(exc, ClientError)
                                        else None,
                                        "acceptance": "unknown",
                                    }
                                ),
                            }
                        ],
                        "isError": True,
                    }
            else:
                outgoing.write(
                    json.dumps(
                        {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Method not found"}}
                    )
                    + "\n"
                )
                outgoing.flush()
                continue
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except json.JSONDecodeError:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        except (ValueError, TypeError, RecursionError):
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "Invalid request or parameters"},
            }
        outgoing.write(json.dumps(response, ensure_ascii=True) + "\n")
        outgoing.flush()


def mcp_command(ns, project, values):
    from src import agentbus_client as client
    from src.agentbus_parser_client import parse_args
    from src.agentbus_scope_client import scope, verify_projection

    identity = client.resolve_identity(ns.identity)
    profile = client.load_profile(values, identity, save_migration=False)
    binding = scope(profile)
    if binding["service_url"] != values.get("AGENTBUS_URL"):
        raise ClientError("MCP profile service mismatch")

    def invoke(argv):
        current = client.load_profile(values, identity, save_migration=False)
        verify_projection(binding, current)
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                operation = parse_args(argv)
        except SystemExit:
            raise ClientError("Invalid tool command arguments") from None
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code = client.CLI_OPERATIONS[operation.command](operation, project, values)
        if code:
            raise ClientError("AgentBus tool failed")
        return output.getvalue()

    return serve(identity, invoke, sys.stdin, sys.stdout)
