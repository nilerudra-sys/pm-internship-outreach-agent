"""Contact discovery for one company: official website people pages, linked job listings, optional Hunter.io.

Website access reuses Phase 3's polite fetcher (robots.txt, per-host delay, 7-day page cache).
Every contact stored has a source URL and an evidence quote for its name and role.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from database import contacts as contacts_repo
from database import research as research_repo
from database.models import utc_now
from integrations.http import PoliteSession
from services.contacts.emails import validate_email
from services.contacts.extract import PersonCandidate, extract_people
from services.contacts.roles import classify_designation
from services.discovery.normalize import registrable_domain
from services.research.verify import name_tokens
from services.research.website import RobotsCache, _fetch_one

MAX_PEOPLE_PAGES = 4
CACHE_DAYS = 7
_PEOPLE_TYPES = (
    ("team", r"team|our-team|people|meet-us|meet-the", 6),
    ("leadership", r"leadership|founders?|management|executives|board", 6),
    ("about", r"about|who-we-are|our-story|company|story", 5),
    ("careers", r"careers?|jobs|join|hiring|work-with-us", 3),
    ("contact", r"contact", 2),
)
_SKIP = re.compile(r"login|signin|sign-in|signup|register|privacy|terms|cookie|legal|blog/|news/|"
                   r"\.(pdf|jpe?g|png|svg|zip|mp4)$", re.I)


@dataclass
class DiscoveryResult:
    company_id: int
    company_name: str
    status: str = "ok"                 # ok | partial | failed
    pages_checked: int = 0
    people_found: int = 0
    added: int = 0
    updated: int = 0
    hunter_used: bool = False
    errors: list[str] = field(default_factory=list)


def _fresh(row: dict | None) -> bool:
    if not row:
        return False
    cutoff = (datetime.now(timezone.utc) - timedelta(days=CACHE_DAYS)).strftime("%Y-%m-%dT%H:%M:%S+00:00")
    return row["fetched_at"] >= cutoff


def _page_row(conn: sqlite3.Connection, company_id: int, url: str) -> dict | None:
    row = conn.execute("SELECT * FROM company_pages WHERE company_id = ? AND url = ?", (company_id, url)).fetchone()
    return dict(row) if row else None


def _usable(row: dict | None) -> bool:
    """Fetched as HTML with text. Short team pages are fine here even if too thin for Phase 3 research."""
    if not row or row.get("http_status") != 200 or row.get("people_text") is None:
        return False
    err = row.get("error") or ""
    return not err or err.startswith("too little readable text")


def people_links(base_url: str, links: list, domain: str, limit: int = MAX_PEOPLE_PAGES) -> list[tuple[str, str]]:
    best: dict[str, tuple[int, str]] = {}
    for href, text in links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        p = urlparse(url)
        if p.scheme not in ("http", "https") or registrable_domain(url) != domain or _SKIP.search(p.path):
            continue
        if url.rstrip("/") == base_url.rstrip("/"):
            continue
        hay = f"{p.path} {text}".lower()
        for ptype, pattern, prio in _PEOPLE_TYPES:
            if re.search(pattern, hay):
                score = prio * 10 - p.path.strip("/").count("/")
                if ptype not in best or score > best[ptype][0]:
                    best[ptype] = (score, url)
                break
    return [(t, u) for t, (_, u) in sorted(best.items(), key=lambda kv: -kv[1][0])][:limit]


def people_pages(conn: sqlite3.Connection, company: dict, http: PoliteSession, robots: RobotsCache,
                 force: bool = False) -> tuple[list[dict], int, list[str]]:
    """Home page plus up to 4 people-related pages. Returns (usable page rows, requests made, errors)."""
    cid, domain = company["id"], company["domain"]
    requests_made, errors = 0, []
    home = None
    for url in (f"https://{domain}/", f"https://www.{domain}/"):
        row = _page_row(conn, cid, url)
        if not force and _fresh(row) and _usable(row) and row.get("links"):
            home = row
            break
        _, _, err = _fetch_one(conn, cid, url, "home", http, robots, domain)
        requests_made += 1
        row = _page_row(conn, cid, url)
        if _usable(row):
            home = row
            break
        errors.append(f"{url}: {err}")   # an unreachable home page is always reported
    if home is None:
        return [], requests_made, errors

    pages = [home]
    for ptype, url in people_links(home["url"], json.loads(home.get("links") or "[]"), domain):
        row = _page_row(conn, cid, url)
        # Re-fetch only if forced, never fetched, stale, or fetched before people_text existed.
        # Pages known to be missing/blocked are cached too, so they are not requested again for 7 days.
        if force or not _fresh(row) or (row.get("error") is None and row.get("people_text") is None):
            _, _, err = _fetch_one(conn, cid, url, ptype, http, robots, domain)
            requests_made += 1
            row = _page_row(conn, cid, url)
            if not _usable(row) and is_real_failure(err):
                errors.append(f"{url}: {err}")
        if _usable(row):
            pages.append(row)
    conn.commit()
    return pages, requests_made, errors


def is_real_failure(err: str | None) -> bool:
    """Network errors and server errors are failures; a missing, blocked or script-only page is not."""
    if not err:
        return False
    expected = ("HTTP 4", "blocked by robots.txt", "too little readable text", "not HTML", "redirected off-site")
    return not err.startswith(expected)


def _store(conn: sqlite3.Connection, company: dict, c: PersonCandidate, source: str,
           opportunity_id: int | None = None) -> str:
    email, email_type = None, "none"
    if c.email:
        check = validate_email(c.email, company["domain"])
        if check.ok and not check.generic and check.domain_matches:
            email, email_type = check.email, "public_verified"   # published on the company's own page
    _, action = contacts_repo.upsert_contact(
        conn, company["id"], full_name=c.full_name, role_title=c.role_title, role_category=c.role_category,
        source=source, source_url=c.source_url, evidence=c.evidence, email=email, email_type=email_type,
        linkedin_url=c.linkedin_url, opportunity_id=opportunity_id)
    return action


def discover_company(conn: sqlite3.Connection, company: dict, http: PoliteSession, robots: RobotsCache,
                     hunter=None, force: bool = False) -> DiscoveryResult:
    """Discover contacts for one company. Never raises: failures are recorded on the run."""
    result = DiscoveryResult(company["id"], company["name"])
    run_id = contacts_repo.start_run(conn, company["id"])
    conn.commit()
    try:
        ignore = name_tokens(company["name"], company["domain"])
        pages, result.pages_checked, page_errors = people_pages(conn, company, http, robots, force)
        result.errors += page_errors
        seen: set[str] = set()

        def handle(cands: list[PersonCandidate], source: str, opportunity_id: int | None = None) -> None:
            for c in cands:
                if c.key in seen and source != "listing":
                    continue
                seen.add(c.key)
                result.people_found += 1
                action = _store(conn, company, c, source, opportunity_id)
                result.added += action == "created"
                result.updated += action == "updated"

        for p in pages[1:] + pages[:1]:   # dedicated people pages before the home page
            handle(extract_people(p["people_text"] or "", p.get("final_url") or p["url"], ignore,
                                  json.loads(p.get("links") or "[]"), company["domain"]), "website")
        for listing in research_repo.linked_listings(conn, company, limit=10):
            handle(extract_people(listing.get("description") or "", listing.get("job_url"), ignore),
                   "listing", listing["id"])

        if hunter is not None and not (not force and contacts_repo.hunter_searched_recently(conn, company["id"])):
            result.hunter_used = True
            try:
                found = hunter.domain_search(company["domain"])
                _record_api_call(conn, "hunter")
                for person in found.people:
                    category = classify_designation(person.position)
                    if category == "other":
                        continue
                    check = validate_email(person.email, company["domain"])
                    email_ok = check.ok and not check.generic
                    _, action = contacts_repo.upsert_contact(
                        conn, company["id"], full_name=person.full_name, role_title=person.position,
                        role_category=category, source="hunter", source_url=person.sources[0],
                        evidence=f"Hunter.io domain search: {person.full_name}, {person.position}; "
                                 f"email found on {', '.join(person.sources[:2])}",
                        email=check.email if email_ok else None, email_type="hunter" if email_ok else "none",
                        email_confidence=person.confidence, email_status=person.verification_status,
                        linkedin_url=person.linkedin_url)
                    result.people_found += 1
                    result.added += action == "created"
                    result.updated += action == "updated"
            except Exception as exc:
                result.errors.append(f"Hunter.io: {str(exc)[:200]}")

        contacts_repo.rescore_company(conn, company["id"])
        if not pages and not result.people_found:
            result.status = "failed"
        elif result.errors:
            result.status = "partial"
        if result.people_found and company.get("status") in ("discovered", "researched"):
            conn.execute("UPDATE companies SET status = 'contacts_found', updated_at = ? WHERE id = ?",
                         (utc_now(), company["id"]))
    except Exception as exc:  # one company must never stop the batch
        conn.rollback()
        result.status = "failed"
        result.errors.append(f"{type(exc).__name__}: {str(exc)[:200]}")
    contacts_repo.finish_run(conn, run_id, status=result.status, pages_checked=result.pages_checked,
                             people_found=result.people_found, contacts_added=result.added,
                             contacts_updated=result.updated, hunter_used=int(result.hunter_used),
                             error="; ".join(result.errors)[:1000] or None)
    conn.commit()
    return result


def _record_api_call(conn: sqlite3.Connection, provider: str) -> None:
    from datetime import date

    conn.execute(
        "INSERT INTO api_usage (provider, day, calls) VALUES (?, ?, 1) "
        "ON CONFLICT (provider, day) DO UPDATE SET calls = calls + 1", (provider, date.today().isoformat()))
