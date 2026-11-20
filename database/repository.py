"""Data-access functions. All take an open sqlite3.Connection; the caller owns the transaction."""

from __future__ import annotations

import sqlite3
from typing import Any

from database.models import (
    EMAIL_TRANSITIONS,
    MARKETS,
    REMOTE_POLICIES,
    ROLE_CATEGORIES,
    SECTORS,
    normalize_domain,
    utc_now,
)


class InvalidTransition(ValueError):
    """Raised when an email status change would break the approval workflow."""


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- activity


def log_activity(
    conn: sqlite3.Connection,
    entity_type: str,
    entity_id: int | None,
    action: str,
    details: str = "",
) -> None:
    conn.execute(
        "INSERT INTO activity_log (entity_type, entity_id, action, details) VALUES (?, ?, ?, ?)",
        (entity_type, entity_id, action, details),
    )


def recent_activity(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return _rows(conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT ?", (limit,)))


# --------------------------------------------------------------------------- companies


def add_company(
    conn: sqlite3.Connection,
    *,
    name: str,
    domain: str,
    market: str,
    sector: str = "Other",
    remote_policy: str = "unknown",
    source: str = "manual",
    **fields: Any,
) -> int:
    """Insert a company and return its id. Raises sqlite3.IntegrityError on duplicate domain."""
    if market not in MARKETS:
        raise ValueError(f"market must be one of {MARKETS}")
    if sector not in SECTORS:
        raise ValueError(f"sector must be one of {SECTORS}")
    if remote_policy not in REMOTE_POLICIES:
        raise ValueError(f"remote_policy must be one of {REMOTE_POLICIES}")
    allowed = {
        "website", "description", "country", "city", "funding_stage",
        "employee_count", "hiring_signal", "source_url",
    }
    extra = {k: v for k, v in fields.items() if k in allowed}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown company fields: {sorted(unknown)}")

    data = {
        "name": name.strip(),
        "domain": normalize_domain(domain),
        "market": market,
        "sector": sector,
        "remote_policy": remote_policy,
        "source": source,
        **extra,
    }
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO companies ({cols}) VALUES ({marks})", tuple(data.values()))
    company_id = cur.lastrowid
    log_activity(conn, "company", company_id, "created", f"{data['name']} ({data['domain']})")
    return company_id


def get_company(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    return dict(row) if row else None


def find_company_by_domain(conn: sqlite3.Connection, domain: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM companies WHERE domain = ?", (normalize_domain(domain),)
    ).fetchone()
    return dict(row) if row else None


def list_companies(
    conn: sqlite3.Connection,
    *,
    market: str | None = None,
    sector: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM companies WHERE 1=1"
    params: list[Any] = []
    for col, val in (("market", market), ("sector", sector), ("status", status)):
        if val:
            sql += f" AND {col} = ?"
            params.append(val)
    sql += " ORDER BY COALESCE(fit_score, -1) DESC, created_at DESC"
    return _rows(conn.execute(sql, params))


def update_company(conn: sqlite3.Connection, company_id: int, **fields: Any) -> None:
    allowed = {
        "name", "website", "description", "market", "sector", "country", "city",
        "remote_policy", "funding_stage", "employee_count", "hiring_signal",
        "research_summary", "fit_score", "status", "source_url",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown company fields: {sorted(unknown)}")
    if not fields:
        return
    assignments = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE companies SET {assignments}, updated_at = ? WHERE id = ?",
        (*fields.values(), utc_now(), company_id),
    )


# --------------------------------------------------------------------------- contacts


def add_contact(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    full_name: str,
    role_category: str = "other",
    **fields: Any,
) -> int:
    if role_category not in ROLE_CATEGORIES:
        raise ValueError(f"role_category must be one of {ROLE_CATEGORIES}")
    allowed = {
        "first_name", "role_title", "email", "email_confidence", "email_status", "email_type",
        "linkedin_url", "source", "source_url", "evidence", "notes", "verification_status", "opportunity_id",
    }
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown contact fields: {sorted(unknown)}")
    if fields.get("email"):
        fields["email"] = fields["email"].strip().lower()
        fields.setdefault("email_type", "unverified")
    from database.migrations import name_norm

    data = {"company_id": company_id, "full_name": full_name.strip(), "name_norm": name_norm(full_name),
            "role_category": role_category, **fields}
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO contacts ({cols}) VALUES ({marks})", tuple(data.values()))
    log_activity(conn, "contact", cur.lastrowid, "created", f"{data['full_name']} @ company {company_id}")
    return cur.lastrowid


def list_contacts(conn: sqlite3.Connection, company_id: int | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT ct.*, c.name AS company_name
        FROM contacts ct JOIN companies c ON c.id = ct.company_id
    """
    params: tuple = ()
    if company_id is not None:
        sql += " WHERE ct.company_id = ?"
        params = (company_id,)
    sql += " ORDER BY ct.created_at DESC"
    return _rows(conn.execute(sql, params))


# --------------------------------------------------------------------------- emails


def create_email_draft(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    contact_id: int,
    subject: str,
    body: str,
    model: str | None = None,
    kind: str = "initial",
    outreach_type: str | None = None,
    opportunity_id: int | None = None,
    facts_used: str | None = None,
    checks: str | None = None,
) -> int:
    """Drafts always start in pending_review: nothing is sent without approval."""
    cur = conn.execute(
        """INSERT INTO emails (company_id, contact_id, kind, subject, body, model, status, outreach_type,
                               opportunity_id, facts_used, checks)
           VALUES (?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, ?)""",
        (company_id, contact_id, kind, subject, body, model, outreach_type, opportunity_id, facts_used, checks),
    )
    log_activity(conn, "email", cur.lastrowid, "drafted", subject)
    return cur.lastrowid


def get_email(conn: sqlite3.Connection, email_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    return dict(row) if row else None


def list_emails(conn: sqlite3.Connection, status: str | None = None) -> list[dict[str, Any]]:
    sql = """
        SELECT e.*, c.name AS company_name, c.market, c.sector,
               ct.full_name AS contact_name, ct.email AS contact_email,
               ct.role_title, ct.role_category
        FROM emails e
        JOIN companies c ON c.id = e.company_id
        JOIN contacts ct ON ct.id = e.contact_id
    """
    params: tuple = ()
    if status:
        sql += " WHERE e.status = ?"
        params = (status,)
    sql += " ORDER BY e.created_at ASC"
    return _rows(conn.execute(sql, params))


def update_email_content(conn: sqlite3.Connection, email_id: int, *, subject: str, body: str) -> None:
    email = get_email(conn, email_id)
    if email is None:
        raise ValueError(f"email {email_id} not found")
    if email["status"] not in ("pending_review", "approved", "rejected", "failed"):
        raise InvalidTransition(f"cannot edit an email with status {email['status']!r}")
    # Editing an approved email sends it back for review, so approval always matches the content.
    new_status = "pending_review" if email["status"] == "approved" else email["status"]
    conn.execute(
        "UPDATE emails SET subject = ?, body = ?, status = ?, approved_at = NULL, updated_at = ? WHERE id = ?",
        (subject, body, new_status, utc_now(), email_id),
    )
    log_activity(conn, "email", email_id, "edited")


def set_email_status(
    conn: sqlite3.Connection,
    email_id: int,
    new_status: str,
    *,
    notes: str | None = None,
    gmail_message_id: str | None = None,
    gmail_thread_id: str | None = None,
    error: str | None = None,
) -> None:
    email = get_email(conn, email_id)
    if email is None:
        raise ValueError(f"email {email_id} not found")
    current = email["status"]
    if new_status not in EMAIL_TRANSITIONS.get(current, set()):
        raise InvalidTransition(f"cannot move email {email_id} from {current!r} to {new_status!r}")

    now = utc_now()
    updates: dict[str, Any] = {"status": new_status, "updated_at": now}
    if new_status == "approved":
        updates["approved_at"] = now
    if new_status == "sent":
        updates.update(sent_at=now, gmail_message_id=gmail_message_id, gmail_thread_id=gmail_thread_id, error=None)
    if new_status == "failed":
        updates["error"] = error
    if notes is not None:
        updates["reviewer_notes"] = notes

    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn.execute(f"UPDATE emails SET {assignments} WHERE id = ?", (*updates.values(), email_id))
    log_activity(conn, "email", email_id, new_status, notes or error or "")


# --------------------------------------------------------------------------- dashboard stats


def pipeline_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]

    return {
        "companies": count("SELECT COUNT(*) FROM companies"),
        "contacts": count("SELECT COUNT(*) FROM contacts"),
        "company_status": dict(
            conn.execute("SELECT status, COUNT(*) FROM companies GROUP BY status").fetchall()
        ),
        "company_market": dict(
            conn.execute("SELECT market, COUNT(*) FROM companies GROUP BY market").fetchall()
        ),
        "email_status": dict(
            conn.execute("SELECT status, COUNT(*) FROM emails GROUP BY status").fetchall()
        ),
    }
