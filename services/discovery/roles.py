"""Role taxonomy for Product Management internships, plus rule-based title pre-classification.

Rules are a cheap first pass (query generation, filtering career-page feeds). The final
category always comes from Gemini reading the actual responsibilities.
"""

from __future__ import annotations

import re

from services.discovery.normalize import normalize_title

CORE_ROLES = (
    "Product Management Intern",
    "Product Intern",
    "Product Manager Intern",
    "Associate Product Manager Intern",
    "APM Intern",
    "Junior Product Manager Intern",
    "Product Trainee",
    "Product Associate Intern",
    "Technical Product Management Intern",
)

RELATED_ROLES = (
    "Product Analyst Intern",
    "Product Strategy Intern",
    "Product Operations Intern",
    "Product Growth Intern",
    "Product Research Intern",
    "Product Development Intern",
    "Product Planning Intern",
    "Product Experience Intern",
    "Product Owner Intern",
)

# Only kept when the responsibilities are genuinely product work (enforced by Gemini).
ADJACENT_ROLES = (
    "Founder's Office Intern",
    "Business Analyst Intern",
    "Strategy and Operations Intern",
    "Growth Strategy Intern",
)

ROLE_GROUPS = {"core_pm": CORE_ROLES, "related_product": RELATED_ROLES, "adjacent": ADJACENT_ROLES}
ALL_ROLES = CORE_ROLES + RELATED_ROLES + ADJACENT_ROLES

CATEGORY_LABELS = {
    "core_pm": "Core Product Management",
    "related_product": "Related Product",
    "adjacent": "Adjacent Opportunity",
    "not_relevant": "Not Relevant",
    "unclassified": "Not analysed yet",
    "unmatched": "No title match",
}

INDUSTRIES = (
    "SaaS", "Fintech", "E-commerce", "Consumer technology", "AI and ML", "EdTech",
    "HealthTech", "Gaming", "Enterprise software", "Developer tools", "Logistics",
    "Mobility", "Media and entertainment", "Telecom", "Banking and financial services",
)

_INTERN = re.compile(r"\b(intern|interns|trainee|apprentice|apprenticeship|co ?op|summer analyst)\b")

# Patterns run on normalize_title() output (lowercase, 'internship' -> 'intern', no punctuation).
_CORE = [
    r"\bassociate product manag(er|ement)\b",
    r"\bapm\b",
    r"\b(junior |jr |technical |associate )?product manag(er|ement)\b",
    r"\bproduct (intern|trainee|associate)\b",
    r"\bpm intern\b",
]
_RELATED = [
    r"\bproduct (analyst|analytics|strategy|operations|ops|growth|research|development|"
    r"planning|experience|owner)\b",
    r"\bgrowth product\b",
]
_ADJACENT = [
    r"\bfounders? office\b",
    r"\bfounder s office\b",
    r"\bbusiness analyst\b",
    r"\bstrategy (and )?op(eration)?s\b",
    r"\bgrowth strategy\b",
    r"\bchief of staff\b",
]

# Phrases specific to product-management work. Generic words that also appear in engineering or
# design listings ("feature", "spec", "sprint", "stakeholder") are deliberately excluded.
PRODUCT_SIGNALS = (
    "roadmap", "prd", "product requirement", "user research", "user interview", "customer interview",
    "product analytics", "a/b test", "ab test", "backlog", "user stor", "product strategy",
    "product launch", "go-to-market", "go to market", "competitive analysis", "competitor analysis",
    "product metric", "customer feedback", "user feedback", "product discovery", "product sense",
    "product roadmap", "feature prioriti", "prioritise features", "prioritize features",
    "market research", "product spec", "funnel analys", "mixpanel", "amplitude",
)

# Titles naming a different function. These are only kept if the title also matches a product role.
_OTHER_FUNCTION = re.compile(
    r"\b(engineer|engineering|developer|scientist|designer|design|sales|account executive|recruit\w*|"
    r"legal|accounting|accountant|finance|hr|talent|marketing|content|writer|support|devops|qa|"
    r"security|hardware|mechanical|electrical|research scientist)\b"
)


def is_internship(title: str, employment_type: str | None = None) -> bool:
    """True if the title (or an employment-type field) says intern/trainee."""
    text = normalize_title(title) + " " + normalize_title(employment_type or "")
    return bool(_INTERN.search(text))


def classify_title(title: str) -> str:
    """'core_pm' | 'related_product' | 'adjacent' | 'unmatched' based on the title alone."""
    t = normalize_title(title)
    # Related patterns are more specific ("product analyst") than the core "product intern".
    if any(re.search(p, t) for p in _RELATED):
        return "related_product"
    if any(re.search(p, t) for p in _CORE):
        return "core_pm"
    if any(re.search(p, t) for p in _ADJACENT):
        return "adjacent"
    return "unmatched"


def product_signal_count(text: str | None) -> int:
    """Number of distinct product-work phrases found in a description."""
    if not text:
        return 0
    low = text.lower()
    return sum(1 for s in PRODUCT_SIGNALS if s in low)


def is_candidate_listing(title: str, description: str | None = None, employment_type: str | None = None) -> bool:
    """Should a feed item (e.g. from a career page) be imported at all?

    Requires an internship, and then either a product-family title or a description with
    clear product responsibilities, so responsibilities can outweigh the exact job title.
    """
    if not is_internship(title, employment_type):
        return False
    if classify_title(title) != "unmatched":
        return True
    if _OTHER_FUNCTION.search(normalize_title(title)):
        return False
    return product_signal_count(description) >= 3
