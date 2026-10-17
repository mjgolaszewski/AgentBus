"""Explicit offline database and disposable profile-scope migration commands."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from src.agentbus_transport_client import ClientError


def migrate_database(ns,values):
    database_path = os.path.abspath(values['AGENTBUS_DB_PATH'])
    argv=[sys.executable,'-m','src.agentbus_channel_migration_service','--db-path',database_path,
        '--legacy-channel',ns.legacy_channel,'--channels',ns.channels]
    if ns.apply:
        argv.append('--apply')
    return subprocess.run(argv,cwd=Path(__file__).resolve().parents[1],check=False).returncode


def migrate_profile(ns,values):
    from src.agentbus_client import atomic_json, checked_profile, identity_path, resolve_identity
    from src.agentbus_codex_wake_client import _state_path, _worker_path, _worker_pid, workspace_status
    from src.agentbus_poll_client import _default_spool, _worker_lock, spool_path
    from src.agentbus_scope_client import FIELDS, scope
    from src.agentbus_wake_inbox_client import _fresh, state_path
    identity=resolve_identity(ns.identity)
    profile,_=checked_profile(values,identity)
    path=identity_path(values,identity)
    if workspace_status(values)["enabled"] or _worker_pid(_worker_path(values)) is not None:
        raise ClientError("Disable and drain the wake worker before scope migration")
    binding_path = _state_path(values, profile["chat_id"])
    binding_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    wake_descriptor = os.open(binding_path.with_suffix(".lock"), os.O_CREAT|os.O_RDWR|getattr(os,"O_NOFOLLOW",0), 0o600)
    descriptor=os.open(_worker_lock(path),os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:
            fcntl.flock(wake_descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ClientError('Stop profile polling and reconcile active wake attempts before scope migration') from None
        expected=scope(profile)
        targets=[(spool_path(path),_default_spool(profile)),
            (state_path(values,profile['chat_id']),None)]
        # Validate every old file before changing any projection.
        pending=[]
        for target,fresh in targets:
            if target.is_symlink():
                raise ClientError('Refusing symlinked projection')
            if not target.exists():
                continue
            raw=target.read_bytes()
            try:
                old=json.loads(raw)
            except (ValueError, UnicodeError):
                raise ClientError("Unreadable projection; evidence retained") from None
            if not isinstance(old, dict):
                raise ClientError("Invalid projection; evidence retained")
            if any(key in old and old[key]!=expected[key] for key in FIELDS):
                raise ClientError('Foreign projection scope; evidence retained')
            if all(old.get(key)==expected[key] for key in FIELDS):
                continue
            if fresh is None:
                generation = old.get('binding_generation')
                if type(generation) is not int or generation < 1:
                    raise ClientError('Invalid wake generation; evidence retained')
                fresh = _fresh(profile,generation)
            pending.append((target,raw,fresh))
        for target,raw,fresh in pending:
            backup=target.with_name(target.name+'.legacy-scope-'+hashlib.sha256(raw).hexdigest()[:16])
            if backup.is_symlink():
                raise ClientError("Refusing symlinked scope backup")
            if backup.exists() and backup.read_bytes()!=raw:
                raise ClientError('Scope backup conflicts; evidence retained')
            if not backup.exists():
                fd=os.open(backup,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
                with os.fdopen(fd,'wb') as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            atomic_json(target,fresh)
        print(json.dumps({'identity':identity,'projections_rebuilt':len(pending),'ack_cursor':profile['ack_cursor'],
            'wake_binding':'unchanged; explicit bind required before any use'}))
    finally:
        os.close(descriptor)
        os.close(wake_descriptor)
    return 0
