"""Phase 5: draft personalised emails for ready contacts. Drafts go to the Review queue; nothing is sent."""

import pandas as pd
import streamlit as st

from config.settings import get_settings
from database.db import get_connection
from dashboard.discovery_ui import MARKET_LABELS
from services.outreach.writer import draft_candidates

settings = get_settings()

st.title("Draft Emails")
st.caption("Gemini drafts one email per company for the top-priority contact, using only verified company facts "
           "(with quotes), the actual listing if there is one, and your profile. Each draft is checked automatically "
           "(numbers, names, facts used, no invented vacancy) and then waits for you in the **Review queue**. "
           "Nothing is sent from this page.")

if not settings.sender_name:
    st.warning("`SENDER_NAME` is not set in `.env`, so drafts end with “Best regards,” and no name. "
               "Set it and restart for a signed email.")

with get_connection() as conn:
    candidates = draft_candidates(conn)
ready = [c for c in candidates if c["ready"]]

c1, c2, c3 = st.columns(3)
c1.metric("Ready to draft", len(ready))
c2.metric("Not ready", len(candidates) - len(ready))
c3.metric("Daily send limit", settings.daily_email_limit, help="Drafting is not limited; sending is (Phase 6).")

if candidates:
    st.dataframe(pd.DataFrame([{
        "company": c["company_name"], "market": MARKET_LABELS.get(c["market"], c["market"]),
        "contact": c["full_name"], "designation": c["role_title"], "score": c["score_total"],
        "type": c["outreach_type"], "verified facts": c["verified_facts"],
        "status": "✅ ready" if c["ready"] else "⛔ " + "; ".join(c["blockers"]),
    } for c in candidates]), width="stretch", hide_index=True,
        column_config={"score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d")})
else:
    st.info("No candidates. Contacts appear here once a company has a top-priority contact and no email in "
            "progress. Use **Contact Discovery** and **Company Research** first.")

if ready:
    with st.container(border=True):
        d1, d2 = st.columns([1, 3])
        limit = d1.number_input("Draft up to", 1, 30, min(5, len(ready)))
        fallback = f", fallback `{settings.gemini_fallback_model}`" if settings.gemini_fallback_model else ""
        d2.caption(f"Model `{settings.gemini_model}`{fallback}. About 30-60 s per draft (one retry if a check "
                   f"fails). Vacancy-based first, then {settings.india_ratio:.0%} India.")
        picked = st.multiselect("Only these (optional)", [f"{c['full_name']} · {c['company_name']}" for c in ready])
        if st.button("✍️ Draft emails", type="primary"):
            from agents.writer_agent import WriterAgent
            from integrations.gemini_client import GeminiClient, GeminiError

            try:
                client = GeminiClient(settings.gemini_api_key, settings.gemini_model, settings.gemini_min_interval,
                                      fallback_model=settings.gemini_fallback_model or None)
            except GeminiError as exc:
                st.error(str(exc))
                st.stop()
            ids = [c["id"] for c in ready if f"{c['full_name']} · {c['company_name']}" in picked] or None
            bar = st.progress(0.0, text="Starting…")
            with get_connection() as conn:
                summary, outcomes = WriterAgent(conn, settings, client).run(
                    int(limit), ids, lambda i, n, t: bar.progress((i - 0.5) / n, text=f"{i}/{n} {t['company_name']}"))
            bar.progress(1.0, text="Done")
            st.success(f"{summary.created} draft(s) added to the Review queue.")
            for o in outcomes:
                if o.email_id:
                    warn = f" · warnings: {'; '.join(o.check.warnings)}" if o.check and o.check.warnings else ""
                    st.write(f"✅ {o.company_name}: drafted with `{o.model}` in {o.attempts} attempt(s){warn}")
                elif o.error:
                    st.error(f"{o.company_name}: {o.error}")
                else:
                    st.caption(f"{o.company_name}: skipped ({o.skipped})")
