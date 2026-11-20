"""Gemini analysis of opportunities: classification, extraction and scoring.

Gemini sees only the stored listing text. Its output can fill empty fields but never overwrites
data from the source, and it never supplies URLs, company names, sources or dates.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from config.settings import PROJECT_ROOT, load_profile
from database import opportunities as opp_repo
from database.models import utc_now
from services.discovery.scoring import DEFAULT_THRESHOLD, final_score, qualify

PROMPT_PATH = PROJECT_ROOT / "prompts" / "opportunity_analysis.md"
MIN_DESCRIPTION_CHARS = 80
MAX_DESCRIPTION_CHARS = 12000

SYSTEM = (
    "You extract facts from job listings. You never invent information. "
    "If a fact is not in the listing text, you return null or 'unknown'."
)


class SubScores(BaseModel):
    product_responsibilities: int = Field(ge=0, le=10)
    internship_eligibility: int = Field(ge=0, le=10)
    engineering_fit: int = Field(ge=0, le=10)
    location_fit: int = Field(ge=0, le=10)
    experience_fit: int = Field(ge=0, le=10)


class OpportunityAnalysis(BaseModel):
    role_category: Literal["core_pm", "related_product", "adjacent", "not_relevant"]
    is_internship: Literal["yes", "no", "unknown"]
    industry: str | None = Field(None, description="Industry as stated or clearly implied by the listing")
    company_product: str | None = Field(None, description="What the company builds, only if the listing says")
    location: str | None = None
    country: str | None = None
    remote_status: Literal["remote", "hybrid", "onsite", "unknown"]
    india_eligible: Literal["yes", "no", "unknown"]
    stipend: str | None = None
    compensation: Literal["paid", "unpaid", "unknown"]
    duration: str | None = None
    experience_required: str | None = None
    product_responsibilities: list[str] = Field(default_factory=list)
    scores: SubScores
    insufficient_information: bool
    reasoning: str = Field(description="Two or three sentences citing the listing")


def _profile_text() -> str:
    try:
        p = load_profile()
    except FileNotFoundError:
        return "B.Tech Computer Science student seeking a Product Management internship."
    a = p.get("applicant", {})
    projects = "; ".join(f"{x.get('name')}: {x.get('summary')}" for x in a.get("projects", []))
    return (
        f"Target role: {a.get('target_role')}. Education: {a.get('education')}, "
        f"specialisation {a.get('specialization')}. Skills: {', '.join(a.get('skills', []))}. "
        f"Projects: {projects}. Based in India; open to remote roles."
    )


def build_prompt(opp: dict, profile: str | None = None) -> str:
    template = Path(PROMPT_PATH).read_text(encoding="utf-8")
    desc = (opp.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    values = {
        "profile": profile or _profile_text(),
        "source": opp.get("primary_source") or "unknown",
        "discovered_at": opp.get("discovered_at") or "unknown",
        "company_name": opp.get("company_name") or "unknown",
        "job_title": opp.get("job_title") or "unknown",
        "location": opp.get("location") or "not stated",
        "remote_status": opp.get("remote_status") or "unknown",
        "stipend": opp.get("stipend") or "not stated",
        "description": desc,
    }
    for k, v in values.items():
        template = template.replace("{" + k + "}", str(v))
    return template


def apply_analysis(conn: sqlite3.Connection, opp: dict, result: OpportunityAnalysis, model: str,
                   threshold: int = DEFAULT_THRESHOLD) -> dict:
    """Persist an analysis. Returns the fields written."""
    text = (opp.get("description") or "").lower()

    # Fill only empty fields. Short free-text values must appear in the listing to be accepted.
    def grounded(value: str | None) -> str | None:
        """Accept verbatim matches, or values whose numbers all appear in the listing."""
        if not value:
            return None
        if value.lower() in text:
            return value
        numbers = re.findall(r"\d[\d,]*", value)
        if numbers and all(n.replace(",", "") in text.replace(",", "") for n in numbers):
            return value
        return None

    opp_repo.fill_missing(conn, opp["id"], {
        "industry": result.industry,
        "company_product": result.company_product,
        "location": grounded(result.location) or None,
        "country": result.country if result.country and result.country.lower() in text else None,
        "stipend": grounded(result.stipend),
        "duration": grounded(result.duration),
        "experience_required": result.experience_required,
    })

    market = opp.get("market") or "unknown"
    if market == "unknown" and result.country:
        market = "india" if result.country.strip().lower() == "india" else "international"

    sub = result.scores.model_dump()
    score, cap_notes = final_score(
        sub, role_category=result.role_category, is_internship=result.is_internship,
        market=market, india_eligible=result.india_eligible,
    )
    status, reason = qualify(score, result.role_category, result.insufficient_information, threshold)
    if cap_notes:
        reason += " (" + "; ".join(cap_notes) + ")"

    fields = {
        "role_category": result.role_category,
        "relevance_score": score,
        "score_breakdown": {**sub, "is_internship": result.is_internship},
        "product_responsibilities": result.product_responsibilities,
        "ai_reasoning": result.reasoning,
        "analysis_model": model,
        "analyzed_at": utc_now(),
        "analysis_error": None,
        "status": status,
        "status_reason": reason,
        "india_eligible": result.india_eligible,
        "market": market,
    }
    # Only upgrade enums that are still unknown, so source data wins.
    if opp.get("remote_status") in (None, "unknown"):
        fields["remote_status"] = result.remote_status
    if opp.get("compensation") in (None, "unknown"):
        fields["compensation"] = result.compensation
    opp_repo.save_analysis(conn, opp["id"], fields)
    return fields


def analyse_opportunity(conn: sqlite3.Connection, opp: dict, client, threshold: int = DEFAULT_THRESHOLD,
                        profile: str | None = None) -> dict:
    """Analyse one opportunity with Gemini. Listings without a description are skipped, not guessed."""
    if len(opp.get("description") or "") < MIN_DESCRIPTION_CHARS:
        opp_repo.save_analysis(conn, opp["id"], {
            "analysis_error": "skipped: no job description to analyse",
            "status_reason": "add the job description so it can be analysed",
        })
        return {"skipped": True}
    try:
        result = client.generate_json(build_prompt(opp, profile), OpportunityAnalysis, system=SYSTEM)
    except Exception as exc:  # recorded and shown in the dashboard
        opp_repo.save_analysis(conn, opp["id"], {"analysis_error": str(exc)[:500]})
        return {"error": str(exc)}
    # Record the model that actually produced the result (primary or fallback).
    model_used = getattr(client, "last_model_used", None) or client.model
    return {**apply_analysis(conn, opp, result, model_used, threshold), "model": model_used}


def analyse_pending(conn: sqlite3.Connection, client, limit: int = 10, threshold: int = DEFAULT_THRESHOLD,
                    on_progress: Callable[[int, int, dict], None] | None = None) -> dict[str, int]:
    """Analyse up to `limit` unanalysed opportunities (India first). Commits after each one."""
    todo = opp_repo.pending_analysis(conn, limit=limit, min_description=MIN_DESCRIPTION_CHARS)
    profile = _profile_text()
    summary = {"analysed": 0, "errors": 0, "qualified": 0, "rejected": 0, "by_model": {}}
    for i, opp in enumerate(todo, start=1):
        result = analyse_opportunity(conn, opp, client, threshold, profile)
        conn.commit()
        if "error" in result:
            summary["errors"] += 1
        else:
            summary["analysed"] += 1
            summary[result.get("status", "")] = summary.get(result.get("status", ""), 0) + 1
            if result.get("model"):
                summary["by_model"][result["model"]] = summary["by_model"].get(result["model"], 0) + 1
        if on_progress:
            on_progress(i, len(todo), opp)
    return summary


def parse_json_field(value: str | None) -> list | dict | None:
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
