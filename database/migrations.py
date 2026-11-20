"""Schema migrations that cannot be expressed as CREATE ... IF NOT EXISTS or ADD COLUMN.

v5 rebuilds `contacts`: SQLite cannot change a CHECK constraint in place, and the role list grew.
The rebuild keeps every row and its id (emails.contact_id points at it) and makes a backup first.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path

OLD_CONTACTS = "contacts_v4"
_SOURCE_MAP = {"manual": "manual", "seed": "seed", "hunter": "hunter"}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def name_norm(full_name: str) -> str:
    """'Dr. Nithin  Kamath' -> 'nithin kamath'."""
    text = re.sub(r"^(dr|mr|mrs|ms|prof)\.?\s+", "", (full_name or "").strip().lower())
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text).split())


def contacts_need_rebuild(conn: sqlite3.Connection) -> bool:
    return _table_exists(conn, "contacts") and "name_norm" not in _columns(conn, "contacts")


def backup(db_path: Path, label: str) -> Path | None:
    """Copy the database file before a destructive migration (skipped for in-memory DBs)."""
    if str(db_path) == ":memory:" or not Path(db_path).exists():
        return None
    target = Path(db_path).with_name(f"{Path(db_path).stem}.backup-{label}{Path(db_path).suffix}")
    if not target.exists():
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    return target


def begin_contacts_rebuild(conn: sqlite3.Connection) -> None:
    """Step 1 (before the schema script): move the old table aside without touching FKs in `emails`."""
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")   # keep emails.contact_id REFERENCES contacts
    conn.execute(f"ALTER TABLE contacts RENAME TO {OLD_CONTACTS}")
    conn.execute("PRAGMA legacy_alter_table = OFF")
    conn.execute("DROP INDEX IF EXISTS idx_contacts_company")
    conn.commit()


def finish_contacts_rebuild(conn: sqlite3.Connection) -> int:
    """Step 2 (after the schema script created the new table): copy rows with their ids, then drop the old one."""
    if not _table_exists(conn, OLD_CONTACTS):
        return 0
    cur = conn.execute(f"SELECT * FROM {OLD_CONTACTS}")
    names = [d[0] for d in cur.description]
    rows = [dict(zip(names, row)) for row in cur.fetchall()]
    existing = {r[0] for r in conn.execute("SELECT id FROM contacts")}
    copied = 0
    for r in rows:
        if r["id"] in existing:
            continue
        email = r.get("email")
        conn.execute(
            """INSERT INTO contacts (id, company_id, full_name, first_name, name_norm, role_title, role_category,
                   email, email_type, email_confidence, email_status, linkedin_url, source, verification_status,
                   do_not_contact, created_at, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unverified', ?, ?, ?, ?)""",
            (r["id"], r["company_id"], r["full_name"], r.get("first_name"), name_norm(r["full_name"]) or f"id{r['id']}",
             r.get("role_title"), r.get("role_category") or "other", email, "unverified" if email else "none",
             r.get("email_confidence"), r.get("email_status") or "unverified", r.get("linkedin_url"),
             _SOURCE_MAP.get(r.get("source") or "manual", "manual"), r.get("do_not_contact") or 0,
             r.get("created_at"), r.get("created_at"), r.get("created_at")),
        )
        copied += 1
    conn.execute(f"DROP TABLE {OLD_CONTACTS}")
    problems = conn.execute("PRAGMA foreign_key_check").fetchall()
    if problems:
        raise RuntimeError(f"foreign key check failed after contacts migration: {problems[:5]}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    return copied
