"""Contacts: de-duplication, evidence history, protected merges, scoring and discovery runs."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from database.migrations import name_norm
from database.models import EMAIL_TYPES, ROLE_CATEGORIES, utc_now
from database.repository import log_activity

EMAIL_STRENGTH = {"none": 0, "unverified": 1, "hunter": 2, "public_verified": 3}
AUTOMATED_SOURCES = ("website", "listing", "hunter")
USER_SOURCES = ("manual", "csv", "linkedin", "google")


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


def _linkedin_key(url: str | None) -> str | None:
    if not url:
        return None
    u = url.lower().split("?")[0].rstrip("/")
    return u.split("linkedin.com", 1)[-1] if "linkedin.com" in u else u


def get_contact(conn: sqlite3.Connection, contact_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    return dict(row) if row else None


def find_duplicate(conn: sqlite3.Connection, company_id: int, full_name: str, email: str | None = None,
                   linkedin_url: str | None = None) -> dict[str, Any] | None:
    """Same company and same normalised name, email, or LinkedIn profile."""
    rows = _rows(conn.execute("SELECT * FROM contacts WHERE company_id = ?", (company_id,)))
    key, li = name_norm(full_name), _linkedin_key(linkedin_url)
    for r in rows:
        if r["name_norm"] == key or (email and r["email"] == email.lower()) or \
                (li and _linkedin_key(r["linkedin_url"]) == li):
            return r
    return None


def add_evidence(conn: sqlite3.Connection, contact_id: int, source: str, source_url: str | None,
                 evidence: str | None, role_title: str | None = None, email: str | None = None) -> None:
    conn.execute(
        "INSERT INTO contact_evidence (contact_id, source, source_url, evidence, role_title, email) "
        "VALUES (?, ?, ?, ?, ?, ?)", (contact_id, source, source_url, evidence, role_title, email))


def evidence_for(conn: sqlite3.Connection, contact_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in contact_ids}
    if contact_ids:
        marks = ", ".join("?" for _ in contact_ids)
        for r in conn.execute(f"SELECT * FROM contact_evidence WHERE contact_id IN ({marks}) ORDER BY id",
                              contact_ids):
            out[r["contact_id"]].append(dict(r))
    return out


def upsert_contact(conn: sqlite3.Connection, company_id: int, *, full_name: str, role_title: str | None,
                   role_category: str, source: str, source_url: str | None, evidence: str | None,
                   email: str | None = None, email_type: str = "none", email_confidence: int | None = None,
                   email_status: str | None = None, linkedin_url: str | None = None,
                   opportunity_id: int | None = None, notes: str | None = None,
                   verification_status: str | None = None) -> tuple[int, str]:
    """Insert or merge a contact. Returns (id, 'created' | 'updated' | 'unchanged').

    Rules: every observation is kept in contact_evidence. On merge, empty fields are filled; an email is
    replaced only by a STRONGER type, and never on a manually verified or user-entered contact; user
    entries (manual/csv) may update their own records.
    """
    if role_category not in ROLE_CATEGORIES:
        raise ValueError(f"role_category must be one of {ROLE_CATEGORIES}")
    if email_type not in EMAIL_TYPES:
        raise ValueError(f"email_type must be one of {EMAIL_TYPES}")
    email = email.strip().lower() if email else None
    if not email:
        email_type = "none"
    now = utc_now()
    automated = source in AUTOMATED_SOURCES
    existing = find_duplicate(conn, company_id, full_name, email, linkedin_url)

    if existing is None:
        status = verification_status or ("evidence_found" if automated and evidence else "unverified")
        cur = conn.execute(
            """INSERT INTO contacts (company_id, full_name, first_name, name_norm, role_title, role_category, email,
                   email_type, email_confidence, email_status, linkedin_url, source, source_url, evidence,
                   opportunity_id, verification_status, notes, discovered_at, last_verified_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (company_id, full_name.strip(), full_name.split()[0], name_norm(full_name), role_title, role_category,
             email, email_type, email_confidence, email_status or "unverified", linkedin_url, source, source_url,
             evidence, opportunity_id, status, notes, now, now if status != "unverified" else None, now))
        contact_id = cur.lastrowid
        add_evidence(conn, contact_id, source, source_url, evidence, role_title, email)
        log_activity(conn, "contact", contact_id, "discovered", f"{full_name} ({role_title}) via {source}")
        return contact_id, "created"

    contact_id = existing["id"]
    add_evidence(conn, contact_id, source, source_url, evidence, role_title, email)
    protected = existing["verification_status"] == "manually_verified" or \
        (automated and existing["source"] in USER_SOURCES)
    updates: dict[str, Any] = {}

    def fill(col: str, value: Any) -> None:
        if value not in (None, "") and existing.get(col) in (None, "", "other"):
            updates[col] = value

    placeholder = '("founded by")'
    if (automated and not protected and role_title and placeholder in (existing.get("role_title") or "")
            and placeholder not in role_title):
        # An explicitly published title beats the generic "founded by" sentence.
        updates.update(role_title=role_title, role_category=role_category, source_url=source_url, evidence=evidence)
    fill("role_title", role_title)
    fill("role_category", role_category if role_category != "other" else None)
    fill("linkedin_url", linkedin_url)
    fill("opportunity_id", opportunity_id)
    fill("source_url", source_url)
    fill("evidence", evidence)
    if email:
        stronger = EMAIL_STRENGTH[email_type] > EMAIL_STRENGTH.get(existing["email_type"], 0)
        if not existing["email"] or (stronger and not protected):
            updates.update(email=email, email_type=email_type, email_confidence=email_confidence,
                           email_status=email_status or "unverified")
    if not automated and not protected:
        # The user's own newer entry wins over their earlier one.
        for col, value in (("role_title", role_title), ("notes", notes)):
            if value:
                updates[col] = value
    if automated and evidence and existing["verification_status"] == "unverified":
        updates["verification_status"] = "evidence_found"
    if automated and evidence and existing["verification_status"] != "manually_verified":
        updates["last_verified_at"] = now
    if verification_status == "manually_verified":
        updates.update(verification_status="manually_verified", last_verified_at=now)

    if not updates:
        return contact_id, "unchanged"
    updates["updated_at"] = now
    conn.execute(f"UPDATE contacts SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
                 (*updates.values(), contact_id))
    return contact_id, "updated"


def edit_contact(conn: sqlite3.Connection, contact_id: int, fields: dict[str, Any], *,
                 mark_verified: bool = False) -> None:
    """Manual edit from the dashboard. Recorded as evidence; optionally marks the contact manually verified."""
    allowed = {"full_name", "role_title", "role_category", "email", "email_type", "linkedin_url", "source_url",
               "notes", "do_not_contact", "opportunity_id"}
    data = {k: v for k, v in fields.items() if k in allowed}
    if "role_category" in data and data["role_category"] not in ROLE_CATEGORIES:
        raise ValueError("invalid role_category")
    if "email" in data:
        data["email"] = (data["email"] or "").strip().lower() or None
        if not data["email"]:
            data["email_type"] = "none"
        elif data.get("email_type", "none") == "none":
            data["email_type"] = "unverified"
    if "full_name" in data:
        data["name_norm"] = name_norm(data["full_name"])
        data["first_name"] = data["full_name"].split()[0]
    now = utc_now()
    if mark_verified:
        data.update(verification_status="manually_verified", last_verified_at=now)
    data["updated_at"] = now
    conn.execute(f"UPDATE contacts SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                 (*data.values(), contact_id))
    add_evidence(conn, contact_id, "manual_edit", fields.get("source_url"),
                 "verified by you" if mark_verified else "edited by you", fields.get("role_title"),
                 fields.get("email"))
    log_activity(conn, "contact", contact_id, "edited", "manually verified" if mark_verified else "")


def delete_contact(conn: sqlite3.Connection, contact_id: int) -> None:
    conn.execute("DELETE FROM contacts WHERE id = ?", (contact_id,))
    log_activity(conn, "contact", contact_id, "deleted")


def list_contacts(conn: sqlite3.Connection, *, company_ids: list[int] | None = None,
                  categories: list[str] | None = None, verification: list[str] | None = None,
                  min_score: int | None = None, search: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT ct.*, c.name AS company_name, c.domain AS company_domain, c.market
             FROM contacts ct JOIN companies c ON c.id = ct.company_id WHERE 1=1"""
    params: list[Any] = []
    for col, values in (("ct.company_id", company_ids), ("ct.role_category", categories),
                        ("ct.verification_status", verification)):
        if values:
            sql += f" AND {col} IN ({', '.join('?' for _ in values)})"
            params.extend(values)
    if min_score:
        sql += " AND COALESCE(ct.score_total, 0) >= ?"
        params.append(min_score)
    if search:
        sql += " AND (LOWER(ct.full_name) LIKE ? OR LOWER(COALESCE(ct.role_title, '')) LIKE ?)"
        params += [f"%{search.lower()}%"] * 2
    sql += " ORDER BY c.name, COALESCE(ct.outreach_priority, 999), ct.id"
    return _rows(conn.execute(sql, params))


def companies_without_contacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return _rows(conn.execute(
        """SELECT c.*, (SELECT status FROM contact_discovery_runs r WHERE r.company_id = c.id
                         ORDER BY r.id DESC LIMIT 1) AS last_run_status
           FROM companies c WHERE NOT EXISTS (SELECT 1 FROM contacts ct WHERE ct.company_id = c.id)
           ORDER BY c.name"""))


def rescore_company(conn: sqlite3.Connection, company_id: int) -> None:
    """Recompute every contact's score and the outreach priority order within the company."""
    from services.contacts.scoring import company_context, score

    company = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone())
    ctx = company_context(conn, company)
    scored = []
    for c in _rows(conn.execute("SELECT * FROM contacts WHERE company_id = ?", (company_id,))):
        s = score(c, ctx, company["domain"])
        linked_to_vacancy = bool(c["opportunity_id"]) and c["opportunity_id"] in ctx.vacancy_ids
        scored.append((c["id"], s, c["do_not_contact"], linked_to_vacancy))
    # Do-not-contact last; people tied to the advertised internship first; then by score.
    order = sorted(scored, key=lambda t: (t[2], not t[3], -t[1].total, t[0]))
    for rank, (cid, s, _, _) in enumerate(order, start=1):
        conn.execute(
            """UPDATE contacts SET score_role = ?, score_authority = ?, score_association = ?, score_contact = ?,
                   score_total = ?, confidence_score = ?, outreach_priority = ? WHERE id = ?""",
            (s.role, s.authority, s.association, s.contact, s.total, s.confidence, rank, cid))


# --------------------------------------------------------------------------- discovery runs


def start_run(conn: sqlite3.Connection, company_id: int) -> int:
    return conn.execute("INSERT INTO contact_discovery_runs (company_id) VALUES (?)", (company_id,)).lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, **fields: Any) -> None:
    allowed = {"status", "pages_checked", "people_found", "contacts_added", "contacts_updated", "hunter_used",
               "error"}
    data = {k: v for k, v in fields.items() if k in allowed}
    data["finished_at"] = utc_now()
    conn.execute(f"UPDATE contact_discovery_runs SET {', '.join(f'{k} = ?' for k in data)} WHERE id = ?",
                 (*data.values(), run_id))


def last_runs(conn: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """SELECT r.* FROM contact_discovery_runs r
           JOIN (SELECT company_id, MAX(id) AS id FROM contact_discovery_runs GROUP BY company_id) m ON m.id = r.id""")
    return {r["company_id"]: dict(r) for r in rows}


def hunter_searched_recently(conn: sqlite3.Connection, company_id: int, days: int = 30) -> bool:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return conn.execute(
        "SELECT 1 FROM contact_discovery_runs WHERE company_id = ? AND hunter_used = 1 AND started_at >= ?",
        (company_id, cutoff)).fetchone() is not None


def companies_for_discovery(conn: sqlite3.Connection, *, company_ids: list[int] | None = None,
                            force: bool = False, recent_days: int = 14) -> list[dict[str, Any]]:
    """Pipeline companies not discovered successfully in the last `recent_days` (failed ones are retried)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=recent_days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    sql = "SELECT c.* FROM companies c WHERE c.status != 'disqualified'"
    params: list[Any] = []
    if company_ids:
        sql += f" AND c.id IN ({', '.join('?' for _ in company_ids)})"
        params += company_ids
    if not force:
        sql += """ AND NOT EXISTS (SELECT 1 FROM contact_discovery_runs r WHERE r.company_id = c.id
                       AND r.status IN ('ok', 'partial') AND r.started_at >= ?)"""
        params.append(cutoff)
    return _rows(conn.execute(sql + " ORDER BY c.id", params))
