"""CSV import: header mapping, row validation, duplicate detection and import."""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from database import opportunities as opp_repo
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead, is_valid_url

MAX_ROWS = 2000
MAX_BYTES = 5 * 1024 * 1024

COLUMNS = (
    "company_name", "website", "job_title", "job_description", "job_url", "location",
    "remote_status", "platform_source", "discovery_date",
)
OPTIONAL_COLUMNS = ("stipend", "duration")

_HEADER_ALIASES = {
    "company": "company_name", "company_name": "company_name", "organisation": "company_name",
    "organization": "company_name", "employer": "company_name",
    "website": "website", "company_website": "website", "company_url": "website", "domain": "website",
    "job_title": "job_title", "title": "job_title", "role": "job_title", "position": "job_title",
    "job_description": "job_description", "description": "job_description", "jd": "job_description",
    "job_url": "job_url", "url": "job_url", "link": "job_url", "job_link": "job_url",
    "application_url": "job_url", "apply_url": "job_url",
    "location": "location", "city": "location",
    "remote_status": "remote_status", "remote": "remote_status", "work_mode": "remote_status",
    "workplace_type": "remote_status",
    "platform_source": "platform_source", "source": "platform_source", "platform": "platform_source",
    "discovery_date": "discovery_date", "date": "discovery_date", "discovered": "discovery_date",
    "date_found": "discovery_date",
    "stipend": "stipend", "salary": "stipend", "compensation": "stipend",
    "duration": "duration",
}

_REMOTE_ALIASES = {
    "remote": "remote", "wfh": "remote", "work from home": "remote", "yes": "remote", "fully remote": "remote",
    "hybrid": "hybrid",
    "onsite": "onsite", "on-site": "onsite", "on site": "onsite", "office": "onsite", "in office": "onsite",
    "in-office": "onsite", "no": "onsite",
    "": "unknown", "unknown": "unknown", "n/a": "unknown", "na": "unknown",
}

_SOURCE_ALIASES = {
    "google": "google", "google search": "google",
    "linkedin": "linkedin", "linkedin jobs": "linkedin",
    "wellfound": "wellfound", "angellist": "wellfound", "angel.co": "wellfound",
    "career page": "career_page", "career_page": "career_page", "careers": "career_page",
    "company website": "career_page", "greenhouse": "career_page", "lever": "career_page", "ashby": "career_page",
    "csv": "csv", "": "csv", "other": "csv", "manual": "manual",
}

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d %b %Y", "%d %B %Y", "%b %d, %Y")


@dataclass
class RowResult:
    row_number: int                       # 1-based, excluding header (matches a spreadsheet row - 1)
    raw: dict[str, str]
    lead: Lead | None = None
    errors: list[str] = field(default_factory=list)
    duplicate_of: str | None = None       # "row 3" or "opportunity #12"

    @property
    def status(self) -> str:
        if self.errors:
            return "invalid"
        return "duplicate" if self.duplicate_of else "new"


@dataclass
class ImportPreview:
    rows: list[RowResult]
    unknown_columns: list[str]
    missing_columns: list[str]

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r.status == status)


def _canon_header(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", (name or "").strip().lower()).strip("_")
    return _HEADER_ALIASES.get(key, key)


def parse_date(value: str) -> str | None:
    value = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def template_csv() -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(COLUMNS + OPTIONAL_COLUMNS)
    writer.writerow([
        "Example Startup", "https://example.com", "Product Management Intern",
        "Work with the PM on roadmap, user research and PRDs...", "https://example.com/careers/pm-intern",
        "Bengaluru, India", "remote", "linkedin", date.today().isoformat(), "INR 20,000/month", "6 months",
    ])
    return buf.getvalue()


def validate_row(row_number: int, raw: dict[str, str], today: date | None = None) -> RowResult:
    today = today or date.today()
    r = RowResult(row_number, raw)
    get = lambda k: (raw.get(k) or "").strip()  # noqa: E731

    company, title = get("company_name"), get("job_title")
    website, job_url = get("website"), get("job_url")
    if not company:
        r.errors.append("company_name is required")
    if not title:
        r.errors.append("job_title is required")
    if not website and not job_url:
        r.errors.append("provide job_url or website")
    if website and not is_valid_url(website if "://" in website else "https://" + website):
        r.errors.append(f"website is not a valid URL: {website!r}")
    if job_url and not is_valid_url(job_url):
        r.errors.append(f"job_url must be a full http(s) URL: {job_url!r}")

    remote_raw = get("remote_status").lower()
    remote = _REMOTE_ALIASES.get(remote_raw)
    if remote is None:
        r.errors.append(f"remote_status must be remote / hybrid / onsite / unknown, got {remote_raw!r}")

    source_raw = get("platform_source").lower()
    source = _SOURCE_ALIASES.get(source_raw)
    if source is None:
        r.errors.append(f"platform_source not recognised: {source_raw!r} "
                        "(use google, linkedin, wellfound, career_page or csv)")

    date_raw = get("discovery_date")
    discovered = today.isoformat()
    if date_raw:
        parsed = parse_date(date_raw)
        if parsed is None:
            r.errors.append(f"discovery_date not understood: {date_raw!r} (use YYYY-MM-DD)")
        elif parsed > today.isoformat():
            r.errors.append(f"discovery_date is in the future: {parsed}")
        else:
            discovered = parsed

    if len(get("job_description")) > 20000:
        r.errors.append("job_description is longer than 20,000 characters")

    if not r.errors:
        try:
            r.lead = Lead(
                company_name=company,
                job_title=title,
                source=source,
                discovered_at=discovered,
                job_url=job_url or None,
                website=(website if "://" in website else f"https://{website}") if website else None,
                description=get("job_description") or None,
                location=get("location") or None,
                remote_status=remote if remote != "unknown" else None,
                stipend=get("stipend") or None,
                duration=get("duration") or None,
            )
        except ValueError as exc:
            r.errors.append(str(exc))
    return r


def preview_csv(conn: sqlite3.Connection, data: bytes | str, today: date | None = None) -> ImportPreview:
    """Parse and validate without writing anything. Flags duplicates within the file and against the DB."""
    if isinstance(data, bytes):
        if len(data) > MAX_BYTES:
            raise ValueError("CSV is larger than 5 MB")
        text = data.decode("utf-8-sig", errors="replace")
    else:
        text = data.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError("CSV has no header row")

    mapping = {h: _canon_header(h) for h in reader.fieldnames}
    known = set(COLUMNS) | set(OPTIONAL_COLUMNS)
    unknown = [h for h, c in mapping.items() if c not in known]
    present = set(mapping.values())
    missing = [c for c in ("company_name", "job_title") if c not in present]
    if not ({"job_url", "website"} & present):
        missing.append("job_url or website")

    rows: list[RowResult] = []
    seen: dict[tuple, int] = {}
    for i, raw in enumerate(reader, start=1):
        if i > MAX_ROWS:
            raise ValueError(f"CSV has more than {MAX_ROWS} rows; split it into smaller files")
        canon = {mapping[k]: (v or "") for k, v in raw.items() if k in mapping and mapping[k] in known}
        if not any(v.strip() for v in canon.values()):
            continue  # skip blank lines
        result = validate_row(i, canon, today)
        if result.lead:
            lead = result.lead
            keys = []
            if lead.job_url_norm:
                keys.append(("url", lead.job_url_norm))
            keys.append(("name", lead.company_name_norm, lead.job_title_norm))
            if lead.company_domain:
                keys.append(("domain", lead.company_domain, lead.job_title_norm))
            earlier = next((seen[k] for k in keys if k in seen), None)
            if earlier is not None:
                result.duplicate_of = f"row {earlier}"
            else:
                for k in keys:
                    seen[k] = i
                existing = opp_repo.find_duplicate(
                    conn, job_url_norm=lead.job_url_norm, company_domain=lead.company_domain,
                    company_name_norm=lead.company_name_norm, job_title_norm=lead.job_title_norm,
                )
                if existing:
                    result.duplicate_of = f"opportunity #{existing['id']}"
        rows.append(result)
    return ImportPreview(rows, unknown, missing)


@dataclass
class ImportSummary:
    created: int = 0
    merged: int = 0
    skipped_invalid: int = 0
    skipped_in_file_duplicates: int = 0


def import_preview(conn: sqlite3.Connection, preview: ImportPreview) -> ImportSummary:
    """Write valid rows. DB duplicates are merged (adds their source URL); in-file repeats are skipped."""
    summary = ImportSummary()
    for row in preview.rows:
        if row.status == "invalid":
            summary.skipped_invalid += 1
        elif row.duplicate_of and row.duplicate_of.startswith("row"):
            summary.skipped_in_file_duplicates += 1
        else:
            result = ingest_lead(conn, row.lead)
            if result.created:
                summary.created += 1
            else:
                summary.merged += 1
    return summary
