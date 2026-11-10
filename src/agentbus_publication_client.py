"""Explicit client publications over the existing session-scoped API."""
from __future__ import annotations

import json
import re
import sys
from uuid import UUID, uuid4

from src.agentbus_lifecycle_client import request_stage
from src.agentbus_scope_client import scoped_api
from src.agentbus_transport_client import ClientError


def canonical_request(value):
    try:
        if str(UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ClientError("Publication identity must be a canonical UUID") from None
    return value


def cli_update(ns, project, values):
    from src.agentbus_client import checked_profile

    text = sys.stdin.read(6001) if ns.text == "-" else ns.text
    if not isinstance(text, str) or not text.strip() or len(text) > 6000:
        raise ClientError("Publication requires 1–6000 text characters")
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", ns.destination):
        raise ClientError("Use an operator-configured destination name")
    from src.agentbus_client import resolve_identity
    profile, info = checked_profile(values, resolve_identity(ns.identity))
    capability = info.get("publication")
    if ("client-publication-v1" not in info.get("features", []) or not isinstance(capability, dict)
            or capability.get("version") != 1):
        raise ClientError("Client publication is disabled or unsupported; no update sent")
    key = canonical_request(ns.message_id) if ns.message_id is not None else str(uuid4())
    payload = {"destination": ns.destination, "text": text}
    epoch = request_stage(values, profile, key, payload, capability.get("request_epoch"),
                          existing=ns.message_id is not None, purpose="publication")
    try:
        result = scoped_api(values, "/v1/publications", profile, payload,
                            publication_key=key, publication_epoch=epoch)
    except ClientError as exc:
        raise ClientError(f"Publication request {key} was not confirmed. Inspect with agentbus update-status "
                          f"--identity {profile['identity']} --message-id {key}; preserve its original stage. {exc}") from None
    print(json.dumps(result, indent=2))
    return 0


def cli_update_status(ns, project, values):
    from src.agentbus_client import control_profile, resolve_identity

    key = canonical_request(ns.message_id) if ns.message_id is not None else None
    profile, _ = control_profile(values, resolve_identity(ns.identity))
    path = "/v1/publications" + ("/" + key if key else "")
    print(json.dumps(scoped_api(values, path, profile), indent=2))
    return 0


def cli_update_cancel(ns, project, values):
    from src.agentbus_client import checked_profile, resolve_identity

    key = canonical_request(ns.message_id)
    profile, _ = checked_profile(values, resolve_identity(ns.identity))
    result = scoped_api(values, "/v1/publications/" + key + "/cancel", profile, {})
    print(json.dumps(result, indent=2))
    return 0
