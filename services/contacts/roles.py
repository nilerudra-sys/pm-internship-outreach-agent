"""Classify published designations into outreach role categories. Interns are excluded."""

from __future__ import annotations

import re

ROLE_LABELS = {
    "founder": "Founder",
    "cofounder": "Co-founder",
    "ceo": "CEO",
    "executive": "Executive (CTO/COO/…)",
    "head_of_product": "Head of Product",
    "product_lead": "Product Lead",
    "product_manager": "Product Manager",
    "hiring_manager": "Hiring Manager",
    "talent_acquisition": "Talent Acquisition",
    "recruiter": "Recruiter",
    "other": "Other",
}
TARGET_ROLES = tuple(k for k in ROLE_LABELS if k != "other")

_EXCLUDE = re.compile(r"\b(intern|interns|internship|trainee|apprentice|student|fellow)\b", re.I)
_NOT_EMPLOYEE = re.compile(r"\b(advisor|adviser|investor|board member|mentor|angel|alumni|alumnus|ex-|former)\b",
                           re.I)

# Ordered: first match wins.
_RULES: tuple[tuple[str, str], ...] = (
    ("cofounder", r"\bco-?\s?founder\b|\bcofounder\b"),
    ("founder", r"\bfounder\b|\bfounding partner\b"),
    ("ceo", r"\bceo\b|\bchief executive\b|\bmanaging director\b|\bmd\b"),
    ("head_of_product", r"\bcpo\b|\bchief product\b|\bhead of product\b|\bvp,?\s+(of\s+)?product\b|"
                        r"\bvice president,?\s+(of\s+)?product\b|\bdirector,?\s+(of\s+)?product\b|"
                        r"\bproduct director\b|\bsvp,?\s+product\b"),
    ("product_lead", r"\bproduct lead\b|\blead product\b|\bgroup product manager\b|\bgpm\b|"
                     r"\bprincipal product manager\b|\bproduct head\b"),
    ("product_manager", r"\bproduct manager\b|\bproduct owner\b|\bapm\b|\bassociate product manager\b|"
                        r"\bproduct management\b"),
    ("hiring_manager", r"\bhiring manager\b"),
    ("talent_acquisition", r"\btalent acquisition\b|\bhead of talent\b|\btalent partner\b|\btalent lead\b|"
                           r"\bhead of people\b|\bpeople partner\b|\bchro\b|\bhr business partner\b|"
                           r"\bhuman resources\b|\bhr manager\b|\bhead of hr\b|\bpeople operations\b"),
    ("recruiter", r"\brecruiter\b|\brecruiting\b|\brecruitment\b|\bsourcer\b"),
    ("executive", r"\bcto\b|\bcoo\b|\bcbo\b|\bcmo\b|\bcfo\b|\bchief\b|\bpresident\b|\bgeneral manager\b"),
)


def classify_designation(title: str | None) -> str:
    """Category for a published title. Interns, advisors, investors and unrelated roles -> 'other'."""
    text = (title or "").strip().lower()
    if not text or _EXCLUDE.search(text) or _NOT_EMPLOYEE.search(text):
        return "other"
    for category, pattern in _RULES:
        if re.search(pattern, text):
            return category
    return "other"


def is_target(title: str | None) -> bool:
    return classify_designation(title) != "other"
