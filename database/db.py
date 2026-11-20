"""SQLite connection management and schema initialisation."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 8  # v2 discovery · v3 research · v4 research · v5 contacts · v6 drafts · v7 send · v8 follow-ups

# Columns added after a table was first created: (table, column, type). Applied if missing.
_ADDED_COLUMNS = [
    ("company_research", "verified_summary", "TEXT"),
    ("company_research", "unreadable_pages", "TEXT"),
    ("company_pages", "links", "TEXT"),
    ("company_pages", "people_text", "TEXT"),
    ("emails", "outreach_type", "TEXT"),
    ("emails", "opportunity_id", "INTEGER REFERENCES opportunities (id) ON DELETE SET NULL"),
    ("emails", "facts_used", "TEXT"),
    ("emails", "checks", "TEXT"),
    ("emails", "recipient", "TEXT"),
    ("emails", "send_started_at", "TEXT"),
    ("emails", "parent_email_id", "INTEGER REFERENCES emails (id) ON DELETE CASCADE"),
    ("emails", "message_id_header", "TEXT"),
    ("emails", "replied_at", "TEXT"),
]


def _default_path() -> Path:
    from config.settings import get_settings

    return get_settings().database_path


def connect(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a connection with foreign keys on, Row results, and WAL for dashboard concurrency."""
    path = Path(db_path) if db_path is not None else _default_path()
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path: Path | str | None = None) -> Path:
    """Create tables if missing and record the schema version. Safe to call repeatedly."""
    from database import migrations

    path = Path(db_path) if db_path is not None else _default_path()
    conn = connect(path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        rebuild_contacts = migrations.contacts_need_rebuild(conn)
        if rebuild_contacts:
            conn.close()
            migrations.backup(path, f"v{current}")
            conn = connect(path)
            migrations.begin_contacts_rebuild(conn)
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        migrations.finish_contacts_rebuild(conn)   # also resumes an interrupted rebuild
        if current < SCHEMA_VERSION:
            # v1 -> v3 only add tables (handled by CREATE IF NOT EXISTS above); v4 adds columns.
            for table, column, col_type in _ADDED_COLUMNS:
                existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
    return path


@contextmanager
def get_connection(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Connection that commits on success and rolls back on error."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
