"""Derive Slack room placement from service configuration and durable ancestry."""

from __future__ import annotations

from src.agentbus_message_assurance_service import route_parts


def route_channel(settings, route: str) -> str:
    parts = route_parts(route)
    repo = parts[1] if parts else None
    return settings.slack_repo_channels.get(repo, settings.slack_channel)


def send_channel(settings, store, message) -> str:
    if message.reply_to_cursor is not None:
        parent = store.message_any(message.reply_to_cursor)
        if parent is None or parent.channel not in settings.allowed_channels:
            raise KeyError(message.reply_to_cursor)
        return parent.channel
    route = message.recipient if message.recipient != "all" else message.sender
    return route_channel(settings, route)


def cursor_channel(settings, store, cursor: int) -> str:
    parent = store.message_any(cursor)
    if parent is None or parent.channel not in settings.allowed_channels:
        raise KeyError(cursor)
    return parent.channel


def thread_channel(settings, store, thread_ts: str) -> str:
    channels = store.channels_for_ts(thread_ts, settings.allowed_channels)
    if len(channels) != 1:
        raise ValueError("Slack thread parent is unknown or ambiguous across configured rooms")
    return channels[0]
