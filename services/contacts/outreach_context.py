"""Everything Phase 5 (email drafting) will need for one contact, assembled from verified data only.

Not an email generator. It returns facts with their quotes and source URLs, and clearly separates
verified data from Gemini interpretation and from unverified contact details.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from config.settings import load_profile
from database import contacts as contacts_repo
from database import research as research_repo
from services.contacts.scoring import company_context

USABLE_VERIFICATION = ("evidence_found", "manually_verified")
SENDABLE_EMAIL_TYPES = ("public_verified", "hunter")


def get_outreach_context(conn: sqlite3.Connection, contact_id: int) -> dict[str, Any] | None:
    contact = contacts_repo.get_contact(conn, contact_id)
    if not contact:
        return None
    company = dict(conn.execute("SELECT * FROM companies WHERE id = ?", (contact["company_id"],)).fetchone())
    ctx = company_context(conn, company)
    research = research_repo.get_research(conn, company["id"]) or {}
    listing = None
    listing_id = contact["opportunity_id"] or (ctx.vacancy_ids[0] if ctx.vacancy_ids else None)
    if listing_id:
        row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (listing_id,)).fetchone()
        listing = dict(row) if row else None
    evidence = contacts_repo.evidence_for(conn, [contact_id])[contact_id]

    verified_person = contact["verification_status"] in USABLE_VERIFICATION
    email_usable = bool(contact["email"]) and (
        contact["email_type"] in SENDABLE_EMAIL_TYPES or contact["verification_status"] == "manually_verified")
    blockers = []
    if not verified_person:
        blockers.append("contact name/role not backed by evidence or verified by you")
    if not email_usable:
        blockers.append("no usable email (none, or unverified)")
    if not research.get("researched_at"):
        blockers.append("company not researched (Phase 3)")
    if contact["do_not_contact"]:
        blockers.append("marked do-not-contact")

    return {
        "outreach_type": ctx.outreach_type,                 # vacancy | speculative
        "company_stage": ctx.stage,
        "ready": not blockers,
        "blockers": blockers,
        "contact": {
            "id": contact["id"],
            "full_name": contact["full_name"] if verified_person else None,
            "first_name": contact["first_name"] if verified_person else None,
            "designation": contact["role_title"] if verified_person else None,
            "role_category": contact["role_category"],
            "verification_status": contact["verification_status"],
            "email": contact["email"] if email_usable else None,
            "email_type": contact["email_type"],
            "linkedin_url": contact["linkedin_url"],
            "score_total": contact["score_total"],
            "sources": [{"source": e["source"], "url": e["source_url"], "evidence": e["evidence"],
                         "observed_at": e["observed_at"]} for e in evidence],
        },
        "company": {
            "id": company["id"], "name": company["name"], "domain": company["domain"], "market": company["market"],
            "verified_summary": research.get("verified_summary"),
            "verified_facts": research.get("facts") or {},          # each with quote + source url + fetched_at
            "interpretation_not_verified": {k: research.get(k) for k in
                                            ("summary", "product", "target_customers", "industry", "candidate_angle")},
        },
        "listing": None if not listing else {
            "id": listing["id"], "job_title": listing["job_title"], "job_url": listing["job_url"],
            "location": listing["location"], "remote_status": listing["remote_status"],
            "product_responsibilities": listing["product_responsibilities"], "status": listing["status"],
        },
        "applicant_profile": load_profile().get("applicant", {}),
    }


def outreach_ready_contacts(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Top-priority contact per company with everything Phase 5 needs (ready or not, with blockers)."""
    out = []
    for row in conn.execute(
            "SELECT id FROM contacts WHERE outreach_priority = 1 AND do_not_contact = 0 ORDER BY score_total DESC"):
        ctx = get_outreach_context(conn, row[0])
        if ctx:
            out.append(ctx)
    return out
