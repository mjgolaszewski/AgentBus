"""Offline, explicit legacy-channel migration using the existing SQLite writer."""
from __future__ import annotations

import fcntl
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.agentbus_channel_store_service import ChannelStore
from src.agentbus_transaction_service import atomic


@contextmanager
def database_lease(path):
    path=Path(path).resolve()
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    lock=path.with_name(path.name+'.service.lock')
    descriptor=os.open(lock,os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
    try:
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Service must be stopped before offline database operations') from None
        yield
    finally:
        os.close(descriptor)


def migrate_channels(path, *, legacy_channel, channels, apply=False):
    path=Path(path)
    if path.is_symlink():
        raise ValueError("Refusing symlinked database for offline migration")
    channels=ChannelStore.validate_channels(channels)
    if legacy_channel not in channels:
        raise ValueError('Explicit legacy channel must be configured')
    with database_lease(path):
        db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            other=db.execute('SELECT DISTINCT channel FROM messages WHERE channel!=?',(legacy_channel,)).fetchall()
            if other:
                raise ValueError('Unexplained legacy channel data requires operator recovery')
            if db.execute('SELECT 1 FROM participation_sessions GROUP BY secret_sha256 HAVING count(*)>1 LIMIT 1').fetchone():
                raise ValueError('Conflicting legacy session credentials require operator recovery')
            report={'legacy_channel':legacy_channel,'channels':list(channels),
                'messages':db.execute('SELECT count(*) FROM messages').fetchone()[0],
                'identities':db.execute('SELECT count(*) FROM chat_identities').fetchone()[0],
                'applied':False}
            registry=db.execute("SELECT 1 FROM sqlite_master WHERE name='bus_channels'").fetchone()
            if registry:
                foreign=db.execute('SELECT 1 FROM chat_channels WHERE channel!=? LIMIT 1',(legacy_channel,)).fetchone()
                if foreign:
                    raise ValueError('Database already has non-legacy channel bindings; do not remigrate')
        finally:
            db.close()
        if apply:
            writer=sqlite3.connect(path)
            writer.row_factory=sqlite3.Row
            writer.execute('PRAGMA foreign_keys=ON')
            try:
                with atomic(writer):
                    registry=ChannelStore(SimpleNamespace(_db=writer,_lock=threading.RLock()))
                    registry.configure(channels,legacy_channel=legacy_channel)
                    writer.execute("INSERT OR REPLACE INTO metadata VALUES ('channel_schema_version','1')")
                report['applied']=True
            finally:
                writer.close()
        return report


def check_startup(path):
    path=Path(path)
    if not path.exists():
        return
    db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        populated=any(db.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in ('messages','chat_identities') if table in tables)
        configured='bus_channels' in tables and db.execute('SELECT 1 FROM bus_channels LIMIT 1').fetchone()
        if populated and not configured:
            raise ValueError('Legacy database requires explicit offline channel migration before service startup')
    finally:
        db.close()


def main():
    import argparse
    import json
    parser=argparse.ArgumentParser(description='Offline explicit channel migration; service must be stopped')
    parser.add_argument('--db-path',required=True)
    parser.add_argument('--legacy-channel',required=True)
    parser.add_argument('--channels',required=True)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:
        result=migrate_channels(args.db_path,legacy_channel=args.legacy_channel,
            channels=tuple(c.strip() for c in args.channels.split(',')),apply=args.apply)
    except (ValueError,sqlite3.Error,OSError) as exc:
        parser.exit(1,f'Channel migration rejected: {exc}\n')
    print(json.dumps(result,sort_keys=True))


if __name__=='__main__':
    main()
