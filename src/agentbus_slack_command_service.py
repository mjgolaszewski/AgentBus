"""Small, human-only Socket Mode command surface over existing AgentBus owners."""

from __future__ import annotations

import re

from src.agentbus_metrics_service import session_metrics
from src.agentbus_send_rate_service import SendRateExceeded

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUFFIX = re.compile(r"[0-9a-f]{6,12}")
HELP = ("/agentbus help | health | enrollments | approve REQUEST_UUID | roster | broadcast TEXT | poll QUESTION | results CONTROL_UUID | status ID | metrics ID | send ID MESSAGE | wake ID MESSAGE | "
        "checkpoint ID | pause ID | resume ID | retire ID CONFIRM. "
        "ID is a unique current identity fragment, last 6–12 hex characters, or full session UUID.")


def resolve_session(store, reference: str, channel: str | None = None) -> str:
    """Resolve only the invoking channel's sessions; never guess across identities."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}", reference):
        raise ValueError("Use a current identity fragment, session suffix, or full session UUID.")
    reference = reference.casefold()
    sessions = store.participation.roster(include_stopped=True, channel=channel)
    if SESSION_ID.fullmatch(reference):
        matches = [item for item in sessions if item['session_id'] == reference]
    else:
        exact = [item for item in sessions if item['route'].casefold() == reference]
        matches = exact or [item for item in sessions if reference in item['route'].casefold()
                            or (SUFFIX.fullmatch(reference) and item['session_id'].endswith(reference))]
    if not matches:
        if re.fullmatch(r"[0-9a-f]{1,5}", reference):
            raise ValueError("No identity matches. Use at least six hexadecimal suffix characters or a full session UUID.")
        raise KeyError(reference)
    if len(matches) != 1:
        raise ValueError("Agent identity or session reference is ambiguous; use the full session UUID.")
    return matches[0]['session_id']


def short_session(session_id: str, all_ids: list[str]) -> str:
    for length in range(6, 13):
        suffix = session_id[-length:]
        if sum(other.endswith(suffix) for other in all_ids) == 1:
            return suffix
    return session_id


def execute_command(payload: dict, settings, store, web_client, *, dashboard=None) -> str:
    """Authorize by Slack's verified Socket Mode user ID and exact session ID."""
    channel = payload.get("channel_id")
    if payload.get("command") != "/agentbus" or not isinstance(channel, str) or channel not in settings.channels:
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
    if action == "health" and len(parts) == 1:
        from src.agentbus_presentation_service import health_text
        from src.agentbus_status_api_service import service_dashboard
        return health_text(dashboard() if dashboard is not None else service_dashboard(settings, store))
    if action == "enrollments" and len(parts) == 1:
        rows = store.channels.pending_enrollments(channel=channel)
        if not rows:
            return "No pending enrollment requests."
        return "Showing up to 20 oldest pending requests; approved requests await join.\n" + "\n".join(
            f"{row['request_id']} | {row['route']} | {row['display_name']} | profile {row['chat_id']} | "
            f"{'approved in ' + row['approved_channel'] if row['approved_channel'] else 'awaiting approval'}"
            for row in rows)
    if action == "approve" and len(parts) == 2:
        if not SESSION_ID.fullmatch(parts[1]):
            return "Use approve with the full enrollment request UUID."
        try:
            store.channels.approve_enrollment(parts[1], channel=channel, actor=f"slack:{user}")
        except (PermissionError, ValueError) as exc:
            return str(exc)
        return f"Enrollment request {parts[1]} approved in this channel. The requester can rerun agentbus join; policy acknowledgement is still required."
    if action == "results" and len(parts) == 2:
        if not SESSION_ID.fullmatch(parts[1]):
            return "Use results with the full control UUID."
        rows = store.controls.status(parts[1], channel=channel)
        if not rows:
            return "No announcement results in this channel."
        completed = sum(row["state"] == "effective" for row in rows)
        lines = [f"{parts[1]}: {completed}/{len(rows)} acknowledged."]
        for row in rows[:8]:
            lines.append(f"{row['session_id']} | {row['state']}" +
                         (f" | {row['answer']}" if row['answer'] is not None else ""))
        if len(rows) > 8:
            lines.append(f"{len(rows) - 8} further receipts; use operator control-status for the full report.")
        return "\n".join(lines)
    if action in {"broadcast", "poll"}:
        text = raw.strip().partition(" ")[2].strip()
        request_key = payload.get("trigger_id")
        if not text or len(text) > 1000 or not isinstance(request_key, str) or not request_key:
            return "Use broadcast TEXT or poll QUESTION (1–1000 characters); a verified Slack request identity is required."
        targets = [item["session_id"] for item in store.participation.roster(include_stopped=False, channel=channel)]
        if not targets:
            return "No joined sessions in this channel."
        try:
            control_id, retained = store.controls.issue_auxiliary(
                kind=action, routes=None, session_ids=targets, reason=text,
                actor=f"slack:{user}:{channel}", request_key=request_key)
        except ValueError as exc:
            return str(exc)
        return f"{action.title()} {control_id}: {len(retained)} session(s); explicit {'answer' if action == 'poll' else 'read acknowledgement'} required."
    if action == "roster" and len(parts) == 1:
        sessions = store.participation.roster(include_stopped=True, channel=channel)
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
        session_id = resolve_session(store, parts[1], channel)
        presence = store.participation.presence(session_id)
    except KeyError:
        return "Unknown session or identity. Use /agentbus roster."
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
        metrics = session_metrics(store, channel, session_id)
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
        from src.agentbus_presentation_service import message_blocks
        from src.agentbus_service import SendMessage, encode_envelope
        try:
            message = SendMessage(sender=f"slack:{user}", recipient=presence["route"],
                                  audience="direct", kind="request", text=parts[2])
            message._sender_assurance = "slack-human"
            store.send_rate.reserve(f"slack:{user}")
        except (ValueError, SendRateExceeded) as exc:
            return str(exc)
        blocks = message_blocks(message)
        response = web_client.chat_postMessage(
            channel=channel, text=encode_envelope(message),
            blocks=blocks,
            parse="none", mrkdwn=False, unfurl_links=False, unfurl_media=False,
        )
        if not response.get("ok") or response.get("channel") != channel:
            return "Slack delivery is uncertain; inspect the channel before retrying."
        stored = store.append(channel, response["ts"], message)
        return f"Delivered to {presence['route']} as cursor {store.channels.public_cursor(channel, stored.cursor)}."
    return HELP
