"""Authorize and validate new messages before posting to Slack."""

from __future__ import annotations

from src.agentbus_message_assurance_service import valid_new_message_routes
from src.agentbus_participation_store_service import StoppedMessageSession


class SessionAuthorityError(PermissionError):
    pass


def validate_new_send(store, channel: str, message, session_token: str | None) -> str:
    if message.audience == "unrouted":
        raise ValueError("unrouted audience is reserved for Slack ingestion")
    if not valid_new_message_routes(message.sender, message.recipient):
        raise ValueError("new messages require canonical agent routes")
    try:
        assurance, rate_key = store.participation.message_principal(message.sender, session_token)
    except StoppedMessageSession:
        raise
    except PermissionError as exc:
        raise SessionAuthorityError(str(exc)) from None
    if assurance == "session":
        message._sender_assurance = "session"
    store.validate_reply(channel, message)
    return rate_key
