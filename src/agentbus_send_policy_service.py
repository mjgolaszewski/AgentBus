"""Authorize and validate new messages before posting to Slack."""

from __future__ import annotations

from src.agentbus_message_assurance_service import valid_new_message_routes


class SessionAuthorityError(PermissionError):
    pass


def validate_new_send(store, channel: str, message, session_token: str | None) -> None:
    if message.audience == "unrouted":
        raise ValueError("unrouted audience is reserved for Slack ingestion")
    if not valid_new_message_routes(message.sender, message.recipient):
        raise ValueError("new messages require canonical agent routes")
    if session_token:
        try:
            store.participation.session_for_secret(session_token, message.sender)
        except PermissionError as exc:
            raise SessionAuthorityError(str(exc)) from None
        message._sender_assurance = "session"
    store.validate_reply(channel, message)
