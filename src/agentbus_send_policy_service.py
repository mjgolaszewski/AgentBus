"""Authorize and validate new messages before posting to Slack."""

from __future__ import annotations

from src.agentbus_message_assurance_service import valid_new_message_routes
from src.agentbus_participation_store_service import StoppedMessageSession
from src.agentbus_reply_policy_service import derive_reply_fields


class SessionAuthorityError(PermissionError):
    pass


def validate_new_send(store, channel: str, message, session_token: str | None) -> str:
    if message.audience == "unrouted":
        raise ValueError("unrouted audience is reserved for Slack ingestion")
    authored_fields = message._authored_fields
    derive_reply_fields(store, channel, message)
    human_reply = False
    if message.recipient.startswith("slack:") and message.reply_to_cursor is not None:
        parent = store.message(channel, message.reply_to_cursor)
        human_reply = (parent is not None and parent.sender == message.recipient
                       and parent.sender_assurance == "slack-human")
    if not (valid_new_message_routes(message.sender, message.recipient)
            or (human_reply and valid_new_message_routes(message.sender, "all"))):
        raise ValueError("new messages require canonical agent routes")
    try:
        assurance, rate_key = store.participation.message_principal(message.sender, session_token)
    except StoppedMessageSession:
        raise
    except PermissionError as exc:
        raise SessionAuthorityError(str(exc)) from None
    if assurance == "session":
        if message.reply_to_cursor is None and message.recipient == "all" and not (
            "audience" in authored_fields and message.audience == "broadcast"
        ):
            raise ValueError("joined sends require an exact recipient or explicit broadcast")
        message._sender_assurance = "session"
    store.validate_reply(channel, message)
    return rate_key
