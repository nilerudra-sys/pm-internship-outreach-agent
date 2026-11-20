"""Find people (name + published designation) on company pages and in job listings.

Rule-based and deterministic. A person is extracted only when a name and a target-role title appear
together (same line, adjacent lines, or one sentence such as "founded by A and B"). The text they
came from is kept as evidence. Nothing is inferred: no name, title or email is invented.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from database.migrations import name_norm
from services.contacts.emails import email_matches_name, validate_email
from services.contacts.roles import classify_designation

_NAME_WORD = r"[A-Z][a-zA-Z'’\-]{1,20}\.?"
_NAME_CORE = rf"{_NAME_WORD}(?:\s+{_NAME_WORD}){{1,3}}"
_NAME_LINE = re.compile(rf"^(?:(?:Dr|Mr|Mrs|Ms|Prof)\.?\s+)?({_NAME_CORE})$")
_SEPARATORS = re.compile(r"\s+[-–—|·•]\s+|\s*,\s+|\s*:\s+")
_PAREN = re.compile(rf"^({_NAME_CORE})\s*\(([^)]{{3,80}})\)$")
_FOUNDED_BY = re.compile(
    rf"\b(?i:co-?founded|founded|started|set up)\s+(?:in\s+\d{{4}}\s+)?by\s+"
    rf"({_NAME_CORE})(?:\s*(?:,|and|&)\s*({_NAME_CORE}))?(?:\s*(?:,|and|&)\s*({_NAME_CORE}))?")
_TITLE_THEN_NAME = re.compile(
    rf"\b((?:Co-?founder|Founder|CEO|Chief [A-Z][a-z]+ Officer|Head of Product|VP,? Product|Product Lead|"
    rf"Talent Acquisition(?: [A-Z][a-z]+)?|Recruiter|Hiring Manager)(?:\s*(?:&|and)\s*[A-Z][A-Za-z ]{{1,20}}?)?)"
    rf",?\s+({_NAME_CORE})\b")
_NAME_COMMA_TITLE = re.compile(   # case-insensitive for the title only, never for the name
    rf"\b({_NAME_CORE}),\s+(?i:(?:our|the|a)\s+)?((?i:co-?founder|founder|ceo|chief [a-z]+ officer|head of product|"
    rf"product lead|product manager|senior recruiter|recruiter|talent acquisition [a-z]+|hiring manager)"
    rf"[a-zA-Z &]*)")

# Capitalised words that are not personal names.
_NOT_NAME = {
    "about", "our", "team", "the", "product", "products", "head", "founder", "founders", "co", "cofounder",
    "chief", "officer", "india", "contact", "careers", "career", "privacy", "terms", "home", "blog", "pricing",
    "login", "sign", "join", "meet", "leadership", "board", "advisors", "investors", "company", "read", "more",
    "learn", "view", "profile", "linkedin", "twitter", "email", "customer", "customers", "support", "help",
    "policy", "pvt", "ltd", "limited", "inc", "technologies", "private", "solutions", "services", "platform",
    "management", "manager", "lead", "director", "president", "vice", "global", "senior", "junior", "people",
    "operations", "engineering", "design", "marketing", "sales", "growth", "hiring", "talent", "acquisition",
    "recruiter", "ceo", "cto", "coo", "cpo", "cfo", "officer", "group", "partner", "partners", "ventures",
    "capital", "fund", "labs", "studio", "app", "apps", "api", "apis", "cloud", "data", "news", "press",
    "open", "roles", "jobs", "apply", "now", "here", "today", "story", "mission", "values", "culture", "life",
    "work", "with", "us", "we", "you", "your", "at", "and", "of", "for", "in", "by", "to", "on", "new",
    "bengaluru", "bangalore", "mumbai", "delhi", "pune", "hyderabad", "chennai", "gurugram", "gurgaon", "noida",
    "remote", "hybrid", "office", "offices", "street", "road", "floor", "building", "copyright", "all", "rights",
    "reserved", "follow", "download", "get", "started", "free", "trial", "demo", "book", "talk", "sales",
}


@dataclass
class PersonCandidate:
    full_name: str
    role_title: str
    role_category: str
    evidence: str
    source_url: str | None
    email: str | None = None          # only if published on the same page next to/for this person
    linkedin_url: str | None = None   # only if linked from the same page with the person's name

    @property
    def key(self) -> str:
        return name_norm(self.full_name)


def plausible_name(text: str, ignore_words: set[str] = frozenset()) -> bool:
    text = re.sub(r"^(?:Dr|Mr|Mrs|Ms|Prof)\.?\s+", "", text.strip())
    words = text.split()
    if not 2 <= len(words) <= 4:
        return False
    for w in words:
        bare = re.sub(r"[^a-z]", "", w.lower())
        if not bare or bare in _NOT_NAME or bare in ignore_words or len(bare) < 2:
            return False
        if not w[0].isupper() or (w.isupper() and len(w) > 2):   # 'CEO', 'API' are not name words
            return False
    return True


def _clean_title(title: str) -> str:
    return " ".join(title.strip(" -–—|·•,:").split())[:120]


def best_name(text: str, ignore: set[str] = frozenset()) -> str | None:
    """'Contact Kavya Menon' -> 'Kavya Menon': drop leading non-name words while 2+ words remain."""
    words = re.sub(r"^(?:Dr|Mr|Mrs|Ms|Prof)\.?\s+", "", text.strip().rstrip(".,;:")).split()
    for start in range(len(words) - 1):
        candidate = " ".join(words[start:])
        if plausible_name(candidate, ignore):
            return candidate
    return None


def _candidate(name: str, title: str, evidence: str, url: str | None, ignore: set[str]) -> PersonCandidate | None:
    name = best_name(name, ignore)
    title = _clean_title(title)
    if not name or len(title.split()) > 12:
        return None
    category = classify_designation(title)
    if category == "other":
        return None
    return PersonCandidate(name, title, category, " ".join(evidence.split())[:300], url)


def extract_people(text: str, source_url: str | None = None, company_words: set[str] = frozenset(),
                   links: list[tuple[str, str]] | None = None, company_domain: str | None = None
                   ) -> list[PersonCandidate]:
    """People with target roles found in `text` (one item per line). `company_words` are excluded from names."""
    lines = [" ".join(ln.split()) for ln in (text or "").split("\n")]
    lines = [ln for ln in lines if ln]
    found: dict[str, PersonCandidate] = {}

    def add(c: PersonCandidate | None) -> None:
        if c and c.key not in found:
            found[c.key] = c

    for i, line in enumerate(lines):
        # 1. "Name" line followed (or preceded) by a short title line.
        m = _NAME_LINE.match(line)
        if m and plausible_name(m.group(1), company_words):
            for j in (i + 1, i - 1):
                if 0 <= j < len(lines) and len(lines[j].split()) <= 10 and classify_designation(lines[j]) != "other":
                    add(_candidate(m.group(1), lines[j], f"{line} — {lines[j]}" if j > i else f"{lines[j]} — {line}",
                                   source_url, company_words))
                    break
        # 2. "Name - Title", "Name | Title", "Name, Title", "Title: Name"; and "Name (Title)".
        pm = _PAREN.match(line)
        if pm:
            add(_candidate(pm.group(1), pm.group(2), line, source_url, company_words))
        parts = _SEPARATORS.split(line, maxsplit=1)
        if len(parts) == 2 and len(line.split()) <= 16:
            left, right = parts
            if plausible_name(left, company_words):
                add(_candidate(left, right, line, source_url, company_words))
            elif plausible_name(right, company_words):
                add(_candidate(right, left, line, source_url, company_words))
        # 3. Sentences: "founded by A and B", "Co-founder & CEO A", "A, our co-founder".
        for fm in _FOUNDED_BY.finditer(line):
            for name in filter(None, fm.groups()):
                add(_candidate(name, "Founder (\"founded by\")", line, source_url, company_words))
        for tm in _TITLE_THEN_NAME.finditer(line):
            add(_candidate(tm.group(2), tm.group(1), line, source_url, company_words))
        for nm in _NAME_COMMA_TITLE.finditer(line):
            add(_candidate(nm.group(1), nm.group(2), line, source_url, company_words))

    people = list(found.values())
    _attach_published_contacts(people, links or [], company_domain)
    return people


def _attach_published_contacts(people: list[PersonCandidate], links: list[tuple[str, str]],
                               company_domain: str | None) -> None:
    """Attach mailto: and LinkedIn links published on the same page, only when clearly that person's."""
    for href, anchor in links:
        href = (href or "").strip()
        low = href.lower()
        if low.startswith("mailto:"):
            check = validate_email(href, company_domain)
            if not check.ok or check.generic or not check.domain_matches:
                continue
            owners = [p for p in people if email_matches_name(check.email, p.full_name)]
            if len(owners) == 1 and owners[0].email is None:
                owners[0].email = check.email
        elif re.match(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/?#]+", low):
            owners = [p for p in people if p.full_name.lower() in (anchor or "").lower()
                      or all(part.lower() in low for part in p.full_name.split()[:2])]
            if len(owners) == 1 and owners[0].linkedin_url is None:
                owners[0].linkedin_url = href.split("?")[0]
