"""Route allowlisted human Slack replies only from durable, proved ancestry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

from src.agentbus_send_rate_service import SendRateExceeded
from src.agentbus_transaction_service import atomic

if TYPE_CHECKING:
    from src.agentbus_service import Message, MessageStore, SendMessage


def append_slack(store: MessageStore, channel: str, slack_ts: str, message: SendMessage,
                 *, operator_user_ids: frozenset[str] = frozenset()) -> Message:
    """Deduplicate, authorize, reserve human quota and append in one transaction."""
    with store._lock, atomic(store._db):
        prior = store.message_by_ts(channel, slack_ts)
        if prior is not None:
            return prior
        routed = route_human_reply(store, channel, message, operator_user_ids=operator_user_ids)
        if routed is not message:
            try:
                store.send_rate._reserve(message.sender)
            except SendRateExceeded:
                routed = message
        return store._append(channel, slack_ts, routed)


def _thread_route(store: MessageStore, channel: str, root: Message) -> str | None:
    from src.agentbus_service import SendMessage

    rows = store._db.execute(
        "SELECT payload, sender_assurance FROM messages WHERE channel = ? AND "
        "(cursor = ? OR thread_ts = ? OR thread_ts IN "
        "(SELECT provider_ref FROM local_delivery_observations WHERE channel = ? AND local_cursor = ?))",
        (channel, root.cursor, root.slack_ts, channel, root.cursor),
    )
    routes = set()
    for row in rows:
        try:
            item = SendMessage.model_validate_json(row['payload'])
        except ValidationError:
            return None
        route = None
        if not item.sender.startswith('slack:'):
            if row['sender_assurance'] != 'session':
                return None
            route = item.sender
        elif row['sender_assurance'] == 'slack-human' and item.audience == 'direct':
            route = item.recipient
        if route is not None:
            current = store.participation.current_route_for(route)
            if current is None or not store.channels.local_target(channel, current):
                return None
            routes.add(current)
    if len(routes) != 1:
        return None
    route = routes.pop()
    owners = [item for item in store.participation.roster(channel=channel) if item['route'] == route]
    return route if len(owners) == 1 else None


def route_human_reply(store: MessageStore, channel: str, message: SendMessage, *,
                      operator_user_ids: frozenset[str] = frozenset()) -> SendMessage:
    """Unknown, mixed, stopped or unauthorized threads remain notification-only."""
    from src.agentbus_service import SendMessage

    if (message._sender_assurance != 'slack-human' or message.audience != 'unrouted'
            or not message.thread_ts or message.sender.removeprefix('slack:') not in operator_user_ids):
        return message
    try:
        root = store.message_by_ts(channel, message.thread_ts)
        if root is None or root.thread_ts is not None:
            return message
        route = _thread_route(store, channel, root)
        if route is None:
            return message
        routed = SendMessage(sender=message.sender, recipient=route, text=message.text,
                             repo=root.repo, kind='request', thread_ts=message.thread_ts,
                             audience='direct', reply_to_cursor=root.cursor)
    except (ValidationError, PermissionError):
        return message
    routed._sender_assurance = message._sender_assurance
    return routed
