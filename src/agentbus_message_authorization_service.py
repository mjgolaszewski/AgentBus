"""Map message-route proof failures to stable HTTP responses."""

from __future__ import annotations

from fastapi import HTTPException

from src.agentbus_participation_store_service import StoppedMessageSession


def require_message_route(store, route: str, session_token: str | None) -> None:
    try:
        store.participation.message_principal(route, session_token)
    except StoppedMessageSession as exc:
        raise HTTPException(423, str(exc)) from None
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from None
