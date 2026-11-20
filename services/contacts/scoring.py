"""Contact scoring in Python (no Gemini): role 40 + authority 25 + association 20 + contact quality 15."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from services.contacts.emails import validate_email

# --------------------------------------------------------------------------- company context

EARLY = ("founder", "cofounder", "ceo", "product_lead", "head_of_product", "executive", "product_manager",
         "hiring_manager", "talent_acquisition", "recruiter")
ESTABLISHED = ("hiring_manager", "head_of_product", "product_lead", "product_manager", "talent_acquisition",
               "recruiter", "executive", "founder", "cofounder", "ceo")
ESTABLISHED_SPECULATIVE = ("head_of_product", "product_lead", "founder", "cofounder", "ceo", "product_manager",
                           "hiring_manager", "executive", "talent_acquisition", "recruiter")

AUTHORITY = {"founder": 25, "cofounder": 25, "ceo": 25, "head_of_product": 22, "executive": 20,
             "hiring_manager": 20, "product_lead": 17, "talent_acquisition": 13, "product_manager": 12,
             "recruiter": 10, "other": 0}

_EARLY_SIGNS = re.compile(r"\b(pre-?seed|seed|series a|angel|bootstrapped|early[- ]stage|stealth|"
                          r"y ?combinator|yc [ws]\d\d)\b", re.I)
_ESTABLISHED_SIGNS = re.compile(r"\b(series [c-z]|ipo|listed on|publicly traded|nse|bse|nasdaq|nyse|unicorn|"
                                r"\d+(\.\d+)?\+? ?(crore|lakh|million|billion) (customers|users|clients)|"
                                r"\d{4,}\+? employees|largest)\b", re.I)


@dataclass
class CompanyContext:
    stage: str            # early | established | unknown
    has_vacancy: bool
    vacancy_ids: list[int]

    @property
    def outreach_type(self) -> str:
        return "vacancy" if self.has_vacancy else "speculative"

    def priority_table(self) -> tuple[str, ...]:
        if self.stage == "established":
            return ESTABLISHED if self.has_vacancy else ESTABLISHED_SPECULATIVE
        if self.stage == "unknown" and self.has_vacancy:
            return ESTABLISHED
        return EARLY


def company_stage(company: dict, research: dict | None) -> str:
    """'early' / 'established' / 'unknown' from stored stage fields and VERIFIED research facts only."""
    texts = [company.get("funding_stage") or "", company.get("employee_count") or ""]
    facts = (research or {}).get("facts") or {}
    texts += [f.get("text", "") for f in facts.get("stage_signals", [])]
    blob = " ".join(texts)
    emp = re.search(r"(\d[\d,]*)\s*(\+|employees|people)?", company.get("employee_count") or "")
    if emp:
        n = int(emp.group(1).replace(",", ""))
        if n >= 200:
            return "established"
        if n < 50:
            return "early"
    if _ESTABLISHED_SIGNS.search(blob):
        return "established"
    if _EARLY_SIGNS.search(blob):
        return "early"
    return "unknown"


def company_context(conn: sqlite3.Connection, company: dict) -> CompanyContext:
    from database import research as research_repo
    from services.discovery.normalize import normalize_company_name

    rows = conn.execute(
        """SELECT id FROM opportunities
           WHERE (company_id = ? OR company_domain = ? OR company_name_norm = ?)
             AND status != 'rejected'
             AND role_category IN ('core_pm', 'related_product', 'adjacent', 'unclassified')
           ORDER BY CASE status WHEN 'qualified' THEN 0 ELSE 1 END, COALESCE(relevance_score, -1) DESC""",
        (company["id"], company["domain"], normalize_company_name(company["name"])),
    ).fetchall()
    ids = [r[0] for r in rows]
    return CompanyContext(company_stage(company, research_repo.get_research(conn, company["id"])), bool(ids), ids)


# --------------------------------------------------------------------------- contact score


@dataclass
class ContactScore:
    role: int
    authority: int
    association: int
    contact: int

    @property
    def total(self) -> int:
        return self.role + self.authority + self.association + self.contact

    @property
    def confidence(self) -> int:
        """How sure we are this person is at this company in this role (0-100)."""
        return round(self.association / 20 * 100)


def score_role(category: str, ctx: CompanyContext, linked_to_vacancy: bool) -> int:
    if category == "other":
        return 0
    if linked_to_vacancy and ctx.has_vacancy:
        return 40
    table = ctx.priority_table()
    rank = table.index(category) if category in table else len(table)
    return max(8, 40 - 4 * rank)


def score_association(contact: dict, company_domain: str | None) -> int:
    """Strength of the evidence that this person holds this role at this company."""
    status, source = contact.get("verification_status"), contact.get("source")
    has_evidence = bool(contact.get("evidence") or contact.get("source_url"))
    if status == "manually_verified":
        return 20
    if source == "website" and has_evidence:
        return 18
    if source == "listing" and has_evidence:
        return 16
    if source == "hunter" and has_evidence:
        return 12
    if source in ("linkedin", "google", "manual", "csv") and has_evidence:
        return 10
    return 0


def score_contact_quality(contact: dict, company_domain: str | None) -> int:
    email, etype = contact.get("email"), contact.get("email_type") or "none"
    if email:
        check = validate_email(email, company_domain)
        if not check.ok:
            return 2 if contact.get("linkedin_url") else 0
        weak = check.generic or not check.domain_matches
        if etype == "public_verified":
            return 8 if weak else 15
        if etype == "hunter":
            strong = (contact.get("email_confidence") or 0) >= 90 and contact.get("email_status") == "valid"
            return 6 if weak else (13 if strong else 10)
        return 4 if weak else 6
    return 4 if contact.get("linkedin_url") else 0


def score(contact: dict, ctx: CompanyContext, company_domain: str | None) -> ContactScore:
    linked = bool(contact.get("opportunity_id")) and contact.get("opportunity_id") in ctx.vacancy_ids
    category = contact.get("role_category") or "other"
    return ContactScore(
        role=score_role(category, ctx, linked),
        authority=AUTHORITY.get(category, 0),
        association=score_association(contact, company_domain),
        contact=score_contact_quality(contact, company_domain),
    )
