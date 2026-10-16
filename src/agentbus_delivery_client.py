"""Negotiate ordinary send custody without changing protocol-2 payloads."""
from __future__ import annotations

import sys
from uuid import UUID, uuid4

from src.agentbus_transport_client import ClientError, profile_session_token


def send_command(ns, values, checked_profile, resolve_identity, api):
    send_profile: dict | None = None
    if ns.command == "reply":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        text = sys.stdin.read() if ns.text == "-" else ns.text
        payload = {
            "sender": identity, "text": text, "repo": profile["repo"], "kind": "reply",
            "reply_to_cursor": ns.to_cursor,
        }
        send_profile = profile
    elif ns.command == "send":
        if ns.reply_to_cursor is not None:
            raise ClientError("Use `agentbus reply --to-cursor CURSOR` so the service derives reply routing.")
        if bool(ns.identity) == bool(ns.sender):
            raise ClientError("Use exactly one of --identity or --sender.")
        sender = ns.sender
        if ns.identity:
            sender = resolve_identity(ns.identity)
            send_profile, _ = checked_profile(values, sender)
        if ns.broadcast and ns.recipient:
            raise ClientError("Use either --to/--recipient or --broadcast.")
        if ns.broadcast and ns.informational:
            raise ClientError("Use either --broadcast or --informational.")
        if not ns.recipient and not ns.broadcast:
            raise ClientError("Every send requires --to RECIPIENT or --broadcast; message kind does not select a destination.")
        if ns.informational and not ns.recipient:
            raise ClientError("Informational messages require --to RECIPIENT.")
        recipient = ns.recipient or "all"
        audience = "broadcast" if ns.broadcast else "informational" if ns.informational else \
                   "direct" if ns.recipient else None
        payload = {"sender": sender, "recipient": recipient, "text": ns.text, "kind": ns.kind}
        if audience:
            payload["audience"] = audience
        if ns.repo:
            payload["repo"] = ns.repo
        elif send_profile is not None:
            payload["repo"] = send_profile["repo"]
        if ns.correlation_id:
            payload["correlation_id"] = ns.correlation_id
        if ns.thread_ts:
            payload["thread_ts"] = ns.thread_ts
        if payload["text"] == "-":
            payload["text"] = sys.stdin.read()

    session = profile_session_token(send_profile or {})
    explicit = getattr(ns, 'message_id', None)
    capable = values.get('_AGENTBUS_DELIVERY_VERSION') == '1' and session is not None
    if explicit is not None:
        try:
            if str(UUID(explicit)) != explicit:
                raise ValueError()
        except ValueError:
            raise ClientError('--message-id must be a canonical UUID.') from None
        if not capable:
            raise ClientError('Server must advertise durable delivery and the sender must have a joined session.')
    if not capable:
        return api(values, '/v1/messages', payload, session_token=session)
    key = explicit or str(uuid4())
    try:
        return api(values, '/v1/messages', payload, session_token=session, delivery_key=key)
    except ClientError as exc:
        raise ClientError(f'{exc} Retry the identical request with --message-id {key}; acceptance may be uncertain.') from None
