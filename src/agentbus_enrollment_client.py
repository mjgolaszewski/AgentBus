"""Local persona creation and operator-granted immutable channel enrollment."""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path

from src.agentbus_transport_client import ClientError, local_prefix


def enrollment_command(ns, project, values, api):
    from src.agentbus_client import (
        NAME_RE,
        REPO_RE,
        adoption_block,
        atomic_json,
        current_repo,
        identity_path,
        load_profile,
        now,
        resolve_identity,
        save_profile,
    )
    if ns.command in {"issue-enrollment", "issue-handoff"}:
        token = os.environ.get("AGENTBUS_OPERATOR_TOKEN", "")
        if not token or token == values.get("AGENTBUS_API_TOKEN"):
            raise ClientError("Export a separate operator capability")
        identity = resolve_identity(ns.identity)
        destination = Path(ns.output)
        if destination.is_symlink() or destination.exists():
            raise ClientError("Grant output already exists; choose a new private path")
        chat_id = getattr(ns, "chat_id", None)
        if ns.command == "issue-handoff":
            chat_id = load_profile(values, identity, save_migration=False)["chat_id"]
        result = api(values, "/v1/sessions/enrollment-grants", {"channel": ns.channel,
            "repo": identity.split(":")[-2], "route": identity, "chat_id": chat_id}, bearer_token=token)
        descriptor = os.open(destination, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        with os.fdopen(descriptor,"w") as stream:
            stream.write(result["enrollment_token"]+"\n")
            stream.flush()
            os.fsync(stream.fileno())
        print(f"Enrollment grant written to {destination}; expires in 600 seconds")
        return 0
    if ns.command == "join":
        identity = resolve_identity(ns.identity)
        profile = load_profile(values, identity, save_migration=False)
        if profile.get("participation"):
            raise ClientError("This profile already has a participation session.")
        pending = profile.get("participation_pending")
        if pending is None:
            pending = {"session_secret": secrets.token_urlsafe(32)}
            grant_file = getattr(ns, "enrollment_file", None) or ns.handoff_file
            if grant_file is None:
                raise ClientError("Join requires an operator-issued --enrollment-file")
            from src.agentbus_rotation_client import _private_token
            pending["enrollment_token"] = _private_token(Path(grant_file))
            profile["participation_pending"] = pending
            save_profile(values, profile)
        result = api(values, "/v1/sessions", {
            "repo": profile["repo"], "route": identity,
            "display_name": profile["display_name"],
            "session_secret": pending["session_secret"],
            "enrollment_token": pending.get("enrollment_token") or pending.get("handoff_token"),
        })
        if ns.handoff_file and result["chat_id"] != profile["chat_id"]:
            raise ClientError("Service chat ID differs from the local profile; join is not committed.")
        if profile.get("channel") is not None and (profile["channel"] != result["channel"] or profile["inbox_id"] != result["inbox_id"]):
            raise ClientError("Enrollment scope differs from the existing profile; evidence retained")
        if profile.get("inbox_id") is None:
            cursor = 0 if profile.get("start_from") == "beginning" else result["high_water_cursor"]
            profile.update(ack_cursor=cursor, observed_cursor=cursor)
        profile.update(channel=result["channel"], inbox_id=result["inbox_id"], scope_verified=True)
        profile["chat_id"] = result["chat_id"]
        profile["participation"] = {
            "session_id": result["session_id"], "session_secret": pending["session_secret"],
            "acknowledged_policy_revision": None, "policy_values": result["values"],
        }
        profile.pop("participation_pending", None)
        save_profile(values, profile)
        print(json.dumps({key: result[key] for key in ("chat_id", "session_id", "revision", "values", "sources", "ack_required")}, indent=2))
        # Bind only the current host-verifiable Codex thread.
        from src.agentbus_codex_wake_client import enroll_current
        try:
            wake = enroll_current(values, identity)
            if wake["status"] != "workspace_disabled":
                print(json.dumps({"codex_wake": wake}))
        except ClientError as exc:
            print(f"Codex wake enrollment pending: {exc}", file=sys.stderr)
        cadence = result["values"]
        print(f"Acknowledge with `agentbus policy-ack --identity {identity} {result['revision']}`. Active-turn checks: `agentbus poll --check --identity {identity}` at work checkpoints; "
              f"back off {cadence['initial_interval_seconds']}s ×{cadence['backoff_factor']} to {cadence['max_interval_seconds']}s after quiet checks. Idle live-wakable chats end turns; the worker keeps checking. Empty checks print nothing.")
        return 0
    if ns.command == "onboard":
        ns.repo = ns.repo or current_repo()
        prefix = local_prefix(values, required=True)
        if not re.fullmatch(NAME_RE, ns.name):
            raise ClientError("Chat names must be lowercase letters, digits, or hyphens (maximum 32).")
        if not re.fullmatch(REPO_RE, ns.repo):
            raise ClientError("Repository names must be lowercase letters, digits, dots, underscores, or hyphens (maximum 64).")
        identity = f"{prefix}:{ns.repo}:{ns.name}"
        if len(identity) > 80:
            raise ClientError("Combined AgentBus identity exceeds 80 characters.")
        path = identity_path(values, identity)
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (directory / ".profiles.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if path.exists():
                if ns.resume:
                    profile = load_profile(values, identity, save_migration=False)
                    print(adoption_block(profile))
                    return 0
                raise ClientError(f"Identity {identity!r} already exists; use --resume to adopt it explicitly.")
            info = {"inbox_id": None, "channel": None}
            cursor = 0
            profile = {
                "schema_version": 2, "chat_id": str(uuid.uuid4()), "identity": identity,
                "repo": ns.repo, "name": ns.name,
                "display_name": ns.display_name or ns.name.replace("-", " ").title(),
                "role": ns.role, "voice": ns.voice,
                "remit": ns.remit or f"Work on {ns.repo}", "values": ns.values,
                "working_style": ns.working_style, "signature": ns.signature,
                "persona_file": ns.persona_file,
                "service_url": values["AGENTBUS_URL"], "inbox_id": info["inbox_id"],
                "channel": info["channel"], "ack_cursor": cursor, "observed_cursor": cursor,
                "created_at": now(), "updated_at": now(), "announcement": None, "start_from": ns.start_from,
            }
            atomic_json(path, profile)
        if ns.announce is not None:
            profile["announcement_pending"] = ns.announce
            save_profile(values, profile)
        print(adoption_block(profile))
        return 0


def announce_pending(values,profile,api):
    from src.agentbus_client import save_profile
    if 'announcement_pending' not in profile:
        return
    text=profile['announcement_pending'] or f"{profile['display_name']} joined as {profile['identity']}. {profile['remit']}"
    key=profile.setdefault('announcement_message_id',str(uuid.uuid4()))
    save_profile(values,profile)
    receipt=api(values,'/v1/messages',{'sender':profile['identity'],'recipient':'all','audience':'broadcast',
        'kind':'introduction','repo':profile['repo'],'text':text},
        session_token=profile['participation']['session_secret'],inbox_id=profile['inbox_id'],delivery_key=key)
    profile['announcement']={'cursor':receipt['cursor'],'slack_ts':receipt['slack_ts']}
    profile.pop('announcement_pending',None)
    save_profile(values,profile)
