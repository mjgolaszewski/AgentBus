"""Client-owned profile snapshots and atomic, conflict-aware persistence."""
from __future__ import annotations

import fcntl
import json
import os
from copy import deepcopy

from src.agentbus_transport_client import ClientError


class Profile(dict):
    """Private in-memory read snapshot; baseline never enters serialized state."""

    def __init__(self, values, *, baseline=None, source_path=None):
        super().__init__(values)
        self.baseline = deepcopy(values if baseline is None else baseline)
        self.source_path = source_path


def _authority(profile):
    session = profile.get('participation') or {}
    if not isinstance(session, dict):
        raise ClientError('Invalid participation state; recovery evidence retained')
    return tuple(profile.get(key) for key in ('identity', 'service_url', 'channel', 'inbox_id', 'chat_id')) + (
        session.get('session_id'), session.get('session_secret'), bool(session.get('stopped')))


def save_profile(values, profile, *, reset_cursors=False):
    from src.agentbus_client import atomic_json, identity_path, now

    path = identity_path(values, profile['identity'])
    if path.is_symlink():
        raise ClientError('Refusing symlinked AgentBus profile')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (path.parent / f'.{path.name}.lock').open('a') as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        result = deepcopy(dict(profile))
        if isinstance(profile, Profile) and not path.exists():
            if profile.source_path is None or profile.source_path == path:
                raise ClientError('Profile disappeared; enrollment recovery is required; evidence retained')
            try:
                legacy = json.loads(profile.source_path.read_text())
                if _authority(legacy) != _authority(profile.baseline):
                    raise ValueError('legacy authority changed')
            except (OSError, ValueError, AttributeError):
                raise ClientError('Legacy profile changed; recovery evidence retained') from None
        if path.exists():
            try:
                current = json.loads(path.read_text())
                if not isinstance(current, dict):
                    raise ValueError('profile must be an object')
            except (OSError, ValueError):
                raise ClientError('Cannot safely update unreadable AgentBus profile') from None
            baseline = getattr(profile, 'baseline', profile)
            if _authority(current) != _authority(baseline):
                raise ClientError('Profile session authority changed; reload before updating; evidence retained')
            if not isinstance(profile, Profile) and any(
                    current.get(key) != profile.get(key) for key in
                    ('participation_pending', 'participation_rotation_pending')):
                raise ClientError('Profile recovery intent changed; reload; evidence retained')
            if baseline.get('inbox_id') is not None and any(
                    baseline.get(key) != profile.get(key) for key in
                    ('service_url', 'channel', 'inbox_id', 'chat_id')):
                raise ClientError('Profile persistence cannot switch channel or service membership')
            if isinstance(profile, Profile):
                result = deepcopy(current)
                absent = object()
                for key in set(baseline) | set(profile):
                    if key in {'updated_at', 'ack_cursor', 'observed_cursor'}:
                        continue
                    before, after = baseline.get(key, absent), profile.get(key, absent)
                    if after == before:
                        continue
                    latest = current.get(key, absent)
                    if latest != before and latest != after:
                        raise ClientError('Concurrent profile update conflicts; reload; evidence retained')
                    if after is absent:
                        result.pop(key, None)
                    else:
                        result[key] = deepcopy(after)
            for field in ('ack_cursor', 'observed_cursor'):
                incoming, existing = profile.get(field, 0), current.get(field, 0)
                if type(incoming) is not int or type(existing) is not int or min(incoming, existing) < 0:
                    raise ClientError('Cannot safely update invalid AgentBus cursors')
                result[field] = incoming if reset_cursors else max(incoming, existing)
        result['updated_at'] = now()
        atomic_json(path, result)
        profile.clear()
        profile.update(result)
        if isinstance(profile, Profile):
            profile.baseline = deepcopy(result)
            profile.source_path = path
