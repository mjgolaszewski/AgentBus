"""Route verified human Slack replies only from durable, proved ancestry."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import ValidationError

if TYPE_CHECKING:
    from src.agentbus_service import Message, MessageStore, SendMessage


def append_slack(store: MessageStore, channel: str, slack_ts: str, message: SendMessage) -> Message:
    """Keep route derivation and append atomic with other local message writes."""
    with store._lock:
        return store.append(channel, slack_ts, route_human_reply(store, channel, message))


def _has_other_agent(store: MessageStore, channel: str, root: Message) -> bool:
    from src.agentbus_service import SendMessage

    rows = store._db.execute(
        "SELECT payload, sender_assurance FROM messages WHERE channel = ? AND thread_ts = ?",
        (channel, root.slack_ts),
    )
    for row in rows:
        try:
            sender = SendMessage.model_validate_json(row["payload"]).sender
        except ValidationError:
            return True
        if sender.startswith("slack:"):
            continue
        if row["sender_assurance"] != "session" or not store.participation.same_chat(root.sender, sender):
            return True
    return False


def route_human_reply(store: MessageStore, channel: str, message: SendMessage) -> SendMessage:
    """Leave uncertain ancestry untouched; never infer a route from Slack text."""
    from src.agentbus_service import SendMessage

    if (message._sender_assurance != "slack-human" or message.audience != "unrouted"
            or not message.thread_ts):
        return message
    try:
        root = store.message_by_ts(channel, message.thread_ts)
    except ValidationError:
        return message
    if root is None or root.sender_assurance != "session" or root.thread_ts is not None:
        return message
    route = store.participation.current_route_for(root.sender)
    if route is None or _has_other_agent(store, channel, root):
        return message
    try:
        routed = SendMessage(
            sender=message.sender, recipient=route, text=message.text,
            repo=root.repo, kind="request", thread_ts=message.thread_ts,
            audience="direct", reply_to_cursor=root.cursor,
        )
    except ValidationError:
        # Extra route metadata must not cause the original human text to vanish.
        return message
    routed._sender_assurance = message._sender_assurance
    return routed
