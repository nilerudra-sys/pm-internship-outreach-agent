"""Domain vocabulary shared by the database, agents and dashboard (mirrors CHECK constraints)."""

from __future__ import annotations

from datetime import datetime, timezone
from urllib.parse import urlparse

MARKETS = ("india", "international")
SECTORS = ("AI", "SaaS", "Fintech", "EdTech", "Consumer Tech", "Other")
REMOTE_POLICIES = ("remote", "remote_india_ok", "hybrid", "onsite", "unknown")
COMPANY_STATUSES = ("discovered", "researched", "contacts_found", "contacted", "disqualified")
ROLE_CATEGORIES = (
    "founder", "cofounder", "ceo", "executive", "head_of_product", "product_lead", "product_manager",
    "hiring_manager", "talent_acquisition", "recruiter", "other",
)
EMAIL_TYPES = ("public_verified", "hunter", "unverified", "none")
VERIFICATION_STATUSES = ("unverified", "evidence_found", "manually_verified")
EMAIL_STATUSES = ("pending_review", "approved", "rejected", "sent", "failed", "replied")

# Hybrid approval: an email can only be sent after a human approves it.
EMAIL_TRANSITIONS: dict[str, set[str]] = {
    "pending_review": {"approved", "rejected"},
    "approved": {"sent", "failed", "pending_review", "rejected"},
    "rejected": {"pending_review"},
    "failed": {"approved", "rejected"},
    "sent": {"replied"},
    "replied": set(),
}


def utc_now() -> str:
    """Current UTC time in the same format as the schema defaults."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def normalize_domain(value: str) -> str:
    """'https://www.Example.com/about' -> 'example.com'."""
    value = value.strip().lower()
    if not value:
        raise ValueError("domain is empty")
    if "://" not in value:
        value = "http://" + value
    host = urlparse(value).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    if "." not in host:
        raise ValueError(f"not a valid domain: {value!r}")
    return host
