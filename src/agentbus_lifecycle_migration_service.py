"""Offline lifecycle cutover preserves legacy custody and refuses silent replay."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

from src.agentbus_announcement_lifecycle_service import AnnouncementLifecycle
from src.agentbus_channel_migration_service import database_lease
from src.agentbus_message_lifecycle_service import LifecyclePolicy, MessageLifecycleStore
from src.agentbus_retention_service import RetentionStore
from src.agentbus_transaction_service import atomic


def _read_inventory(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    required = {'metadata', 'messages', 'bus_channels', 'chat_channels', 'local_delivery_obligations'}
    if not required <= tables:
        raise ValueError('Lifecycle cutover requires the channel-isolated database baseline')
    marker = db.execute("SELECT value FROM metadata WHERE key='message_lifecycle_version'").fetchone()
    if marker and marker[0] != '1':
        raise ValueError('Unsupported lifecycle schema requires owner recovery')
    channels = db.execute('SELECT channel,inbox_id,next_position FROM bus_channels ORDER BY channel').fetchall()
    if not 1 <= len(channels) <= 16:
        raise ValueError('Lifecycle channel registry is empty or outside its bound')
    if db.execute('SELECT 1 FROM messages WHERE channel NOT IN (SELECT channel FROM bus_channels) LIMIT 1').fetchone():
        raise ValueError('Unexplained message channel data requires recovery')
    if db.execute('PRAGMA foreign_key_check').fetchone():
        raise ValueError('Existing custody has foreign key violations')
    pinned_where = ' WHERE cursor IN (SELECT cursor FROM lifecycle_payloads WHERE legacy_unknown=1)' if marker else ''
    pinned = db.execute('SELECT COALESCE(sum(length(CAST(payload AS BLOB))),0) FROM messages' + pinned_where).fetchone()[0]
    return {'already_enabled': bool(marker), 'messages': db.execute('SELECT count(*) FROM messages').fetchone()[0],
            'local_requests': db.execute('SELECT count(*) FROM local_delivery_obligations').fetchone()[0],
            'channels': [dict(r) for r in channels],
            'transport_states': dict(db.execute('SELECT state,count(*) FROM local_delivery_obligations GROUP BY state')),
            'legacy_pickup_classification': 'unknown_pinned_no_automatic_expiry_or_handled_inference',
            'legacy_pinned_payload_bytes':pinned}


def migrate_lifecycle(path, *, policy=None, apply=False, backup_path=None, instant=None):
    path = Path(path)
    supplied_policy = policy
    policy = policy or LifecyclePolicy()
    if path.is_symlink() or not path.is_file():
        raise ValueError('Lifecycle migration requires an existing non-symlink database')
    with database_lease(path):
        source = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        source.row_factory = sqlite3.Row
        try:
            report = _read_inventory(source)
            if report['already_enabled'] and supplied_policy is None:
                prior = source.execute("SELECT value FROM metadata WHERE key='message_lifecycle_policy'").fetchone()
                if prior is None:
                    raise ValueError('Migrated policy custody is missing')
                policy = LifecyclePolicy(**json.loads(prior[0]))
            page_size = source.execute('PRAGMA page_size').fetchone()[0]
            allocated = max(path.stat().st_size, source.execute('PRAGMA page_count').fetchone()[0] * page_size)
            reusable = source.execute('PRAGMA freelist_count').fetchone()[0] * page_size
            sidecars = sum(candidate.stat().st_size for suffix in ('-wal','-shm')
                           if (candidate := Path(str(path) + suffix)).exists())
            report['storage'] = {'physical_bytes':allocated + sidecars, 'reusable_bytes':reusable,
                'accounted_bytes':allocated - reusable + sidecars, 'budget_bytes':policy.storage_bytes,
                'admission_headroom_bytes':max(0, policy.storage_bytes - allocated + reusable - sidecars),
                'scope':'occupied_database_pages_WAL_SHM_archives_included_not_backups_clients_or_OS_quota'}
            report.update(applied=False, policy=vars(policy), legacy_allocation_after_cutover='closed',
                          old_binary='channel compatibility view plus new connection writer guard',
                          rollback='restore verified backup only before new accepted writes; otherwise repair forward')
            if not apply:
                return report
            if report['already_enabled']:
                prior = source.execute("SELECT value FROM metadata WHERE key='message_lifecycle_policy'").fetchone()
                if prior is None or json.loads(prior[0]) != vars(policy):
                    raise ValueError('Already migrated policy differs; do not implicitly recut epochs')
                report['applied'] = True
                return report
            if backup_path is None:
                raise ValueError('Apply requires an explicit new verified backup path')
            backup = Path(backup_path)
            if backup.resolve() == path.resolve() or backup.is_symlink():
                raise ValueError('Backup must be a distinct new file')
            descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
            os.close(descriptor)
            destination = sqlite3.connect(backup)
            try:
                source.backup(destination)
                if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup integrity verification failed')
                if destination.execute('SELECT count(*) FROM messages').fetchone()[0] != report['messages']:
                    raise ValueError('Backup message inventory differs')
            finally:
                destination.close()
            report['backup'] = str(backup.resolve())
            report['backup_sha256'] = hashlib.sha256(backup.read_bytes()).hexdigest()
        finally:
            source.close()
        writer = sqlite3.connect(path)
        writer.row_factory = sqlite3.Row
        writer.execute('PRAGMA foreign_keys=ON')
        try:
            with atomic(writer):
                owner = SimpleNamespace(_db=writer, _lock=threading.RLock())
                lifecycle = MessageLifecycleStore(owner)
                owner.lifecycle = lifecycle
                RetentionStore(owner)
                AnnouncementLifecycle(owner)
                lifecycle.activate(policy, instant=instant, legacy=True)
                if writer.execute('PRAGMA foreign_key_check').fetchone():
                    raise ValueError('Migration did not retain reference custody')
                after = _read_inventory(writer)
                if after['messages'] != report['messages'] or after['channels'] != report['channels']:
                    raise ValueError('Lifecycle migration changed message or channel identities')
            report['applied'] = True
            return report
        finally:
            writer.close()


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description='Stopped-service lifecycle cutover; dry-run unless --apply')
    parser.add_argument('--db-path', required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--backup')
    args = parser.parse_args()
    try:
        report = migrate_lifecycle(args.db_path, apply=args.apply, backup_path=args.backup)
    except (ValueError, sqlite3.Error, OSError) as exc:
        parser.exit(1, f'Lifecycle migration refused: {exc}\n')
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
