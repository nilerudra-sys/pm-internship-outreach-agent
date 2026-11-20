"""Send approved emails through Gmail, with every safety check enforced in code.

Guards (all re-checked immediately before each send):
- status is 'approved' (any edit sends a draft back to review, so approval matches the content)
- the contact has a valid email address and is not marked do-not-contact
- the daily cap (max 15, in your timezone) has room
- a send lock is taken first, so a double click or second tab cannot send twice

If Gmail fails, the email becomes 'failed' with the error and can be re-approved. If the app stops
between Gmail accepting the email and the database update, the lock remains and the email is shown as
"send state unknown" until you check your Gmail Sent folder and resolve it.
"""

from __future__ import annotations

import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from config.settings import Settings
from database import repository
from database.models import utc_now
from integrations.gmail import GmailNotConnected, build_message
from services import quota
from services.contacts.emails import validate_email
from services.discovery.scoring import mix_by_market


@dataclass
class SendResult:
    email_id: int
    sent: bool = False
    skipped: str | None = None
    error: str | None = None
    gmail_message_id: str | None = None
    recipient: str | None = None


@dataclass
class BatchResult:
    results: list[SendResult] = field(default_factory=list)
    stopped: str | None = None

    @property
    def sent(self) -> int:
        return sum(r.sent for r in self.results)


def _email_row(conn: sqlite3.Connection, email_id: int) -> dict | None:
    row = conn.execute(
        """SELECT e.*, ct.email AS contact_email, ct.do_not_contact, ct.full_name AS contact_name,
                  c.domain AS company_domain, c.market, c.name AS company_name
           FROM emails e JOIN contacts ct ON ct.id = e.contact_id JOIN companies c ON c.id = e.company_id
           WHERE e.id = ?""", (email_id,)).fetchone()
    return dict(row) if row else None


def preflight(conn: sqlite3.Connection, email: dict, settings: Settings) -> str | None:
    """Reason this email must not be sent now, or None if it may."""
    if email["status"] != "approved":
        return f"status is {email['status']!r}, not approved"
    if email.get("send_started_at"):
        return "a send is already in progress or its result is unknown"
    if email["do_not_contact"]:
        return "contact is marked do-not-contact"
    check = validate_email(email["contact_email"], email["company_domain"])
    if not check.ok:
        return "contact has no valid email address"
    if quota.remaining_today(conn, settings.daily_email_limit, settings.timezone) <= 0:
        return f"daily limit of {settings.daily_email_limit} emails reached"
    return None


def _take_lock(conn: sqlite3.Connection, email_id: int) -> bool:
    cur = conn.execute(
        "UPDATE emails SET send_started_at = ? WHERE id = ? AND status = 'approved' AND send_started_at IS NULL",
        (utc_now(), email_id))
    conn.commit()
    return cur.rowcount == 1


def send_email(conn: sqlite3.Connection, email_id: int, gmail, settings: Settings) -> SendResult:
    """Send one approved email now. Never raises for expected failures; returns what happened."""
    result = SendResult(email_id)
    email = _email_row(conn, email_id)
    if email is None:
        result.skipped = "email not found"
        return result
    reason = preflight(conn, email, settings)
    if reason:
        result.skipped = reason
        return result
    if not _take_lock(conn, email_id):
        result.skipped = "another send of this email is in progress"
        return result
    # Re-check the cap after taking the lock (another tab may have sent in the meantime).
    if quota.remaining_today(conn, settings.daily_email_limit, settings.timezone) <= 0:
        conn.execute("UPDATE emails SET send_started_at = NULL WHERE id = ?", (email_id,))
        conn.commit()
        result.skipped = f"daily limit of {settings.daily_email_limit} emails reached"
        return result

    recipient = validate_email(email["contact_email"], email["company_domain"]).email
    parent = _email_row(conn, email["parent_email_id"]) if email.get("parent_email_id") else None
    if parent and (parent["status"] == "replied" or parent.get("replied_at")):
        conn.execute("UPDATE emails SET send_started_at = NULL WHERE id = ?", (email_id,))
        conn.commit()
        result.skipped = "the contact already replied to the original email"
        return result
    msg = build_message(recipient, email["subject"], email["body"], settings.sender_email or None,
                        settings.sender_name or None, in_reply_to=(parent or {}).get("message_id_header"))
    thread_id = (parent or {}).get("gmail_thread_id")
    try:
        sent = gmail.send(msg, thread_id=thread_id) if thread_id else gmail.send(msg)
    except Exception as exc:
        conn.execute("UPDATE emails SET send_started_at = NULL WHERE id = ?", (email_id,))
        repository.set_email_status(conn, email_id, "failed", error=str(exc)[:500])
        conn.commit()
        result.error = str(exc)[:300]
        return result

    repository.set_email_status(conn, email_id, "sent", gmail_message_id=sent["id"],
                                gmail_thread_id=sent.get("threadId"))
    conn.execute("UPDATE emails SET recipient = ?, send_started_at = NULL, message_id_header = ? WHERE id = ?",
                 (recipient, msg["Message-ID"], email_id))
    conn.execute("UPDATE companies SET status = 'contacted', updated_at = ? WHERE id = ? AND status != 'disqualified'",
                 (utc_now(), email["company_id"]))
    conn.commit()
    result.sent, result.gmail_message_id, result.recipient = True, sent["id"], recipient
    return result


def sendable(conn: sqlite3.Connection, india_ratio: float) -> list[dict]:
    """Approved, unlocked emails in send order: vacancy-based first, then 80/20 by market."""
    rows = [dict(r) for r in conn.execute(
        """SELECT e.id, e.outreach_type, e.approved_at, c.market FROM emails e
           JOIN companies c ON c.id = e.company_id
           WHERE e.status = 'approved' AND e.send_started_at IS NULL
           ORDER BY CASE e.outreach_type WHEN 'vacancy' THEN 0 ELSE 1 END, e.approved_at, e.id""")]
    return mix_by_market(rows, india_ratio)


def send_batch(conn: sqlite3.Connection, gmail, settings: Settings, limit: int,
               on_progress: Callable[[int, int, SendResult], None] | None = None,
               sleep: Callable[[float], None] = time.sleep) -> BatchResult:
    """Send up to `limit` approved emails (never beyond today's remaining cap), pausing between sends."""
    batch = BatchResult()
    remaining = quota.remaining_today(conn, settings.daily_email_limit, settings.timezone)
    queue = sendable(conn, settings.india_ratio)[:max(0, min(limit, remaining))]
    if not queue and remaining <= 0:
        batch.stopped = f"daily limit of {settings.daily_email_limit} reached"
    for i, item in enumerate(queue, start=1):
        if i > 1 and settings.send_interval_seconds > 0:
            # Human-like spacing; also keeps well within Gmail's sending limits.
            sleep(settings.send_interval_seconds * random.uniform(0.8, 1.2))
        r = send_email(conn, item["id"], gmail, settings)
        batch.results.append(r)
        if on_progress:
            on_progress(i, len(queue), r)
        if r.error and "HTTP 4" in r.error and any(code in r.error for code in ("401", "403", "429")):
            batch.stopped = f"stopped after a Gmail authorisation or rate-limit error: {r.error[:120]}"
            break
        if r.skipped and "daily limit" in r.skipped:
            batch.stopped = r.skipped
            break
    return batch


def send_test_to_self(conn: sqlite3.Connection, email_id: int, gmail, settings: Settings) -> dict:
    """Send a copy of any draft to SENDER_EMAIL with a [TEST] subject. Does not change its status or the cap."""
    if not settings.sender_email:
        raise GmailNotConnected("Set SENDER_EMAIL in .env to send a test to yourself.")
    email = _email_row(conn, email_id)
    if email is None:
        raise ValueError("email not found")
    msg = build_message(settings.sender_email, f"[TEST] {email['subject']}",
                        f"(Test copy. Would be sent to {email['contact_name']} <{email['contact_email']}>.)\n\n"
                        + email["body"], settings.sender_email, settings.sender_name or None)
    out = gmail.send(msg)
    repository.log_activity(conn, "email", email_id, "test_sent", settings.sender_email)
    return out


def stuck_sends(conn: sqlite3.Connection) -> list[dict]:
    """Emails whose send started but never finished (app stopped mid-send): result unknown."""
    return [dict(r) for r in conn.execute(
        """SELECT e.id, e.subject, e.send_started_at, ct.full_name AS contact_name, ct.email AS contact_email,
                  c.name AS company_name
           FROM emails e JOIN contacts ct ON ct.id = e.contact_id JOIN companies c ON c.id = e.company_id
           WHERE e.status = 'approved' AND e.send_started_at IS NOT NULL ORDER BY e.send_started_at""")]


def resolve_stuck(conn: sqlite3.Connection, email_id: int, was_sent: bool) -> None:
    """After you checked Gmail's Sent folder: mark it sent, or release it so it can be sent again."""
    email = _email_row(conn, email_id)
    if email is None or email["status"] != "approved" or not email.get("send_started_at"):
        raise ValueError("email is not in an unknown send state")
    if was_sent:
        repository.set_email_status(conn, email_id, "sent", notes="marked sent after checking Gmail Sent folder")
        conn.execute("UPDATE emails SET recipient = ?, send_started_at = NULL WHERE id = ?",
                     (email["contact_email"], email_id))
    else:
        conn.execute("UPDATE emails SET send_started_at = NULL WHERE id = ?", (email_id,))
        repository.log_activity(conn, "email", email_id, "send_released", "not found in Gmail Sent folder")
    conn.commit()
