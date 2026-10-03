"""Small, human-only Socket Mode command surface over existing AgentBus owners."""

from __future__ import annotations

import re

from src.agentbus_metrics_service import session_metrics
from src.agentbus_send_rate_service import SendRateExceeded

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUFFIX = re.compile(r"[0-9a-f]{6,12}")
HELP = ("/agentbus help | roster | status ID | metrics ID | send ID MESSAGE | wake ID MESSAGE | "
        "checkpoint ID | pause ID | resume ID | retire ID CONFIRM. "
        "ID is a unique last 6–12 hex characters or a full session UUID.")


def resolve_session(store, reference: str) -> str:
    if SESSION_ID.fullmatch(reference):
        store.participation.session_state(reference)
        return reference
    if not SUFFIX.fullmatch(reference):
        raise ValueError("Use at least six hexadecimal suffix characters or a full session UUID.")
    with store._lock:
        rows = store._db.execute(
            "SELECT session_id FROM participation_sessions WHERE session_id LIKE ?",
            (f"%{reference}",),
        ).fetchall()
    if not rows:
        raise KeyError(reference)
    if len(rows) != 1:
        raise ValueError("Session suffix is ambiguous; use more characters or the full ID.")
    return rows[0]["session_id"]


def short_session(session_id: str, all_ids: list[str]) -> str:
    for length in range(6, 13):
        suffix = session_id[-length:]
        if sum(other.endswith(suffix) for other in all_ids) == 1:
            return suffix
    return session_id


def execute_command(payload: dict, settings, store, web_client) -> str:
    """Authorize by Slack's verified Socket Mode user ID and exact session ID."""
    if payload.get("command") != "/agentbus" or payload.get("channel_id") != settings.slack_channel:
        return "AgentBus commands are available only in the configured channel."
    user = payload.get("user_id")
    if not isinstance(user, str) or user not in settings.slack_operator_user_ids:
        return "AgentBus operator command access is not enabled for this Slack user."
    raw = payload.get("text", "")
    if not isinstance(raw, str) or len(raw) > 4096:
        return "Command text is too long."
    parts = raw.strip().split(maxsplit=2)
    if not parts or parts[0] == "help":
        return HELP
    action = parts[0]
    if action == "roster" and len(parts) == 1:
        sessions = store.participation.roster(include_stopped=True)
        if not sessions:
            return "No joined sessions."
        all_ids = [item["session_id"] for item in sessions]
        return "\n".join(
            f"{item['route']} | {short_session(item['session_id'], all_ids)} | {item['state']} | "
            f"work {'paused' if item['work_paused'] else 'active'}"
            for item in sessions[:50]
        ) + (f"\n… {len(sessions) - 50} more" if len(sessions) > 50 else "")
    if len(parts) < 2:
        return HELP
    try:
        session_id = resolve_session(store, parts[1])
        presence = store.participation.presence(session_id)
    except KeyError:
        return "Unknown session ID. Use /agentbus roster."
    except ValueError as exc:
        return str(exc)
    if action == "status" and len(parts) == 2:
        return (f"{presence['route']} | {session_id}\n"
                f"Participation: {presence['state']}; work: "
                f"{'paused' if presence['work_paused'] else 'active'}; "
                f"last contact: {presence['last_client_contact_at']}; "
                f"pending controls: {len(presence['outstanding_controls'])}. "
                "Host wake availability is local to the adapter.")
    if action == "metrics" and len(parts) == 2:
        metrics = session_metrics(store, settings.slack_channel, session_id)
        return (f"{metrics['route']} | {session_id}\n"
                f"Sent: {metrics['sent_messages']}; received: {metrics['received_messages']}; "
                f"received high-water cursor: {metrics['received_high_water_cursor']}; "
                f"channel high-water cursor: {metrics['channel_high_water_cursor']}.\n"
                f"Controls: {metrics['control_count']} total, "
                f"{metrics['outstanding_control_count']} outstanding; "
                f"work: {'paused' if metrics['work_paused'] else 'active'}; "
                f"last contact: {metrics['last_client_contact_at']}; "
                f"last semantic ack: {metrics['last_semantic_ack_at']}; "
                f"current backoff: {metrics['current_backoff_seconds']}s.\n"
                "Acknowledged inbox cursor and host wake attempts are private client/host state.")
    if action in {"pause", "resume"} and len(parts) == 2:
        paused = action == "pause"
        with store._lock:
            current = store.participation.presence(session_id)
            if current["state"] == "stopped":
                return "Retired sessions cannot change work hold."
            if current["work_paused"] == paused:
                return f"Work is already {'paused' if paused else 'active'} for {current['route']}."
            try:
                control_id, _ = store.controls.issue_auxiliary(
                    kind="pause_work" if paused else "resume_work", routes=None,
                    session_ids=[session_id], reason=f"Slack operator {user} {'paused' if paused else 'resumed'} work",
                    actor=f"slack:{user}")
            except ValueError as exc:
                return str(exc)
        return (f"Work {'paused' if paused else 'resumed'} for {current['route']} ({session_id}); "
                f"control {control_id}. Addressed messages can still wake this conversation.")
    if action == "checkpoint" and len(parts) == 2:
        try:
            control_id, _ = store.controls.issue_auxiliary(
                kind="checkpoint_request", routes=None, session_ids=[session_id],
                reason=f"Slack operator {user} requested a checkpoint", actor=f"slack:{user}")
        except ValueError as exc:
            return str(exc)
        return f"Checkpoint requested for {presence['route']}; control {control_id}."
    if action == "retire":
        if len(parts) != 3 or parts[2] != "CONFIRM":
            return f"Retiring {presence['route']} is irreversible. Run /agentbus retire {session_id} CONFIRM."
        with store._lock:
            pending = store._db.execute(
                "SELECT c.control_id FROM operator_controls AS c "
                "JOIN control_targets AS t USING(control_id) "
                "WHERE t.session_id = ? AND c.kind = 'stop_end_turn' "
                "AND t.state != 'effective' ORDER BY c.issued_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if pending is not None:
                return f"Retirement is already pending for {presence['route']}; control {pending['control_id']}."
            try:
                control_id, _ = store.controls.issue_stop(
                    routes=None, session_ids=[session_id],
                    reason=f"Slack operator {user} retired session", actor=f"slack:{user}")
            except ValueError as exc:
                return str(exc)
        return f"Durable stop issued for {presence['route']}; control {control_id}. Retirement awaits the agent's acknowledgement."
    if action in {"send", "wake"} and len(parts) == 3:
        if presence["state"] == "stopped":
            return "This session is retired."
        from src.agentbus_presentation_service import visible_text
        from src.agentbus_service import SendMessage, encode_envelope
        try:
            message = SendMessage(sender=f"slack:{user}", recipient=presence["route"],
                                  audience="direct", kind="request", text=parts[2])
            message._sender_assurance = "slack-human"
            store.send_rate.reserve(f"slack:{user}")
        except (ValueError, SendRateExceeded) as exc:
            return str(exc)
        display = visible_text(parts[2])
        blocks: list[dict[str, object]] = [
            {"type": "context", "elements": [{"type": "plain_text",
             "text": f"slack:{user} → {presence['route']} · request · direct"}]},
        ]
        blocks.extend({"type": "section", "text": {"type": "plain_text", "text": display[i:i + 3000]}}
                      for i in range(0, len(display), 3000))
        response = web_client.chat_postMessage(
            channel=settings.slack_channel, text=encode_envelope(message),
            blocks=blocks,
            parse="none", unfurl_links=False, unfurl_media=False,
        )
        if not response.get("ok") or response.get("channel") != settings.slack_channel:
            return "Slack delivery is uncertain; inspect the channel before retrying."
        stored = store.append(settings.slack_channel, response["ts"], message)
        return f"Delivered to {presence['route']} as cursor {stored.cursor}."
    return HELP
