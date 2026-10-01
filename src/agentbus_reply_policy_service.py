"""Validate reply and Slack thread ancestry before any external post."""

from __future__ import annotations


def validate_reply(store, channel: str, message) -> None:
    if message.thread_ts is not None and store.message_by_ts(channel, message.thread_ts) is None:
        raise ValueError("thread parent is unknown to this inbox")
    if message.reply_to_cursor is None:
        return
    parent = store.message(channel, message.reply_to_cursor)
    if parent is None:
        raise KeyError(message.reply_to_cursor)
    if message.thread_ts is not None and message.thread_ts not in {parent.slack_ts, parent.thread_ts}:
        raise ValueError("reply cursor belongs to a different Slack thread")
    same_chat = store.participation.same_chat
    allowed = same_chat(parent.sender, message.sender) or parent.audience == "broadcast" or \
              (parent.audience == "direct" and same_chat(parent.recipient, message.sender)) or \
              (parent.audience == "unrouted" and
               (claimant := store.claimant(channel, parent.cursor)) is not None and
               same_chat(claimant, message.sender))
    if not allowed:
        raise PermissionError("sender is not an intended responder for the parent message")
    if not same_chat(message.recipient, parent.sender) or message.audience != "direct":
        raise ValueError("replies must be direct messages to the parent sender")
