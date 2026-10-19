"""Small, human-only Socket Mode command surface over existing AgentBus owners."""

from __future__ import annotations

import re

from src.agentbus_metrics_service import session_metrics
from src.agentbus_send_rate_service import SendRateExceeded

SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SUFFIX = re.compile(r"[0-9a-f]{6,12}")
HELP = "\n".join([
    "*🧭 AgentBus operator help*",
    "*Inspect*\n`/agentbus health` — private dashboard\n`/agentbus roster` — this channel's agents",
    "`/agentbus workers` — observed service workers\n`/agentbus doctor` — read-only diagnostic guidance\n"
    "`/agentbus pending [ID]` — bounded channel work and controls\n`/agentbus explain CURSOR` — retained delivery/outcome evidence\n"
    "`/agentbus status ID`\n`/agentbus metrics ID`",
    "*Enroll*\n`/agentbus enrollments` — pending requests\n`/agentbus approve REQUEST_UUID` — approve in this channel",
    "*Message*\n`/agentbus send ID MESSAGE`\n`/agentbus wake ID MESSAGE` — request attention; host admission is separate",
    "*Ask everyone in this channel*\n`/agentbus broadcast TEXT` — read acknowledgement required\n"
    "`/agentbus poll QUESTION` — answer required\n`/agentbus results CONTROL_UUID` — receipts and answers",
    "*Control*\n`/agentbus checkpoint ID`\n`/agentbus pause ID` · `/agentbus resume ID`\n"
    "`/agentbus retire ID CONFIRM` — irreversible stop",
    "*Targets:* ID is a unique current identity fragment, last 6–12 hex characters, or full session UUID. "
    "Ambiguous matches require the full UUID. Commands cannot target another channel.",
    "`/agentbus help` — show this guide\n",
    "*Lifecycle*\n`/agentbus lifecycle` — this channel's recipient outcomes\n"
    "`/agentbus lifecycle settle` — preview due transitions\n"
    "`/agentbus lifecycle settle CONFIRM` — apply with a durable receipt",
    "`/agentbus lifecycle archive` · `/agentbus lifecycle purge` — preview eligible history\n"
    "Append `CONFIRM` to apply a bounded batch. Purge permanently retires eligible history.",
    "*Storage*\n`/agentbus storage` — service-wide footprint\n"
    "`/agentbus storage checkpoint CONFIRM` — nonblocking service-wide WAL maintenance; no custody deletion",
])
HELP = HELP.replace('\n*', '\n\n*').replace(' · ', '\n')


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
    if action in {'workers', 'doctor', 'pending', 'explain'}:
        from src.agentbus_operator_presentation_service import explain_text, pending_text, workers_text
        from src.agentbus_operator_query_service import doctor_checks, explain_snapshot, pending_snapshot
        if action in {'workers', 'doctor'}:
            if len(parts) != 1:
                return f'Use `/agentbus {action}` without a target.'
            from src.agentbus_status_api_service import service_dashboard
            snapshot = dashboard() if dashboard is not None else service_dashboard(settings, store)
            if action == 'workers':
                return workers_text(snapshot)
            return '*Doctor — read-only checks*\n\n' + '\n\n'.join(
                f'*{name}: {state}*\n{guidance}' for name, state, guidance in doctor_checks(snapshot))
        if action == 'explain':
            if len(parts) != 2 or not re.fullmatch(r'[1-9][0-9]{0,17}', parts[1]):
                return 'Use `/agentbus explain CURSOR` with a positive channel-local cursor.'
            return explain_text(explain_snapshot(store, channel, int(parts[1])))
        if len(parts) > 2:
            return 'Use `/agentbus pending` or `/agentbus pending ID`.'
        try:
            target = resolve_session(store, parts[1], channel) if len(parts) == 2 else None
        except KeyError:
            return 'Unknown session or identity. Use /agentbus roster.'
        except ValueError as exc:
            return str(exc)
        return pending_text(pending_snapshot(store, channel, target))
    if action == 'storage':
        if len(parts) == 3 and parts[1:] == ['checkpoint','CONFIRM']:
            import sqlite3
            try:
                result = store.retention.checkpoint()
            except (sqlite3.Error, OSError, ValueError):
                return 'WAL maintenance unavailable or deferred; custody is retained. Inspect operator diagnostics before retrying.'
            return (f"*🧹 Service-wide WAL maintenance:* {result['status']}\n"
                    f"Physical allocation `{result['physical_bytes']}` bytes · reusable pages `{result['reusable_bytes']}` bytes\n"
                    'No message, receipt, retry identity or credential rows were deleted. This is not an ACK or a vacuum.')
        if len(parts) != 1 and parts[1:] != ['checkpoint']:
            return 'Use `/agentbus storage` to inspect; `/agentbus storage checkpoint CONFIRM` applies service-wide WAL maintenance.'
        storage = store.retention.storage()
        return (f"*💾 Service-wide storage*\nPhysical allocation `{storage['physical_bytes']}` bytes\n"
                f"Reusable database pages `{storage['reusable_bytes']}` bytes\n"
                f"Archive-only maintenance headroom `{storage['archive_maintenance_bytes']}` bytes; accounted archive limit `{storage['archive_limit_bytes']}` bytes\n"
                'Checkpoint maintenance is service-wide, nonblocking and preserves custody. '
                'Use `/agentbus storage checkpoint CONFIRM` to apply; no generic cache flush is available.')
    if action == "lifecycle":
        from src.agentbus_message_lifecycle_service import LifecycleRejected
        if len(parts) == 1:
            status = store.lifecycle.status(channel)
            if not status['enabled']:
                return 'Recipient lifecycle is not enabled; upgrade and offline migration are required.'
            states = status['recipients']
            return '*📬 Recipient lifecycle*\n' + '\n'.join(
                f"{state}: `{states.get(state, 0)}`" for state in
                ('unclaimed', 'picked_up', 'handled', 'expired_unclaimed', 'recovery_required'))
        if parts[1] not in {'settle','archive','purge'} or (len(parts) == 3 and parts[2] != 'CONFIRM'):
            return 'Use `/agentbus lifecycle settle|archive|purge` to preview; append `CONFIRM` to apply.'
        try:
            operation = store.lifecycle.reconcile if parts[1] == 'settle' else getattr(store.retention, parts[1])
            result = operation(channel, actor=f'slack:{user}',
                request_key=payload.get('trigger_id'), apply=len(parts) == 3)
        except LifecycleRejected as exc:
            return str(exc)
        if parts[1] != 'settle':
            return (f"*{'Applied' if result['applied'] else 'Preview'} · {parts[1]} · this channel only*\n"
                    f"Eligible payloads: `{result['eligible']}` · batch limit `{result['batch_limit']}`\n"
                    'Open, uncertain, legacy-unknown and referenced custody stays pinned. No acknowledgement is inferred.')
        return (f"*{'Applied' if result['applied'] else 'Preview'} · this channel only*\n"
                f"Unclaimed → expired: `{result['expired_unclaimed']}`\n"
                f"Picked up → recovery required: `{result['recovery_required']}`\n"
                f"Broadcast / poll unread → expired: `{result['expired_unread']}`\n"
                'No messages are resent, deleted or acknowledged.')
    if action == "mah" and len(parts) == 1:
        return ("*🌿 MAH!*\n"
                "🚌 Next stop: the Department of Extremely Important Doorbells.\n"
                "Your imaginary ticket has been punched. Your actual queues have not.\n"
                "Please keep all hands, feet, and writer locks inside the bus.")
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
            f"{'approved in ' + row['approved_channel'] if row['approved_channel'] else 'awaiting approval'}\n"
            f"`/agentbus approve {row['request_id']}`"
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
            lines.append(f"{row['session_id']} | {row.get('lifecycle_state', row['state'])}" +
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
        return f"*👥 Channel roster*\n`{len(sessions)}` sessions\n\n" + "\n\n".join(
            f"*{item['route']}*\nSession `{short_session(item['session_id'], all_ids)}`\n"
            f"Participation: `{item['state']}`; work {'paused' if item['work_paused'] else 'active'}"
            for item in sessions[:50]
        ) + (f"\n… `{len(sessions) - 50}` more" if len(sessions) > 50 else "") + "\nHost wake availability is separate from participation."
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
            store.retention.reserve_storage(len(message.model_dump_json().encode('utf-8')))
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
