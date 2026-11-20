import streamlit as st

from database import opportunities as opp_repo
from database.db import get_connection
from dashboard.discovery_ui import opportunity_filters, opportunity_table

st.title("Rejected Opportunities")
st.caption("Rejected by Gemini scoring or by you. Select rows to see the reason, mark them qualified, "
           "or reset them so the next analysis decides again.")

with get_connection() as conn:
    filters = opportunity_filters(conn, key="rejected")
    rows = opp_repo.list_opportunities(conn, **filters, statuses=["rejected"])
    opportunity_table(conn, rows, key="rejected")
