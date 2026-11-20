"""Gemini company research grounded in fetched website pages and discovered listings.

Two kinds of output, kept apart:
- VERIFIED facts: each cites source ids and carries a supporting quote. A fact is kept only if it
  passes the sentence-level checks in services/research/verify.py for every source it cites.
  `verified_summary` (copied to companies.research_summary) is built only from these facts.
- INTERPRETATION: summary, product, target_customers, business_model, industry, candidate_angle,
  reasoning and the sub-scores. Useful context, never to be stated in an email as fact.

Pages that could not be read (robots.txt, JavaScript-only, HTTP errors) are listed to Gemini as
unknown, so missing evidence is not treated as negative evidence.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from config.settings import PROJECT_ROOT
from database import repository
from database import research as research_repo
from database.models import utc_now
from services.discovery.analysis import _profile_text
from services.research.verify import check_multi, name_tokens

PROMPT_PATH = PROJECT_ROOT / "prompts" / "company_research.md"
SOURCE_CHARS = 5000           # per source, to keep prompts small
MIN_GROUNDING = 0.5           # loose check used only to drop obviously unsupported interpretation fields
NEUTRAL_SCORE = 5

SYSTEM = ("You summarise companies strictly from provided sources. Every fact cites a source id and copies "
          "a supporting quote word for word. You never use outside knowledge and never invent details.")

FACT_CATEGORIES = ("product_facts", "stage_signals", "hiring_signals", "remote_signals", "outreach_hooks")
INTERPRETATION_FIELDS = ("summary", "product", "target_customers", "business_model", "industry",
                         "candidate_angle", "reasoning")

WEIGHTS = {
    "pm_opportunity": 0.30,
    "product_depth": 0.20,
    "remote_india_fit": 0.20,
    "background_fit": 0.15,
    "personalisation_potential": 0.15,
}

_STOP = set("""a an and are as at be by for from has have in into is it its of on or our that the their them
they this to was we with will you your who what which can more most also than other such only over under
about across through using used use help helps make makes new all any each every one two""".split())


class Fact(BaseModel):
    text: str
    source: str = Field(description="Source id(s), e.g. P1 or 'P1, P2'")
    quote: str = Field(default="", description="The supporting sentence copied word for word from the source")


class ResearchScores(BaseModel):
    product_depth: int = Field(ge=0, le=10)
    pm_opportunity: int = Field(ge=0, le=10)
    remote_india_fit: int = Field(ge=0, le=10)
    background_fit: int = Field(ge=0, le=10)
    personalisation_potential: int = Field(ge=0, le=10)


class CompanyResearch(BaseModel):
    summary: str
    product: str | None = None
    target_customers: str | None = None
    business_model: str | None = None
    industry: str | None = None
    product_facts: list[Fact] = Field(default_factory=list)
    stage_signals: list[Fact] = Field(default_factory=list)
    hiring_signals: list[Fact] = Field(default_factory=list)
    remote_signals: list[Fact] = Field(default_factory=list)
    outreach_hooks: list[Fact] = Field(default_factory=list)
    candidate_angle: str | None = None
    scores: ResearchScores
    insufficient_information: bool
    reasoning: str


@dataclass
class Source:
    id: str
    kind: str        # page | listing
    url: str | None
    title: str
    text: str
    fetched_at: str | None = None


# --------------------------------------------------------------------------- sources + prompt


def build_sources(pages: list[dict], listings: list[dict]) -> list[Source]:
    sources = []
    for i, p in enumerate(pages, start=1):
        body = "\n".join(x for x in (p.get("description"), p.get("text")) if x)
        sources.append(Source(f"P{i}", "page", p.get("final_url") or p.get("url"),
                              f"{p.get('page_type', 'page')}: {p.get('title') or ''}".strip(),
                              body[:SOURCE_CHARS], p.get("fetched_at")))
    for i, o in enumerate(listings, start=1):
        sources.append(Source(f"L{i}", "listing", o.get("job_url"), f"job listing: {o.get('job_title')}",
                              (o.get("description") or "")[:SOURCE_CHARS], o.get("discovered_at")))
    return sources


def build_prompt(company: dict, sources: list[Source], profile: str | None = None,
                 unreadable: list[dict] | None = None) -> str:
    blocks = "\n\n".join(f"[{s.id}] {s.title} ({s.url or 'no url'})\n<<<\n{s.text}\n>>>" for s in sources)
    missing = "\n".join(f"- {u.get('page_type') or 'page'}: {u['url']} ({u['reason']})" for u in unreadable or [])
    values = {
        "profile": profile or _profile_text(),
        "company_name": company["name"],
        "domain": company["domain"],
        "market": company.get("market") or "unknown",
        "sources": blocks,
        "unreadable": missing or "(none)",
    }
    template = PROMPT_PATH.read_text(encoding="utf-8")
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


# --------------------------------------------------------------------------- validation + scoring


def _key_words(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9][a-z0-9+.#-]*", text.lower())
    return {w.strip(".-") for w in words if (len(w) >= 4 or w.isdigit()) and w not in _STOP}


def is_grounded(fact: str, source_text: str, threshold: float = MIN_GROUNDING) -> bool:
    """Loose whole-document overlap. Used ONLY to drop obviously unsupported interpretation fields;
    facts use the sentence-level checks in verify.py."""
    words = _key_words(fact)
    if not words:
        return False
    src = source_text.lower()
    return sum(1 for w in words if w in src) / len(words) >= threshold


def _cited_ids(source: str) -> list[str]:
    return [p.strip().strip("[]").upper() for p in re.split(r"[,;/ ]+", source or "") if p.strip().strip("[]")]


def validate_facts(result: CompanyResearch, sources: list[Source],
                   ignore: set[str] | None = None) -> tuple[dict[str, list[dict]], int]:
    """Keep only facts supported, sentence by sentence, by every source they cite."""
    by_id = {s.id: s for s in sources}
    ignore = ignore or set()
    kept: dict[str, list[dict]] = {}
    dropped = 0
    for category in FACT_CATEGORIES:
        kept[category] = []
        for fact in getattr(result, category):
            ids = _cited_ids(fact.source)
            if not ids or any(i not in by_id for i in ids):
                dropped += 1
                continue
            text = strip_citations(fact.text)
            ok, quotes, _ = check_multi(text, [(i, by_id[i].text + "\n" + by_id[i].title) for i in ids],
                                        ignore, fact.quote)
            if not ok:
                dropped += 1
                continue
            cited = [{"source_id": q["source_id"], "url": by_id[q["source_id"]].url, "quote": q["quote"],
                      "fetched_at": by_id[q["source_id"]].fetched_at} for q in quotes]
            kept[category].append({
                "text": text, "verified": True,
                "source_id": cited[0]["source_id"], "url": cited[0]["url"], "quote": cited[0]["quote"],
                "sources": cited,
            })
    return kept, dropped


def fit_score(scores: dict[str, int], *, insufficient: bool, hooks: int) -> tuple[int, list[str]]:
    total = round(sum(WEIGHTS[k] * max(0, min(10, int(scores.get(k, 0)))) for k in WEIGHTS) * 10)
    notes = []
    if insufficient and total > 40:
        total, notes = 40, notes + ["sources too thin to judge the company"]
    if hooks == 0 and total > 60:
        total, notes = 60, notes + ["no verified personalisation details"]
    return total, notes


def adjust_for_unreadable(scores: dict[str, int], unreadable: list[dict],
                          facts: dict[str, list[dict]]) -> tuple[dict[str, int], list[str]]:
    """An unreadable careers page means hiring is unknown, not absent: never score it below neutral."""
    notes = []
    careers_unreadable = any((u.get("page_type") == "careers") for u in unreadable)
    if careers_unreadable and not facts.get("hiring_signals") and scores["pm_opportunity"] < NEUTRAL_SCORE:
        scores = {**scores, "pm_opportunity": NEUTRAL_SCORE}
        notes.append("careers page unreadable: hiring treated as unknown")
    return scores, notes


def verified_summary(facts: dict[str, list[dict]], limit: int = 3) -> str | None:
    """Short summary made only from verified facts (product first, then stage, then hooks)."""
    picked, seen = [], set()
    for category in ("product_facts", "stage_signals", "outreach_hooks"):
        for f in facts.get(category, []):
            if f["text"].lower() not in seen and len(picked) < limit:
                seen.add(f["text"].lower())
                picked.append(f["text"].rstrip(".") + ".")
    return " ".join(picked) or None


# --------------------------------------------------------------------------- run


_INLINE_CITE = re.compile(r"\s*\[(?:[PL]\d+\s*,?\s*)+\]")


def strip_citations(text: str | None) -> str | None:
    """'Serves traders [P1, P2].' -> 'Serves traders.' (sources are stored separately)."""
    if text is None:
        return None
    return re.sub(r"\s+([.,;:])", r"\1", _INLINE_CITE.sub("", text)).strip()


def _grounded_or_none(value: str | None, sources: list[Source]) -> str | None:
    """Interpretation fields are dropped if clearly unrelated to every source."""
    value = strip_citations(value)
    if not value:
        return None
    return value if any(is_grounded(value, s.text + " " + s.title, 0.5) for s in sources) else None


def research_company(conn: sqlite3.Connection, company: dict, pages: list[dict], client,
                     profile: str | None = None, unreadable: list[dict] | None = None) -> dict[str, Any]:
    """Run Gemini research for one company and persist it. Returns the saved fields (or an error)."""
    listings = research_repo.linked_listings(conn, company)
    sources = build_sources(pages, listings)
    if unreadable is None:
        unreadable = research_repo.unreadable_pages(conn, company["id"])
    if not sources:
        err = "no readable sources: website pages could not be fetched and no listings are linked"
        research_repo.save_research(conn, company["id"], {"error": err, "researched_at": None,
                                                          "unreadable_pages": unreadable})
        return {"error": err}

    try:
        result: CompanyResearch = client.generate_json(
            build_prompt(company, sources, profile, unreadable), CompanyResearch, system=SYSTEM)
    except Exception as exc:
        err = str(exc)[:500]
        research_repo.save_research(conn, company["id"], {"error": err, "researched_at": None})
        return {"error": err}

    facts, dropped = validate_facts(result, sources, name_tokens(company["name"], company["domain"]))
    sub, unreadable_notes = adjust_for_unreadable(result.scores.model_dump(), unreadable, facts)
    score, notes = fit_score(sub, insufficient=result.insufficient_information, hooks=len(facts["outreach_hooks"]))
    notes = unreadable_notes + notes
    model = getattr(client, "last_model_used", None) or client.model
    summary_verified = verified_summary(facts)
    data = {
        # interpretation (not verified)
        "summary": strip_citations(result.summary),
        "product": _grounded_or_none(result.product, sources),
        "target_customers": _grounded_or_none(result.target_customers, sources),
        "business_model": _grounded_or_none(result.business_model, sources),
        "industry": strip_citations(result.industry),
        "candidate_angle": strip_citations(result.candidate_angle),
        "reasoning": result.reasoning,
        "score_breakdown": {**sub, **({"notes": notes} if notes else {})},
        # verified
        "facts": facts,
        "verified_summary": summary_verified,
        "dropped_facts": dropped,
        # provenance
        "sources": [{"id": s.id, "kind": s.kind, "url": s.url, "title": s.title, "fetched_at": s.fetched_at}
                    for s in sources],
        "unreadable_pages": unreadable,
        "fit_score": score,
        "insufficient": result.insufficient_information,
        "model": model,
        "researched_at": utc_now(),
        "error": None,
    }
    research_repo.save_research(conn, company["id"], data)

    # The company record only gets verified text. Never downgrade a company already further along.
    status = company["status"] if company["status"] != "discovered" else "researched"
    conn.execute(
        "UPDATE companies SET research_summary = ?, fit_score = ?, status = ?, updated_at = ? WHERE id = ?",
        (summary_verified, score, status, utc_now(), company["id"]),
    )
    if facts["hiring_signals"] and not company.get("hiring_signal"):
        repository.update_company(conn, company["id"], hiring_signal=facts["hiring_signals"][0]["text"][:300])
    return data
