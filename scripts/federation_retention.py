"""Custodian-only offline preview/apply; no provider, credential or service changes."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.agentbus_channel_migration_service import database_lease
from src.agentbus_federation_retirement_service import FederationRetirement


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    database = args.database.expanduser().resolve(strict=True)
    identity = database.stat()
    if not database.is_file() or args.database.is_symlink():
        parser.error('Database must be an existing regular file')
    with database_lease(database):
        db = sqlite3.connect(database.as_uri() + ('?mode=rw' if args.apply else '?mode=ro'), uri=True)
        db.row_factory = sqlite3.Row
        try:
            current = database.stat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise ValueError('Database identity changed before lease acquisition')
            db.execute('PRAGMA foreign_keys=ON')
            owner = FederationRetirement(db)
            scope = {'device':identity.st_dev, 'inode':identity.st_ino}
            if args.apply:
                descriptor = os.open(args.plan, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'rb') as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_size > 262144:
                        raise ValueError('Preview file is not an eligible bounded regular file')
                    raw = source.read(262145)
                    if len(raw) > 262144:
                        raise ValueError('Preview file exceeds its bound')
                    plan = json.loads(raw)
                if plan['database_identity'] != scope:
                    raise ValueError('Preview belongs to another database file')
                result = owner.apply(plan['retirement'])
            else:
                result = {'database_identity':scope, 'retirement':owner.preview()}
                descriptor = os.open(args.plan, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(descriptor, 'w') as output:
                    output.write(json.dumps(result, indent=2) + '\n')
            print(json.dumps(result, indent=2))
        finally:
            db.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
