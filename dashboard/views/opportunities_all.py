import streamlit as st

from database import opportunities as opp_repo
from database.db import get_connection
from dashboard.discovery_ui import SOURCE_LABELS, analysis_panel, opportunity_filters, opportunity_table

st.title("All Opportunities")

with get_connection() as conn:
    stats = opp_repo.discovery_stats(conn)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Total", stats["total"])
c2.metric("Qualified", stats["status"].get("qualified", 0))
c3.metric("Rejected", stats["status"].get("rejected", 0))
c4.metric("Not analysed", stats["unanalysed"])
india, intl = stats["market"].get("india", 0), stats["market"].get("international", 0)
c5.metric("India share", f"{india / (india + intl):.0%}" if india + intl else "—", help="Target 80%")
if stats["source"]:
    st.caption("By first source: " + " · ".join(f"{SOURCE_LABELS.get(k, k)} {v}" for k, v in stats["source"].items()))

with st.container(border=True):
    analysis_panel("all")

with get_connection() as conn:
    filters = opportunity_filters(conn, key="all")
    status = st.segmented_control("Status", ["new", "qualified", "rejected"], selection_mode="multi",
                                  default=["new", "qualified"], key="all_status")
    rows = opp_repo.list_opportunities(conn, **filters, statuses=status or None)
    opportunity_table(conn, rows, key="all")
