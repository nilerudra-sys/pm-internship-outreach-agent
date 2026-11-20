"""CSV import of contacts you collected (e.g. from LinkedIn or Google): validation, de-duplication, import."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field

from database import contacts as contacts_repo
from database.migrations import name_norm
from services.contacts.emails import validate_email
from services.contacts.roles import classify_designation
from services.discovery.normalize import is_valid_url, normalize_company_name, registrable_domain

MAX_ROWS = 2000
COLUMNS = ("company", "full_name", "current_role", "professional_email", "linkedin_url", "source_url", "notes")
_ALIASES = {
    "company": "company", "company_name": "company", "organisation": "company", "organization": "company",
    "full_name": "full_name", "name": "full_name", "contact": "full_name", "person": "full_name",
    "current_role": "current_role", "role": "current_role", "title": "current_role", "designation": "current_role",
    "position": "current_role", "job_title": "current_role",
    "professional_email": "professional_email", "email": "professional_email", "work_email": "professional_email",
    "linkedin_url": "linkedin_url", "linkedin": "linkedin_url", "linkedin_profile": "linkedin_url",
    "source_url": "source_url", "source": "source_url", "url": "source_url", "evidence_url": "source_url",
    "notes": "notes", "note": "notes", "comments": "notes",
}


@dataclass
class ContactRow:
    row_number: int
    raw: dict[str, str]
    company: dict | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    category: str = "other"
    email: str | None = None

    @property
    def status(self) -> str:
        if self.errors:
            return "invalid"
        return "duplicate" if self.duplicate_of else "new"


@dataclass
class ContactPreview:
    rows: list[ContactRow]
    unknown_columns: list[str]
    missing_columns: list[str]

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)


def template_csv() -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    w.writerow(["Example Startup", "Asha Rao", "Co-founder & CEO", "", "https://www.linkedin.com/in/example",
                "https://www.linkedin.com/in/example", "Found via LinkedIn search"])
    return buf.getvalue()


def _company_index(conn: sqlite3.Connection) -> dict[str, dict]:
    index = {}
    for c in conn.execute("SELECT * FROM companies"):
        c = dict(c)
        index[normalize_company_name(c["name"])] = c
        index[c["domain"]] = c
    return index


def preview_contacts_csv(conn: sqlite3.Connection, data: bytes | str) -> ContactPreview:
    text = data.decode("utf-8-sig", errors="replace") if isinstance(data, bytes) else data.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")
    mapping = {h: _ALIASES.get(re.sub(r"[^a-z0-9]+", "_", (h or "").lower()).strip("_"), None)
               for h in reader.fieldnames}
    unknown = [h for h, c in mapping.items() if c is None]
    present = set(mapping.values())
    missing = [c for c in ("company", "full_name", "current_role") if c not in present]
    if not ({"linkedin_url", "source_url"} & present):
        missing.append("linkedin_url or source_url")

    companies = _company_index(conn)
    rows: list[ContactRow] = []
    seen: dict[tuple, int] = {}
    for i, raw in enumerate(reader, start=1):
        if i > MAX_ROWS:
            raise ValueError(f"CSV has more than {MAX_ROWS} rows")
        r = {mapping[k]: (v or "").strip() for k, v in raw.items() if k in mapping and mapping[k]}
        if not any(r.values()):
            continue
        row = ContactRow(i, r)
        company_key = r.get("company", "")
        company = companies.get(normalize_company_name(company_key)) or \
            companies.get(registrable_domain("https://" + company_key) or "")
        if not company_key:
            row.errors.append("company is required")
        elif not company:
            row.errors.append(f"company {company_key!r} is not in your pipeline; add it first")
        row.company = company
        name = r.get("full_name", "")
        if len(name_norm(name).split()) < 1 or len(name) < 3:
            row.errors.append("full_name is required")
        role = r.get("current_role", "")
        if not role:
            row.errors.append("current_role is required")
        else:
            row.category = classify_designation(role)
            if re.search(r"\b(intern|interns|internship|trainee)\b", role, re.I):
                row.errors.append("interns are not outreach targets")
            elif row.category == "other":
                row.warnings.append("role is not a target role; it will score low")
        evidence = [u for u in (r.get("source_url"), r.get("linkedin_url")) if u]
        if not evidence:
            row.errors.append("source_url or linkedin_url is required as evidence")
        for col in ("source_url", "linkedin_url"):
            if r.get(col) and not is_valid_url(r[col]):
                row.errors.append(f"{col} must be a full http(s) URL")
        if r.get("linkedin_url") and "linkedin.com/in/" not in r["linkedin_url"].lower():
            row.warnings.append("linkedin_url is not a profile URL (linkedin.com/in/...)")
        if r.get("professional_email"):
            check = validate_email(r["professional_email"], company["domain"] if company else None)
            if not check.ok:
                row.errors.append(f"invalid email: {r['professional_email']!r}")
            else:
                row.email = check.email
                row.warnings += [p for p in check.problems]
        if not row.errors and company:
            keys = [("name", company["id"], name_norm(name))]
            if row.email:
                keys.append(("email", company["id"], row.email))
            earlier = next((seen[k] for k in keys if k in seen), None)
            if earlier:
                row.duplicate_of = f"row {earlier}"
            else:
                for k in keys:
                    seen[k] = i
                dup = contacts_repo.find_duplicate(conn, company["id"], name, row.email, r.get("linkedin_url"))
                if dup:
                    row.duplicate_of = f"contact #{dup['id']} ({dup['full_name']})"
        rows.append(row)
    return ContactPreview(rows, unknown, missing)


@dataclass
class ContactImportSummary:
    created: int = 0
    merged: int = 0
    skipped_invalid: int = 0
    skipped_repeats: int = 0


def import_contacts(conn: sqlite3.Connection, preview: ContactPreview) -> ContactImportSummary:
    summary = ContactImportSummary()
    touched: set[int] = set()
    for row in preview.rows:
        if row.status == "invalid":
            summary.skipped_invalid += 1
            continue
        if row.duplicate_of and row.duplicate_of.startswith("row"):
            summary.skipped_repeats += 1
            continue
        r = row.raw
        _, action = contacts_repo.upsert_contact(
            conn, row.company["id"], full_name=r["full_name"], role_title=r["current_role"],
            role_category=row.category, source="csv", source_url=r.get("source_url") or r.get("linkedin_url"),
            evidence=f"CSV import: {r['full_name']}, {r['current_role']} (source: "
                     f"{r.get('source_url') or r.get('linkedin_url')})",
            email=row.email, email_type="unverified" if row.email else "none",
            linkedin_url=r.get("linkedin_url") or None, notes=r.get("notes") or None)
        summary.created += action == "created"
        summary.merged += action != "created"
        touched.add(row.company["id"])
    for cid in touched:
        contacts_repo.rescore_company(conn, cid)
    return summary
