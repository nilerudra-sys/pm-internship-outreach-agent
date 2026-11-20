"""Follow-ups and reply tracking.

Follow-ups use a fixed template (no AI, so nothing can be invented), go to the Review queue like any
draft, and are sent in the original Gmail thread. At most `max_followups` per original email, never after
a reply, never to do-not-contact contacts. Replies are marked manually or detected from Gmail headers.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from config.settings import Settings
from database import repository
from database.models import utc_now

TEMPLATE = ("Hi {first},\n\n"
            "I wanted to gently follow up on my note below about a product internship at {company}. "
            "I know things get busy, so no pressure at all.\n\n"
            "If there could be room for a product intern, even a short 15-minute call would mean a lot. "
            "And if it's not the right time, a quick no is completely fine too.\n\n"
            "Best regards,{signature}")


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def due_followups(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Sent originals older than FOLLOWUP_AFTER_DAYS with no reply and fewer than MAX_FOLLOWUPS follow-ups."""
    if settings.max_followups <= 0:
        return []
    rows = conn.execute(
        """SELECT e.*, c.name AS company_name, ct.first_name, ct.full_name AS contact_name, ct.do_not_contact,
                  (SELECT COUNT(*) FROM emails f WHERE f.parent_email_id = e.id AND f.status != 'rejected')
                    AS followups
           FROM emails e JOIN companies c ON c.id = e.company_id JOIN contacts ct ON ct.id = e.contact_id
           WHERE e.kind = 'initial' AND e.status = 'sent' AND e.replied_at IS NULL AND e.sent_at <= ?
             AND ct.do_not_contact = 0
           ORDER BY e.sent_at""", (_cutoff(settings.followup_after_days),)).fetchall()
    return [dict(r) for r in rows if r["followups"] < settings.max_followups]


def create_followup(conn: sqlite3.Connection, original: dict, settings: Settings) -> int:
    subject = original["subject"] if original["subject"].lower().startswith("re:") else f"Re: {original['subject']}"
    body = TEMPLATE.format(first=original["first_name"] or "there", company=original["company_name"],
                           signature=f"\n{settings.sender_name}" if settings.sender_name else "")
    eid = repository.create_email_draft(conn, company_id=original["company_id"], contact_id=original["contact_id"],
                                        subject=subject, body=body, model="template", kind="follow_up",
                                        outreach_type=original.get("outreach_type"))
    conn.execute("UPDATE emails SET parent_email_id = ? WHERE id = ?", (original["id"], eid))
    return eid


def create_due_followups(conn: sqlite3.Connection, settings: Settings) -> list[int]:
    ids = [create_followup(conn, o, settings) for o in due_followups(conn, settings)]
    conn.commit()
    return ids


def mark_replied(conn: sqlite3.Connection, email_id: int, note: str = "marked replied") -> None:
    """Mark an email (and its thread's original) as replied; withdraw any unsent follow-ups."""
    email = repository.get_email(conn, email_id)
    root_id = email["parent_email_id"] or email_id
    for eid in {root_id, email_id}:
        e = repository.get_email(conn, eid)
        if e["status"] == "sent":
            repository.set_email_status(conn, eid, "replied", notes=note)
        conn.execute("UPDATE emails SET replied_at = COALESCE(replied_at, ?) WHERE id = ?", (utc_now(), eid))
    for f in conn.execute("SELECT id, status FROM emails WHERE parent_email_id = ? AND status IN "
                          "('pending_review', 'approved')", (root_id,)).fetchall():
        repository.set_email_status(conn, f["id"], "rejected", notes="contact replied; follow-up withdrawn")
    conn.commit()


def check_replies(conn: sqlite3.Connection, gmail, settings: Settings) -> dict[str, int]:
    """Look at each sent thread's headers in Gmail; mark replies, and flag bounces as do-not-contact."""
    out = {"checked": 0, "replies": 0, "bounces": 0, "errors": 0}
    rows = conn.execute("""SELECT id, contact_id, gmail_thread_id FROM emails
                           WHERE status = 'sent' AND replied_at IS NULL AND gmail_thread_id IS NOT NULL
                             AND parent_email_id IS NULL""").fetchall()
    for r in rows:
        out["checked"] += 1
        try:
            state = gmail.thread_reply_state(r["gmail_thread_id"], settings.sender_email or "")
        except Exception:
            out["errors"] += 1
            continue
        if state == "reply":
            mark_replied(conn, r["id"], "reply detected in Gmail")
            out["replies"] += 1
        elif state == "bounce":
            conn.execute("UPDATE contacts SET do_not_contact = 1, notes = COALESCE(notes || ' · ', '') || "
                         "'email bounced' WHERE id = ?", (r["contact_id"],))
            repository.log_activity(conn, "email", r["id"], "bounced")
            out["bounces"] += 1
    conn.commit()
    return out
