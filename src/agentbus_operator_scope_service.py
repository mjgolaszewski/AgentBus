"""Explicit scope parsing and private result rendering for operator federation."""

from __future__ import annotations

import json
import re

from src.agentbus_presentation_service import section_blocks
from src.agentbus_slack_ui_service import OperatorAction, OperatorReply, action_button


def parse_scope(raw):
    pieces = raw.strip().split(maxsplit=1)
    action = pieces[0] if pieces else ""
    rest = pieces[1].strip() if len(pieces) > 1 else ""
    if action not in {"health", "broadcast"} or not rest.startswith("--"):
        return None
    if rest == "--" or rest.startswith("-- "):
        text = rest[2:].lstrip()
        if action == "health" or not text:
            raise ValueError("This command requires a valid scope or message")
        return "local", None, text
    parts = rest.split(maxsplit=1)
    flag = parts[0]
    tail = parts[1] if len(parts) > 1 else ""
    if flag == "--location":
        target = tail.split(maxsplit=1)
        if not target or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", target[0]):
            raise ValueError("Use --location with one approved location")
        location = target[0]
        text = target[1] if len(target) > 1 else ""
    elif flag == "--federated":
        location, text = None, tail
    else:
        raise ValueError("Unsupported scope option; use -- before literal flag-like text")
    if text.startswith("-- "):
        text = text[3:].lstrip()
    elif text.startswith("--"):
        raise ValueError("Scope selectors conflict or an option is unknown")
    if (action == "health" and text) or (action == "broadcast" and not text):
        raise ValueError("Health accepts no text; broadcast requires text")
    return action, location, text


def operator_results(owner, request_id, actor, channel, page=0):
    rows = owner.results(request_id, actor, channel)
    pages = max(1, (len(rows) + 3) // 4)
    if not 0 <= page < pages:
        raise ValueError("Choose an existing result page")
    pending=sum(row["state"] in {"pending","unreachable_or_unknown"} for row in rows)
    unsupported=sum(row["state"] in {"unsupported","denied","expired"} for row in rows)
    sections = [f"*Federated operator results* — page `{page + 1}/{pages}`\nLocations `{len(rows)}`; pending/unknown `{pending}`; denied/expired/unsupported `{unsupported}`. These are transport and read observations."]
    for row in rows[page * 4 : page * 4 + 4]:
        lines = [f"*{row['location']}*: `{row['state']}`"]
        if "sampled_at" in row:
            age = max(0, int(owner.bridge.now() - row["sampled_at"]))
            lines.append(f"Sample age `{age}s`" + (" — stale" if age > 60 else ""))
        if "targets" in row:
            lines.append(f"Read receipts `{row['read_acknowledged']}/{row['targets']}`; execution not inferred.")
        if "health" in row:
            from src.agentbus_presentation_service import slack_escape

            health = row["health"]
            runtime = health.get("runtime") or {}

            def value(item):
                return slack_escape(str(item))[:80]

            lines.append(
                f"Version `{value(health.get('version', 'not observed'))}`; Slack `{value(health.get('slack_connected', 'not observed'))}`"
            )
            lines.append(
                f"Python `{value(runtime.get('python', 'not observed'))}`; uptime `{value(runtime.get('uptime_seconds', 'not observed'))}`"
            )
            channels = health.get("channels") or []
            lines.append(
                "Authorized channels: "
                + (", ".join(value(c.get("channel", "unknown")) for c in channels[:4]) or "not observed")
            )
            federation = health.get("federation") or {}
            lines.append(
                f"Federation initialized `{value(federation.get('initialized', 'not observed'))}`; monitoring not inferred."
            )
        sections.append("\n".join(lines))
    text = "\n\n".join(sections)
    buttons = []
    for label, index in [("Previous", page - 1), ("Refresh", page), ("Next", page + 1)]:
        if 0 <= index < pages:
            buttons.append(action_button(label, OperatorAction("operator_results", channel, request_id, index)))
    blocks = section_blocks(text)
    blocks.append({"type": "actions", "elements": buttons})
    return OperatorReply(text, blocks, outcome="accepted", receipt=request_id)


def scoped_command(parsed, payload, settings, store):
    operation, location, text = parsed
    owner = getattr(store, "operator_federation", None)
    if owner is None:
        return "Operator federation is not configured; no local fallback or remote action occurred."
    if location is not None:
        matches = [key for key, binding in owner.bridge.bindings.items() if location in {key, binding.route_prefix}]
        if location in {owner.bridge.identity.instance_id, owner.bridge.identity.route_prefix}:
            matches.append(owner.bridge.identity.instance_id)
        if len(set(matches)) != 1:
            return "Unknown or ambiguous approved location."
        location = matches[0]
    try:
        request_id = owner.start(
            actor=payload["user_id"],
            channel=payload["channel_id"],
            operation=operation,
            location=location,
            text=text,
            request_key=payload.get("trigger_id"),
        )
        return operator_results(owner, request_id, payload["user_id"], payload["channel_id"])
    except ValueError as exc:
        return str(exc)


def recent_operator_buttons(owner, actor, channel):
    with owner.store._lock:
        rows = owner.store._db.execute(
            "SELECT request_id,spec FROM operator_fanout WHERE actor=? AND channel=? ORDER BY created DESC LIMIT 4",
            (actor, channel),
        ).fetchall()
    blocks = []
    for row in rows:
        kind = json.loads(row["spec"])["operation"]
        blocks.extend(section_blocks("Federated " + kind))
        blocks.append(
            {
                "type": "actions",
                "elements": [action_button("Results", OperatorAction("operator_results", channel, row["request_id"]))],
            }
        )
    return blocks
