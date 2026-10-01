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
    if ns.session_id:
        if ns.repo or ns.chat_id:
            raise ClientError("Choose --session-id or --repo with --chat-id.")
        response = api(values, f"/v1/sessions/{ns.session_id}/policy",
                       bearer_token=_operator_token(values))
    else:
        if not ns.repo or not ns.chat_id:
            raise ClientError("Policy show needs --repo and --chat-id or --session-id.")
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
    payload = {
        "kind": ns.kind, "routes": ns.to, "all_current": ns.all, "reason": ns.reason,
    }
    if ns.kind == "temporary_policy_override":
        if not ns.values_file or ns.duration_seconds is None:
            raise ClientError("Temporary override needs --values-file and --duration-seconds.")
        try:
            overlay = json.loads(Path(ns.values_file).read_text())
        except (OSError, ValueError):
            raise ClientError("Override values file must contain valid JSON.") from None
        if not isinstance(overlay, dict):
            raise ClientError("Override values file must contain a JSON object.")
        payload.update({"values": overlay, "duration_seconds": ns.duration_seconds})
    elif ns.values_file or ns.duration_seconds is not None:
        raise ClientError("Only temporary overrides accept policy values and duration.")
    response = api(values, "/v1/controls", payload, bearer_token=_operator_token(values))
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


def roster(ns, values: dict[str, str]) -> int:
    from src.agentbus_codex_wake_client import local_binding_projection

    query = "?include_stopped=true" if ns.all else ""
    response = api(values, f"/v1/sessions/roster{query}",
                   bearer_token=_operator_token(values))
    sessions = response["sessions"]
    for session in sessions:
        session["local_codex_binding"] = local_binding_projection(
            values, session["chat_id"], session["session_id"])
    if ns.json:
        print(json.dumps(response, indent=2))
    else:
        print("ROUTE\tPRESENCE\tSESSION\tLOCAL_CODEX_THREAD")
        for session in sessions:
            binding = session["local_codex_binding"]
            thread = binding["thread_id"] if binding and binding["session_matches"] else "—"
            print(f"{session['route']}\t{session['state']}\t{session['session_id']}\t{thread}")
    return 0


def claim_recovery(ns, values: dict[str, str]) -> int:
    response = api(values, f"/v1/messages/{ns.cursor}/claim-recovery", {
        "new_identity": ns.to, "reason": ns.reason,
    }, bearer_token=_operator_token(values))
    print(json.dumps(response, indent=2))
    return 0
