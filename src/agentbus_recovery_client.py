"""Compact private resume observations without acknowledgement or host ownership."""
import hashlib
import json
import math
import os
import time
from urllib.parse import urlencode

from src.agentbus_scope_client import scope, scoped_api, verify_projection
from src.agentbus_transport_client import ClientError


def cli_resume(ns, project, values):
    from src.agentbus_client import atomic_json, consumer_state_dir, control_profile, identity_path, resolve_identity
    from src.agentbus_poll_client import foreground_check_in

    profile, presence = control_profile(values, resolve_identity(ns.identity))
    binding = scope(profile)
    root = consumer_state_dir(values)
    path = root / (".resume-" + hashlib.sha256(binding["chat_id"].encode()).hexdigest() + ".json")
    previous = None
    if path.exists() or path.is_symlink():
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                if os.fstat(source.fileno()).st_mode & 0o077:
                    raise ValueError()
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            retained = json.loads(raw)
            verify_projection(retained, profile)
            previous = retained["observed_at"]
            if isinstance(previous, bool) or not isinstance(previous, (int, float)) or not math.isfinite(previous):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError):
            raise ClientError("Resume checkpoint is invalid; preserve it for recovery") from None
    instant = time.time()
    controls, inbox = None, None
    if presence.get("state") not in {"stopped", "joining"}:
        controls = foreground_check_in(values, identity_path(values, profile["identity"]).resolve(), profile)
        held = (presence.get("state") == "stopping" or presence.get("work_paused") is True
                or controls.get("ack_required") is True
                or any(item.get("kind") in {"stop_end_turn", "pause_work"} for item in controls.get("controls", [])))
        if not held:
            query = urlencode({"identity": profile["identity"], "after": int(profile.get("ack_cursor", 0)), "limit": 20})
            inbox = scoped_api(values, "/v1/inbox?" + query, profile)
            if (not isinstance(inbox.get("messages"), list) or len(inbox["messages"]) > 20
                    or type(inbox.get("next_cursor")) is not int or type(inbox.get("has_more")) is not bool):
                raise ClientError("Resume inbox response is invalid; checkpoint retained")
    snapshot = {**binding, "observed_at": instant, "last_semantic_ack_cursor": int(profile.get("ack_cursor", 0))}
    atomic_json(path, snapshot)
    result = {"scope": binding, "presence": presence, "controls": controls,
              "unmonitored_gap": {"from": previous, "to": instant, "continuous_monitoring": False,
                                  "reason": "unknown_prior_checkpoint" if previous is None else "between_resume_observations"},
              "mailbox": inbox, "read_is_not_acknowledgement": True,
              "host_execution_or_wake": "not_requested", "semantic_ack_cursor": snapshot["last_semantic_ack_cursor"]}
    print(json.dumps(result, indent=2))
    return 0
