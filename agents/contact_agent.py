"""Contact discovery agent: companies with vacancies first, then 80% India / 20% international."""

from __future__ import annotations

from collections.abc import Callable

from agents.base import AgentResult, BaseAgent
from database import contacts as contacts_repo
from integrations.http import PoliteSession
from services.contacts.discovery import DiscoveryResult, discover_company
from services.contacts.scoring import company_context
from services.discovery.scoring import mix_by_market
from services.research.website import RobotsCache


class ContactAgent(BaseAgent):
    name = "contacts"

    def __init__(self, conn, settings, http: PoliteSession | None = None, hunter=None):
        super().__init__(conn, settings)
        self.http = http or PoliteSession(min_interval=2.0, timeout=20.0)
        self.robots = RobotsCache(self.http)
        self.hunter = hunter

    def targets(self, limit: int, company_ids: list[int] | None = None, force: bool = False) -> list[dict]:
        todo = contacts_repo.companies_for_discovery(self.conn, company_ids=company_ids, force=force)
        for c in todo:
            c["_has_vacancy"] = company_context(self.conn, c).has_vacancy
        # Vacancy first, then fit; mix_by_market keeps that order within each market.
        todo.sort(key=lambda c: (not c["_has_vacancy"], -(c.get("fit_score") or 0), c["id"]))
        return mix_by_market(todo, self.settings.india_ratio)[:limit]

    def run(self, limit: int = 5, company_ids: list[int] | None = None, force: bool = False,
            on_progress: Callable[[int, int, dict], None] | None = None) -> tuple[AgentResult, list[DiscoveryResult]]:
        summary = AgentResult(self.name)
        details: list[DiscoveryResult] = []
        targets = self.targets(limit, company_ids, force)
        for i, company in enumerate(targets, start=1):
            if on_progress:
                on_progress(i, len(targets), company)
            r = discover_company(self.conn, company, self.http, self.robots, self.hunter, force)
            details.append(r)
            summary.processed += 1
            summary.created += r.added
            if r.status == "failed":
                summary.errors.append(f"{company['name']}: {'; '.join(r.errors) or 'failed'}")
        return summary, details
