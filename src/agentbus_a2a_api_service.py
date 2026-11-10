"""Authenticated loopback transport for the narrowly declared A2A adapter."""
import ipaddress
import sqlite3

from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

from src.agentbus_a2a_service import A2ARejected, exact, zero_history
from src.agentbus_channel_api_service import resolve_principal
from src.agentbus_delivery_api_service import send_message


async def admit_mailbox(request, body, key):
    headers = {k.lower(): v for k, v in request.headers.items()}
    headers["idempotency-key"] = key
    headers["x-agentbus-delivery-version"] = "2" if request.app.state.store.lifecycle.enabled else "1"
    # Keep the caller's original epoch. This adapter never allocates a fresh
    # epoch for an uncertain request and never enters immediate provider posting.
    child = Request({**request.scope, "headers": [(k.encode(), v.encode()) for k, v in headers.items()]})
    return await send_message(child, body, headers.get("x-agentbus-session-token"), Response())


async def api_a2a(request: Request):
    return await dispatch(request)


def local_adapter(request):
    settings = request.app.state.settings
    if not settings.a2a_enabled:
        raise HTTPException(404, "Not found")
    client = request.client.host if request.client is not None else ""
    server = request.scope.get("server")
    try:
        if (not ipaddress.ip_address(client).is_loopback or not server
                or not ipaddress.ip_address(server[0]).is_loopback or server[1] != settings.bind_port):
            raise ValueError()
    except ValueError:
        raise HTTPException(403, "Loopback transport required") from None
    return resolve_principal(request)


def agent_card(request):
    settings = request.app.state.settings
    host = "[::1]" if settings.bind_host == "::1" else "127.0.0.1"
    return {"name": "AgentBus local mailbox", "description": "Mailbox admission only; no recipient execution guarantee.",
            "version": "0.11.0", "supportedInterfaces": [{"url": f"http://{host}:{settings.bind_port}/a2a",
                "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
            "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": True},
            "defaultInputModes": ["text/plain"], "defaultOutputModes": ["application/json"],
            "securitySchemes": {"bearer": {"httpAuthSecurityScheme": {"scheme": "Bearer"}},
                "session": {"apiKeySecurityScheme": {"location": "header", "name": "X-AgentBus-Session-Token"}},
                "inbox": {"apiKeySecurityScheme": {"location": "header", "name": "X-AgentBus-Inbox-ID"}}},
            "securityRequirements": [{"schemes": {"bearer": {"list": []}, "session": {"list": []}, "inbox": {"list": []}}}],
            "skills": [{"id": "mailbox.accept", "name": "Accept mailbox delivery",
                "description": "One exact recipient and text part; completion means origin durable admission only.",
                "tags": ["mailbox", "delivery"]}]}


async def dispatch(request):
    principal = local_adapter(request)
    correlation = None
    try:
        payload = await request.json()
    except (ValueError, UnicodeError):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Invalid JSON payload"}}
    try:
        if (not isinstance(payload, dict) or set(payload) - {"jsonrpc", "id", "method", "params"}
                or payload.get("jsonrpc") != "2.0" or type(payload.get("id")) not in {str, int}
                or isinstance(payload.get("id"), str) and len(payload["id"]) > 128
                or not isinstance(payload.get("method"), str)):
            raise A2ARejected(-32600, "Invalid request envelope")
        correlation = payload["id"]
        if request.headers.get("A2A-Version") != "1.0":
            raise A2ARejected(-32009, "A2A version1.0 required")
        if request.headers.get("A2A-Extensions"):
            raise A2ARejected(-32602, "Extensions are unsupported")
        params = payload.get("params", {})
        owner = request.app.state.a2a
        method = payload["method"]
        if method == "GetExtendedAgentCard":
            exact(params, set())
            result = agent_card(request)
        elif method == "SendMessage":
            from functools import partial
            result = await owner.send(principal, params, partial(admit_mailbox, request))
        elif method == "ListTasks":
            result = owner.list_tasks(principal, params)
        elif method in {"GetTask", "CancelTask"}:
            exact(params, {"id", "historyLength"} if method == "GetTask" else {"id"})
            if not zero_history(params.get("historyLength", 0)):
                raise A2ARejected(-32602, "Task message history is unsupported")
            row = owner.row(principal, params.get("id"))
            if method == "CancelTask":
                raise A2ARejected(-32002, "Accepted mailbox delivery cannot be revoked")
            result = owner.task(principal, row)
        elif method in {"SendStreamingMessage", "SubscribeToTask"}:
            raise A2ARejected(-32004, "Streaming and task subscriptions are unsupported")
        elif method in {"CreateTaskPushNotificationConfig", "GetTaskPushNotificationConfig", "ListTaskPushNotificationConfigs", "DeleteTaskPushNotificationConfig"}:
            raise A2ARejected(-32003, "Push notification configuration is unsupported")
        else:
            raise A2ARejected(-32601, "Method is unavailable")
        return {"jsonrpc": "2.0", "id": correlation, "result": result}
    except (sqlite3.Error, OSError):
        raise HTTPException(503, "Mailbox admission could not be confirmed; inspect the original task before retrying") from None
    except (ValidationError, A2ARejected) as exc:
        return {"jsonrpc": "2.0", "id": correlation, "error": {
            "code": exc.code if isinstance(exc, A2ARejected) else -32602,
            "message": exc.message if isinstance(exc, A2ARejected) else "Invalid mailbox message"}}
