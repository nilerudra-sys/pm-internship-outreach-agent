"""Deterministic checks on an email draft against the exact material Gemini was given.

Errors block the draft (it is regenerated once, then dropped). Warnings are shown in the Review queue.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from services.research.verify import content_words, name_tokens

MIN_WORDS, MAX_WORDS = 70, 175          # body incl. greeting and sign-off (prompt asks for 80-150 + sign-off)
MAX_SUBJECT_WORDS = 10
MAX_COPIED_WORDS = 9                     # longest run of words copied verbatim from a fact
MAX_SKILLS_LISTED = 2

CLICHES = (
    r"i hope (this|my) (email|message|note) finds you", r"\bi am writing to\b", r"\bi'?m writing to\b",
    r"\bimpressive\b", r"\bimpressed\b", r"\besteemed\b", r"\badmire\b", r"\bi am (truly |very |deeply )?passionate",
    r"\breach(ing)? out\b", r"\bkindly\b", r"\bdo the needful\b", r"\bi came across\b", r"\bi have (closely )?followed\b",
    r"\bto whom it may concern\b", r"\bplease find\b", r"\bi would be honou?red\b", r"\bgreat opportunity\b",
    r"\bleverage\b", r"\bsynergy\b", r"\bin today'?s (fast-paced|competitive)\b",
)
_SELF_OPENING = re.compile(r"^(i am|i'm|my name|i hope|i am writing|i have been|as a)\b", re.I)
_CLAUSE_SPLIT = re.compile(r"[,;:.!?—–]|\band\b|\bso\b|\bbecause\b|\bwhich\b|\bwhere\b|\bbut\b|\bwhile\b", re.I)
# Neutral words allowed next to a project name besides the profile's own wording.
PROJECT_NEUTRAL = {"build", "built", "launch", "develop", "creat", "made", "work", "project", "experienc", "app",
                   "recently", "also", "earlier", "college", "side", "own", "my", "called"}
MIN_PROJECT_CLAIM_COVERAGE = 0.75
_PROJECT_REFERENCE = re.compile(
    r"\b(this|the|that) (project|platform|tool|product|app)\b|\b(building|developing|creating|launching) (it|this)\b",
    re.I)

# Capitalised words that are fine anywhere (greetings, pronouns, months, generic role and degree words).
COMMON_CAPITALISED = set("""
hi hello dear best regards thanks thank i i'm i've i'd i'll my your you we our it this that the a an as at in on for
with if would could happy looking please also since while recently currently during after before when what how why
product products management manager managers intern internship internships pm pms founder founders team teams
monday tuesday wednesday thursday friday saturday sunday january february march april may june july august september
october november december summer winter spring b.tech btech computer science engineering cloud computing ai ml api apis
saas india indian sir madam hope kind warm sincerely cheers glad excited keen curious would've i'm
""".split())

_PLACEHOLDER = re.compile(r"[\[\]{}<>]|\bTODO\b|lorem ipsum|\bXXX+\b|\bN/A\b|\[name\]|your name", re.I)
_NUMBER = re.compile(r"\d+(?:[.,]\d+)*")
_CAPITALISED = re.compile(r"\b[A-Z][A-Za-z0-9&’'.\-]*[A-Za-z0-9]|\b[A-Z]\b")
_SENTENCE_START = re.compile(r"(?:^|[.!?:]\s+|\n\s*)([A-Z][A-Za-z’']*)")
_VACANCY_TALK = re.compile(
    r"\b(your|the) (opening|vacancy|job posting|posting|job post|open role|open position|advertised role)\b|"
    r"\b(role|position|internship|opening) (that )?you (posted|advertised|are hiring for)\b|"
    r"\b(saw|noticed|came across) (your|the) (job|internship|role|opening|posting)\b", re.I)


@dataclass
class DraftCheck:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    facts_verified: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict[str, list[str]]:
        return {"errors": self.errors, "warnings": self.warnings, "facts_verified": self.facts_verified}


def _flatten(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(_flatten(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten(v) for v in value)
    return "" if value is None else str(value)


def allowed_material(material: dict) -> str:
    """Everything the draft may draw on: the prompt's facts, listing, recipient and profile."""
    return _flatten([material.get(k) for k in
                     ("company_name", "company_domain", "first_name", "full_name", "designation", "facts",
                      "listing", "profile", "signature", "ask")])


def _norm_number(n: str) -> str:
    return n.replace(",", "").rstrip(".")


def _words_lower(text: str) -> set[str]:
    return {w.strip("'’.-").lower() for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9&’'.\-]*", text)}


def _longest_copied_run(text: str, source: str) -> int:
    """Length (in words) of the longest word sequence that appears verbatim in both texts."""
    a = re.findall(r"[a-z0-9₹%.+]+", text.lower())
    b = re.findall(r"[a-z0-9₹%.+]+", source.lower())
    best = 0
    prev = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                best = max(best, cur[j])
        prev = cur
    return best


def _first_sentence_after_greeting(body: str) -> str:
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if lines and re.match(r"^(hi|hello|dear|hey)\b", lines[0], re.I):
        first = lines[0].split(",", 1)[1].strip() if "," in lines[0] and len(lines[0].split(",", 1)[1].strip()) > 3 \
            else (lines[1] if len(lines) > 1 else "")
    else:
        first = lines[0] if lines else ""
    return first


def style_problems(subject: str, body: str, material: dict) -> list[str]:
    """Style rules that make a draft read like a person, not a template. Treated as errors (one retry)."""
    problems = []
    low = f"{subject}\n{body}".lower()
    for pattern in CLICHES:
        m = re.search(pattern, low)
        if m:
            problems.append(f"cliché or flattery: “{m.group(0)}”")
    if _SELF_OPENING.match(_first_sentence_after_greeting(body)):
        problems.append("opens with the student; open with something about the company")
    for fid, fact in (material.get("facts") or {}).items():
        run = _longest_copied_run(body, fact["text"])
        if run >= MAX_COPIED_WORDS:
            problems.append(f"copies {run} words in a row from fact {fid}; paraphrase it")
    skills = [s for s in (material.get("profile") or {}).get("skills") or [] if s.lower() in body.lower()]
    if len(skills) > MAX_SKILLS_LISTED:
        problems.append(f"lists {len(skills)} skills like a CV; connect one project or skill instead")

    first = material.get("first_name") or ""
    lines = [ln for ln in body.strip().splitlines()]
    if first and not re.match(rf"^(hi|hello|dear)\s+{re.escape(first)}\s*,\s*$", (lines[0] if lines else ""), re.I):
        problems.append(f"start with “Hi {first},” on its own line")
    paragraphs = [p for p in re.split(r"\n\s*\n", body.strip()) if p.strip()]
    if len(paragraphs) < 4:   # greeting + 3 short paragraphs (+ sign-off)
        problems.append("write 3 short paragraphs separated by blank lines")
    if not re.search(r"\n\s*best regards,?\s*(\n|$)", body, re.I):
        problems.append("put “Best regards,” on its own line at the end")
    return problems


def project_claim_problems(body: str, material: dict) -> list[str]:
    """Any clause naming one of the student's projects may only use the profile's own description of it.
    Catches invented details such as "SignalAI taught me to structure features for user needs"."""
    profile = material.get("profile") or {}
    projects = [p.get("name") for p in profile.get("projects") or [] if p.get("name")]
    if not projects:
        return []
    allowed = content_words(_flatten(profile), set()) | PROJECT_NEUTRAL
    problems = []
    mentioned = [p for p in projects if p.lower() in body.lower()]
    for clause in _CLAUSE_SPLIT.split(body):
        named = [p for p in projects if p.lower() in clause.lower()]
        if not named and mentioned and _PROJECT_REFERENCE.search(clause):
            named = mentioned   # "this project", "building it" etc. refer back to the named project
        if not named:
            continue
        words = content_words(clause, set()) - content_words(" ".join(named), set())
        if not words:
            continue
        unsupported = words - allowed
        if len(unsupported) / len(words) > 1 - MIN_PROJECT_CLAIM_COVERAGE:
            problems.append(f"adds details about {', '.join(named)} that are not in your profile: "
                            f"“{' '.join(clause.split())[:90]}”")
    return problems


def check_draft(subject: str, body: str, facts_used: list[str], material: dict) -> DraftCheck:
    """`material` is what the prompt contained: company_name, first_name, designation, outreach_type,
    facts {id: {text, quote}}, listing, profile, signature."""
    out = DraftCheck()
    allowed = allowed_material(material)
    allowed_words = _words_lower(allowed)
    allowed_numbers = {_norm_number(n) for n in _NUMBER.findall(allowed)}
    full = f"{subject}\n{body}"

    # Structure
    if not subject.strip():
        out.errors.append("empty subject")
    elif len(subject.split()) > MAX_SUBJECT_WORDS:
        out.warnings.append(f"subject has {len(subject.split())} words")
    words = len(body.split())
    if words < MIN_WORDS or words > MAX_WORDS:
        out.errors.append(f"body has {words} words (expected {MIN_WORDS}-{MAX_WORDS})")
    if _PLACEHOLDER.search(full):
        out.errors.append("contains a placeholder or bracket")
    first = material.get("first_name") or ""
    if first and first.lower() not in body[:80].lower():
        out.errors.append(f"does not greet {first} by first name")
    signature = material.get("signature")
    if signature and signature.lower() not in body[-120:].lower():
        out.warnings.append("signature missing at the end")

    # Numbers must come from the material, exactly.
    for n in _NUMBER.findall(full):
        if _norm_number(n) not in allowed_numbers:
            out.errors.append(f"number not in the provided facts or profile: {n}")

    # Names / proper nouns must come from the material (sentence-initial words are skipped).
    starts = {m.start(1) for m in _SENTENCE_START.finditer(full)}
    unknown = set()
    for m in _CAPITALISED.finditer(full):
        if m.start() in starts:
            continue
        word = re.sub(r"[’']s$", "", m.group(0)).strip(".-’'").lower()
        if word and word not in COMMON_CAPITALISED and word not in allowed_words:
            unknown.add(m.group(0))
    if unknown:
        out.errors.append(f"names not in the provided material: {', '.join(sorted(unknown))}")

    # Verified facts must actually be used.
    facts = material.get("facts") or {}
    ignore = name_tokens(material.get("company_name"), material.get("company_domain"))
    body_words = content_words(body, ignore)
    for fid in facts_used:
        fact = facts.get(fid.strip().upper())
        if fact is None:
            out.errors.append(f"cites unknown fact {fid}")
            continue
        if len(content_words(fact["text"], ignore) & body_words) >= 2:
            out.facts_verified.append(fid.strip().upper())
        else:
            out.warnings.append(f"fact {fid} is cited but not really used")
    if facts and not out.facts_verified:
        out.errors.append("does not use any verified company fact")

    # Speculative emails must not claim there is a vacancy.
    if material.get("outreach_type") == "speculative" and _VACANCY_TALK.search(body):
        out.errors.append("refers to a job opening, but no internship is advertised")
    out.errors += style_problems(subject, body, material)
    out.errors += project_claim_problems(body, material)

    listing = material.get("listing") or {}
    if material.get("outreach_type") == "vacancy" and listing.get("job_title"):
        if not content_words(listing["job_title"], ignore) & body_words:
            out.warnings.append("does not mention the advertised role")
    return out
