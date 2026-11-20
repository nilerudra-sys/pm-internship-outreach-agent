"""Data access for discovered opportunities, their sources, and career boards."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from database.models import utc_now
from database.repository import log_activity

OPP_STATUSES = ("new", "qualified", "rejected")

# Fields Gemini/analysis may fill. They only ever fill empty values (see fill_missing).
FILLABLE = (
    "description", "location", "country", "industry", "company_product", "stipend", "duration",
    "experience_required", "posted_date", "company_domain",
)


def _rows(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
    return [dict(r) for r in cur.fetchall()]


# --------------------------------------------------------------------------- lookup / dedup


def find_duplicate(
    conn: sqlite3.Connection,
    *,
    job_url_norm: str | None,
    company_domain: str | None,
    company_name_norm: str,
    job_title_norm: str,
) -> dict[str, Any] | None:
    """Same job URL, or same company (domain or normalised name) with the same normalised title."""
    if job_url_norm:
        row = conn.execute("SELECT * FROM opportunities WHERE job_url_norm = ?", (job_url_norm,)).fetchone()
        if row:
            return dict(row)
    row = conn.execute(
        """SELECT * FROM opportunities
           WHERE job_title_norm = ?
             AND ((? IS NOT NULL AND company_domain = ?) OR company_name_norm = ?)
           ORDER BY id LIMIT 1""",
        (job_title_norm, company_domain, company_domain, company_name_norm),
    ).fetchone()
    return dict(row) if row else None


def get_opportunity(conn: sqlite3.Connection, opp_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM opportunities WHERE id = ?", (opp_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- writes


def insert_opportunity(conn: sqlite3.Connection, data: dict[str, Any]) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    cur = conn.execute(f"INSERT INTO opportunities ({cols}) VALUES ({marks})", tuple(data.values()))
    log_activity(conn, "opportunity", cur.lastrowid, "discovered",
                 f"{data['job_title']} @ {data['company_name']} via {data['primary_source']}")
    return cur.lastrowid


def add_source(conn: sqlite3.Connection, opp_id: int, source: str, url: str | None, discovered_at: str) -> bool:
    """Record where an opportunity was seen. Returns False if this exact source+URL was already recorded."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO opportunity_sources (opportunity_id, source, url, discovered_at) VALUES (?, ?, ?, ?)",
        (opp_id, source, url or "", discovered_at),
    )
    return cur.rowcount > 0


def fill_missing(conn: sqlite3.Connection, opp_id: int, values: dict[str, Any]) -> list[str]:
    """Set fields only where the stored value is empty. Never overwrites existing data."""
    current = get_opportunity(conn, opp_id) or {}
    updates = {
        k: v for k, v in values.items()
        if k in FILLABLE and v not in (None, "") and current.get(k) in (None, "")
    }
    # Upgrade 'unknown' enums when a source knows better.
    for key in ("remote_status", "market", "compensation", "india_eligible"):
        if values.get(key) not in (None, "", "unknown") and current.get(key) == "unknown":
            updates[key] = values[key]
    if not current.get("job_url") and values.get("job_url"):
        updates["job_url"] = values["job_url"]
        updates["job_url_norm"] = values.get("job_url_norm")
    if updates:
        assignments = ", ".join(f"{k} = ?" for k in updates)
        conn.execute(
            f"UPDATE opportunities SET {assignments}, updated_at = ? WHERE id = ?",
            (*updates.values(), utc_now(), opp_id),
        )
    return list(updates)


def save_analysis(conn: sqlite3.Connection, opp_id: int, fields: dict[str, Any]) -> None:
    allowed = {
        "role_category", "relevance_score", "score_breakdown", "product_responsibilities",
        "ai_reasoning", "analysis_model", "analyzed_at", "analysis_error", "status", "status_reason",
        "india_eligible", "remote_status", "compensation", "market", "country",
    }
    data = {k: v for k, v in fields.items() if k in allowed}
    for key in ("score_breakdown", "product_responsibilities"):
        if key in data and not isinstance(data[key], str) and data[key] is not None:
            data[key] = json.dumps(data[key])
    opp = get_opportunity(conn, opp_id)
    if opp and opp["status_locked"]:
        data.pop("status", None)
        data.pop("status_reason", None)
    if not data:
        return
    assignments = ", ".join(f"{k} = ?" for k in data)
    conn.execute(
        f"UPDATE opportunities SET {assignments}, updated_at = ? WHERE id = ?",
        (*data.values(), utc_now(), opp_id),
    )
    if "analysis_error" in data and data["analysis_error"] and "relevance_score" not in data:
        log_activity(conn, "opportunity", opp_id, "analysis_failed", data["analysis_error"][:200])
        return
    log_activity(conn, "opportunity", opp_id, "analysed",
                 f"{data.get('role_category')} · score {data.get('relevance_score')}"
                 + (f" · model {data['analysis_model']}" if data.get("analysis_model") else ""))


def set_status(conn: sqlite3.Connection, opp_id: int, status: str, reason: str = "", lock: bool = True) -> None:
    """Manual status change from the dashboard; locked so re-analysis won't undo it."""
    if status not in OPP_STATUSES:
        raise ValueError(f"status must be one of {OPP_STATUSES}")
    conn.execute(
        "UPDATE opportunities SET status = ?, status_reason = ?, status_locked = ?, updated_at = ? WHERE id = ?",
        (status, reason, int(lock), utc_now(), opp_id),
    )
    log_activity(conn, "opportunity", opp_id, f"status:{status}", reason)


def link_company(conn: sqlite3.Connection, company_name_norm: str, company_id: int) -> int:
    cur = conn.execute(
        "UPDATE opportunities SET company_id = ?, updated_at = ? WHERE company_name_norm = ?",
        (company_id, utc_now(), company_name_norm),
    )
    return cur.rowcount


# --------------------------------------------------------------------------- queries


def list_opportunities(
    conn: sqlite3.Connection,
    *,
    sources: list[str] | None = None,
    statuses: list[str] | None = None,
    countries: list[str] | None = None,
    markets: list[str] | None = None,
    remote_statuses: list[str] | None = None,
    categories: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    min_score: int | None = None,
    include_unscored: bool = True,
    ids: list[int] | None = None,
) -> list[dict[str, Any]]:
    """Filtered list. `sources` matches any source the opportunity was seen on, not only the first."""
    sql = "SELECT o.* FROM opportunities o WHERE 1=1"
    params: list[Any] = []

    def any_of(column: str, values: list[Any] | None) -> None:
        nonlocal sql
        if values:
            sql += f" AND {column} IN ({', '.join('?' for _ in values)})"
            params.extend(values)

    if sources:
        marks = ", ".join("?" for _ in sources)
        sql += (f" AND (o.primary_source IN ({marks}) OR EXISTS (SELECT 1 FROM opportunity_sources s "
                f"WHERE s.opportunity_id = o.id AND s.source IN ({marks})))")
        params.extend(sources + sources)
    any_of("o.status", statuses)
    any_of("o.market", markets)
    any_of("o.remote_status", remote_statuses)
    any_of("o.id", ids)
    if countries:
        known = [c for c in countries if c != "Unknown"]
        clauses = []
        if known:
            clauses.append(f"o.country IN ({', '.join('?' for _ in known)})")
            params.extend(known)
        if "Unknown" in countries:
            clauses.append("o.country IS NULL")
        sql += " AND (" + " OR ".join(clauses) + ")"
    if categories:
        # Match the Gemini category, or the title guess for not-yet-analysed listings.
        marks = ", ".join("?" for _ in categories)
        sql += (f" AND (o.role_category IN ({marks}) OR "
                f"(o.role_category = 'unclassified' AND o.title_category IN ({marks})))")
        params.extend(categories + categories)
    if date_from:
        sql += " AND o.discovered_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND o.discovered_at <= ?"
        params.append(date_to)
    if min_score:
        sql += " AND (o.relevance_score >= ?" + (" OR o.relevance_score IS NULL)" if include_unscored else ")")
        params.append(min_score)
    elif not include_unscored:
        sql += " AND o.relevance_score IS NOT NULL"
    sql += " ORDER BY COALESCE(o.relevance_score, -1) DESC, o.discovered_at DESC, o.id DESC"
    return _rows(conn.execute(sql, params))


def sources_for(conn: sqlite3.Connection, opp_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not opp_ids:
        return {}
    marks = ", ".join("?" for _ in opp_ids)
    out: dict[int, list[dict[str, Any]]] = {i: [] for i in opp_ids}
    for row in conn.execute(
        f"SELECT * FROM opportunity_sources WHERE opportunity_id IN ({marks}) ORDER BY id", opp_ids
    ):
        out[row["opportunity_id"]].append(dict(row))
    return out


def distinct_countries(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT DISTINCT country FROM opportunities WHERE country IS NOT NULL ORDER BY country")
    return [r[0] for r in rows] + ["Unknown"]


def pending_analysis(conn: sqlite3.Connection, limit: int = 20, min_description: int = 80) -> list[dict[str, Any]]:
    """Unanalysed listings that have enough text to analyse, India first (80/20 priority)."""
    return _rows(conn.execute(
        """SELECT * FROM opportunities
           WHERE analyzed_at IS NULL AND LENGTH(COALESCE(description, '')) >= ?
           ORDER BY CASE market WHEN 'india' THEN 0 WHEN 'unknown' THEN 1 ELSE 2 END, id
           LIMIT ?""",
        (min_description, limit),
    ))


def discovery_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    def grouped(col: str) -> dict[str, int]:
        return dict(conn.execute(f"SELECT {col}, COUNT(*) FROM opportunities GROUP BY {col}").fetchall())

    return {
        "total": conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0],
        "status": grouped("status"),
        "market": grouped("market"),
        "source": grouped("primary_source"),
        "category": grouped("role_category"),
        "unanalysed": conn.execute("SELECT COUNT(*) FROM opportunities WHERE analyzed_at IS NULL").fetchone()[0],
    }


# --------------------------------------------------------------------------- career boards


def add_career_board(conn: sqlite3.Connection, *, company_name: str, ats: str, slug: str,
                     website: str | None = None, market_hint: str = "unknown") -> int:
    cur = conn.execute(
        "INSERT INTO career_boards (company_name, ats, slug, website, market_hint) VALUES (?, ?, ?, ?, ?)",
        (company_name.strip(), ats, slug.strip(), website, market_hint),
    )
    return cur.lastrowid


def list_career_boards(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM career_boards" + (" WHERE enabled = 1" if enabled_only else "") + " ORDER BY company_name"
    return _rows(conn.execute(sql))


def mark_board_checked(conn: sqlite3.Connection, board_id: int, result: str) -> None:
    conn.execute(
        "UPDATE career_boards SET last_checked_at = ?, last_result = ? WHERE id = ?",
        (utc_now(), result, board_id),
    )


def set_board_enabled(conn: sqlite3.Connection, board_id: int, enabled: bool) -> None:
    conn.execute("UPDATE career_boards SET enabled = ? WHERE id = ?", (int(enabled), board_id))


def delete_career_board(conn: sqlite3.Connection, board_id: int) -> None:
    conn.execute("DELETE FROM career_boards WHERE id = ?", (board_id,))
