"""Compact private resume observations without acknowledgement or host ownership."""
import hashlib
import json
import math
import os
import re
import stat
import time
from urllib.parse import urlencode
from uuid import UUID

from src.agentbus_scope_client import scope, scoped_api, verify_projection
from src.agentbus_transport_client import ClientError


def validate_recovery_context(value, binding):
    """Strict metadata only; reported source/owners are never authority or custody."""
    error = 'Recovery context is invalid or belongs to another scope; preserve it for recovery'
    def instant(item):
        return item is None or (type(item) in (int, float) and math.isfinite(item) and item >= 0)
    def request(item):
        return item is None or (isinstance(item, str) and len(item) == 36 and str(UUID(item)) == item)
    def cursor(item):
        return item is None or (type(item) is int and 0 < item < 2**63)
    try:
        fields = {'version', 'scope', 'reported_at', 'source', 'next_update_due_at', 'held_owners', 'receipts'}
        if (not isinstance(value, dict) or set(value) != fields or type(value['version']) is not int
                or value['version'] != 1 or value['scope'] != binding
                or not instant(value['reported_at']) or not instant(value['next_update_due_at'])):
            raise ValueError()
        source = value['source']
        if source is not None:
            if (not isinstance(source, dict) or set(source) != {'commit', 'tree', 'branch', 'dirty_paths'}
                    or any(v is not None and (not isinstance(v, str) or not re.fullmatch('[0-9a-f]{40}', v))
                           for v in (source['commit'], source['tree']))
                    or (source['commit'] is None) != (source['tree'] is None)
                    or (source['branch'] is not None and (not isinstance(source['branch'], str)
                        or not 1 <= len(source['branch']) <= 128 or not source['branch'].isprintable()))):
                raise ValueError()
            paths = source['dirty_paths']
            if (not isinstance(paths, list) or len(paths) > 128
                    or any(not isinstance(v, str) or not 1 <= len(v) <= 512 or not v.isprintable() for v in paths)
                    or len(set(paths)) != len(paths)):
                raise ValueError()
        owners, receipts = value['held_owners'], value['receipts']
        if not isinstance(owners, list) or len(owners) > 16 or not isinstance(receipts, list) or len(receipts) > 32:
            raise ValueError()
        for owner in owners:
            if (not isinstance(owner, dict) or set(owner) != {'owner', 'receipt_cursor', 'request_id'}
                    or not isinstance(owner['owner'], str)
                    or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}', owner['owner'])
                    or not cursor(owner['receipt_cursor']) or not request(owner['request_id'])):
                raise ValueError()
        for receipt in receipts:
            if (not isinstance(receipt, dict) or set(receipt) != {'kind', 'cursor', 'request_id'}
                    or receipt['kind'] not in {'message', 'publication', 'control', 'resource'}
                    or not cursor(receipt['cursor']) or not request(receipt['request_id'])
                    or receipt['cursor'] is None and receipt['request_id'] is None):
                raise ValueError()
    except (KeyError, ValueError, TypeError, OverflowError, AttributeError):
        raise ClientError(error) from None
    return value


def recovery_context_record(record, profile, values):
    if (not isinstance(record, dict) or set(record) != {'input_sha256', 'value'}
            or not isinstance(record['input_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', record['input_sha256'])):
        raise ClientError('Recovery context record is invalid; preserve the checkpoint')
    validate_recovery_context(record['value'], scope(profile))
    rendered = json.dumps(record, sort_keys=True)
    sensitive = [(profile.get('participation') or {}).get('session_secret'),
                 *(values.get(key) for key in ('AGENTBUS_API_TOKEN', 'AGENTBUS_SLACK_BOT_TOKEN', 'AGENTBUS_SLACK_APP_TOKEN'))]
    if any(isinstance(secret, str) and len(secret) >= 24 and secret in rendered for secret in sensitive):
        raise ClientError('Recovery context contains credential material; preserve the input and remove secrets')
    return record


def load_recovery_context(path, profile, values):
    from pathlib import Path
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError()
            raw = source.read(16385)
        if len(raw) > 16384:
            raise ValueError()
        value = json.loads(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        raise ClientError('Recovery context file is not bounded regular JSON; input and checkpoint retained') from None
    return recovery_context_record({'input_sha256': hashlib.sha256(raw).hexdigest(), 'value': value}, profile, values)


def write_resume_checkpoint(path, snapshot, baseline):
    """Compare-and-swap under a short local lock, never held across API calls."""
    import fcntl

    from src.agentbus_client import atomic_json
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path.with_suffix('.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        if not stat.S_ISREG(os.fstat(lock.fileno()).st_mode):
            raise ClientError('Resume lock is invalid; preserve state')
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = None
        if path.exists() or path.is_symlink():
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, 'rb') as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                    raise ClientError('Resume checkpoint changed; preserve state')
                current = source.read(16385)
        if current != baseline:
            raise ClientError('Resume checkpoint changed concurrently; preserve the newer state and retry the read')
        atomic_json(path, snapshot)


def cli_resume(ns, project, values):
    from src.agentbus_client import consumer_state_dir, control_profile, identity_path, resolve_identity
    from src.agentbus_poll_client import foreground_check_in

    profile, presence = control_profile(values, resolve_identity(ns.identity))
    binding = scope(profile)
    compact = getattr(ns, 'unhandled', False)
    root = consumer_state_dir(values)
    path = root / (".resume-" + hashlib.sha256(binding["chat_id"].encode()).hexdigest() + ".json")
    previous = None
    context = None
    checkpoint_baseline = None
    if path.exists() or path.is_symlink():
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                if not stat.S_ISREG(os.fstat(source.fileno()).st_mode) or os.fstat(source.fileno()).st_mode & 0o077:
                    raise ValueError()
                raw = source.read(16385)
            if len(raw) > 16384:
                raise ValueError()
            checkpoint_baseline = raw
            retained = json.loads(raw)
            if not isinstance(retained, dict):
                raise ValueError()
            verify_projection(retained, profile)
            previous = retained["observed_at"]
            if "recovery_context" in retained:
                context = recovery_context_record(retained["recovery_context"], profile, values)
            if isinstance(previous, bool) or not isinstance(previous, (int, float)) or not math.isfinite(previous):
                raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, RecursionError, OverflowError):
            raise ClientError("Resume checkpoint is invalid; preserve it for recovery") from None
    supplied = getattr(ns, 'context_file', None)
    if supplied is not None:
        context = load_recovery_context(supplied, profile, values)
    instant = time.time()
    if previous is not None and instant < previous:
        raise ClientError('Resume clock precedes the checkpoint; preserve state for recovery')
    snapshot = {**binding, "observed_at": instant, "last_semantic_ack_cursor": int(profile.get("ack_cursor", 0))}
    if context is not None:
        snapshot['recovery_context'] = context
    if len((json.dumps(snapshot, indent=2, ensure_ascii=False) + chr(10)).encode()) > 16384:
        raise ClientError('Recovery checkpoint exceeds its bound; prior state and input retained')
    controls, inbox = None, None
    if presence.get("state") not in {"stopped", "joining"}:
        controls = foreground_check_in(values, identity_path(values, profile["identity"]).resolve(), profile)
        held = (presence.get("state") == "stopping" or presence.get("work_paused") is True
                or controls.get("ack_required") is True
                or any(item.get("kind") in {"stop_end_turn", "pause_work"} for item in controls.get("controls", [])))
        if not held:
            if compact and profile.get('message_lifecycle_version') != 1:
                raise ClientError('Compact resume requires matching message lifecycle support; no history state inferred')
            after = int(profile.get("ack_cursor", 0))
            retained, scanned, omitted = [], 0, 0
            retired_gap: bool | None = False
            for page_index in range(5 if compact else 1):
                query = urlencode({"identity": profile["identity"], "after": after, "limit": 20})
                inbox = scoped_api(values, "/v1/inbox?" + query, profile)
                if (not isinstance(inbox.get("messages"), list) or len(inbox["messages"]) > 20
                        or type(inbox.get("next_cursor")) is not int or type(inbox.get("has_more")) is not bool
                        or inbox['next_cursor'] < after or inbox['has_more'] and inbox['next_cursor'] <= after):
                    raise ClientError("Resume inbox response is invalid; checkpoint retained")
                if compact and (any(not isinstance(m, dict) or type(m.get('cursor')) is not int
                        or not after < m['cursor'] <= inbox['next_cursor'] for m in inbox['messages'])
                        or [m['cursor'] for m in inbox['messages']] != sorted({m['cursor'] for m in inbox['messages']})):
                    raise ClientError('Resume inbox ordering is invalid; checkpoint retained')
                gap = inbox.get('retired_history')
                if gap is True or retired_gap is True:
                    retired_gap = True
                elif gap is not False:
                    retired_gap = None
                scanned += len(inbox['messages'])
                for message in inbox['messages']:
                    lifecycle = message.get('lifecycle')
                    if compact and isinstance(lifecycle, dict) and lifecycle.get('state') in {'handled', 'expired_unclaimed'}:
                        omitted += 1
                    else:
                        retained.append(message)
                after = inbox['next_cursor']
                if not inbox['has_more']:
                    break
            if compact:
                inbox = {**inbox, 'messages': retained[-20:], 'retired_history': retired_gap, 'resume_projection': {
                    'view': 'nonterminal_within_scanned_window', 'pages_scanned': page_index + 1,
                    'messages_scanned': scanned, 'terminal_omitted': omitted,
                    'earlier_nonterminal_not_shown': max(0, len(retained) - 20),
                    'scan_limit_reached': inbox['has_more'],
                    'presentation_and_semantic_ack_advanced': False}}
    write_resume_checkpoint(path, snapshot, checkpoint_baseline)
    result = {"scope": binding, "presence": presence, "controls": controls,
              "unmonitored_gap": {"from": previous, "to": instant, "continuous_monitoring": False,
                                  "reason": "unknown_prior_checkpoint" if previous is None else "between_resume_observations"},
              "mailbox": inbox, "read_is_not_acknowledgement": True,
              "host_execution_or_wake": "not_requested", "semantic_ack_cursor": snapshot["last_semantic_ack_cursor"]}
    if context is not None:
        due = context['value']['next_update_due_at']
        result['recovery_context'] = {**context, 'evidence_role': 'consumer_reported_context_only',
            'deadline_state': 'unknown' if due is None else 'past_due' if due < instant else 'upcoming',
            'execution_authority': False, 'automatic_update_or_retry': False}
    print(json.dumps(result, indent=2))
    return 0
