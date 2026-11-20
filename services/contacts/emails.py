"""Email validation. Addresses are only ever taken from a source; none are constructed from patterns."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from services.discovery.normalize import registrable_domain

_EMAIL = re.compile(r"^[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+$")
GENERIC_LOCAL_PARTS = {
    "info", "hello", "hi", "contact", "support", "help", "careers", "career", "jobs", "job", "hr", "hiring",
    "team", "admin", "sales", "press", "media", "marketing", "office", "mail", "enquiry", "enquiries",
    "queries", "noreply", "no-reply", "privacy", "legal", "billing", "accounts", "talent", "people", "recruitment",
    "partnerships", "feedback", "grievance", "compliance", "investors", "care",
}
WEBMAIL = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "proton.me", "protonmail.com",
           "rediffmail.com", "live.com", "aol.com", "zoho.com"}


@dataclass
class EmailCheck:
    email: str | None                       # normalised, or None if unusable
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.email is not None

    @property
    def generic(self) -> bool:
        return "generic mailbox" in self.problems

    @property
    def domain_matches(self) -> bool:
        return "domain does not match company" not in self.problems and "webmail address" not in self.problems


def validate_email(email: str | None, company_domain: str | None = None) -> EmailCheck:
    """Syntax check plus warnings: generic mailbox, webmail, domain mismatch."""
    value = (email or "").strip().lower().removeprefix("mailto:").split("?")[0]
    if not value:
        return EmailCheck(None, ["no email"])
    if not _EMAIL.match(value) or ".." in value:
        return EmailCheck(None, ["invalid email syntax"])
    local, domain = value.split("@", 1)
    problems = []
    if local in GENERIC_LOCAL_PARTS:
        problems.append("generic mailbox")
    if domain in WEBMAIL:
        problems.append("webmail address")
    elif company_domain and registrable_domain("https://" + domain) != registrable_domain("https://" + company_domain):
        problems.append("domain does not match company")
    return EmailCheck(value, problems)


def email_matches_name(email: str, full_name: str) -> bool:
    """True if the local part plausibly belongs to this person ('nithin@', 'n.kamath@', 'nithink@').
    Used only to attach an email that is PUBLISHED next to the person, never to construct one."""
    local = email.split("@", 1)[0].lower()
    parts = [p for p in re.sub(r"[^a-z ]", " ", full_name.lower()).split() if len(p) >= 2]
    if not parts:
        return False
    first, last = parts[0], parts[-1]
    letters = re.sub(r"[^a-z]", "", local)
    return (first in letters and len(first) >= 3) or (last in letters and len(last) >= 3) or \
        letters in (first[0] + last, first + last[0])
