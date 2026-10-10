"""Consistent online SQLite backup, including committed WAL data. Never overwrites files."""
import argparse
from pathlib import Path
import sqlite3
from sqlalchemy.engine import make_url
from app.db import URL

def backup(destination):
    source=Path(make_url(URL).database).resolve()
    target=Path(destination).resolve()
    if source==target: raise ValueError('Backup destination must differ from the live database')
    if not source.exists(): raise ValueError('Source database does not exist')
    target.parent.mkdir(parents=True,exist_ok=True)
    # Exclusive create prevents overwriting an existing backup.
    with target.open('xb'): pass
    try:
        with sqlite3.connect(f'{source.as_uri()}?mode=ro',uri=True) as original, sqlite3.connect(target) as copied:
            original.backup(copied)
            if copied.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('Backup integrity check failed')
        return target
    except Exception:
        target.unlink(missing_ok=True)
        raise

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination',help='New backup file path; existing files are never overwritten')
    args=parser.parse_args()
    print(f'Verified backup: {backup(args.destination)}')
