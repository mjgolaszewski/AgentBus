"""Deterministic model-visible projection over complete AgentBus poll events."""

from __future__ import annotations

import json


def idle_cadence(values: dict) -> str:
    """Describe the client's declared ladder using its effective base and cap."""
    initial, maximum = values['initial_interval_seconds'], values['max_interval_seconds']
    steps = list(dict.fromkeys(min(maximum, initial * n) for n in (1, 2, 4, 8, 12, 16, 24, 32)))
    if steps[-1] < maximum:
        steps.append(maximum)
    minutes = all(step % 60 == 0 for step in steps)
    unit, divisor = ('min', 60) if minutes else ('s', 1)
    return 'idle checks ' + ', '.join(f'{step / divisor:g}' for step in steps) + ' ' + unit


def render_event(event: dict, *, json_mode: bool, budget_bytes: int | None) -> str:
    """Compact replaceable text without clipping controls or claimed work."""
    if json_mode:
        return json.dumps(event, ensure_ascii=True)
    kind = event["kind"]
    if kind == "CONTROL":
        control = event["control"]
        instruction = {
            "broadcast": " Read this announcement, then explicitly acknowledge with agentbus ack-control ID.",
            "poll": " Read this question and answer with agentbus ack-control ID --answer TEXT. Delivery does not acknowledge it.",
            "pause_work": " Hold substantive work; remain reachable for messages and controls.",
            "resume_work": " Work hold lifted; resume the prior task after acknowledging this control.",
        }.get(control["kind"], "")
        return (f"CONTROL {control['control_id']} {control['kind']}: "
                f"{json.dumps(control['reason'], ensure_ascii=True)}{instruction}")
    if kind == "POLICY_CHANGED":
        values = event["values"]
        return (f"POLICY_CHANGED {event['revision']} ACK_REQUIRED "
                f"{idle_cadence(values)} via agentbus poll; empty checks are silent")
    if kind == "ATTENTION_REQUIRED":
        return f"ATTENTION_REQUIRED {event['reason']}"
    if kind != "MESSAGE":
        raise ValueError(f"unknown poll event kind {kind}")
    message = event["message"]
    lifecycle = message.get('lifecycle')
    state = lifecycle.get('state') if isinstance(lifecycle, dict) else None
    status = f" [{state}]" if isinstance(state, str) else ""
    if isinstance(lifecycle, dict) and lifecycle.get('lease_elapsed') is True:
        status += " [lease elapsed; inspect message-status]"
    elif isinstance(lifecycle, dict) and lifecycle.get('deadline_elapsed') is True:
        status += " [pickup deadline elapsed]"
    rendered = (f"MESSAGE {message['cursor']} {message['sender']} -> {message['recipient']} "
                f"({message.get('kind', 'message')}){status}: "
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
