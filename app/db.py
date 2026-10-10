import os
from sqlalchemy import create_engine, event
from sqlalchemy.dialects import sqlite
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from dotenv import load_dotenv
load_dotenv()
URL = os.getenv("DATABASE_URL", "sqlite:///./dev.db")
if not URL.startswith("sqlite"):
    raise RuntimeError("This application is configured for SQLite. Use a sqlite:/// DATABASE_URL.")
_kw = {"connect_args": {"check_same_thread": False, "timeout": 15}}
engine = create_engine(URL, **_kw)
if URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def configure_sqlite(connection, _):
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=15000")
        cursor.close()

SessionLocal = sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    with SessionLocal() as s:
        yield s


def insert_fn():
    return sqlite.insert



def upsert(session, model, rows, keys, update, chunk=5000):
    """Bulk INSERT ... ON CONFLICT DO UPDATE (SQLite)."""
    for i in range(0, len(rows), chunk):
        stmt = insert_fn()(model)
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_={c: stmt.excluded[c] for c in update})
        session.execute(stmt, rows[i:i + chunk])
