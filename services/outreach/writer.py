"""Draft personalised emails with Gemini from verified material only, check them, and queue for review.

Flow per contact: outreach context (Phase 4) -> material (verified facts, listing, profile, verified
recipient) -> Gemini draft -> deterministic checks -> one retry with the check errors as feedback ->
stored as pending_review, or dropped. Nothing is sent from here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from config.settings import PROJECT_ROOT
from database import repository
from services.contacts.outreach_context import get_outreach_context
from services.outreach.checks import DraftCheck, check_draft

PROMPT_PATH = PROJECT_ROOT / "prompts" / "outreach_email.md"
MAX_FACTS = 6
SYSTEM = ("You write short, honest cold emails for a student. You only use the material provided and never "
          "invent facts, names, numbers or achievements.")

FACT_ORDER = ("outreach_hooks", "product_facts", "stage_signals", "hiring_signals", "remote_signals")
ACTIVE_STATUSES = ("pending_review", "approved", "sent", "replied")


class EmailDraft(BaseModel):
    subject: str
    body: str
    facts_used: list[str] = Field(default_factory=list, description="Ids like F1 of the facts mentioned")
    projects_mentioned: list[str] = Field(default_factory=list)


@dataclass
class DraftOutcome:
    contact_id: int
    company_name: str
    email_id: int | None = None
    attempts: int = 0
    model: str | None = None
    check: DraftCheck | None = None
    error: str | None = None
    skipped: str | None = None
    rejected: list[list[str]] = field(default_factory=list)   # check errors of each rejected attempt


# --------------------------------------------------------------------------- material + prompt


def build_material(ctx: dict, signature: str | None) -> dict[str, Any]:
    """Only verified data goes in. Gemini's interpretation of the company is deliberately excluded."""
    facts: dict[str, dict] = {}
    for category in FACT_ORDER:
        for f in (ctx["company"]["verified_facts"] or {}).get(category, []):
            if len(facts) < MAX_FACTS and f["text"] not in {x["text"] for x in facts.values()}:
                facts[f"F{len(facts) + 1}"] = {"text": f["text"], "quote": f.get("quote"), "url": f.get("url"),
                                               "category": category}
    profile = ctx["applicant_profile"] or {}
    vacancy = ctx["outreach_type"] == "vacancy" and ctx["listing"]
    return {
        "ask": ("whether you could be considered for the role, or a short call about it"
                if vacancy else "a short 15-minute call, or whether they would consider a product intern"),
        "company_name": ctx["company"]["name"],
        "company_domain": ctx["company"]["domain"],
        "first_name": ctx["contact"]["first_name"],
        "full_name": ctx["contact"]["full_name"],
        "designation": ctx["contact"]["designation"],
        "outreach_type": ctx["outreach_type"],
        "listing": ctx["listing"] if ctx["outreach_type"] == "vacancy" else None,
        "facts": facts,
        "profile": {k: profile.get(k) for k in ("target_role", "education", "specialization", "interests",
                                                "skills", "projects")},
        "signature": signature,
    }


def _profile_text(p: dict) -> str:
    projects = "\n".join(
        f"- {x.get('name')}" + (f" ({x['url']})" if x.get("url") else "") + f": {x.get('summary')}"
        + (f" Problem it solves: {x['problem']}" if x.get("problem") else "")
        for x in p.get("projects") or [])
    return (f"Looking for: {p.get('target_role')}\nEducation: {p.get('education')}, specialisation "
            f"{p.get('specialization')}\nSkills: {', '.join(p.get('skills') or [])}\n"
            f"Interests: {', '.join(p.get('interests') or [])}\nProjects:\n{projects}")


def build_prompt(material: dict, feedback: list[str] | None = None) -> str:
    vacancy = material["outreach_type"] == "vacancy" and material.get("listing")
    listing = material.get("listing") or {}
    values = {
        "outreach_rule": ("The company advertises this internship; say you are interested in it by its title."
                          if vacancy else
                          "The company has NOT advertised an internship. Do not mention or imply an opening; "
                          "ask whether they would consider a product intern."),
        "ask": material["ask"],
        "signature": material.get("signature") or "(no name: end after 'Best regards,')",
        "first_name": material["first_name"],
        "designation": material["designation"],
        "company_name": material["company_name"],
        "outreach_type": "VACANCY (advertised internship)" if vacancy else "SPECULATIVE (no advertised internship)",
        "listing": (f"ADVERTISED ROLE\nTitle: {listing.get('job_title')}\nLocation: {listing.get('location')}\n"
                    f"Remote: {listing.get('remote_status')}\nLink: {listing.get('job_url')}") if vacancy else "",
        "facts": "\n".join(f"[{fid}] {f['text']}" for fid, f in material["facts"].items()),
        "profile": _profile_text(material["profile"]),
    }
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    for k, v in values.items():
        prompt = prompt.replace("{" + k + "}", str(v))
    if feedback:
        prompt += ("\n\nYOUR PREVIOUS DRAFT WAS REJECTED BY AUTOMATIC CHECKS. Fix every problem:\n"
                   + "\n".join(f"- {f}" for f in feedback))
    return prompt


# --------------------------------------------------------------------------- generation


TEMPERATURE = 0.6


def normalize_format(body: str, first_name: str | None, signature: str | None) -> str:
    """Purely mechanical: one greeting line "Hi {first}," and a closing "Best regards," + signature.
    Wording in between is never changed."""
    text = body.strip().replace("\r\n", "\n")
    if first_name:
        # Drop whatever greeting the model wrote ("Hi there,", "Dear Asha,", "Asha,") before adding the real one.
        text = re.sub(r"^(?:hi|hello|dear|hey|greetings)\b[^\n,!]{0,40}[,!]?\s*", "", text, count=1, flags=re.I)
        text = re.sub(rf"^{re.escape(first_name)}\s*[,!:\-–—]\s*", "", text, count=1, flags=re.I)
        text = f"Hi {first_name},\n\n{text.lstrip()}"
    sig = re.escape(signature) if signature else r""
    text = re.sub(rf"\s*(?:best|warm|kind)?\s*regards,?\s*(?:{sig})?\s*$", "", text, flags=re.I).rstrip()
    return f"{text}\n\nBest regards," + (f"\n{signature}" if signature else "")


def generate_draft(client, material: dict, max_attempts: int = 2
                   ) -> tuple[EmailDraft | None, DraftCheck, int, str, list[list[str]]]:
    """Returns (draft or None, last check, attempts, model, errors of each rejected attempt).
    A rejected draft is regenerated once with its check errors as feedback."""
    feedback: list[str] | None = None
    check = DraftCheck(errors=["not generated"])
    rejected: list[list[str]] = []
    model = getattr(client, "model", "")
    for attempt in range(1, max_attempts + 1):
        draft = client.generate_json(build_prompt(material, feedback), EmailDraft, system=SYSTEM,
                                     temperature=TEMPERATURE)
        model = getattr(client, "last_model_used", None) or model
        draft.subject = draft.subject.strip()
        draft.body = normalize_format(draft.body, material.get("first_name"), material.get("signature"))
        check = check_draft(draft.subject, draft.body, draft.facts_used, material)
        if check.ok:
            return draft, check, attempt, model, rejected
        rejected.append(check.errors)
        feedback = check.errors
    return None, check, max_attempts, model, rejected


def has_active_email(conn: sqlite3.Connection, company_id: int) -> bool:
    marks = ", ".join("?" for _ in ACTIVE_STATUSES)
    return conn.execute(f"SELECT 1 FROM emails WHERE company_id = ? AND status IN ({marks})",
                        (company_id, *ACTIVE_STATUSES)).fetchone() is not None


def draft_for_contact(conn: sqlite3.Connection, contact_id: int, client, signature: str | None,
                      allow_second_for_company: bool = False) -> DraftOutcome:
    ctx = get_outreach_context(conn, contact_id)
    if ctx is None:
        return DraftOutcome(contact_id, "?", skipped="contact not found")
    outcome = DraftOutcome(contact_id, ctx["company"]["name"])
    if not ctx["ready"]:
        outcome.skipped = "not ready: " + "; ".join(ctx["blockers"])
        return outcome
    if not allow_second_for_company and has_active_email(conn, ctx["company"]["id"]):
        outcome.skipped = "company already has a draft or sent email"
        return outcome
    material = build_material(ctx, signature)
    if not material["facts"]:
        outcome.skipped = "no verified company facts to personalise with"
        return outcome
    try:
        draft, check, attempts, model, rejected = generate_draft(client, material)
    except Exception as exc:
        outcome.error = str(exc)[:300]
        return outcome
    outcome.attempts, outcome.model, outcome.check, outcome.rejected = attempts, model, check, rejected
    if draft is None:
        outcome.error = "draft failed checks twice: " + " | ".join("; ".join(r) for r in rejected)
        repository.log_activity(conn, "contact", contact_id, "draft_rejected", outcome.error[:300])
        return outcome
    used = [{"id": fid, **material["facts"][fid]} for fid in check.facts_verified]
    listing = material.get("listing") or {}
    outcome.email_id = repository.create_email_draft(
        conn, company_id=ctx["company"]["id"], contact_id=contact_id, subject=draft.subject, body=draft.body,
        model=model, outreach_type=ctx["outreach_type"], opportunity_id=listing.get("id"),
        facts_used=json.dumps(used, ensure_ascii=False),
        checks=json.dumps({**check.as_dict(), "rejected_attempts": rejected}, ensure_ascii=False))
    return outcome


def recheck_email(conn: sqlite3.Connection, email: dict, signature: str | None = None) -> DraftCheck | None:
    """Re-run the checks on a stored draft's CURRENT text (e.g. after your edits), against the facts it used.
    `signature` defaults to SENDER_NAME from settings."""
    ctx = get_outreach_context(conn, email["contact_id"])
    if ctx is None:
        return None
    if signature is None:
        from config.settings import get_settings

        signature = get_settings().sender_name or None
    if not signature:
        # No SENDER_NAME configured: accept whatever you signed with after the closing line.
        tail = email["body"].rsplit("Best regards,", 1)
        signature = tail[1].strip() or None if len(tail) == 2 else None
    material = build_material(ctx, signature)
    used = json.loads(email["facts_used"]) if email.get("facts_used") else []
    if used:
        material["facts"] = {f["id"]: f for f in used}
    material["outreach_type"] = email.get("outreach_type") or material["outreach_type"]
    if not material.get("first_name"):
        return None
    return check_draft(email["subject"], email["body"], list(material["facts"]), material)


def draft_candidates(conn: sqlite3.Connection) -> list[dict]:
    """Each company's top-priority contact (no email in progress for that company) with readiness info."""
    rows = conn.execute(
        """SELECT ct.id, ct.company_id, c.name AS company_name, c.market, c.fit_score, ct.full_name,
                  ct.role_title, ct.score_total
           FROM contacts ct JOIN companies c ON c.id = ct.company_id
           WHERE ct.outreach_priority = 1 AND ct.do_not_contact = 0
           ORDER BY ct.score_total DESC""").fetchall()
    out = []
    for r in rows:
        r = dict(r)
        if has_active_email(conn, r["company_id"]):
            continue
        ctx = get_outreach_context(conn, r["id"])
        r.update(ready=ctx["ready"], blockers=ctx["blockers"], outreach_type=ctx["outreach_type"],
                 verified_facts=sum(len(v) for v in (ctx["company"]["verified_facts"] or {}).values()))
        out.append(r)
    return out
