"""Hybrid approval: every drafted email is reviewed here before it can be sent."""

import json

import streamlit as st

from config.settings import get_settings
from database import repository
from database.db import get_connection
from dashboard.components import MARKET_LABELS
from services import quota
from services.outreach.writer import recheck_email

settings = get_settings()

st.title("Review queue")
st.caption("Edit, approve or reject drafts. Nothing is sent without your approval.")

with get_connection() as conn:
    pending = repository.list_emails(conn, "pending_review")
    approved = repository.list_emails(conn, "approved")
    remaining = quota.remaining_today(conn, settings.daily_email_limit, settings.timezone)

with get_connection() as conn:
    sent_list = repository.list_emails(conn, "sent")
    failed = repository.list_emails(conn, "failed")

tab_pending, tab_approved, tab_sent = st.tabs([f"Pending ({len(pending)})", f"Approved ({len(approved)})",
                                               f"Sent ({len(sent_list)}) · Failed ({len(failed)})"])

with tab_sent:
    from services.sending.followups import check_replies, create_due_followups, due_followups, mark_replied

    with get_connection() as conn:
        due = due_followups(conn, settings)
    f1, f2 = st.columns(2)
    if f1.button(f"✉️ Create {len(due)} follow-up draft(s)", disabled=not due,
                 help=f"For emails sent {settings.followup_after_days}+ days ago with no reply. "
                      "They appear under Pending for you to review."):
        with get_connection() as conn:
            create_due_followups(conn, settings)
        st.rerun()
    if f2.button("🔄 Check Gmail for replies", disabled=not settings.gmail_token_file.exists()):
        from integrations.gmail import GmailClient, GmailNotConnected

        try:
            client = GmailClient(settings.gmail_token_file)
            with get_connection() as conn:
                r = check_replies(conn, client, settings)
            st.success(f"Checked {r['checked']}: {r['replies']} replied, {r['bounces']} bounced.")
        except GmailNotConnected as exc:
            st.warning(f"{exc} You can also mark replies manually below.")
    with get_connection() as conn:
        open_sent = repository.list_emails(conn, "sent")
    if open_sent:
        pick = st.selectbox("Got a reply? Mark it", [f"#{e['id']} · {e['company_name']} · {e['subject']}"
                                                     for e in open_sent], key="mark_replied")
        if st.button("✅ Mark replied (stops follow-ups)"):
            with get_connection() as conn:
                mark_replied(conn, int(pick.split(" ·")[0][1:]))
            st.rerun()
    for email in failed:
        with st.container(border=True):
            st.error(f"**{email['subject']}** → {email['contact_name']}: {email['error']}")
            if st.button("↻ Approve again to retry", key=f"retry_{email['id']}"):
                with get_connection() as conn:
                    repository.set_email_status(conn, email["id"], "approved", notes="retry after failure")
                st.rerun()
    if sent_list:
        import pandas as pd

        st.dataframe(pd.DataFrame([{
            "sent (UTC)": (e["sent_at"] or "")[:16], "company": e["company_name"], "to": e.get("recipient") or
            e["contact_email"], "subject": e["subject"], "type": e.get("outreach_type"),
            "gmail id": e["gmail_message_id"]} for e in reversed(sent_list)]), width="stretch", hide_index=True)
    elif not failed:
        st.info("Nothing sent yet.")

with tab_pending:
    if not pending:
        st.info("No drafts waiting for review.")
    for email in pending:
        with st.container(border=True):
            kind = {"vacancy": "📌 Vacancy-based", "speculative": "💡 Speculative"}.get(email.get("outreach_type"), "")
            st.markdown(
                f"**{email['company_name']}** · {MARKET_LABELS[email['market']]} · {email['sector']}"
                + (f" · {kind}" if kind else "") + "  \n"
                f"To: {email['contact_name']} ({email['role_title'] or email['role_category']}) "
                f"&lt;{email['contact_email'] or 'no email'}&gt;"
                + (f"  \nDrafted by `{email['model']}`" if email.get("model") else "")
            )
            facts = json.loads(email["facts_used"]) if email.get("facts_used") else []
            checks = json.loads(email["checks"]) if email.get("checks") else {}
            if facts:
                with st.expander(f"✅ Verified facts used ({len(facts)}): check them before approving"):
                    for f in facts:
                        st.markdown(f"- {f['text']}" + (f" · [source]({f['url']})" if f.get("url") else ""))
                        if f.get("quote"):
                            st.caption(f"“{f['quote']}”")
            for w in checks.get("warnings", []):
                st.caption(f"⚠️ {w}")
            for n, errs in enumerate(checks.get("rejected_attempts") or [], start=1):
                st.caption(f"↻ Attempt {n} was rejected and rewritten: {'; '.join(errs)}")
            if email.get("checks"):
                with get_connection() as conn:
                    live = recheck_email(conn, email)
                if live and live.errors:
                    st.warning("Automatic check on the current text (advice only, your edits may be "
                               "intentional): " + "; ".join(live.errors))
            with st.form(f"review_{email['id']}"):
                subject = st.text_input("Subject", email["subject"])
                body = st.text_area("Body", email["body"], height=220)
                notes = st.text_input("Notes (optional)", email["reviewer_notes"] or "")
                b1, b2, b3 = st.columns(3)
                save = b1.form_submit_button("💾 Save")
                approve = b2.form_submit_button("✅ Approve", type="primary")
                reject = b3.form_submit_button("❌ Reject")

            if approve and not email["contact_email"]:
                st.error("This contact has no email address; cannot approve.")
            elif save or approve or reject:
                with get_connection() as conn:
                    if subject != email["subject"] or body != email["body"]:
                        repository.update_email_content(conn, email["id"], subject=subject, body=body)
                    if approve:
                        repository.set_email_status(conn, email["id"], "approved", notes=notes or None)
                    elif reject:
                        repository.set_email_status(conn, email["id"], "rejected", notes=notes or None)
                st.rerun()

with tab_approved:
    from integrations.gmail import GmailClient, GmailNotConnected
    from services.sending.sender import (
        resolve_stuck,
        send_batch,
        send_email,
        send_test_to_self,
        stuck_sends,
    )

    gmail_ready = settings.gmail_token_file.exists()
    st.write(f"Remaining sends today: **{remaining}** of {settings.daily_email_limit}"
             + ("" if gmail_ready else " · ⚠️ Gmail not connected (Settings → Connect Gmail)"))

    def _gmail():
        try:
            return GmailClient(settings.gmail_token_file)
        except GmailNotConnected as exc:
            st.error(str(exc))
            return None

    with get_connection() as conn:
        stuck = stuck_sends(conn)
    for s in stuck:
        with st.container(border=True):
            st.warning(f"Send state unknown for **{s['subject']}** → {s['contact_name']} <{s['contact_email']}> "
                       f"(started {s['send_started_at'][:16]} UTC). Check your Gmail **Sent** folder, then:")
            k1, k2 = st.columns(2)
            if k1.button("It was sent", key=f"stuck_sent_{s['id']}"):
                with get_connection() as conn:
                    resolve_stuck(conn, s["id"], True)
                st.rerun()
            if k2.button("It was not sent (allow resend)", key=f"stuck_release_{s['id']}"):
                with get_connection() as conn:
                    resolve_stuck(conn, s["id"], False)
                st.rerun()

    ready_to_send = [e for e in approved if not e.get("send_started_at")]
    if not ready_to_send:
        st.info("No approved emails.")
    else:
        with st.container(border=True):
            b1, b2 = st.columns([1, 3])
            n = b1.number_input("Send up to", 1, max(1, min(len(ready_to_send), remaining or 1)),
                                min(len(ready_to_send), max(remaining, 1)), key="batch_n")
            b2.caption(f"Vacancy-based first, then {settings.india_ratio:.0%} India. About "
                       f"{settings.send_interval_seconds:g} s between emails. Never more than today's remaining "
                       f"limit.")
            if st.button(f"📨 Send next {int(n)} approved", type="primary", disabled=not gmail_ready or remaining <= 0):
                gmail = _gmail()
                if gmail:
                    bar = st.progress(0.0, text="Sending…")
                    with get_connection() as conn:
                        batch = send_batch(conn, gmail, settings, int(n),
                                           lambda i, t, r: bar.progress(i / t, text=f"{i}/{t} sent"))
                    st.success(f"Sent {batch.sent} email(s).")
                    for r in batch.results:
                        if r.error:
                            st.error(f"Email #{r.email_id}: {r.error}")
                        elif r.skipped:
                            st.caption(f"Email #{r.email_id} skipped: {r.skipped}")
                    if batch.stopped:
                        st.warning(batch.stopped)

    for email in ready_to_send:
        with st.container(border=True):
            st.markdown(
                f"**{email['subject']}**  \n{email['company_name']} → {email['contact_name']} "
                f"&lt;{email['contact_email']}&gt;"
            )
            with st.expander("Preview"):
                st.text(email["body"])
            c1, c2, c3 = st.columns(3)
            if c1.button("📨 Send now", key=f"send_{email['id']}", type="primary",
                         disabled=not gmail_ready or remaining <= 0,
                         help=None if gmail_ready else "Connect Gmail in Settings first."):
                gmail = _gmail()
                if gmail:
                    with get_connection() as conn:
                        r = send_email(conn, email["id"], gmail, settings)
                    if r.sent:
                        st.success(f"Sent to {r.recipient}.")
                        st.rerun()
                    st.error(r.error or f"Not sent: {r.skipped}")
            if c2.button("🧪 Send test to myself", key=f"test_{email['id']}", disabled=not gmail_ready,
                         help="Sends a [TEST] copy to SENDER_EMAIL. Does not count toward the daily limit."):
                gmail = _gmail()
                if gmail:
                    try:
                        with get_connection() as conn:
                            send_test_to_self(conn, email["id"], gmail, settings)
                        st.success(f"Test copy sent to {settings.sender_email}.")
                    except Exception as exc:
                        st.error(str(exc))
            if c3.button("↩️ Back to review", key=f"unapprove_{email['id']}"):
                with get_connection() as conn:
                    repository.set_email_status(conn, email["id"], "pending_review")
                st.rerun()
