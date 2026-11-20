from datetime import date

from database import repository
from services import quota


def test_day_bounds_ist():
    start, end = quota.local_day_bounds_utc("Asia/Kolkata", date(2026, 11, 17))
    assert start == "2026-11-16T18:30:00+00:00"
    assert end == "2026-11-17T18:30:00+00:00"


def test_next_market_keeps_80_20():
    india = intl = 0
    for _ in range(100):
        if quota.next_market(india, intl, 0.8) == "india":
            india += 1
        else:
            intl += 1
    assert (india, intl) == (80, 20)


def test_next_market_starts_with_india():
    assert quota.next_market(0, 0, 0.8) == "india"


def _sent_email(conn, company_id, contact_id):
    email_id = repository.create_email_draft(
        conn, company_id=company_id, contact_id=contact_id, subject="s", body="b"
    )
    repository.set_email_status(conn, email_id, "approved")
    repository.set_email_status(conn, email_id, "sent")
    return email_id


def test_sent_today_and_remaining(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    assert quota.remaining_today(conn, 15, "Asia/Kolkata") == 15
    for _ in range(3):
        _sent_email(conn, company_id, contact_id)
    assert quota.sent_today(conn, "Asia/Kolkata") == 3
    assert quota.remaining_today(conn, 15, "Asia/Kolkata") == 12


def test_remaining_respects_hard_cap(conn):
    assert quota.remaining_today(conn, 100, "Asia/Kolkata") == 15


def test_market_mix_counts_committed_only(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    repository.create_email_draft(conn, company_id=company_id, contact_id=contact_id, subject="s", body="b")
    assert quota.market_mix(conn) == {"india": 0, "international": 0}
    _sent_email(conn, company_id, contact_id)
    assert quota.market_mix(conn) == {"india": 1, "international": 0}
