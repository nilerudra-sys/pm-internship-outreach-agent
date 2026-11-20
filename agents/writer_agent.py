"""Writer agent: drafts emails for ready contacts, vacancy-based first, then 80% India / 20% international.
Drafts go to the Review queue (pending_review). This agent never sends anything."""

from __future__ import annotations

from collections.abc import Callable

from agents.base import AgentResult, BaseAgent
from services.discovery.scoring import mix_by_market
from services.outreach.writer import DraftOutcome, draft_candidates, draft_for_contact


class WriterAgent(BaseAgent):
    name = "writer"

    def __init__(self, conn, settings, gemini_client):
        super().__init__(conn, settings)
        self.client = gemini_client

    def targets(self, limit: int, contact_ids: list[int] | None = None) -> list[dict]:
        ready = [c for c in draft_candidates(self.conn) if c["ready"]]
        if contact_ids:
            ready = [c for c in ready if c["id"] in contact_ids]
        ready.sort(key=lambda c: (c["outreach_type"] != "vacancy", -(c["score_total"] or 0), c["id"]))
        return mix_by_market(ready, self.settings.india_ratio)[:limit]

    def run(self, limit: int = 5, contact_ids: list[int] | None = None,
            on_progress: Callable[[int, int, dict], None] | None = None) -> tuple[AgentResult, list[DraftOutcome]]:
        summary = AgentResult(self.name)
        outcomes: list[DraftOutcome] = []
        targets = self.targets(limit, contact_ids)
        signature = self.settings.sender_name or None
        for i, t in enumerate(targets, start=1):
            if on_progress:
                on_progress(i, len(targets), t)
            o = draft_for_contact(self.conn, t["id"], self.client, signature)
            self.conn.commit()
            outcomes.append(o)
            summary.processed += 1
            if o.email_id:
                summary.created += 1
            elif o.error:
                summary.errors.append(f"{o.company_name}: {o.error}")
            else:
                summary.skipped += 1
        return summary, outcomes
