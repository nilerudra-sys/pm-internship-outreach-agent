"""Daily sending cap and the 80/20 India/international market mix."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from config.settings import HARD_DAILY_CAP

_FMT = "%Y-%m-%dT%H:%M:%S+00:00"


def local_day_bounds_utc(tz_name: str, day: date | None = None) -> tuple[str, str]:
    """UTC [start, end) timestamps covering one calendar day in the given timezone."""
    tz = ZoneInfo(tz_name)
    day = day or datetime.now(tz).date()
    start_local = datetime.combine(day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).strftime(_FMT),
        end_local.astimezone(timezone.utc).strftime(_FMT),
    )


def sent_today(conn: sqlite3.Connection, tz_name: str, day: date | None = None) -> int:
    start, end = local_day_bounds_utc(tz_name, day)
    return conn.execute(
        "SELECT COUNT(*) FROM emails WHERE sent_at >= ? AND sent_at < ? AND status IN ('sent', 'replied')",
        (start, end),
    ).fetchone()[0]


def remaining_today(conn: sqlite3.Connection, daily_limit: int, tz_name: str) -> int:
    limit = max(0, min(daily_limit, HARD_DAILY_CAP))
    return max(0, limit - sent_today(conn, tz_name))


def market_mix(conn: sqlite3.Connection) -> dict[str, int]:
    """Count outreach committed per market (approved + sent + replied)."""
    rows = conn.execute(
        """SELECT c.market, COUNT(*) FROM emails e
           JOIN companies c ON c.id = e.company_id
           WHERE e.status IN ('approved', 'sent', 'replied')
           GROUP BY c.market"""
    ).fetchall()
    mix = {"india": 0, "international": 0}
    mix.update(dict(rows))
    return mix


def next_market(india: int, international: int, india_ratio: float) -> str:
    """Which market the next email should target to keep the mix closest to india_ratio."""
    total_after = india + international + 1
    return "india" if india < india_ratio * total_after else "international"
