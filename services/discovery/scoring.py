"""Deterministic scoring and qualification from Gemini's sub-scores, plus 80/20 prioritisation."""

from __future__ import annotations

from typing import Any

WEIGHTS = {
    "product_responsibilities": 0.35,
    "internship_eligibility": 0.20,
    "location_fit": 0.20,
    "engineering_fit": 0.15,
    "experience_fit": 0.10,
}
DEFAULT_THRESHOLD = 60


def weighted_score(sub: dict[str, int]) -> int:
    """Combine 0-10 sub-scores into 0-100."""
    total = sum(WEIGHTS[k] * max(0, min(10, int(sub.get(k, 0)))) for k in WEIGHTS)
    return round(total * 10)


def final_score(
    sub: dict[str, int],
    *,
    role_category: str,
    is_internship: str,
    market: str,
    india_eligible: str,
) -> tuple[int, list[str]]:
    """Weighted score with hard caps. Returns (score, notes explaining any cap)."""
    score = weighted_score(sub)
    notes: list[str] = []

    def cap(limit: int, why: str) -> None:
        nonlocal score
        if score > limit:
            score = limit
            notes.append(why)

    if role_category == "not_relevant":
        cap(20, "not a product role")
    if role_category == "adjacent" and sub.get("product_responsibilities", 0) < 5:
        cap(40, "adjacent role without strong product duties")
    if is_internship == "no":
        cap(30, "not an internship")
    if india_eligible == "no":
        cap(25, "does not accept applicants based in India")
    elif market == "international" and india_eligible == "unknown":
        cap(70, "international role; India eligibility not confirmed")
    return score, notes


def qualify(score: int, role_category: str, insufficient: bool,
            threshold: int = DEFAULT_THRESHOLD) -> tuple[str, str]:
    """(status, reason) for an analysed opportunity."""
    if insufficient:
        return "new", "not enough information in the listing; add the full description"
    if role_category == "not_relevant":
        return "rejected", "not a product role"
    if score < threshold:
        return "rejected", f"score {score} below threshold {threshold}"
    return "qualified", f"score {score} ≥ {threshold}"


def mix_by_market(items: list[dict[str, Any]], india_ratio: float = 0.8,
                  key: str = "market") -> list[dict[str, Any]]:
    """Reorder (already score-sorted) items so any prefix keeps roughly india_ratio India.

    Items with unknown market are treated as India-side only after India runs out, so they never
    crowd out confirmed Indian opportunities.
    """
    from services.quota import next_market

    india = [i for i in items if i.get(key) == "india"]
    intl = [i for i in items if i.get(key) == "international"]
    unknown = [i for i in items if i.get(key) not in ("india", "international")]
    india_side = india + unknown
    out: list[dict[str, Any]] = []
    counts = {"india": 0, "international": 0}
    while india_side or intl:
        market = next_market(counts["india"], counts["international"], india_ratio)
        if market == "india" and not india_side:
            market = "international"
        if market == "international" and not intl:
            market = "india"
        out.append((india_side if market == "india" else intl).pop(0))
        counts[market] += 1
    return out
