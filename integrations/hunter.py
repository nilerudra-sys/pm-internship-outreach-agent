"""Hunter.io Domain Search (optional; used only when HUNTER_API_KEY is set).

Only addresses Hunter FOUND on public pages are accepted: results with provenance "inferred"
(worked out from the company's email pattern) or without any public source are discarded, and the
`pattern` field is ignored. This app never constructs email addresses itself.
Each Domain Search request uses one Hunter credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from integrations.http import PoliteSession

BASE = "https://api.hunter.io/v2"
DEPARTMENTS = "executive,management,product,hr"


class HunterError(RuntimeError):
    pass


@dataclass
class HunterPerson:
    full_name: str
    position: str
    email: str
    confidence: int | None
    verification_status: str | None
    linkedin_url: str | None
    sources: list[str] = field(default_factory=list)


@dataclass
class HunterResult:
    people: list[HunterPerson]
    discarded_inferred: int = 0
    discarded_no_source: int = 0
    discarded_incomplete: int = 0


def parse_domain_search(payload: dict) -> HunterResult:
    result = HunterResult([])
    for e in (payload.get("data") or {}).get("emails") or []:
        if (e.get("provenance") or "").lower() == "inferred":
            result.discarded_inferred += 1
            continue
        uris = [s.get("uri") for s in e.get("sources") or [] if s.get("uri")]
        if not uris:
            result.discarded_no_source += 1
            continue
        first, last = (e.get("first_name") or "").strip(), (e.get("last_name") or "").strip()
        position = (e.get("position") or e.get("position_raw") or "").strip()
        if not (first and last and position and e.get("value")):
            result.discarded_incomplete += 1
            continue
        status = (e.get("verification") or {}).get("status")
        result.people.append(HunterPerson(
            full_name=f"{first} {last}", position=position, email=e["value"].lower(),
            confidence=e.get("confidence"),
            verification_status=status if status in ("valid", "accept_all", "invalid", "unknown") else None,
            linkedin_url=e.get("linkedin"), sources=uris[:5],
        ))
    return result


class HunterClient:
    def __init__(self, api_key: str, http: PoliteSession | None = None):
        if not api_key:
            raise HunterError("HUNTER_API_KEY is not set")
        self.api_key = api_key
        self.http = http or PoliteSession(min_interval=1.0)

    def domain_search(self, domain: str, limit: int = 10) -> HunterResult:
        params = {"domain": domain, "api_key": self.api_key, "type": "personal", "department": DEPARTMENTS,
                  "required_field": "full_name,position", "limit": min(limit, 10)}
        try:
            payload = self.http.get_json(f"{BASE}/domain-search", params=params)
        except Exception as exc:
            text = str(exc)
            # Never echo the API key (it is in the request URL of HTTP errors).
            raise HunterError(text.replace(self.api_key, "***")[:300]) from None
        return parse_domain_search(payload)
