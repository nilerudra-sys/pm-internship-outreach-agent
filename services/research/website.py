"""Fetch a few public pages from a company's own website, respecting robots.txt.

Limits: at most `max_pages` pages per company (default 5), one request every 2 s per host (or
the site's Crawl-delay, up to 10 s), pages cached for 7 days, same site only, HTML only.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

from database import research as research_repo
from database.models import utc_now
from integrations.http import ROBOTS_AGENT, PoliteSession
from services.discovery.normalize import registrable_domain
from services.research.html_text import extract

MAX_PAGES = 5
MIN_CONTENT_LINES = 2        # lines of 8+ words needed; fewer means JavaScript-only / boilerplate
MIN_LINE_WORDS = 8
CACHE_DAYS = 7

# (page type, pattern on path or anchor text, priority). Higher priority is fetched first.
_PAGE_TYPES = (
    ("about", r"about|company|our-story|who-we-are|mission|why-us", 5),
    ("careers", r"careers?|jobs|join|work-with-us|hiring|life-at|internship", 5),
    ("product", r"products?|platform|features|solutions?|how-it-works|services", 4),
    ("customers", r"customers|case-stud|success-stor", 2),
    ("pricing", r"pricing|plans", 2),
    ("team", r"team|leadership|founders", 2),
)
_SKIP_PATH = re.compile(
    r"login|log-in|signin|sign-in|signup|sign-up|register|cart|checkout|privacy|terms|cookie|legal|"
    r"\.(pdf|jpe?g|png|gif|svg|zip|mp4|webp|ico|xml|json)$", re.I)


@dataclass
class FetchReport:
    pages: list[dict]
    fetched: int = 0
    from_cache: bool = False
    blocked: int = 0
    errors: list[str] | None = None


class RobotsCache:
    """robots.txt per host, following RFC 9309: 4xx -> allowed, 5xx/unreachable -> disallowed."""

    def __init__(self, http: PoliteSession):
        self.http = http
        self._cache: dict[str, RobotFileParser | bool] = {}

    def _load(self, scheme: str, host: str) -> RobotFileParser | bool:
        key = f"{scheme}://{host}"
        if key in self._cache:
            return self._cache[key]
        try:
            page = self.http.get_page(f"{key}/robots.txt", max_bytes=500_000)
            if page.status == 200:
                parser = RobotFileParser()
                parser.parse(page.text.splitlines())
                delay = parser.crawl_delay(ROBOTS_AGENT)
                if delay:
                    self.http.host_intervals[host] = min(float(delay), 10.0)
                result: RobotFileParser | bool = parser
            elif 400 <= page.status < 500:
                result = True
            else:
                result = False
        except Exception:
            result = False
        self._cache[key] = result
        return result

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        rules = self._load(p.scheme, p.hostname or "")
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(ROBOTS_AGENT, url)


def _clean_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme, p.netloc.lower(), p.path or "/", "", p.query, ""))


def candidate_links(base_url: str, links: list[tuple[str, str]], domain: str, limit: int) -> list[tuple[str, str]]:
    """Best same-site links for research, at most one per page type: [(page_type, url)]."""
    scored: dict[str, tuple[int, str]] = {}
    for href, text in links:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        url = _clean_url(urljoin(base_url, href))
        p = urlparse(url)
        if p.scheme not in ("http", "https") or registrable_domain(url) != domain:
            continue
        if _SKIP_PATH.search(p.path) or url.rstrip("/") == base_url.rstrip("/"):
            continue
        haystack = f"{p.hostname} {p.path} {text}".lower()
        for page_type, pattern, priority in _PAGE_TYPES:
            if re.search(pattern, haystack):
                depth_penalty = p.path.strip("/").count("/")
                score = priority * 10 - depth_penalty
                if page_type not in scored or score > scored[page_type][0]:
                    scored[page_type] = (score, url)
                break
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][0])
    return [(ptype, url) for ptype, (_, url) in ranked[:limit]]


def _fetch_one(conn, company_id: int, url: str, page_type: str, http: PoliteSession, robots: RobotsCache,
               domain: str):
    """Fetch and store one page. Returns (page_row or None, extracted PageText or None, error or None)."""
    if not robots.allowed(url):
        research_repo.upsert_page(conn, company_id, url, page_type=page_type, error="blocked by robots.txt")
        return None, None, "blocked by robots.txt"
    try:
        page = http.get_page(url)
    except Exception as exc:
        err = f"{type(exc).__name__}: {str(exc)[:150]}"
        research_repo.upsert_page(conn, company_id, url, page_type=page_type, error=err)
        return None, None, err
    if registrable_domain(page.final_url) != domain:
        err = f"redirected off-site to {urlparse(page.final_url).hostname}"
        research_repo.upsert_page(conn, company_id, url, page_type=page_type, http_status=page.status, error=err)
        return None, None, err
    if page.status != 200 or "html" not in page.content_type.lower():
        err = f"HTTP {page.status}" if page.status != 200 else f"not HTML ({page.content_type})"
        research_repo.upsert_page(conn, company_id, url, page_type=page_type, http_status=page.status, error=err)
        return None, None, err
    parsed = extract(page.text)
    body = parsed.text
    err = None if has_real_content(body) else "too little readable text (site may need JavaScript)"
    research_repo.upsert_page(conn, company_id, url, final_url=page.final_url, page_type=page_type,
                              http_status=page.status, title=parsed.title, description=parsed.description,
                              text=body, error=err, links=json.dumps(parsed.links[:400]),
                              people_text=parsed.people_text)
    row = None if err else {"url": url, "final_url": page.final_url, "page_type": page_type,
                            "title": parsed.title, "description": parsed.description, "text": body,
                            "fetched_at": utc_now()}
    return row, parsed, err


def has_real_content(text: str) -> bool:
    """At least two sentence-like lines (8+ words). Menus, cookie banners and JS shells fail this."""
    lines = [ln for ln in (text or "").split("\n") if len(ln.split()) >= MIN_LINE_WORDS]
    return len(lines) >= MIN_CONTENT_LINES


def fetch_company_pages(conn: sqlite3.Connection, company: dict, http: PoliteSession, robots: RobotsCache,
                        *, max_pages: int = MAX_PAGES, force: bool = False) -> FetchReport:
    """Home page plus the best about/careers/product pages. Uses the 7-day cache unless force."""
    company_id, domain = company["id"], company["domain"]
    if not force:
        cached = research_repo.cached_pages(conn, company_id, CACHE_DAYS)
        if cached:
            return FetchReport(cached, from_cache=True, errors=[])

    report = FetchReport([], errors=[])
    home_row = home_parsed = None
    for home in (f"https://{domain}/", f"https://www.{domain}/"):
        home_row, home_parsed, err = _fetch_one(conn, company_id, home, "home", http, robots, domain)
        report.fetched += err != "blocked by robots.txt"
        if err == "blocked by robots.txt":
            report.blocked += 1
        if home_parsed is not None:
            base = home
            break
        report.errors.append(f"{home}: {err}")
    else:
        return report

    if home_row:
        report.pages.append(home_row)
    for page_type, url in candidate_links(base, home_parsed.links, domain, max_pages - 1):
        row, _, err = _fetch_one(conn, company_id, url, page_type, http, robots, domain)
        if err == "blocked by robots.txt":
            report.blocked += 1
        else:
            report.fetched += 1
        if row:
            report.pages.append(row)
        elif err:
            report.errors.append(f"{url}: {err}")
    conn.commit()
    return report
