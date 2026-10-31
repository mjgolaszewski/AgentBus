"""Private immutable request staging before any lifecycle send can reach the network."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlencode
from uuid import NAMESPACE_URL, UUID, uuid5

from src.agentbus_scope_client import scope, scoped_api
from src.agentbus_transport_client import ClientError


def request_stage(values, profile, key, payload, epoch, *, existing=False):
    binding = scope(profile)
    if binding['service_url'] != values.get('AGENTBUS_URL'):
        raise ClientError('Request stage service differs from the joined profile')
    root = Path(values['AGENTBUS_STATE_DIR']).expanduser()
    directory = root / 'send-requests'
    if any(path.is_symlink() for path in (root, *root.parents, directory)):
        raise ClientError('Request stage directory cannot use symbolic links')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.stat().st_mode & 0o077:
        raise ClientError('Request stage directory must be private to its owner')
    name = hashlib.sha256(json.dumps([binding, key], sort_keys=True).encode()).hexdigest()
    path = directory / (name + '.json')
    authored = json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=True)
    fingerprint = hashlib.sha256(authored.encode()).hexdigest()
    expected = {'scope':binding, 'request_id':key, 'payload':payload, 'fingerprint':fingerprint}
    if not path.exists() and existing:
        raise ClientError('Original lifecycle request stage is missing; omit --message-id for a new first send. Never replace the epoch or identity of an uncertain prior request')
    if not path.exists():
        if not isinstance(epoch, str) or not re.fullmatch('[0-9a-f]{64}', epoch):
            raise ClientError('No current service-issued request epoch is available')
        content = json.dumps({**expected, 'epoch':epoch}, sort_keys=True).encode()
        if len(content) > 65536:
            raise ClientError('Request stage exceeds its bounded size')
        temporary = directory / ('.stage-' + secrets.token_hex(16))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            try:
                os.link(temporary, path, follow_symlinks=False)
            except FileExistsError:
                pass  # A concurrent exact submit must validate the winning immutable stage.
            parent = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            temporary.unlink(missing_ok=True)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, 'rb') as source:
            if os.fstat(source.fileno()).st_mode & 0o077:
                raise ClientError('Request stage is not private; evidence retained')
            raw = source.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        retained = json.loads(raw)
        if any(retained.get(field) != value for field, value in expected.items()):
            raise ValueError()
        if not isinstance(retained.get('epoch'), str) or not re.fullmatch('[0-9a-f]{64}', retained['epoch']):
            raise ValueError()
    except (OSError, ValueError, TypeError, AttributeError):
        raise ClientError('Request stage is invalid or differs from this request; evidence retained') from None
    return retained['epoch']


def receipt_identity(profile, cursor):
    return str(uuid5(NAMESPACE_URL, 'agentbus:pickup:' + json.dumps([scope(profile), cursor], sort_keys=True)))


def message_command(ns, values, profile, *, api_call=None):
    binding = scope(profile)
    if ns.cursor < 1:
        raise ClientError('Message cursor must be positive')
    path = f'/v1/messages/{ns.cursor}/'
    if ns.command == 'message-status':
        return scoped_api(values, path + 'outcome', profile, api_call=api_call)
    token = ns.receipt_id or receipt_identity(profile, ns.cursor)
    try:
        if str(UUID(token)) != token:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise ClientError('Pickup receipt must be a canonical UUID') from None
    return scoped_api(values, path + ns.command, profile,
        {'session_id':binding['session_id'], 'receipt_id':token}, api_call=api_call)


def filter_wake_refs(values, profile, refs, state, *, api_call=None):
    """Revalidate bounded cached references; terminal projection cleanup is never an ACK."""
    if profile.get('message_lifecycle_version') != 1:
        return refs
    retired = state.get('lifecycle_terminal_cursors', [])
    if not isinstance(retired, list) or len(retired) > 4096 or any(type(cursor) is not int or cursor < 1 for cursor in retired):
        raise ClientError('Lifecycle wake projection is invalid; evidence retained')
    ack = int(profile.get('ack_cursor', 0))
    retired = {cursor for cursor in retired if cursor > ack}
    selected = []
    recovery = set()
    examined = 0
    pending_controls = None
    for ref in refs:
        kind, _, raw = ref.partition(':')
        if kind == 'CONTROL':
            if pending_controls is None:
                binding = scope(profile)
                presence = scoped_api(values, f"/v1/sessions/{binding['session_id']}/self-presence", profile,
                                      api_call=api_call, timeout_seconds=3)
                if (presence.get('session_id') != binding['session_id'] or presence.get('chat_id') != binding['chat_id']
                        or not isinstance(presence.get('outstanding_controls'), list)):
                    raise ClientError('Control wake scope could not be revalidated')
                pending_controls = {item['control_id'] for item in presence['outstanding_controls']
                                    if isinstance(item, dict) and isinstance(item.get('control_id'), str)}
            if raw in pending_controls:
                selected.append(ref)
        elif kind != 'MESSAGE':
            selected.append(ref)
        else:
            if not raw.isdecimal() or int(raw) < 1:
                raise ClientError('Lifecycle wake reference is invalid')
            cursor = int(raw)
            if cursor in retired:
                continue
            if examined >= 32:
                break
            examined += 1
            result = scoped_api(values, f'/v1/messages/{cursor}/outcome', profile,
                                api_call=api_call, timeout_seconds=3)
            if result.get('cursor') != cursor or result.get('scope') != 'recipient':
                raise ClientError('Lifecycle wake outcome does not match the addressed reference')
            if result.get('state') in {'handled','expired_unclaimed'}:
                retired.add(cursor)
            elif result.get('state') == 'recovery_required' or result.get('lease_elapsed') is True:
                recovery.add(cursor)
            elif ((result.get('state') == 'unclaimed' and result.get('deadline_elapsed') is False)
                  or (result.get('state') == 'picked_up' and result.get('lease_elapsed') is False
                      and result.get('owned_by_this_session') is True)):
                selected.append(ref)
        if len(selected) >= 8:
            break
    if len(retired) > 4096:
        raise ClientError('Lifecycle wake projection capacity exhausted; evidence retained')
    state['lifecycle_terminal_cursors'] = sorted(retired)
    state['lifecycle_recovery_required'] = sorted(recovery)
    return selected


def pickup_wake_refs(values, profile, refs, *, api_call=None):
    if profile.get('message_lifecycle_version') != 1:
        return
    from types import SimpleNamespace
    for ref in refs:
        if ref.startswith('MESSAGE:'):
            command = SimpleNamespace(command='pickup', cursor=int(ref.partition(':')[2]), receipt_id=None)
            result = message_command(command, values, profile, api_call=api_call)
            if result.get('state') != 'picked_up':
                raise ClientError('Lifecycle work changed before host submission; no turn submitted')


def acknowledge_handled(values, profile, through, *, api_call=None):
    """Commit semantic receipts before allowing local cursor or presentation retirement."""
    if profile.get('message_lifecycle_version') != 1:
        return {'through':through}
    binding = scope(profile)
    after = int(profile['ack_cursor'])
    counts = {'handled':0, 'already_handled':0, 'expired_unclaimed':0, 'legacy_unknown':0}
    for _ in range(32):
        if after >= through:
            break
        page = scoped_api(values, '/v1/inbox?' + urlencode({'identity':profile['identity'], 'after':after, 'limit':100}),
                          profile, api_call=api_call)
        messages, next_cursor, has_more = page.get('messages'), page.get('next_cursor'), page.get('has_more')
        if not isinstance(messages, list) or len(messages) > 100 or type(next_cursor) is not int or type(has_more) is not bool:
            raise ClientError('Lifecycle acknowledgement page is invalid; local state retained')
        receipts = []
        previous = after
        for message in messages:
            if not isinstance(message, dict) or type(message.get('cursor')) is not int or message['cursor'] <= previous:
                raise ClientError('Lifecycle acknowledgement cursors are invalid; local state retained')
            previous = message['cursor']
            if previous > through:
                continue
            lifecycle = message.get('lifecycle')
            if not isinstance(lifecycle, dict):
                raise ClientError('Lifecycle outcome metadata is missing; local state retained')
            state = lifecycle.get('state')
            if state == 'legacy_unknown':
                counts['legacy_unknown'] += 1
            elif state != 'not_targeted':
                if state not in {'unclaimed','picked_up','handled','expired_unclaimed','recovery_required'}:
                    raise ClientError('Lifecycle outcome is unsupported; local state retained')
                receipts.append({'cursor':previous, 'receipt_id':receipt_identity(profile, previous)})
        if receipts:
            result = scoped_api(values, '/v1/receipts/handled', profile,
                {'session_id':binding['session_id'], 'receipts':receipts}, api_call=api_call)
            if (result.get('session_id') != binding['session_id']
                    or any(type(result.get(key)) is not int or result[key] < 0 for key in ('handled','already_handled','expired_unclaimed'))
                    or sum(result[key] for key in ('handled','already_handled','expired_unclaimed')) != len(receipts)):
                raise ClientError('Server handling receipt is invalid; local state retained')
            for key in ('handled','already_handled','expired_unclaimed'):
                counts[key] += result[key]
        if next_cursor < after or (has_more and next_cursor <= after):
            raise ClientError('Lifecycle acknowledgement page did not advance; local state retained')
        after = through if not has_more else min(through, next_cursor)
    return {'through':after, 'requested_through':through, 'more':after < through, 'outcomes':counts}
