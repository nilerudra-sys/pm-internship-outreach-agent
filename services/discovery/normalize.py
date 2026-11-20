"""Normalisation of raw listings into Leads, plus the keys used for deduplication."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

SOURCES = ("google", "linkedin", "wellfound", "career_page", "csv", "manual")
REMOTE_STATUSES = ("remote", "hybrid", "onsite", "unknown")

# Hosts that list jobs for many companies: their domain is not the employer's domain.
JOB_BOARD_HOSTS = (
    "linkedin.com", "wellfound.com", "angel.co", "google.com", "greenhouse.io",
    "lever.co", "ashbyhq.com", "workable.com", "indeed.com", "naukri.com",
    "internshala.com", "glassdoor.com", "glassdoor.co.in", "instahyre.com",
    "cutshort.io", "unstop.com", "foundit.in", "iimjobs.com", "hirist.tech",
    "smartrecruiters.com", "myworkdayjobs.com", "keka.com", "darwinbox.in",
    "zohorecruit.com", "zohorecruit.in", "freshteam.com", "ycombinator.com",
    "workatastartup.com", "remoteok.com", "weworkremotely.com", "notion.site",
)

_LEGAL_SUFFIXES = {
    "pvt", "private", "ltd", "limited", "inc", "incorporated", "llc", "llp",
    "corp", "corporation", "co", "company", "gmbh", "plc", "pte", "sa", "bv",
}

_TRACKING_PARAMS = re.compile(
    r"^(utm_.*|gclid|fbclid|msclkid|trk|trkinfo|refid|trackingid|ref|referer|referrer|"
    r"src|source|lipi|originalsubdomain|position|pagenum|ebp|eid|gh_src|lever-source.*|"
    r"lever-origin|_ga|mc_cid|mc_eid|si)$",
    re.IGNORECASE,
)

INDIA_PLACES = (
    "india", "bengaluru", "bangalore", "mumbai", "new delhi", "delhi", "ncr", "gurgaon",
    "gurugram", "noida", "pune", "hyderabad", "chennai", "kolkata", "ahmedabad", "jaipur",
    "kochi", "cochin", "chandigarh", "indore", "coimbatore", "thiruvananthapuram",
    "trivandrum", "bhubaneswar", "lucknow", "nagpur", "surat", "vadodara", "mysore",
    "mysuru", "goa", "visakhapatnam", "vizag",
)
_REMOTE_WORDS = re.compile(r"\b(remote|work from home|wfh|anywhere|distributed|fully[- ]remote)\b", re.I)
_HYBRID_WORDS = re.compile(r"\bhybrid\b", re.I)
_ONSITE_WORDS = re.compile(r"\b(on[- ]?site|in[- ]office|office[- ]based|in person)\b", re.I)
_WORLDWIDE = re.compile(r"\b(anywhere|worldwide|global(ly)?|apac|asia)\b", re.I)


# --------------------------------------------------------------------------- keys


def normalize_company_name(name: str) -> str:
    """'Razorpay Software Pvt. Ltd.' -> 'razorpay software'."""
    text = html.unescape(name or "").lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9 ]+", " ", text)
    words = [w for w in text.split() if w]
    while words and (words[-1] in _LEGAL_SUFFIXES or words[-1] == "and"):
        words.pop()
    return " ".join(words)


def normalize_title(title: str) -> str:
    """'Product Management Intern (Remote) - 2026' -> 'product management intern'."""
    text = html.unescape(title or "").lower()
    text = re.sub(r"\((remote|hybrid|on-?site|wfh|paid|unpaid|[^)]*\d{4}[^)]*)\)", " ", text)
    text = text.replace("internship", "intern").replace("&", " and ")
    text = re.sub(r"\b(19|20)\d{2}\b", " ", text)
    text = re.sub(r"[^a-z0-9' ]+", " ", text).replace("'", "")
    return " ".join(text.split())


def host_of(url: str) -> str:
    host = (urlparse(url if "://" in url else "https://" + url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_job_board(url_or_host: str) -> bool:
    host = host_of(url_or_host) if ("/" in url_or_host or "://" in url_or_host) else url_or_host.lower()
    return any(host == h or host.endswith("." + h) for h in JOB_BOARD_HOSTS)


def registrable_domain(url: str) -> str | None:
    """Best-effort employer domain: 'https://careers.acme.co.in/x' -> 'acme.co.in'."""
    host = host_of(url)
    if not host or "." not in host:
        return None
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"co", "com", "net", "org", "ac", "gov"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def normalize_job_url(url: str | None) -> str | None:
    """Canonical form for dedup: drops tracking params, fragments, www, and LinkedIn/Wellfound slugs."""
    if not url or not url.strip():
        return None
    raw = url.strip()
    parsed = urlparse(raw if "://" in raw else "https://" + raw)
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host:
        return None
    path = re.sub(r"/{2,}", "/", parsed.path).rstrip("/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=False))

    if host.endswith("linkedin.com"):
        job_id = query.get("currentJobId")
        m = re.search(r"/jobs/view/(?:[^/]*?-)?(\d{6,})", path)
        if m:
            job_id = m.group(1)
        if job_id:
            return f"linkedin.com/jobs/view/{job_id}"
    if host.endswith("wellfound.com") or host.endswith("angel.co"):
        m = re.search(r"/jobs/(\d+)", path)
        if m:
            return f"wellfound.com/jobs/{m.group(1)}"

    kept = sorted((k, v) for k, v in query.items() if not _TRACKING_PARAMS.match(k))
    q = urlencode(kept)
    return f"{host}{path.lower() if host.endswith(('lever.co', 'ashbyhq.com')) else path}" + (f"?{q}" if q else "")


def is_valid_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.hostname) and "." in (parsed.hostname or "")


# --------------------------------------------------------------------------- field inference


def detect_market(location: str | None) -> tuple[str, str | None]:
    """(market, country) from free-text location. 'Remote' alone is unknown."""
    text = (location or "").lower()
    if not text.strip():
        return "unknown", None
    if any(re.search(rf"\b{re.escape(p)}\b", text) for p in INDIA_PLACES):
        return "india", "India"
    stripped = _REMOTE_WORDS.sub(" ", _HYBRID_WORDS.sub(" ", text))
    stripped = re.sub(r"[^a-z ]+", " ", stripped).strip()
    if not stripped or _WORLDWIDE.fullmatch(stripped.strip()):
        return "unknown", None
    # Use the last comma-separated part as a country guess (e.g. "Berlin, Germany").
    last = (location or "").split(",")[-1].strip()
    country = last.title() if last and not _REMOTE_WORDS.search(last) else None
    return "international", country


def detect_remote(*texts: str | None) -> str:
    joined = " ".join(t for t in texts if t)
    if _HYBRID_WORDS.search(joined):
        return "hybrid"
    if _REMOTE_WORDS.search(joined):
        return "remote"
    if _ONSITE_WORDS.search(joined):
        return "onsite"
    return "unknown"


def detect_compensation(*texts: str | None) -> str:
    joined = " ".join(t for t in texts if t).lower()
    if re.search(r"\bunpaid\b|\bno stipend\b|\bwithout stipend\b", joined):
        return "unpaid"
    if re.search(r"\bstipend\b|₹|\binr\b|\brs\.?\s?\d|\$\s?\d|\bper month\b|/month\b|\bpaid internship\b|\bper hour\b", joined):
        return "paid"
    return "unknown"


_SECTOR_RULES = (
    (r"fintech|bank|financ|payment|insur|lending|wealth", "Fintech"),
    (r"edtech|education|learning", "EdTech"),
    (r"\bai\b|machine learning|\bml\b|artificial intelligence|genai", "AI"),
    (r"saas|enterprise|developer|\bb2b\b|software|cloud|devtools", "SaaS"),
    (r"consumer|e-?commerce|gaming|media|entertainment|social|retail|d2c", "Consumer Tech"),
)


def sector_for_industry(industry: str | None) -> str:
    """Map any industry text onto the outreach pipeline's sector list (unmatched -> 'Other')."""
    text = (industry or "").lower()
    for pattern, sector in _SECTOR_RULES:
        if re.search(pattern, text):
            return sector
    return "Other"


def clean_text(value: str | None, limit: int = 20000) -> str | None:
    """Strip HTML tags/entities and collapse whitespace."""
    if not value:
        return None
    text = html.unescape(value)
    text = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "\n- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:limit] or None


# --------------------------------------------------------------------------- lead


@dataclass
class Lead:
    """A raw opportunity from any source, before it is merged into the database."""

    company_name: str
    job_title: str
    source: str
    discovered_at: str = field(default_factory=lambda: date.today().isoformat())
    job_url: str | None = None
    website: str | None = None
    description: str | None = None
    location: str | None = None
    remote_status: str | None = None
    country: str | None = None
    market: str | None = None
    stipend: str | None = None
    duration: str | None = None
    posted_date: str | None = None
    source_url: str | None = None  # where it was found, if different from job_url

    def __post_init__(self) -> None:
        if self.source not in SOURCES:
            raise ValueError(f"source must be one of {SOURCES}")
        self.company_name = " ".join((self.company_name or "").split())
        self.job_title = " ".join(html.unescape(self.job_title or "").split())
        if not self.company_name:
            raise ValueError("company name is required")
        if not self.job_title:
            raise ValueError("job title is required")
        self.description = clean_text(self.description)
        if self.remote_status not in REMOTE_STATUSES:
            self.remote_status = detect_remote(self.location, self.job_title)
        if self.market not in ("india", "international"):
            market, country = detect_market(self.location)
            self.market = market
            self.country = self.country or country

    @property
    def company_name_norm(self) -> str:
        return normalize_company_name(self.company_name)

    @property
    def job_title_norm(self) -> str:
        return normalize_title(self.job_title)

    @property
    def job_url_norm(self) -> str | None:
        return normalize_job_url(self.job_url)

    @property
    def company_domain(self) -> str | None:
        """Employer domain from the website, or from the job URL if it is a company careers site."""
        for candidate in (self.website, self.job_url):
            if candidate and is_valid_url(candidate if "://" in candidate else "https://" + candidate):
                if not is_job_board(candidate):
                    return registrable_domain(candidate)
        return None

    @property
    def compensation(self) -> str:
        return detect_compensation(self.stipend, self.description)
