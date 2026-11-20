"""Data access for company website pages and Gemini company research."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from database.models import utc_now
from database.repository import log_activity

_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def _cutoff(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime(_FMT)


# --------------------------------------------------------------------------- pages


def upsert_page(conn: sqlite3.Connection, company_id: int, url: str, **fields: Any) -> None:
    allowed = {"final_url", "page_type", "http_status", "title", "description", "text", "error", "links",
               "people_text"}
    data = {k: v for k, v in fields.items() if k in allowed}
    data["fetched_at"] = utc_now()
    cols = ", ".join(["company_id", "url", *data])
    marks = ", ".join("?" for _ in range(len(data) + 2))
    updates = ", ".join(f"{k} = excluded.{k}" for k in data)
    conn.execute(
        f"INSERT INTO company_pages ({cols}) VALUES ({marks}) "
        f"ON CONFLICT (company_id, url) DO UPDATE SET {updates}",
        (company_id, url, *data.values()),
    )


def cached_pages(conn: sqlite3.Connection, company_id: int, max_age_days: float = 7) -> list[dict[str, Any]]:
    """Successfully fetched pages newer than max_age_days."""
    rows = conn.execute(
        """SELECT * FROM company_pages
           WHERE company_id = ? AND error IS NULL AND http_status = 200 AND fetched_at >= ?
           ORDER BY id""",
        (company_id, _cutoff(max_age_days)),
    )
    return [dict(r) for r in rows]


def unreadable_pages(conn: sqlite3.Connection, company_id: int, max_age_days: float = 7) -> list[dict[str, Any]]:
    """Pages tried recently that could not be used (robots.txt, JavaScript-only, HTTP errors)."""
    rows = conn.execute(
        """SELECT url, page_type, error AS reason, fetched_at FROM company_pages
           WHERE company_id = ? AND error IS NOT NULL AND fetched_at >= ? ORDER BY id""",
        (company_id, _cutoff(max_age_days)),
    )
    return [dict(r) for r in rows]


def all_pages(conn: sqlite3.Connection, company_id: int) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM company_pages WHERE company_id = ? ORDER BY id", (company_id,))]


# --------------------------------------------------------------------------- research


def save_research(conn: sqlite3.Connection, company_id: int, data: dict[str, Any]) -> None:
    allowed = {
        "summary", "product", "target_customers", "business_model", "industry", "facts", "candidate_angle",
        "fit_score", "score_breakdown", "reasoning", "insufficient", "sources", "dropped_facts", "model",
        "researched_at", "error", "verified_summary", "unreadable_pages",
    }
    row = {k: v for k, v in data.items() if k in allowed}
    for key in ("facts", "score_breakdown", "sources", "unreadable_pages"):
        if key in row and row[key] is not None and not isinstance(row[key], str):
            row[key] = json.dumps(row[key], ensure_ascii=False)
    if "insufficient" in row:
        row["insufficient"] = int(bool(row["insufficient"]))
    cols = ", ".join(["company_id", *row])
    marks = ", ".join("?" for _ in range(len(row) + 1))
    updates = ", ".join(f"{k} = excluded.{k}" for k in row)
    conn.execute(
        f"INSERT INTO company_research ({cols}) VALUES ({marks}) "
        f"ON CONFLICT (company_id) DO UPDATE SET {updates}",
        (company_id, *row.values()),
    )
    if row.get("error"):
        log_activity(conn, "company", company_id, "research_failed", row["error"][:200])
    else:
        log_activity(conn, "company", company_id, "researched",
                     f"fit {row.get('fit_score')} · model {row.get('model')}")


def get_research(conn: sqlite3.Connection, company_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM company_research WHERE company_id = ?", (company_id,)).fetchone()
    if not row:
        return None
    out = dict(row)
    for key in ("facts", "score_breakdown", "sources", "unreadable_pages"):
        try:
            out[key] = json.loads(out[key]) if out[key] else None
        except ValueError:
            out[key] = None
    return out


def companies_needing_research(conn: sqlite3.Connection, *, max_age_days: float = 30,
                               company_ids: list[int] | None = None, force: bool = False) -> list[dict[str, Any]]:
    """Pipeline companies with no successful research newer than max_age_days (all of company_ids if force)."""
    sql = """SELECT c.*, r.researched_at, r.error AS research_error
             FROM companies c LEFT JOIN company_research r ON r.company_id = c.id
             WHERE c.status != 'disqualified'"""
    params: list[Any] = []
    if company_ids:
        sql += f" AND c.id IN ({', '.join('?' for _ in company_ids)})"
        params.extend(company_ids)
    if not force:
        sql += " AND (r.researched_at IS NULL OR r.error IS NOT NULL OR r.researched_at < ?)"
        params.append(_cutoff(max_age_days))
    sql += " ORDER BY c.created_at"
    return [dict(r) for r in conn.execute(sql, params)]


def linked_listings(conn: sqlite3.Connection, company: dict[str, Any], limit: int = 3) -> list[dict[str, Any]]:
    """Discovered listings for this company (by link, domain or name), best first, with a description."""
    rows = conn.execute(
        """SELECT * FROM opportunities
           WHERE (company_id = ? OR company_domain = ? OR company_name_norm = ?)
             AND LENGTH(COALESCE(description, '')) > 0
           ORDER BY COALESCE(relevance_score, -1) DESC, id
           LIMIT ?""",
        (company["id"], company["domain"], _norm_name(company["name"]), limit),
    )
    return [dict(r) for r in rows]


def _norm_name(name: str) -> str:
    from services.discovery.normalize import normalize_company_name

    return normalize_company_name(name)


def research_overview(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT c.id, c.name, c.domain, c.market, c.sector, c.status, c.fit_score,
                  r.researched_at, r.error AS research_error, r.insufficient, r.model,
                  (SELECT COUNT(*) FROM company_pages p WHERE p.company_id = c.id AND p.http_status = 200
                     AND p.error IS NULL) AS pages
           FROM companies c LEFT JOIN company_research r ON r.company_id = c.id
           ORDER BY COALESCE(c.fit_score, -1) DESC, c.name"""
    )
    return [dict(r) for r in rows]
