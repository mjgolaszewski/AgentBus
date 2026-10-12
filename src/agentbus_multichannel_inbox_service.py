"""Indexed global-cursor inbox projection across operator-configured Slack rooms."""

from __future__ import annotations

from src.agentbus_channel_policy_service import route_channel, thread_channel
from src.agentbus_service import InboxMessage, InboxPage


def read_inbox(settings, store, identity: str, after: int, limit: int,
               thread_ts: str | None, context: bool, channel: str | None) -> InboxPage:
    if channel is not None and channel not in settings.allowed_channels:
        raise ValueError("unknown Slack room")
    if channel is None and thread_ts is not None and len(settings.allowed_channels) > 1:
        channel = thread_channel(settings, store, thread_ts)
    if channel is not None or context or len(settings.allowed_channels) == 1:
        return store.inbox(channel or settings.slack_channel, identity, after, limit,
                           thread_ts, context)
    return inbox_across_rooms(store, settings.allowed_channels,
                              route_channel(settings, identity), identity, after, limit)


def inbox_across_rooms(store, channels: frozenset[str], home: str, identity: str,
                       after: int, limit: int) -> InboxPage:
    routes = store.participation.routes_for(identity)
    room_marks = ",".join("?" for _ in channels)
    route_marks = ",".join("?" for _ in routes)
    rooms = tuple(sorted(channels))
    # Each arm has a selective channel/audience/route or claim index. Cursor
    # is global, so limiting each arm to one page cannot hide an earlier item.
    arms = [
        (f"SELECT cursor FROM messages WHERE channel IN ({room_marks}) "
         f"AND audience = 'direct' AND recipient IN ({route_marks}) AND cursor > ? "
         "ORDER BY cursor LIMIT ?", (*rooms, *routes, after, limit + 1)),
        (f"SELECT cursor FROM messages WHERE channel IN ({room_marks}) "
         f"AND audience = 'informational' AND recipient IN ({route_marks}) AND cursor > ? "
         "ORDER BY cursor LIMIT ?", (*rooms, *routes, after, limit + 1)),
        ("SELECT cursor FROM messages WHERE channel = ? AND audience = 'broadcast' "
         "AND recipient = 'all' AND cursor > ? ORDER BY cursor LIMIT ?",
         (home, after, limit + 1)),
        (f"SELECT cursor FROM claims WHERE channel IN ({room_marks}) "
         f"AND identity IN ({route_marks}) AND cursor > ? ORDER BY cursor LIMIT ?",
         (*rooms, *routes, after, limit + 1)),
    ]
    source = " UNION ".join(f"SELECT cursor FROM ({sql})" for sql, _ in arms)
    params = tuple(value for _, values in arms for value in values)
    with store._lock:
        high = store._db.execute(
            f"SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel IN ({room_marks})",
            rooms,
        ).fetchone()[0]
        rows = store._db.execute(
            f"SELECT m.*, c.identity AS claim_identity FROM ({source}) AS candidate "
            "JOIN messages AS m ON m.cursor = candidate.cursor "
            "LEFT JOIN claims AS c ON c.channel = m.channel AND c.cursor = m.cursor "
            "ORDER BY m.cursor LIMIT ?",
            (*params, limit + 1),
        ).fetchall()
    visible = []
    for row in rows[:limit]:
        message = store._message(row)
        addressed = message.audience in {"direct", "informational"} and message.recipient in routes
        reason = ("addressed to this identity" if addressed else
                  "explicit broadcast" if message.audience == "broadcast" else
                  "claimed by this identity")
        visible.append(InboxMessage(**message.model_dump(), actionable=True, action_reason=reason))
    has_more = len(rows) > limit
    next_cursor = visible[-1].cursor if has_more else max(after, high)
    return InboxPage(messages=visible, next_cursor=next_cursor, has_more=has_more)
