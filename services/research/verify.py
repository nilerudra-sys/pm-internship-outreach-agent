"""Sentence-level verification of research facts against their cited sources.

A fact is accepted only if a small window of the cited text (up to three adjacent lines/sentences,
located from Gemini's verbatim quote when possible) passes every check:

1. Claim words   - risky claim words in the fact (launched, hiring, intern, remote, raised, acquired,
                   partnered, profitable, founded, IPO, award...) must appear in the window.
2. Numbers       - every number in the fact appears in the window next to the same unit word
                   ("1.8 crore" will not match "9 lakh crore").
3. Negation      - a negated fact needs a negated window; a window that negates one of the fact's own
                   words ("does not charge") rejects a positive fact ("charges").
4. Coverage      - at least MIN_COVERAGE of the fact's content words (excluding the company's name and
                   filler words, with light stemming and a few synonyms) appear in the window.

The window is returned as a quote so a human can check it. This is deliberately conservative:
dropping a true fact is acceptable, keeping a false one is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_COVERAGE = 0.5
MAX_QUOTE = 320
WINDOW = 3

_STOP = set("""a an and are as at be by for from has have had in into is it its of on or our that the their
them they this to was were we with will you your who what which can more most also than other such only
over under about across through using used use help helps make makes all any each every one two
been being do does did so if then there these those here very just get gets got per via within upon
including include includes like well while where when how why us out up off it's i me my""".split())

_SYNONYMS = {"client": "customer", "user": "customer", "consumer": "customer", "startup": "startup",
             "start-up": "startup", "firm": "company", "organisation": "company", "organization": "company",
             "internship": "intern", "offering": "offer", "provide": "offer", "app": "app", "application": "app"}

# Claim words whose presence in a fact must be backed by the same word in the source (stemmed).
_CLAIM_STEMS = {"launch", "hir", "intern", "remot", "rais", "seri", "acquir", "acquisition", "partner",
                "partnership", "profit", "profitabl", "ipo", "layoff", "award", "found", "valuation",
                "unicorn", "revenu", "expand", "expansion", "opening", "vacancy", "vacanci"}

_STRONG_NEG = {"not", "never", "cannot", "nor", "neither"}
_ABSENCE = {"no", "without", "zero", "none"}   # plus hyphenated "X-free", handled separately
_HYPHEN_FREE = re.compile(r"\b([a-z]+)-free\b")

_TOKEN = re.compile(r"[a-z0-9]+(?:'[a-z]+)?(?:[.,][0-9]+)*")
_SPLIT = re.compile(r"(?<=[.!?;])\s+(?=[A-Z0-9₹$\"'(])|\n+")


@dataclass
class Support:
    ok: bool
    coverage: float
    quote: str | None
    reason: str = ""


def _stem(word: str) -> str:
    if re.fullmatch(r"\d+(?:\.\d+)?", word):
        return word
    word = _SYNONYMS.get(word, word)
    if len(word) > 4 and not word.endswith("ss"):
        word = re.sub(r"(ing|ed|es|s)$", "", word)
    if len(word) > 3 and word.endswith("e"):
        word = word[:-1]
    return _SYNONYMS.get(word, word)


def raw_tokens(text: str) -> list[str]:
    """Lowercase tokens in order (incl. filler words); numbers normalised ('20,000' -> '20000')."""
    out = []
    for tok in _TOKEN.findall((text or "").lower().replace("’", "'")):
        tok = tok.rstrip(".,")
        if tok[:1].isdigit():
            out.append(tok.replace(",", ""))
        elif tok.endswith("n't"):
            out.append("not")
        else:
            out.extend(t for t in re.split(r"[.,']", tok) if t)
    return out


def tokens(text: str) -> list[str]:
    return [_stem(t) for t in raw_tokens(text)]


def _is_number(tok: str) -> bool:
    return bool(re.fullmatch(r"\d+(?:\.\d+)?", tok))


def name_tokens(*names: str | None) -> set[str]:
    """Tokens of the company name / domain: they appear everywhere and prove nothing."""
    out: set[str] = set()
    for n in names:
        if n:
            out.update(tokens(n.replace(".", " ")))
    return out - {"com", "in", "io", "co"}


_NAME = re.compile(r"\b[A-Z][A-Za-z0-9&]+")


def proper_names(fact: str, ignore: set[str]) -> set[str]:
    """Capitalised words after the first word (places, investors, partners, products): each must be in
    the source. The fact's first word is skipped because it is capitalised anyway."""
    names = set()
    for m in _NAME.finditer(fact):
        if m.start() == 0:
            continue
        t = _stem(m.group(0).lower())
        if t not in ignore and t not in _STOP and len(t) >= 2:
            names.add(t)
    return names


def content_words(text: str, ignore: set[str]) -> set[str]:
    out = set()
    for raw in raw_tokens(text):
        if raw in _STRONG_NEG or raw in _ABSENCE or raw in _STOP or raw == "free":
            continue
        t = _stem(raw)
        if t not in _STOP and t not in ignore and (len(t) >= 3 or _is_number(t)):
            out.add(t)
    return out


def segments(text: str) -> list[str]:
    return [" ".join(p.split()) for p in _SPLIT.split(text or "") if p and p.strip()]


def windows(text: str, size: int = WINDOW) -> list[str]:
    segs = segments(text)
    out = []
    for n in range(1, size + 1):
        out += [" ".join(segs[i:i + n]) for i in range(len(segs) - n + 1)]
    return out


def _number_contexts(toks: list[str]) -> dict[str, set[str]]:
    """number -> its neighbouring content words (previous and next non-filler token)."""
    ctx: dict[str, set[str]] = {}
    content = [(i, t) for i, t in enumerate(toks) if t not in _STOP]
    for k, (_, t) in enumerate(content):
        if _is_number(t):
            near = {content[j][1] for j in (k - 1, k + 1) if 0 <= j < len(content) and not _is_number(content[j][1])}
            ctx.setdefault(t, set()).update(near)
    return ctx


def _strong_negation_of(raw: list[str], fact_words: set[str]) -> bool:
    """'does not charge brokerage': a strong negation followed (within 3 words) by one of the fact's words."""
    return any(t in _STRONG_NEG and any(_stem(w) in fact_words for w in raw[i + 1:i + 4])
               for i, t in enumerate(raw))


def _absence_of(text: str, raw: list[str], fact_words: set[str]) -> bool:
    """'no gimmicks, spam or push notifications', 'without fees' (next 6 words, so lists are covered),
    'zero commission' (next 2 words) or 'commission-free' (hyphenated only)."""
    if any(_stem(m.group(1)) in fact_words for m in _HYPHEN_FREE.finditer(text.lower())):
        return True
    for i, t in enumerate(raw):
        span = 6 if t in ("no", "without", "none") else 2
        if t in _ABSENCE and any(_stem(w) in fact_words for w in raw[i + 1:i + 1 + span]):
            return True
    return False


def _claims_absence(text: str, raw: list[str]) -> bool:
    return any(t in _ABSENCE for t in raw) or bool(_HYPHEN_FREE.search(text.lower()))


def evaluate(fact: str, window: str, ignore: set[str], min_coverage: float = MIN_COVERAGE,
             source_tokens: set[str] | None = None) -> Support:
    """Check one window. Names are checked against the whole cited source (`source_tokens`);
    everything else must be in the window itself."""
    words = content_words(fact, ignore)
    if len(words) < 2:
        return Support(False, 0.0, None, "too few content words to verify")
    ftoks, wtoks = tokens(fact), tokens(window)
    wset = set(wtoks)
    coverage = len(words & wset) / len(words)

    def fail(reason: str) -> Support:
        return Support(False, coverage, None, reason)

    missing_claims = {w for w in words if w in _CLAIM_STEMS} - wset
    if missing_claims:
        return fail(f"claim not in source: {', '.join(sorted(missing_claims))}")

    missing_names = proper_names(fact, ignore) - (source_tokens if source_tokens is not None else wset)
    if missing_names:
        return fail(f"name not in source: {', '.join(sorted(missing_names))}")

    fctx, wctx = _number_contexts(ftoks), _number_contexts(wtoks)
    for num, near in fctx.items():
        if num not in wctx:
            return fail(f"number {num} not in source")
        if near and wctx[num] and not (near & wctx[num]):
            return fail(f"number {num} refers to something else in the source")

    fraw, wraw = raw_tokens(fact), raw_tokens(window)
    fstrong = any(t in _STRONG_NEG for t in fraw)
    fabsent = _claims_absence(fact, fraw)
    w_negates = _strong_negation_of(wraw, words)
    w_absence = _absence_of(window, wraw, words)
    if fstrong and not (w_negates or w_absence):
        return fail("fact is negated but the source does not negate it")
    if fabsent and not fstrong and not (w_absence or w_negates):
        return fail("fact claims an absence the source does not state")
    if not fstrong and not fabsent and (w_negates or w_absence):
        return fail("source negates this claim")

    if len(words & wset) < 2 or coverage < min_coverage:
        return fail(f"only {coverage:.0%} of key words supported")
    return Support(True, coverage, window[:MAX_QUOTE])


def _normalise(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9₹$% ]+", " ", (s or "").lower()).split())


def locate_quote(quote: str | None, source_text: str) -> list[str]:
    """Windows containing Gemini's quote (if it really is in the source), plus a neighbour line each side."""
    q = _normalise(quote or "")
    if len(q) < 12:
        return []
    segs = segments(source_text)
    for i in range(len(segs)):
        for n in range(1, WINDOW + 1):
            joined = " ".join(segs[i:i + n])
            if q in _normalise(joined):
                lo, hi = max(0, i - 1), min(len(segs), i + n + 1)
                return [joined, " ".join(segs[lo:hi])]
    return []


def check_support(fact: str, source_text: str, ignore: set[str], quote: str | None = None,
                  min_coverage: float = MIN_COVERAGE) -> Support:
    """Best supporting window; Gemini's verbatim quote is tried first, then every window."""
    candidates = locate_quote(quote, source_text) + windows(source_text)
    source_tokens = set(tokens(source_text))
    best = Support(False, 0.0, None, "no supporting sentence")
    for w in candidates:
        s = evaluate(fact, w, ignore, min_coverage, source_tokens)
        if s.ok:
            return s
        if s.coverage > best.coverage or best.reason == "no supporting sentence":
            best = s   # report the reason from the closest window
    return best


def check_multi(fact: str, cited: list[tuple[str, str]], ignore: set[str], quote: str | None = None,
                min_coverage: float = MIN_COVERAGE) -> tuple[bool, list[dict], str]:
    """Verify a fact citing one or more sources [(source_id, text)].

    One source: the normal window check. Several: each cited source must contribute its own
    supporting window, and the combined quotes must pass every check.
    """
    if len(cited) == 1:
        sid, text = cited[0]
        s = check_support(fact, text, ignore, quote, min_coverage)
        return s.ok, ([{"source_id": sid, "quote": s.quote}] if s.ok else []), s.reason

    words = content_words(fact, ignore)
    quotes = []
    for sid, text in cited:
        best, best_hits = None, set()
        for w in locate_quote(quote, text) + windows(text):
            hits = words & set(tokens(w))
            if len(hits) > len(best_hits):
                best, best_hits = w, hits
        if not best or len(best_hits) < 2:
            return False, [], f"cited source {sid} does not support any part of the fact"
        quotes.append({"source_id": sid, "quote": best[:MAX_QUOTE]})
    all_source_tokens = set().union(*(set(tokens(text)) for _, text in cited))
    combined = evaluate(fact, " ".join(q["quote"] for q in quotes), ignore, min_coverage, all_source_tokens)
    if not combined.ok:
        return False, [], combined.reason
    return True, quotes, ""
