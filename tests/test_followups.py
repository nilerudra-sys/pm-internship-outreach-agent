from types import SimpleNamespace

from config.settings import Settings
from database import repository
from integrations.gmail import GmailClient, build_message
from services.sending.followups import check_replies, create_due_followups, due_followups, mark_replied
from services.sending.sender import send_email
from tests.test_sending import FakeGmail, make_email

S = Settings(sender_email="riya@gmail.com", sender_name="Riya Sharma", send_interval_seconds=0,
             followup_after_days=5, max_followups=1)


class ThreadGmail(FakeGmail):
    def send(self, msg, thread_id=None):
        self.thread_ids = getattr(self, "thread_ids", []) + [thread_id]
        return super().send(msg)


def _sent_original(conn, days_ago=6):
    eid = make_email(conn)
    send_email(conn, eid, FakeGmail(), S)
    conn.execute("UPDATE emails SET sent_at = datetime('now', ?) || '+00:00' WHERE id = ?", (f"-{days_ago} days", eid))
    conn.execute("UPDATE emails SET sent_at = REPLACE(sent_at, ' ', 'T') WHERE id = ?", (eid,))
    return eid


def test_followup_due_only_after_delay_and_once(conn):
    assert due_followups(conn, S) == []
    recent = make_email(conn, name="Recent Co", domain="recent.in")
    send_email(conn, recent, FakeGmail(), S)
    orig = _sent_original(conn)
    assert [d["id"] for d in due_followups(conn, S)] == [orig]
    [fid] = create_due_followups(conn, S)
    f = repository.get_email(conn, fid)
    assert f["kind"] == "follow_up" and f["status"] == "pending_review" and f["parent_email_id"] == orig
    assert f["subject"] == "Re: Hello Demo Freight" and f["body"].startswith("Hi Asha,")
    assert f["body"].endswith("Best regards,\nRiya Sharma") and f["model"] == "template"
    assert due_followups(conn, S) == []                          # max one follow-up


def test_followup_sent_in_same_thread_with_headers(conn):
    orig = _sent_original(conn)
    [fid] = create_due_followups(conn, S)
    repository.set_email_status(conn, fid, "approved")
    gmail = ThreadGmail()
    r = send_email(conn, fid, gmail, S)
    assert r.sent and gmail.thread_ids == ["t1"]
    parent = repository.get_email(conn, orig)
    assert gmail.sent[0]["In-Reply-To"] == parent["message_id_header"] and parent["message_id_header"]


def test_reply_stops_followups(conn):
    orig = _sent_original(conn)
    [fid] = create_due_followups(conn, S)
    repository.set_email_status(conn, fid, "approved")
    mark_replied(conn, orig)
    assert repository.get_email(conn, orig)["status"] == "replied"
    assert repository.get_email(conn, fid)["status"] == "rejected"
    assert due_followups(conn, S) == []


def test_followup_not_sent_if_reply_arrived_after_approval(conn):
    orig = _sent_original(conn)
    [fid] = create_due_followups(conn, S)
    repository.set_email_status(conn, fid, "approved")
    conn.execute("UPDATE emails SET replied_at = '2026-11-20T00:00:00+00:00' WHERE id = ?", (orig,))
    r = send_email(conn, fid, FakeGmail(), S)
    assert not r.sent and "already replied" in r.skipped


def _thread_service(froms):
    msgs = [{"payload": {"headers": [{"name": "From", "value": f}]}} for f in froms]
    threads = SimpleNamespace(get=lambda **kw: SimpleNamespace(execute=lambda: {"messages": msgs}))
    return SimpleNamespace(users=lambda: SimpleNamespace(threads=lambda: threads))


def test_reply_and_bounce_detection(conn):
    me = "Riya <riya@gmail.com>"
    assert GmailClient(service=_thread_service([me, "Asha <asha@x.in>"]), reply_tracking=True) \
        .thread_reply_state("t", "riya@gmail.com") == "reply"
    assert GmailClient(service=_thread_service([me]), reply_tracking=True) \
        .thread_reply_state("t", "riya@gmail.com") is None
    assert GmailClient(service=_thread_service([me, "Mail Delivery <mailer-daemon@google.com>"]),
                       reply_tracking=True).thread_reply_state("t", "riya@gmail.com") == "bounce"

    orig = _sent_original(conn)
    out = check_replies(conn, GmailClient(service=_thread_service([me, "asha@x.in"]), reply_tracking=True), S)
    assert out["replies"] == 1 and repository.get_email(conn, orig)["status"] == "replied"


def test_bounce_marks_contact_do_not_contact(conn):
    orig = _sent_original(conn)
    gm = GmailClient(service=_thread_service(["riya@gmail.com", "mailer-daemon@google.com"]), reply_tracking=True)
    assert check_replies(conn, gm, S)["bounces"] == 1
    cid = repository.get_email(conn, orig)["contact_id"]
    assert conn.execute("SELECT do_not_contact FROM contacts WHERE id = ?", (cid,)).fetchone()[0] == 1
    assert due_followups(conn, S) == []


def test_in_reply_to_header():
    msg = build_message("a@b.in", "Re: x", "body", "riya@gmail.com", in_reply_to="<abc@gmail.com>")
    assert msg["In-Reply-To"] == "<abc@gmail.com>" and msg["References"] == "<abc@gmail.com>"
