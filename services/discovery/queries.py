"""Search query generation for Google, LinkedIn Jobs and Wellfound.

No search API is called. Each query becomes a link you open in your own browser; listings you
find are brought back through quick-add or CSV import. This keeps discovery free and within each
site's terms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, quote_plus, urlencode

from services.discovery.roles import ADJACENT_ROLES, ALL_ROLES, CORE_ROLES, INDUSTRIES, ROLE_GROUPS

PLATFORMS = ("google", "linkedin", "wellfound")

GOOGLE_RECENCY = {"Past 24 hours": "qdr:d", "Past week": "qdr:w", "Past month": "qdr:m", "Any time": ""}
LINKEDIN_RECENCY = {"Past 24 hours": "r86400", "Past week": "r604800", "Past month": "r2592000", "Any time": ""}

ATS_SITES = "(site:boards.greenhouse.io OR site:job-boards.greenhouse.io OR site:jobs.lever.co OR site:jobs.ashbyhq.com)"


@dataclass(frozen=True)
class SearchQuery:
    platform: str          # google | linkedin | wellfound
    market: str            # india | international
    role: str | None       # role title searched, if any
    category: str          # core_pm | related_product | adjacent | industry | company
    intent: str            # short label, e.g. "India", "Remote (India)", "Remote, accepts India"
    text: str              # human-readable query
    url: str               # link to open


def _category_of(role: str) -> str:
    for cat, roles in ROLE_GROUPS.items():
        if role in roles:
            return cat
    return "core_pm"


def _q(role: str) -> str:
    return f'"{role}"'


def _short(role: str) -> str:
    """'Product Management Intern' -> 'Product Management' (for 'intern OR internship' queries)."""
    return re.sub(r"\s+(intern|trainee)$", "", role, flags=re.I).strip()


# --------------------------------------------------------------------------- URL builders


def google_url(query: str, recency: str = "Past month") -> str:
    params = {"q": query}
    tbs = GOOGLE_RECENCY.get(recency, "")
    if tbs:
        params["tbs"] = tbs
    return "https://www.google.com/search?" + urlencode(params, quote_via=quote_plus)


def linkedin_url(keywords: str, location: str, remote: bool = False, recency: str = "Past month") -> str:
    """LinkedIn Jobs search filtered to the Internship experience level (f_E=1)."""
    params = {"keywords": keywords, "location": location, "f_E": "1"}
    if remote:
        params["f_WT"] = "2"
    tpr = LINKEDIN_RECENCY.get(recency, "")
    if tpr:
        params["f_TPR"] = tpr
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params, quote_via=quote)


def wellfound_role_url(role_slug: str, location_slug: str | None = None) -> str:
    """Wellfound role pages: /role/l/<role>/<location> or /role/r/<role> for remote."""
    if location_slug:
        return f"https://wellfound.com/role/l/{role_slug}/{location_slug}"
    return f"https://wellfound.com/role/r/{role_slug}"


# --------------------------------------------------------------------------- generators


def google_queries(recency: str = "Past month") -> list[SearchQuery]:
    out: list[SearchQuery] = []

    def add(market: str, role: str | None, category: str, intent: str, text: str) -> None:
        out.append(SearchQuery("google", market, role, category, intent, text, google_url(text, recency)))

    for role in ALL_ROLES:
        cat = _category_of(role)
        short = _short(role)
        add("india", role, cat, "India", f'{_q(role)} India')
        add("india", role, cat, "India startups", f'"{short}" (intern OR internship) startup India')
        add("india", role, cat, "Remote (India)", f'{_q(role)} remote India')
        add("international", role, cat, "Remote, accepts India",
            f'{_q(role)} remote ("India" OR "anywhere" OR "worldwide" OR "APAC")')
        if role in CORE_ROLES:
            add("india", role, cat, "LinkedIn via Google", f'site:linkedin.com/jobs/view {_q(role)} India')
            add("india", role, cat, "Wellfound via Google", f'site:wellfound.com/jobs {_q(role)} India')
            add("international", role, cat, "Career pages (ATS)", f'{ATS_SITES} {_q(role)} remote')
            add("india", role, cat, "Career pages (ATS) India", f'{ATS_SITES} {_q(role)} India')

    for industry in INDUSTRIES:
        add("india", None, "industry", f"Industry: {industry}",
            f'("product intern" OR "product management intern" OR "APM intern") {industry} India')
        add("international", None, "industry", f"Industry: {industry} (remote)",
            f'("product intern" OR "product management intern") {industry} remote "India"')
    return out


def linkedin_queries(recency: str = "Past month") -> list[SearchQuery]:
    out: list[SearchQuery] = []
    for role in ALL_ROLES:
        cat = _category_of(role)
        out.append(SearchQuery("linkedin", "india", role, cat, "India", f"{role} · India",
                               linkedin_url(role, "India", recency=recency)))
        out.append(SearchQuery("linkedin", "india", role, cat, "Remote (India)", f"{role} · India · Remote",
                               linkedin_url(role, "India", remote=True, recency=recency)))
        out.append(SearchQuery("linkedin", "international", role, cat, "Remote worldwide",
                               f"{role} · Worldwide · Remote",
                               linkedin_url(role, "Worldwide", remote=True, recency=recency)))
    return out


_WELLFOUND_ROLE_SLUGS = {
    "core_pm": "product-manager",
    "related_product": "product-analyst",
    "adjacent": "business-analyst",
}
_WELLFOUND_CITIES = ("india", "bangalore", "mumbai", "delhi", "gurgaon", "pune", "hyderabad")


def wellfound_queries(recency: str = "Past month") -> list[SearchQuery]:
    out: list[SearchQuery] = []
    for cat, slug in _WELLFOUND_ROLE_SLUGS.items():
        for city in _WELLFOUND_CITIES:
            out.append(SearchQuery("wellfound", "india", None, cat, f"Role page: {city.title()}",
                                   f"Wellfound {slug} · {city}", wellfound_role_url(slug, city)))
        out.append(SearchQuery("wellfound", "international", None, cat, "Role page: Remote",
                               f"Wellfound {slug} · remote", wellfound_role_url(slug)))

    # Wellfound has no intern-specific listing page, so intern searches go through Google.
    for role in ALL_ROLES:
        cat = _category_of(role)
        text = f'site:wellfound.com/jobs {_q(role)}'
        out.append(SearchQuery("wellfound", "india", role, cat, "Intern listings (via Google)",
                               text + " India", google_url(text + " India", recency)))
        if role in CORE_ROLES:
            out.append(SearchQuery("wellfound", "international", role, cat, "Remote intern listings (via Google)",
                                   text + " remote", google_url(text + " remote", recency)))

    # Early-stage companies likely to consider unsolicited internship applications.
    for industry in INDUSTRIES:
        text = f'site:wellfound.com/company India {industry} startup "Seed" OR "Series A"'
        out.append(SearchQuery("wellfound", "india", None, "company", f"Early-stage companies: {industry}",
                               text, google_url(text, "Any time")))
    remote_text = 'site:wellfound.com/company "remote" "India" startup product'
    out.append(SearchQuery("wellfound", "international", None, "company", "Remote-first companies hiring in India",
                           remote_text, google_url(remote_text, "Any time")))
    return out


def all_queries(platform: str, recency: str = "Past month") -> list[SearchQuery]:
    builders = {"google": google_queries, "linkedin": linkedin_queries, "wellfound": wellfound_queries}
    if platform not in builders:
        raise ValueError(f"platform must be one of {PLATFORMS}")
    return builders[platform](recency)


def plan_queries(queries: list[SearchQuery], limit: int, india_ratio: float = 0.8) -> list[SearchQuery]:
    """Pick `limit` queries keeping the India/international mix at india_ratio.

    Within each market, queries are taken round-robin across roles/industries so each role-title
    variation gets searched before any role gets a second variant.
    """
    from services.quota import next_market

    buckets: dict[str, list[SearchQuery]] = {"india": [], "international": []}
    for market in buckets:
        groups: dict[str, list[SearchQuery]] = {}
        for q in queries:
            if q.market == market:
                groups.setdefault(q.role or q.intent, []).append(q)
        lists = list(groups.values())
        while any(lists):
            for lst in lists:
                if lst:
                    buckets[market].append(lst.pop(0))

    picked: list[SearchQuery] = []
    counts = {"india": 0, "international": 0}
    while len(picked) < limit and (buckets["india"] or buckets["international"]):
        market = next_market(counts["india"], counts["international"], india_ratio)
        if not buckets[market]:
            market = "international" if market == "india" else "india"
        picked.append(buckets[market].pop(0))
        counts[market] += 1
    return picked


__all__ = [
    "ADJACENT_ROLES", "PLATFORMS", "SearchQuery", "all_queries", "google_queries", "linkedin_queries",
    "wellfound_queries", "plan_queries", "google_url", "linkedin_url", "wellfound_role_url",
]
