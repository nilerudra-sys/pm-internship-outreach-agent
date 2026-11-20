"""Merge leads from any source into the opportunities table with deduplication."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from database import opportunities as opp_repo
from services.discovery.normalize import Lead
from services.discovery.roles import classify_title


@dataclass
class IngestResult:
    opportunity_id: int
    created: bool                 # False = merged into an existing opportunity
    new_source: bool              # a new source/URL was recorded
    filled_fields: list[str]


def ingest_lead(conn: sqlite3.Connection, lead: Lead) -> IngestResult:
    """Insert a new opportunity, or merge into the duplicate it matches.

    Duplicates are matched on job URL, or on company (domain / normalised name) plus normalised
    title. Merging never overwrites existing values; it only fills gaps and records the source URL.
    """
    url_norm = lead.job_url_norm
    domain = lead.company_domain
    existing = opp_repo.find_duplicate(
        conn,
        job_url_norm=url_norm,
        company_domain=domain,
        company_name_norm=lead.company_name_norm,
        job_title_norm=lead.job_title_norm,
    )
    source_url = lead.source_url or lead.job_url

    if existing:
        filled = opp_repo.fill_missing(conn, existing["id"], {
            "description": lead.description,
            "location": lead.location,
            "country": lead.country,
            "stipend": lead.stipend,
            "duration": lead.duration,
            "posted_date": lead.posted_date,
            "company_domain": domain,
            "remote_status": lead.remote_status,
            "market": lead.market,
            "compensation": lead.compensation,
            "job_url": lead.job_url,
            "job_url_norm": url_norm,
        })
        new_source = opp_repo.add_source(conn, existing["id"], lead.source, source_url, lead.discovered_at)
        if lead.job_url and source_url != lead.job_url:
            new_source |= opp_repo.add_source(conn, existing["id"], lead.source, lead.job_url, lead.discovered_at)
        return IngestResult(existing["id"], False, new_source, filled)

    opp_id = opp_repo.insert_opportunity(conn, {
        "company_name": lead.company_name,
        "company_name_norm": lead.company_name_norm,
        "company_domain": domain,
        "job_title": lead.job_title,
        "job_title_norm": lead.job_title_norm,
        "job_url": lead.job_url,
        "job_url_norm": url_norm,
        "description": lead.description,
        "location": lead.location,
        "country": lead.country,
        "market": lead.market,
        "remote_status": lead.remote_status,
        "stipend": lead.stipend,
        "compensation": lead.compensation,
        "duration": lead.duration,
        "title_category": classify_title(lead.job_title),
        "primary_source": lead.source,
        "posted_date": lead.posted_date,
        "discovered_at": lead.discovered_at,
    })
    opp_repo.add_source(conn, opp_id, lead.source, source_url, lead.discovered_at)
    if lead.job_url and source_url != lead.job_url:
        opp_repo.add_source(conn, opp_id, lead.source, lead.job_url, lead.discovered_at)
    return IngestResult(opp_id, True, True, [])
