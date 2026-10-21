#!/usr/bin/env python3
"""Local service lifecycle and HTTP client for AgentBus."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from src.agentbus_delivery_client import send_command
from src.agentbus_transport_client import ClientError, api, configuration, profile_session_token

NAME_RE = r"^[a-z0-9][a-z0-9-]{0,31}$"
REPO_RE = r"^[a-z0-9][a-z0-9._-]{0,63}$"
PUBLIC_PERSONA_KEYS = ("schema_version", "chat_id", "identity", "repo", "name",
                       "display_name", "role", "voice", "remit", "values",
                       "working_style", "signature", "persona_file")


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def consumer_state_dir(values: dict[str, str]) -> Path:
    configured = values.get("AGENTBUS_CONSUMER_STATE_DIR")
    xdg_state = Path(values.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    state = Path(configured or xdg_state / "agentbus/consumers").expanduser()
    if state.is_symlink() or (state.exists() and not state.is_dir()):
        raise ClientError(f"Refusing unsafe AgentBus consumer state directory {state}.")
    return state


def identity_path(values: dict[str, str], identity: str) -> Path:
    if not identity or "/" in identity or "\\" in identity or identity in {".", ".."}:
        raise ClientError("Invalid AgentBus identity.")
    return consumer_state_dir(values) / f"{identity}.json"


def atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def resolve_identity(explicit: str | None) -> str:
    identity = explicit or os.environ.get("AGENTBUS_IDENTITY")
    if not identity:
        raise ClientError("Select a chat with --identity PREFIX:REPO:NAME or AGENTBUS_IDENTITY.")
    return identity


def current_repo() -> str:
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            stdin=subprocess.DEVNULL, text=True, capture_output=True, timeout=3, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ClientError("Cannot determine the current Git repository; pass --repo explicitly.") from None
    if result.returncode != 0 or not result.stdout.strip():
        raise ClientError("Run onboarding inside a Git repository or pass --repo explicitly.")
    return Path(result.stdout.strip()).name


def load_profile(values: dict[str, str], identity: str, *, save_migration: bool = True) -> dict:
    path = identity_path(values, identity)
    if path.is_symlink():
        raise ClientError(f"Refusing symlinked AgentBus profile {path}.")
    try:
        profile = json.loads(path.read_text())
    except FileNotFoundError:
        if ":" not in identity:
            raise ClientError(f"Unknown AgentBus identity {identity!r}; run `agentbus onboard`.") from None
        legacy_path = identity_path(values, identity.split(":")[-2])
        if legacy_path.is_symlink() or not legacy_path.is_file():
            raise ClientError(f"Unknown AgentBus identity {identity!r}; run `agentbus onboard`.") from None
        try:
            profile = json.loads(legacy_path.read_text())
        except (ValueError, OSError):
            raise ClientError(f"Cannot read valid profile {legacy_path}.") from None
        if not isinstance(profile, dict) or profile.get("identity") != identity:
            raise ClientError(f"Unknown AgentBus identity {identity!r}; run `agentbus onboard`.")
    except (ValueError, OSError):
        raise ClientError(f"Cannot read valid profile {path}.") from None
    if not isinstance(profile, dict):
        raise ClientError(f"Cannot read valid profile {path}.")
    if profile.get("schema_version") == 1:
        legacy = dict(profile)
        repo = str(profile.get("repo") or identity.split(":")[-2])
        profile = {
            "schema_version": 2, "chat_id": str(uuid.uuid4()), "identity": identity,
            "repo": repo, "name": identity.split(":")[-1],
            "display_name": profile.get("display_name", identity), "role": "maintainer",
            "voice": "direct, practical, and collaborative",
            "remit": f"Maintain {repo}", "persona_file": profile.get("persona_file"),
            "service_url": values.get("AGENTBUS_URL"), "inbox_id": None,
            "channel": profile.get("slack_channel"),
            "ack_cursor": int(profile.get("next_cursor", 0)),
            "observed_cursor": int(profile.get("next_cursor", 0)),
            "created_at": now(), "updated_at": now(), "legacy_metadata": legacy,
        }
        if save_migration:
            raise ClientError("Legacy profile requires operator-granted enrollment; no automatic inbox binding")
    return profile


def save_profile(values: dict[str, str], profile: dict, *, reset_cursors: bool = False) -> None:
    profile["updated_at"] = now()
    path = identity_path(values, profile["identity"])
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path.parent / f".{path.name}.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not reset_cursors and path.exists():
            from src.agentbus_scope_client import scope

            try:
                current = json.loads(path.read_text())
                same_scope = current.get("identity") == profile["identity"] and scope(current) == scope(profile)
            except ClientError:
                same_scope = False  # Unjoined enrollment has no session scope yet.
            except (OSError, ValueError, AttributeError):
                raise ClientError("Cannot safely update unreadable AgentBus profile") from None
            if same_scope:
                try:
                    profile.update({field: max(int(profile.get(field, 0)), int(current.get(field, 0)))
                                    for field in ("ack_cursor", "observed_cursor")})
                except (TypeError, ValueError):
                    raise ClientError("Cannot safely update invalid AgentBus cursors") from None
        atomic_json(path, profile)


def record_observed_cursor(values: dict[str, str], profile: dict, cursor: int) -> dict:
    """Record complete foreground observation, without acknowledging or stale-profile writes."""
    from src.agentbus_scope_client import scope

    if type(cursor) is not int or cursor < 1:
        raise ClientError("Message observation requires a positive public cursor")
    path = identity_path(values, profile["identity"])
    with (path.parent / f".{path.name}.lock").open("a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = load_profile(values, profile["identity"], save_migration=False)
        if (scope(current) != scope(profile) or current.get("identity") != profile["identity"]
                or profile_session_token(current) != profile_session_token(profile)
                or current["participation"].get("stopped")):
            raise ClientError("Message observation scope changed; evidence retained")
        if type(current.get("observed_cursor", 0)) is not int:
            raise ClientError("Message observation cursor is invalid; evidence retained")
        current["observed_cursor"] = max(current.get("observed_cursor", 0), cursor)
        current["updated_at"] = now()
        atomic_json(path, current)
        return current


def checked_profile(values: dict[str, str], identity: str) -> tuple[dict, dict]:
    profile = load_profile(values, identity)
    info = api(values, "/v1/info", session_token=profile_session_token(profile), inbox_id=profile.get("inbox_id"))
    required = {"session-required", "channel-isolation-v1", "channel-local-cursors-v1"}
    if not required <= set(info.get("features", [])):
        raise ClientError("Service does not support required channel isolation; no legacy fallback")
    if (profile.get("chat_id") != info.get("chat_id") or (profile.get("participation") or {}).get("session_id") != info.get("session_id") or
            profile.get("inbox_id") != info["inbox_id"] or profile.get("channel") != info["channel"] or
            profile.get("service_url") != values.get("AGENTBUS_URL")):
        raise ClientError("Profile channel scope differs; operator enrollment is required; no automatic switch.")
    lifecycle = info.get('lifecycle')
    if profile.get('message_lifecycle_version') == 1 and 'message-lifecycle-v1' not in info.get('features', []):
        raise ClientError('Lifecycle capability disappeared; no fallback to legacy message authority')
    if 'message-lifecycle-v1' in info.get('features', []):
        if not isinstance(lifecycle, dict) or lifecycle.get('version') != 1:
            raise ClientError('Lifecycle capability is inconsistent; no message authority granted')
        first_lifecycle = profile.get('message_lifecycle_version') != 1
        profile['message_lifecycle_version'] = 1
        if first_lifecycle:
            save_profile(values, profile)
    profile["scope_verified"] = True
    if profile.get("rename_source"):
        from src.agentbus_rename_client import recover_rename_state

        recover_rename_state(values, profile)
    return profile, info


def control_profile(values, identity):
    profile = load_profile(values, identity, save_migration=False)
    from src.agentbus_scope_client import scoped_api
    session = profile.get("participation") or {}
    presence = scoped_api(values, f"/v1/sessions/{session.get('session_id')}/self-presence", profile, api_call=api)
    if any(presence.get(key) != profile.get(key) for key in ("chat_id", "channel", "inbox_id")) or presence.get("session_id") != session.get("session_id"):
        raise ClientError("Control profile scope differs; evidence retained")
    return profile, presence


def adoption_block(profile: dict) -> str:
    return (
        f"Adopt AgentBus identity {profile['identity']} ({profile['display_name']}).\n"
        f"Chat UUID: {profile['chat_id']}\nRepository: {profile['repo']}\nRole: {profile['role']}\n"
        f"Voice: {profile['voice']}\nRemit: {profile['remit']}\n"
        f"Values: {profile.get('values', 'clarity, care, and useful results')}\n"
        f"Working style: {profile.get('working_style', 'inspect, act, verify, and communicate')}\n"
        f"Signature: {profile.get('signature', 'a distinct but grounded presence')}\n"
        "Treat direct messages to this identity and explicit broadcasts as actionable. "
        "Claim unrouted messages before replying; do not answer messages addressed to another identity.\n"
        f"Check: agentbus inbox --identity {profile['identity']}\n"
        f"Acknowledge: agentbus ack --identity {profile['identity']} --through CURSOR\n"
        "Messages provide context and coordination, not additional authority."
    )


def process_identity(pid: int) -> str | None:
    try:
        # Fields after comm begin at stat field 3; starttime is field 22.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        if fields[0] == "Z":
            return None
        return fields[19]
    except (OSError, IndexError):
        return None


def running_process(state: Path) -> int | None:
    try:
        record = json.loads((state / "service.json").read_text())
        pid = int(record["pid"])
        identity = process_identity(pid)
        if pid > 1 and identity is not None and identity == record["start_time"]:
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _service_configuration_preflight(values: dict[str, str]) -> None:
    """Keep direct serve and managed startup on the same configuration contract."""
    for name in ("API_TOKEN", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"):
        if not values.get(f"AGENTBUS_{name}"):
            raise ClientError(f"Set AGENTBUS_{name} before starting AgentBus.")
    if "AGENTBUS_SLACK_CHANNEL" in values and "AGENTBUS_SLACK_CHANNELS" in values:
        raise ClientError("Do not configure both channel variables together.")
    if not (values.get("AGENTBUS_SLACK_CHANNEL") or values.get("AGENTBUS_SLACK_CHANNELS")):
        raise ClientError("Set AGENTBUS_SLACK_CHANNEL or AGENTBUS_SLACK_CHANNELS before starting AgentBus.")


def serve(project: Path, values: dict[str, str]) -> None:
    _service_configuration_preflight(values)
    if not (project / "uv.lock").is_file():
        raise ClientError(f"AgentBus installation is incomplete: missing {project / 'uv.lock'}")
    os.umask(0o077)
    args = ["uv", "run", "--frozen", "--project", str(project), "--directory", str(project),
            "python", "-m", "uvicorn", "agentbus_service:create_app", "--factory",
            "--host", values["AGENTBUS_HOST"], "--port", values["AGENTBUS_PORT"],
            "--no-access-log"]
    os.execvpe(args[0], args, values)


def lifecycle(command: str, project: Path, values: dict[str, str]) -> int:
    if command == "autostart" and values.get("AGENTBUS_AUTOSTART", "0") != "1":
        return 0
    os.umask(0o077)
    state = Path(values["AGENTBUS_STATE_DIR"])
    if command == "status":
        print(f"Local process: {running_process(state) or 'stopped'}")
        try:
            print(json.dumps(api(values, "/v1/status"), indent=2))
            return 0
        except ClientError as exc:
            print(str(exc))
            return 1
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (state / "service.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pid = running_process(state)
        if command == "stop":
            if not pid:
                print("AgentBus is stopped.")
                return 0
            os.killpg(pid, signal.SIGTERM)
            for _ in range(100):
                if running_process(state) is None:
                    (state / "service.json").unlink(missing_ok=True)
                    print("AgentBus stopped.")
                    return 0
                time.sleep(0.1)
            raise ClientError("AgentBus is still shutting down; inspect .state/service.log.")
        if pid:
            print(f"AgentBus already running (PID {pid}).")
            return 0
        _service_configuration_preflight(values)
        launcher = project / "agentbus"
        with (state / "service.log").open("ab") as log:
            child = subprocess.Popen([sys.executable, str(launcher), "serve"], env=values,
                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, umask=0o077)
        identity = process_identity(child.pid)
        (state / "service.json").write_text(json.dumps({"pid": child.pid, "start_time": identity}))
        # Dependencies may need downloading on the first run. Report starting honestly.
        for _ in range(30):
            if child.poll() is not None:
                (state / "service.json").unlink(missing_ok=True)
                raise ClientError(f"AgentBus failed to start; inspect {state / 'service.log'}")
            time.sleep(0.1)
        print(f"AgentBus started (PID {child.pid}); check `agentbus status` for Slack connectivity.")
        print(f"Log: {state / 'service.log'}")
        return 0


def _execute_command(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    if ns.command == "serve":
        serve(project, values)
        return 0
    if ns.command in {"start", "stop", "status", "autostart"}:
        return lifecycle(ns.command, project, values)
    if ns.command in {"migrate-channels", "migrate-profile-scope"}:
        from src.agentbus_channel_migration_client import migrate_database, migrate_profile
        return migrate_database(ns, values) if ns.command == "migrate-channels" else migrate_profile(ns, values)
    if ns.command in {"issue-handoff", "issue-enrollment", "join", "onboard"}:
        from src.agentbus_enrollment_client import enrollment_command
        return enrollment_command(ns, project, values, api)
    if ns.command == "policy-ack":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        session = profile.get("participation")
        if not session:
            raise ClientError("This chat is not joined; run `agentbus join`.")
        result = api(values, f"/v1/sessions/{session['session_id']}/policy-ack",
                     {"revision": ns.revision}, session_token=session["session_secret"], inbox_id=profile["inbox_id"])
        from src.agentbus_poll_client import ensure_worker, policy_values_for_revision, retire_event

        path = identity_path(values, identity).resolve()
        delivered = policy_values_for_revision(path, profile, ns.revision)
        if delivered is not None:
            session["policy_values"] = delivered
        session["acknowledged_policy_revision"] = ns.revision
        save_profile(values, profile)
        retire_event(path, profile, "POLICY_CHANGED", ns.revision)
        from src.agentbus_enrollment_client import announce_pending
        announce_pending(values, profile, api)
        ensure_worker(path, values)
        print(f"ACK {result['receipt']} POLICY")
        return 0
    if ns.command == "poll":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        session = profile.get("participation")
        if not session:
            raise ClientError("This chat is not joined; run `agentbus join`.")
        if session.get("stopped"):
            raise ClientError("This participation session has stopped.")
        from src.agentbus_poll_client import ensure_worker, peek_event, retire_event
        from src.agentbus_presentation_client import render_event

        path = identity_path(values, identity).resolve()
        last_ensure = 0.0
        while True:
            if time.monotonic() - last_ensure >= 5:
                ensure_worker(path, values)
                last_ensure = time.monotonic()
            event = peek_event(path, profile, quiet=bool(profile.get("quiet_mode", False)))
            if event is not None:
                budget = session["policy_values"].get("presentation_budget_bytes")
                rendered = render_event(event, json_mode=ns.json, budget_bytes=budget)
                if event["kind"] == "MESSAGE" and (ns.json or budget is None or
                        rendered == render_event(event, json_mode=False, budget_bytes=None)):
                    profile = record_observed_cursor(values, profile, event["message"]["cursor"])
                if rendered:
                    print(rendered)
                if event["kind"] == "MESSAGE":
                    retire_event(path, profile, "MESSAGE", event["id"])
                return 0
            if ns.check:
                return 0
            time.sleep(0.25)
    if ns.command == "check-in":
        identity = resolve_identity(ns.identity)
        profile, _ = control_profile(values, identity)
        session = profile.get("participation")
        if not session:
            raise ClientError("This chat is not joined; run `agentbus join`.")
        result = api(values, f"/v1/sessions/{session['session_id']}/check-in",
                     {}, session_token=session["session_secret"], inbox_id=profile["inbox_id"])
        print(json.dumps(result, indent=2))
        return 0
    if ns.command == "ack-control":
        identity = resolve_identity(ns.identity)
        profile, _ = control_profile(values, identity)
        session = profile.get("participation")
        if not session:
            raise ClientError("This chat is not joined; run `agentbus join`.")
        report = None
        if ns.report_file:
            try:
                report = json.loads(Path(ns.report_file).read_text())
            except (OSError, ValueError):
                raise ClientError("Checkpoint report file must contain valid JSON.") from None
        payload = {"report": report}
        if getattr(ns, "answer", None) is not None:
            payload["answer"] = ns.answer
        result = api(values, f"/v1/controls/{ns.control_id}/targets/{session['session_id']}/ack",
                     payload, session_token=session["session_secret"], inbox_id=profile["inbox_id"])
        stopped = result["directive"] == "STOP"
        if stopped:
            session["stopped"] = True
            save_profile(values, profile)
        from src.agentbus_poll_client import retire_event

        retire_event(identity_path(values, identity).resolve(), profile, "CONTROL", ns.control_id)
        print(f"ACK {result['receipt']} RECORDED" + ("\nSTOP" if stopped else ""))
        return 0
    if ns.command == "agents":
        bound, _ = checked_profile(values, resolve_identity(getattr(ns, "identity", None)))
        directory = consumer_state_dir(values)
        profiles = []
        if directory.is_dir():
            for path in sorted(directory.glob("*.json")):
                try:
                    data = json.loads(path.read_text())
                    if isinstance(data, dict) and all(data.get(k) == bound.get(k) for k in ("service_url", "inbox_id", "channel")):
                        profiles.append({key: data.get(key) for key in
                                         ("identity", "display_name", "repo", "role", "chat_id")})
                except (OSError, ValueError):
                    continue
        if ns.json:
            print(json.dumps(profiles, indent=2, ensure_ascii=False))
        else:
            for item in profiles:
                print(f"{item['identity'] or '(legacy)'}\t{item['display_name']}\t{item['role']}")
        return 0
    if ns.command == "persona":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        changes = {key: getattr(ns, key) for key in
                   ("display_name", "role", "voice", "remit", "values", "working_style",
                    "signature", "persona_file")
                   if getattr(ns, key) is not None}
        if changes:
            if any(not value.strip() for value in changes.values()):
                raise ClientError("Persona fields must not be blank.")
            profile.update(changes)
            save_profile(values, profile)
        public = {key: profile.get(key) for key in PUBLIC_PERSONA_KEYS}
        print(json.dumps(public, indent=2, ensure_ascii=False) if ns.json else adoption_block(profile))
        return 0
    if ns.command == "inbox":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        historical = ns.after is not None
        after = ns.after if historical else int(profile["ack_cursor"])
        query = {"identity": identity, "after": after, "limit": ns.limit}
        if ns.thread_ts:
            query["thread_ts"] = ns.thread_ts
        if ns.context:
            query["context"] = "true"
        result = api(values, "/v1/inbox?" + urlencode(query), session_token=profile_session_token(profile), inbox_id=profile["inbox_id"])
        if not historical and result["messages"]:
            profile = record_observed_cursor(values, profile,
                                             max(item["cursor"] for item in result["messages"]))
        result["ack_cursor"] = profile["ack_cursor"]
    elif ns.command == "ack":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        if ns.through < int(profile["ack_cursor"]):
            raise ClientError("Acknowledgements are monotonic and cannot move backward.")
        if ns.through > int(profile["observed_cursor"]):
            raise ClientError("Cannot acknowledge beyond the highest cursor observed by this chat.")
        from src.agentbus_lifecycle_client import acknowledge_handled
        handling = acknowledge_handled(values, profile, ns.through, api_call=api)
        acknowledged = handling['through']
        profile["ack_cursor"] = acknowledged
        save_profile(values, profile)
        from src.agentbus_poll_client import retire_acknowledged_messages

        retired = retire_acknowledged_messages(
            identity_path(values, identity).resolve(), profile, acknowledged
        )
        result = {"identity": identity, "ack_cursor": acknowledged,
                  **({'lifecycle':handling} if profile.get('message_lifecycle_version') == 1 else {}),
                  "presentation_messages_retired": retired}
    elif ns.command == "rebind":
        identity = resolve_identity(ns.identity)
        profile = load_profile(values, identity, save_migration=False)
        info = api(values, "/v1/info", session_token=profile_session_token(profile), inbox_id=profile.get("inbox_id"))
        if (profile.get("channel") != info["channel"] or profile.get("inbox_id") != info["inbox_id"] or profile.get("service_url") != values["AGENTBUS_URL"]):
            raise ClientError("Rebind cannot switch channel or service membership")
        cursor = 0 if ns.start_from == "beginning" else info["high_water_cursor"]
        profile.update(inbox_id=info["inbox_id"], channel=info["channel"],
                       service_url=values["AGENTBUS_URL"], ack_cursor=cursor,
                       observed_cursor=cursor)
        save_profile(values, profile, reset_cursors=True)
        result = {"identity": identity, "inbox_id": info["inbox_id"], "ack_cursor": cursor}
    elif ns.command == "claim":
        identity = resolve_identity(ns.identity)
        profile, _ = checked_profile(values, identity)
        result = api(values, f"/v1/messages/{ns.cursor}/claim", {"identity": identity}, session_token=profile_session_token(profile), inbox_id=profile["inbox_id"])
    elif ns.command in {"reply", "send"}:
        result = send_command(ns, values, checked_profile, resolve_identity, api)
    else:  # Raw history remains inside the authenticated membership.
        profile, _ = checked_profile(values, resolve_identity(getattr(ns, "identity", None)))
        query = {key: value for key, value in vars(ns).items() if key not in {"command", "identity"} and value is not None}
        result = api(values, "/v1/messages?" + urlencode(query), session_token=profile_session_token(profile), inbox_id=profile["inbox_id"])
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def cli_serve(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_lifecycle(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_lifecycle_message(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_lifecycle_client import message_command
    identity = resolve_identity(ns.identity)
    profile = load_profile(values, identity, save_migration=False)
    print(json.dumps(message_command(ns, values, profile, api_call=api), indent=2))
    return 0


def cli_message_status(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_scope_client import scoped_api
    if ns.cursor < 1:
        raise ClientError('Message cursor must be positive')
    profile = load_profile(values, resolve_identity(ns.identity), save_migration=False)
    print(json.dumps(scoped_api(values, f'/v1/messages/{ns.cursor}/outcome', profile, api_call=api), indent=2))
    return 0


def cli_send(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_read(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_onboard(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_persona(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_agents(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_inbox(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_ack(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_rebind(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_claim(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_reply(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_issue_handoff(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_issue_rotation(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_rotation_client import issue_rotation

    return issue_rotation(resolve_identity(ns.identity), ns.output, values)


def cli_rotate_secret(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_rotation_client import rotate_secret

    return rotate_secret(resolve_identity(ns.identity), ns.rotation_file, ns.replace_pending, values)


def cli_join(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_policy_ack(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_check_in(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_ack_control(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_poll(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    return _execute_command(ns, project, values)


def cli_quiet(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    identity = resolve_identity(ns.identity)
    profile, _ = checked_profile(values, identity)
    if ns.mode != "status":
        profile["quiet_mode"] = ns.mode == "on"
        save_profile(values, profile)
    print("QUIET " + ("ON" if profile.get("quiet_mode", False) else "OFF"))
    return 0


def cli_policy_set(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import policy_set

    return policy_set(ns, values)


def cli_policy_show(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import policy_show

    return policy_show(ns, values)


def cli_control_stop(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import control_stop

    return control_stop(ns, values)


def cli_control_issue(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import control_issue

    return control_issue(ns, values)


def cli_control_status(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import control_status

    return control_status(ns, values)


def cli_session_presence(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import session_presence

    return session_presence(ns, values)


def cli_roster(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import roster

    return roster(ns, values)


def cli_rename(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_rename_client import rename_chat

    return rename_chat(ns, values)


def cli_claim_recovery(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_operator_client import claim_recovery

    return claim_recovery(ns, values)


def cli_codex_wake(ns: argparse.Namespace, project: Path, values: dict[str, str]) -> int:
    from src.agentbus_codex_wake_client import cli_codex_wake as run

    return run(ns, project, values)


# BCF inventories this closed population, and main dispatches through it.
CLI_OPERATIONS = {
    'pickup': cli_lifecycle_message,
    'complete': cli_lifecycle_message,
    'message-status': cli_message_status,
    "serve": cli_serve,
    "start": cli_lifecycle,
    "stop": cli_lifecycle,
    "status": cli_lifecycle,
    "autostart": cli_lifecycle,
    "send": cli_send,
    "read": cli_read,
    "onboard": cli_onboard,
    "persona": cli_persona,
    "agents": cli_agents,
    "inbox": cli_inbox,
    "ack": cli_ack,
    "rebind": cli_rebind,
    "claim": cli_claim,
    "reply": cli_reply,
    "issue-handoff": cli_issue_handoff,
    "issue-enrollment": cli_issue_handoff,
    "migrate-channels": cli_issue_handoff,
    "migrate-profile-scope": cli_issue_handoff,
    "issue-rotation": cli_issue_rotation,
    "rotate-secret": cli_rotate_secret,
    "join": cli_join,
    "policy-ack": cli_policy_ack,
    "check-in": cli_check_in,
    "ack-control": cli_ack_control,
    "poll": cli_poll,
    "quiet": cli_quiet,
    "policy-set": cli_policy_set,
    "policy-show": cli_policy_show,
    "control-stop": cli_control_stop,
    "control-issue": cli_control_issue,
    "control-status": cli_control_status,
    "session-presence": cli_session_presence,
    "roster": cli_roster,
    "rename": cli_rename,
    "claim-recovery": cli_claim_recovery,
    "codex-wake": cli_codex_wake,
}


def main(argv: list[str] | None = None) -> int:
    from src.agentbus_parser_client import parse_args

    ns = parse_args(argv)
    try:
        project, values = configuration(include_operator=ns.command in {
            "serve", "start", "autostart",
        })
        return CLI_OPERATIONS[ns.command](ns, project, values)
    except (ClientError, OSError) as exc:
        print(f"agentbus: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
