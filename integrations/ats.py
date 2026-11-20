"""Official public job-board APIs for company career pages (no authentication, no scraping).

Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
Lever:      https://api.lever.co/v0/postings/{company}?mode=json
Ashby:      https://api.ashbyhq.com/posting-api/job-board/{name}?includeCompensation=true
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from integrations.http import PoliteSession

ATS_TYPES = ("greenhouse", "lever", "ashby")


@dataclass
class AtsJob:
    title: str
    url: str | None
    location: str | None
    description: str | None
    employment_type: str | None = None
    workplace: str | None = None        # remote / hybrid / onsite when the ATS says so
    compensation: str | None = None
    posted_date: str | None = None


def detect_board(url: str) -> tuple[str, str] | None:
    """Recognise a careers URL: 'https://jobs.lever.co/acme/123' -> ('lever', 'acme')."""
    parsed = urlparse(url.strip() if "://" in url else "https://" + url.strip())
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io") and parts:
        return "greenhouse", parts[0]
    if host == "boards-api.greenhouse.io" and len(parts) >= 3 and parts[1] == "boards":
        return "greenhouse", parts[2]
    if host == "jobs.lever.co" and parts:
        return "lever", parts[0]
    if host == "jobs.ashbyhq.com" and parts:
        return "ashby", parts[0]
    return None


def _date_from_ms(value) -> str | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _date_prefix(value) -> str | None:
    return value[:10] if isinstance(value, str) and re.match(r"\d{4}-\d{2}-\d{2}", value) else None


def parse_greenhouse(data: dict) -> list[AtsJob]:
    jobs = []
    for j in data.get("jobs", []):
        jobs.append(AtsJob(
            title=j.get("title") or "",
            url=j.get("absolute_url"),
            location=(j.get("location") or {}).get("name"),
            description=j.get("content"),
            posted_date=_date_prefix(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs


def parse_lever(data: list) -> list[AtsJob]:
    jobs = []
    for j in data or []:
        cats = j.get("categories") or {}
        lists = "\n".join(
            f"{block.get('text', '')}\n{block.get('content', '')}" for block in j.get("lists") or []
        )
        description = "\n\n".join(
            x for x in (j.get("descriptionPlain") or j.get("description"), lists,
                        j.get("additionalPlain") or j.get("additional")) if x
        )
        salary = j.get("salaryRange") or {}
        comp = None
        if salary.get("min") is not None:
            comp = f"{salary.get('currency', '')} {salary.get('min')}-{salary.get('max')} {salary.get('interval', '')}".strip()
        jobs.append(AtsJob(
            title=j.get("text") or "",
            url=j.get("hostedUrl"),
            location=cats.get("location") or ", ".join(cats.get("allLocations") or []) or None,
            description=description or None,
            employment_type=cats.get("commitment"),
            workplace={"remote": "remote", "hybrid": "hybrid", "on-site": "onsite", "onsite": "onsite"}.get(
                (j.get("workplaceType") or "").lower()),
            compensation=comp,
            posted_date=_date_from_ms(j.get("createdAt")),
        ))
    return jobs


def parse_ashby(data: dict) -> list[AtsJob]:
    jobs = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        workplace = (j.get("workplaceType") or "").lower()
        if j.get("isRemote") and not workplace:
            workplace = "remote"
        comp = (j.get("compensation") or {}).get("compensationTierSummary")
        jobs.append(AtsJob(
            title=j.get("title") or "",
            url=j.get("jobUrl") or j.get("applyUrl"),
            location=j.get("location"),
            description=j.get("descriptionPlain") or j.get("descriptionHtml"),
            employment_type=j.get("employmentType"),
            workplace={"remote": "remote", "hybrid": "hybrid", "onsite": "onsite", "on-site": "onsite"}.get(workplace),
            compensation=comp,
            posted_date=_date_prefix(j.get("publishedAt")),
        ))
    return jobs


def fetch_jobs(ats: str, slug: str, http: PoliteSession | None = None) -> list[AtsJob]:
    """All currently published jobs on one board."""
    http = http or PoliteSession()
    slug = slug.strip().strip("/")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", slug):
        raise ValueError(f"invalid board slug: {slug!r}")
    if ats == "greenhouse":
        return parse_greenhouse(http.get_json(
            f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}))
    if ats == "lever":
        return parse_lever(http.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}))
    if ats == "ashby":
        return parse_ashby(http.get_json(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}))
    raise ValueError(f"ats must be one of {ATS_TYPES}")
