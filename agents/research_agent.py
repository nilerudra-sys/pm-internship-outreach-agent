"""Research agent: website pages + Gemini research for pipeline companies, 80/20 India first."""

from __future__ import annotations

from collections.abc import Callable

from agents.base import AgentResult, BaseAgent
from database import research as research_repo
from integrations.http import PoliteSession
from services.discovery.analysis import _profile_text
from services.discovery.scoring import mix_by_market
from services.research.company_research import research_company
from services.research.website import RobotsCache, fetch_company_pages


class ResearchAgent(BaseAgent):
    name = "research"

    def __init__(self, conn, settings, gemini_client, http: PoliteSession | None = None):
        super().__init__(conn, settings)
        self.client = gemini_client
        self.http = http or PoliteSession(min_interval=2.0, timeout=20.0)
        self.robots = RobotsCache(self.http)

    def targets(self, limit: int, company_ids: list[int] | None = None, force: bool = False) -> list[dict]:
        todo = research_repo.companies_needing_research(self.conn, company_ids=company_ids, force=force)
        return mix_by_market(todo, self.settings.india_ratio)[:limit]

    def run(self, limit: int = 5, company_ids: list[int] | None = None, force: bool = False,
            on_progress: Callable[[int, int, dict, str], None] | None = None) -> AgentResult:
        result = AgentResult(self.name)
        targets = self.targets(limit, company_ids, force)
        profile = _profile_text()
        for i, company in enumerate(targets, start=1):
            if on_progress:
                on_progress(i, len(targets), company, "fetching website")
            report = fetch_company_pages(self.conn, company, self.http, self.robots, force=force)
            if on_progress:
                on_progress(i, len(targets), company, f"analysing {len(report.pages)} pages")
            out = research_company(self.conn, company, report.pages, self.client, profile)
            self.conn.commit()
            result.processed += 1
            if out.get("error"):
                result.errors.append(f"{company['name']}: {out['error']}")
            else:
                result.created += 1
            self.record("company_done", f"{company['name']}: {len(report.pages)} pages"
                        + (" (cached)" if report.from_cache else "")
                        + (f", {report.blocked} blocked by robots.txt" if report.blocked else ""),
                        entity_type="company", entity_id=company["id"])
            self.conn.commit()
        return result
