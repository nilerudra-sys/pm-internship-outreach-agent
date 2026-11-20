import pandas as pd
import streamlit as st

from config.settings import get_settings
from database import repository
from database.db import get_connection
from dashboard.components import MARKET_LABELS
from services import quota

settings = get_settings()

with get_connection() as conn:
    stats = repository.pipeline_stats(conn)
    sent = quota.sent_today(conn, settings.timezone)
    mix = quota.market_mix(conn)
    activity = repository.recent_activity(conn, limit=15)

st.title("Product Internship Outreach")
st.caption("Discover → research → find contacts → draft → **you approve** → send")

emails = stats["email_status"]
c1, c2, c3, c4 = st.columns(4)
c1.metric("Companies", stats["companies"])
c2.metric("Contacts", stats["contacts"])
c3.metric("Awaiting your review", emails.get("pending_review", 0))
c4.metric("Sent today", f"{sent} / {settings.daily_email_limit}")

if stats["companies"] == 0:
    st.info(
        "No data yet. Add a company on **Companies & contacts**, or load demo rows with "
        "`python -m database.seed`."
    )

left, right = st.columns(2)

with left:
    st.subheader("Market mix")
    total = mix["india"] + mix["international"]
    target = settings.india_ratio
    india_share = mix["india"] / total if total else 0.0
    st.write(
        f"{MARKET_LABELS['india']}: **{mix['india']}** · "
        f"{MARKET_LABELS['international']}: **{mix['international']}** "
        f"(target {target:.0%} / {1 - target:.0%})"
    )
    st.progress(india_share, text=f"India share of approved/sent: {india_share:.0%}")
    nxt = quota.next_market(mix["india"], mix["international"], target)
    st.caption(f"Next email should target: {MARKET_LABELS[nxt]}")

with right:
    st.subheader("Email pipeline")
    if emails:
        df = pd.DataFrame({"status": list(emails), "count": list(emails.values())}).set_index("status")
        st.bar_chart(df, horizontal=True)
    else:
        st.caption("No emails drafted yet.")

st.subheader("Companies by stage")
if stats["company_status"]:
    st.bar_chart(pd.Series(stats["company_status"], name="companies"))
else:
    st.caption("No companies yet.")

st.subheader("Recent activity")
if activity:
    st.dataframe(
        pd.DataFrame(activity)[["created_at", "entity_type", "action", "details"]],
        width="stretch",
        hide_index=True,
    )
else:
    st.caption("Nothing yet.")
