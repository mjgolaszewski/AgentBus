"""Read-only service health and authenticated transport status."""

from __future__ import annotations

import hmac

from fastapi import Request


def api_health(request: Request) -> dict:
    return {"status": "ok"}


def _operator(request):
    token=request.headers.get('Authorization','')
    scheme,_,value=token.partition(' ')
    expected=request.app.state.settings.operator_token
    return bool(expected and scheme.lower()=='bearer' and hmac.compare_digest(value.encode(),expected.encode()))


def authenticate_status(request:Request):
    if not _operator(request):
        from src.agentbus_service import authenticate
        authenticate(request,request.headers.get('Authorization'))


def api_status(request: Request) -> dict:
    result={"status":"ok","slack_connected":bool(request.app.state.socket is not None and request.app.state.socket.is_connected())}
    if _operator(request):
        result['database_bytes']=request.app.state.settings.database_bytes()
    return result
