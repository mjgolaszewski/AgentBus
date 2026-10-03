"""Deterministic model-visible projection over complete AgentBus poll events."""

from __future__ import annotations

import json


def render_event(event: dict, *, json_mode: bool, budget_bytes: int | None) -> str:
    """Compact replaceable text without clipping controls or claimed work."""
    if json_mode:
        return json.dumps(event, ensure_ascii=True)
    kind = event["kind"]
    if kind == "CONTROL":
        control = event["control"]
        instruction = {
            "pause_work": " Hold substantive work; remain reachable for messages and controls.",
            "resume_work": " Work hold lifted; resume the prior task after acknowledging this control.",
        }.get(control["kind"], "")
        return (f"CONTROL {control['control_id']} {control['kind']}: "
                f"{json.dumps(control['reason'], ensure_ascii=True)}{instruction}")
    if kind == "POLICY_CHANGED":
        values = event["values"]
        return (f"POLICY_CHANGED {event['revision']} ACK_REQUIRED "
                f"active-check {values['initial_interval_seconds']}s ×{values['backoff_factor']} "
                f"to {values['max_interval_seconds']}s via agentbus poll --check")
    if kind == "ATTENTION_REQUIRED":
        return f"ATTENTION_REQUIRED {event['reason']}"
    if kind != "MESSAGE":
        raise ValueError(f"unknown poll event kind {kind}")
    message = event["message"]
    rendered = (f"MESSAGE {message['cursor']} {message['sender']} -> {message['recipient']} "
                f"({message.get('kind', 'message')}): "
                f"{json.dumps(message['text'], ensure_ascii=True)}")
    protected = message.get("action_reason") in {"claimed by this identity", "addressed to this identity"}
    if protected or budget_bytes is None:
        return rendered
    if budget_bytes < 1:
        raise ValueError("presentation budget must be positive")
    encoded = rendered.encode("utf-8")
    if len(encoded) <= budget_bytes:
        return rendered
    if budget_bytes == 1:
        return "~"
    return encoded[:budget_bytes - 1].decode("utf-8", errors="ignore") + "~"
