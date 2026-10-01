"""Operator CLI adapters for revisioned policy, stop control, and presence."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlencode

from src.agentbus_transport_client import ClientError, api


def _operator_token(values: dict[str, str]) -> str:
    token = os.environ.get("AGENTBUS_OPERATOR_TOKEN", "")
    if not token or token == values.get("AGENTBUS_API_TOKEN"):
        raise ClientError("Export a separate AGENTBUS_OPERATOR_TOKEN for operator commands.")
    return token


def policy_set(ns, values: dict[str, str]) -> int:
    try:
        payload = json.loads(Path(ns.values_file).read_text())
    except (OSError, ValueError):
        raise ClientError("Policy values file must contain valid JSON.") from None
    if not isinstance(payload, dict):
        raise ClientError("Policy values file must contain a JSON object.")
    response = api(values, "/v1/policy/revisions", {
        "scope": ns.scope, "scope_key": ns.scope_key, "values": payload,
    }, bearer_token=_operator_token(values))
    print(f"POLICY REVISION {response['revision_id']}")
    return 0


def policy_show(ns, values: dict[str, str]) -> int:
    query = urlencode({"repo": ns.repo, "chat_id": ns.chat_id})
    response = api(values, f"/v1/policy/effective?{query}")
    print(json.dumps(response, indent=2))
    return 0


def control_stop(ns, values: dict[str, str]) -> int:
    response = api(values, "/v1/controls/stop", {
        "routes": ns.to, "all_current": ns.all, "reason": ns.reason,
    }, bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0


def control_issue(ns, values: dict[str, str]) -> int:
    response = api(values, "/v1/controls", {
        "kind": ns.kind, "routes": ns.to, "all_current": ns.all, "reason": ns.reason,
    }, bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0


def control_status(ns, values: dict[str, str]) -> int:
    response = api(values, f"/v1/controls/{ns.control_id}",
                   bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0


def session_presence(ns, values: dict[str, str]) -> int:
    response = api(values, f"/v1/sessions/{ns.session_id}/presence",
                   bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0


def claim_recovery(ns, values: dict[str, str]) -> int:
    response = api(values, f"/v1/messages/{ns.cursor}/claim-recovery", {
        "new_identity": ns.to, "reason": ns.reason,
    }, bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0
