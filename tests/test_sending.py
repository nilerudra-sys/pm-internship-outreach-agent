import base64
import email as email_lib
from email import policy
from types import SimpleNamespace

import pytest

from config.settings import Settings
from database import contacts as contacts_repo
from database import repository
from database.models import utc_now
from integrations.gmail import (
    SCOPES,
    GmailClient,
    GmailNotConnected,
    GmailSendError,
    build_message,
    load_credentials,
)
from services import quota
from services.sending.sender import (
    resolve_stuck,
    send_batch,
    send_email,
    send_test_to_self,
    sendable,
    stuck_sends,
)

SETTINGS = Settings(sender_email="riya@gmail.com", sender_name="Riya Sharma", send_interval_seconds=0)


class FakeGmail:
    def __init__(self, fail_with=None):
        self.sent, self.fail_with = [], fail_with

    def send(self, msg):
        if self.fail_with:
            raise self.fail_with
        self.sent.append(msg)
        return {"id": f"m{len(self.sent)}", "threadId": f"t{len(self.sent)}"}


def make_email(conn, name="Demo Freight", domain="demofreight.in", market="india", status="approved",
               contact_email="asha@demofreight.in", outreach_type="speculative"):
    cid = repository.add_company(conn, name=name, domain=domain, market=market)
    contact_id, _ = contacts_repo.upsert_contact(
        conn, cid, full_name=f"Asha {name.split()[0]}", role_title="CEO", role_category="ceo", source="website",
        source_url="u", evidence="e", email=contact_email, email_type="public_verified" if contact_email else "none")
    eid = repository.create_email_draft(conn, company_id=cid, contact_id=contact_id, subject=f"Hello {name}",
                                        body="Hi Asha,\n\nBody with ₹15,000.\n\nBest regards,\nRiya Sharma",
                                        outreach_type=outreach_type)
    if status == "approved":
        repository.set_email_status(conn, eid, "approved")
    return eid


# ------------------------------------------------------------------ message building + Gmail client


def test_build_message_is_utf8_plain_text_with_headers():
    msg = build_message("asha@demofreight.in", "Product internship", "Hi Asha,\n\nStipend ₹15,000.",
                        "riya@gmail.com", "Riya Sharma")
    assert msg["To"] == "asha@demofreight.in" and msg["From"] == "Riya Sharma <riya@gmail.com>"
    assert msg["Message-ID"].endswith("@gmail.com>")
    parsed = email_lib.message_from_bytes(msg.as_bytes(), policy=policy.default)
    assert "₹15,000" in parsed.get_content() and parsed.get_content_type() == "text/plain"
    assert build_message("a@b.in", "s", "b")["From"] is None    # Gmail fills in the sender


def test_gmail_client_sends_raw_base64url():
    calls = {}

    class Messages:
        def send(self, userId, body):
            calls.update(userId=userId, body=body)
            return SimpleNamespace(execute=lambda: {"id": "abc", "threadId": "thr"})

    service = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: Messages()))
    out = GmailClient(service=service).send(build_message("a@b.in", "Subj", "Body ₹"))
    assert out == {"id": "abc", "threadId": "thr"} and calls["userId"] == "me"
    raw = base64.urlsafe_b64decode(calls["body"]["raw"])
    assert b"Subject: Subj" in raw


def test_gmail_client_wraps_http_errors():
    from googleapiclient.errors import HttpError

    class Messages:
        def send(self, userId, body):
            def boom():
                raise HttpError(SimpleNamespace(status=403, reason="Forbidden"), b'{"error": "denied"}')
            return SimpleNamespace(execute=boom)

    service = SimpleNamespace(users=lambda: SimpleNamespace(messages=lambda: Messages()))
    with pytest.raises(GmailSendError) as exc:
        GmailClient(service=service).send(build_message("a@b.in", "s", "b"))
    assert exc.value.status == 403 and "HTTP 403" in str(exc.value)


def test_send_only_scope_and_missing_token(tmp_path):
    assert SCOPES == ["https://www.googleapis.com/auth/gmail.send"]
    with pytest.raises(GmailNotConnected):
        load_credentials(tmp_path / "missing.json")
    bad = tmp_path / "token.json"
    bad.write_text("{}", encoding="utf-8")
    with pytest.raises(GmailNotConnected):
        load_credentials(bad)


# ------------------------------------------------------------------ guards


def test_sends_approved_email_and_records_everything(conn):
    eid = make_email(conn)
    gmail = FakeGmail()
    r = send_email(conn, eid, gmail, SETTINGS)
    assert r.sent and r.recipient == "asha@demofreight.in" and r.gmail_message_id == "m1"
    e = repository.get_email(conn, eid)
    assert e["status"] == "sent" and e["gmail_message_id"] == "m1" and e["gmail_thread_id"] == "t1"
    assert e["recipient"] == "asha@demofreight.in" and e["send_started_at"] is None and e["sent_at"]
    assert repository.get_company(conn, e["company_id"])["status"] == "contacted"
    assert gmail.sent[0]["Subject"] == "Hello Demo Freight"
    assert quota.sent_today(conn, SETTINGS.timezone) == 1


@pytest.mark.parametrize("setup, reason", [
    (lambda conn: make_email(conn, status="pending"), "not approved"),
    (lambda conn: make_email(conn, contact_email=None), "no valid email"),
])
def test_unsendable_emails_are_refused(conn, setup, reason):
    eid = setup(conn)
    gmail = FakeGmail()
    r = send_email(conn, eid, gmail, SETTINGS)
    assert not r.sent and reason in r.skipped and gmail.sent == []


def test_do_not_contact_is_respected(conn):
    eid = make_email(conn)
    conn.execute("UPDATE contacts SET do_not_contact = 1")
    r = send_email(conn, eid, FakeGmail(), SETTINGS)
    assert r.skipped == "contact is marked do-not-contact"


def test_editing_after_approval_blocks_sending(conn):
    eid = make_email(conn)
    repository.update_email_content(conn, eid, subject="Changed", body="Hi Asha,\n\nNew text.")
    r = send_email(conn, eid, FakeGmail(), SETTINGS)
    assert not r.sent and "pending_review" in r.skipped


def test_daily_cap_is_never_exceeded(conn):
    for i in range(15):
        e = make_email(conn, name=f"Sent Co {i}", domain=f"sentco{i}.in")
        send_email(conn, e, FakeGmail(), SETTINGS)
    assert quota.sent_today(conn, SETTINGS.timezone) == 15
    extra = make_email(conn, name="One More", domain="onemore.in")
    gmail = FakeGmail()
    r = send_email(conn, extra, gmail, SETTINGS)
    assert not r.sent and "daily limit of 15" in r.skipped and gmail.sent == []
    assert send_batch(conn, gmail, SETTINGS, 10).sent == 0


def test_lock_prevents_double_send(conn):
    eid = make_email(conn)
    conn.execute("UPDATE emails SET send_started_at = ? WHERE id = ?", (utc_now(), eid))
    gmail = FakeGmail()
    r = send_email(conn, eid, gmail, SETTINGS)
    assert not r.sent and "in progress" in r.skipped and gmail.sent == []
    assert [s["id"] for s in stuck_sends(conn)] == [eid]


def test_gmail_failure_marks_failed_and_releases_lock(conn):
    eid = make_email(conn)
    r = send_email(conn, eid, FakeGmail(fail_with=GmailSendError("Gmail rejected the message (HTTP 500)", 500)),
                   SETTINGS)
    assert r.error and not r.sent
    e = repository.get_email(conn, eid)
    assert e["status"] == "failed" and "HTTP 500" in e["error"] and e["send_started_at"] is None
    assert quota.sent_today(conn, SETTINGS.timezone) == 0
    repository.set_email_status(conn, eid, "approved")        # retry path
    assert send_email(conn, eid, FakeGmail(), SETTINGS).sent


def test_resolve_stuck(conn):
    a, b = make_email(conn), make_email(conn, name="Beta Co", domain="betaco.in")
    for eid in (a, b):
        conn.execute("UPDATE emails SET send_started_at = ? WHERE id = ?", (utc_now(), eid))
    resolve_stuck(conn, a, was_sent=True)
    resolve_stuck(conn, b, was_sent=False)
    assert repository.get_email(conn, a)["status"] == "sent"
    eb = repository.get_email(conn, b)
    assert eb["status"] == "approved" and eb["send_started_at"] is None
    with pytest.raises(ValueError):
        resolve_stuck(conn, a, was_sent=True)


# ------------------------------------------------------------------ batch


def test_batch_order_cap_and_spacing(conn):
    for i in range(5):
        make_email(conn, name=f"India Co {i}", domain=f"indiaco{i}.in")
    for i in range(2):
        make_email(conn, name=f"World Co {i}", domain=f"worldco{i}.com", market="international",
                   outreach_type="vacancy" if i == 1 else "speculative")
    order = [r["id"] for r in sendable(conn, 0.8)]
    first_markets = [conn.execute("SELECT c.market FROM emails e JOIN companies c ON c.id=e.company_id "
                                  "WHERE e.id=?", (i,)).fetchone()[0] for i in order[:5]]
    assert first_markets.count("india") == 4
    sleeps = []
    settings = Settings(sender_email="riya@gmail.com", send_interval_seconds=40)
    batch = send_batch(conn, FakeGmail(), settings, 3, sleep=sleeps.append)
    assert batch.sent == 3 and len(sleeps) == 2 and all(32 <= s <= 48 for s in sleeps)


def test_batch_stops_on_authorisation_error(conn):
    for i in range(3):
        make_email(conn, name=f"Co {i}", domain=f"co{i}.in")
    gmail = FakeGmail(fail_with=GmailSendError("Gmail rejected the message (HTTP 401): invalid credentials", 401))
    batch = send_batch(conn, gmail, SETTINGS, 3)
    assert len(batch.results) == 1 and "authorisation" in batch.stopped


def test_batch_never_exceeds_remaining_cap(conn):
    for i in range(14):
        send_email(conn, make_email(conn, name=f"Done {i}", domain=f"done{i}.in"), FakeGmail(), SETTINGS)
    for i in range(4):
        make_email(conn, name=f"Left {i}", domain=f"left{i}.in")
    batch = send_batch(conn, FakeGmail(), SETTINGS, 10)
    assert batch.sent == 1 and quota.sent_today(conn, SETTINGS.timezone) == 15


# ------------------------------------------------------------------ test send


def test_send_test_to_self_changes_nothing(conn):
    eid = make_email(conn, status="pending")
    gmail = FakeGmail()
    send_test_to_self(conn, eid, gmail, SETTINGS)
    msg = gmail.sent[0]
    assert msg["To"] == "riya@gmail.com" and msg["Subject"].startswith("[TEST] ")
    assert "Would be sent to Asha Demo <asha@demofreight.in>" in msg.get_content()
    assert repository.get_email(conn, eid)["status"] == "pending_review"
    assert quota.sent_today(conn, SETTINGS.timezone) == 0
    with pytest.raises(GmailNotConnected):
        send_test_to_self(conn, eid, gmail, Settings())


def test_v6_emails_gain_send_columns(tmp_path):
    import sqlite3

    from database.db import connect, init_db

    path = tmp_path / "v6.db"
    init_db(path)
    c = sqlite3.connect(path)
    c.execute("ALTER TABLE emails DROP COLUMN recipient")
    c.execute("ALTER TABLE emails DROP COLUMN send_started_at")
    c.execute("PRAGMA user_version = 6")
    c.commit()
    c.close()
    init_db(path)
    cols = {r[1] for r in connect(path).execute("PRAGMA table_info(emails)")}
    assert {"recipient", "send_started_at"} <= cols
