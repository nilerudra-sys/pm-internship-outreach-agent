"""Check registered company career boards and import internship listings that look product-related."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from database import opportunities as opp_repo
from integrations.ats import fetch_jobs
from integrations.http import PoliteSession
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead
from services.discovery.roles import is_candidate_listing

RECHECK_HOURS = 6


@dataclass
class BoardResult:
    board: str
    fetched: int = 0
    matched: int = 0
    created: int = 0
    merged: int = 0
    skipped_recent: bool = False
    error: str | None = None


@dataclass
class CheckSummary:
    boards: list[BoardResult] = field(default_factory=list)

    @property
    def created(self) -> int:
        return sum(b.created for b in self.boards)


def _recently_checked(board: dict, hours: int) -> bool:
    if not board.get("last_checked_at"):
        return False
    checked = datetime.strptime(board["last_checked_at"], "%Y-%m-%dT%H:%M:%S+00:00").replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - checked < timedelta(hours=hours)


def check_board(conn: sqlite3.Connection, board: dict, http: PoliteSession, force: bool = False) -> BoardResult:
    label = f"{board['company_name']} ({board['ats']}/{board['slug']})"
    result = BoardResult(label)
    if not force and _recently_checked(board, RECHECK_HOURS):
        result.skipped_recent = True
        return result
    try:
        jobs = fetch_jobs(board["ats"], board["slug"], http)
    except Exception as exc:
        result.error = str(exc)[:300]
        opp_repo.mark_board_checked(conn, board["id"], f"error: {result.error}")
        return result

    result.fetched = len(jobs)
    for job in jobs:
        if not is_candidate_listing(job.title, job.description, job.employment_type):
            continue
        result.matched += 1
        lead = Lead(
            company_name=board["company_name"],
            job_title=job.title,
            source="career_page",
            job_url=job.url,
            website=board.get("website"),
            description=job.description,
            location=job.location,
            remote_status=job.workplace,
            stipend=job.compensation,
            posted_date=job.posted_date,
        )
        # The board's market hint only applies when the job location says nothing.
        if lead.market == "unknown" and board.get("market_hint") in ("india", "international"):
            lead.market = board["market_hint"]
        ingested = ingest_lead(conn, lead)
        if ingested.created:
            result.created += 1
        else:
            result.merged += 1
    opp_repo.mark_board_checked(
        conn, board["id"], f"{result.fetched} jobs, {result.matched} matched, {result.created} new")
    return result


def check_all_boards(conn: sqlite3.Connection, http: PoliteSession | None = None, force: bool = False,
                     board_ids: list[int] | None = None) -> CheckSummary:
    http = http or PoliteSession()
    summary = CheckSummary()
    for board in opp_repo.list_career_boards(conn, enabled_only=True):
        if board_ids and board["id"] not in board_ids:
            continue
        summary.boards.append(check_board(conn, board, http, force))
        conn.commit()
    return summary
