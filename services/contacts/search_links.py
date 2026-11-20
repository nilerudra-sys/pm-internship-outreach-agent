"""Google and LinkedIn search links for finding people at a company. Opened by you in your own browser;
the app never fetches these pages."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, urlencode

from services.discovery.queries import google_url

PEOPLE_ROLES = (
    ("founder", "founder"),
    ("cofounder", "co-founder"),
    ("ceo", "CEO"),
    ("head_of_product", "Head of Product"),
    ("product_lead", "Product Lead"),
    ("product_manager", "Product Manager"),
    ("hiring_manager", "hiring manager"),
    ("talent_acquisition", "talent acquisition"),
    ("recruiter", "recruiter"),
)
LINKEDIN_ROLES = ("founder", "co-founder", "CEO", "Head of Product", "Product Lead", "Product Manager",
                  "Talent Acquisition", "Recruiter")


@dataclass(frozen=True)
class PeopleSearch:
    platform: str     # google | linkedin
    role: str
    text: str
    url: str


def linkedin_people_url(company: str, title: str) -> str:
    """LinkedIn people search (requires you to be signed in; opened manually)."""
    return "https://www.linkedin.com/search/results/people/?" + urlencode(
        {"keywords": f"{company} {title}", "origin": "GLOBAL_SEARCH_HEADER"}, quote_via=quote)


def google_people_searches(company: str, domain: str | None = None) -> list[PeopleSearch]:
    name = f'"{company}"'
    out = [PeopleSearch("google", role, f"{name} {label}", google_url(f"{name} {label}", "Any time"))
           for role, label in PEOPLE_ROLES]
    for role, label in (("founder", "founder"), ("product_manager", "product manager"),
                        ("head_of_product", "head of product"), ("recruiter", "recruiter"),
                        ("talent_acquisition", "talent acquisition")):
        text = f'site:linkedin.com/in {name} {label}'
        out.append(PeopleSearch("google", role, text, google_url(text, "Any time")))
    if domain:
        text = f"site:{domain} (team OR leadership OR founders OR about)"
        out.append(PeopleSearch("google", "team_page", text, google_url(text, "Any time")))
    return out


def linkedin_people_searches(company: str) -> list[PeopleSearch]:
    return [PeopleSearch("linkedin", t.lower(), f"{company} {t}", linkedin_people_url(company, t))
            for t in LINKEDIN_ROLES]
