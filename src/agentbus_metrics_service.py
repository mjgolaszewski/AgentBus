"""Read-only service metrics for one immutable participation session."""

from __future__ import annotations


def session_metrics(store, channel: str, session_id: str) -> dict:
    presence = store.participation.presence(session_id)
    with store._lock:
        routes = [presence["route"]]
        routes.extend(row["route"] for row in store._db.execute(
            "SELECT route FROM routing_aliases WHERE chat_id = ?", (presence["chat_id"],)
        ))
        placeholders = ",".join("?" for _ in routes)
        received = store._db.execute(
            f"SELECT COUNT(*) AS count, MAX(cursor) AS high_water FROM messages "
            f"WHERE channel = ? AND recipient IN ({placeholders})",
            (channel, *routes),
        ).fetchone()
        sent = store._db.execute(
            f"SELECT COUNT(*) AS count, MAX(cursor) AS high_water FROM messages "
            f"WHERE channel = ? AND json_extract(payload, '$.sender') IN ({placeholders})",
            (channel, *routes),
        ).fetchone()
        latest = store._db.execute(
            "SELECT COALESCE(MAX(cursor), 0) FROM messages WHERE channel = ?", (channel,)
        ).fetchone()[0]
        control_count = store._db.execute(
            "SELECT COUNT(*) FROM control_targets WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return {
        "session_id": session_id, "route": presence["route"],
        "received_messages": received["count"], "sent_messages": sent["count"],
        "received_high_water_cursor": received["high_water"],
        "sent_high_water_cursor": sent["high_water"],
        "channel_high_water_cursor": latest, "control_count": control_count,
        "outstanding_control_count": len(presence["outstanding_controls"]),
        "last_client_contact_at": presence["last_client_contact_at"],
        "last_semantic_ack_at": presence["last_semantic_ack_at"],
        "current_backoff_seconds": presence["current_backoff_seconds"],
        "work_paused": presence["work_paused"],
        "acknowledged_cursor": None,
        "host_wake_attempts": None,
    }
