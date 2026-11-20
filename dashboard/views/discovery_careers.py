import sqlite3

import pandas as pd
import streamlit as st

from database import opportunities as opp_repo
from database.db import get_connection
from dashboard.discovery_ui import opportunity_filters, opportunity_table
from integrations.ats import ATS_TYPES, detect_board
from services.discovery.career_pages import RECHECK_HOURS, check_all_boards

st.title("Career Page Discoveries")
st.markdown(
    "Checks company career pages through the **official public job-board APIs** of Greenhouse, Lever "
    "and Ashby (free, no key, no scraping). Only internships with product-related titles or clearly "
    "product-focused descriptions are imported.  \n"
    f"Each board is checked at most every {RECHECK_HOURS} hours unless you force it, with a pause "
    "between requests."
)

tab_boards, tab_found = st.tabs(["🏢 Career boards", "📋 Discovered here"])

with tab_boards:
    with st.expander("➕ Add a company career board", expanded=False):
        with st.form("add_board", clear_on_submit=True):
            url = st.text_input("Careers URL", placeholder="https://jobs.lever.co/company  ·  "
                                "https://boards.greenhouse.io/company  ·  https://jobs.ashbyhq.com/company")
            c1, c2, c3 = st.columns(3)
            company = c1.text_input("Company name*")
            website = c2.text_input("Company website", placeholder="https://company.com")
            market = c3.selectbox("Market", ["india", "international", "unknown"])
            st.caption("Or enter the board directly:")
            d1, d2 = st.columns(2)
            ats = d1.selectbox("ATS", ("auto-detect from URL",) + ATS_TYPES)
            slug = d2.text_input("Board slug", placeholder="company")
            if st.form_submit_button("Add board", type="primary"):
                detected = detect_board(url) if url else None
                if ats != "auto-detect from URL" and slug:
                    detected = (ats, slug.strip())
                if not company.strip():
                    st.error("Company name is required.")
                elif not detected:
                    st.error("Could not recognise a Greenhouse, Lever or Ashby board. Paste the careers URL "
                             "or choose the ATS and slug.")
                else:
                    try:
                        with get_connection() as conn:
                            opp_repo.add_career_board(conn, company_name=company, ats=detected[0],
                                                      slug=detected[1], website=website or None,
                                                      market_hint=market)
                        st.success(f"Added {company} ({detected[0]}/{detected[1]}).")
                    except sqlite3.IntegrityError:
                        st.error("That board is already registered.")

    with get_connection() as conn:
        boards = opp_repo.list_career_boards(conn)

    if not boards:
        st.info("No career boards yet. Many startups host jobs on Greenhouse, Lever or Ashby: open a "
                "company's Careers page, and if the URL is on one of those domains, add it above.")
    else:
        df = pd.DataFrame(boards)[["id", "company_name", "ats", "slug", "market_hint", "enabled",
                                   "last_checked_at", "last_result"]]
        edited = st.data_editor(
            df, hide_index=True, width="stretch", key="boards_editor",
            disabled=["id", "company_name", "ats", "slug", "market_hint", "last_checked_at", "last_result"],
            column_config={"enabled": st.column_config.CheckboxColumn("Enabled")},
        )
        changed = [
            (int(r["id"]), bool(r["enabled"])) for _, r in edited.iterrows()
            if bool(r["enabled"]) != bool(df.loc[df["id"] == r["id"], "enabled"].iloc[0])
        ]
        if changed:
            with get_connection() as conn:
                for board_id, enabled in changed:
                    opp_repo.set_board_enabled(conn, board_id, enabled)
            st.rerun()

        c1, c2, c3 = st.columns([1, 1, 2])
        force = c2.checkbox("Ignore the 6-hour wait", value=False)
        if c1.button("🔄 Check enabled boards", type="primary"):
            with st.spinner("Checking career boards…"), get_connection() as conn:
                summary = check_all_boards(conn, force=force)
            for b in summary.boards:
                if b.error:
                    st.error(f"{b.board}: {b.error}")
                elif b.skipped_recent:
                    st.caption(f"{b.board}: checked recently, skipped")
                else:
                    st.write(f"{b.board}: {b.fetched} jobs · {b.matched} product internships · "
                             f"{b.created} new · {b.merged} already known")
        with c3.popover("🗑️ Remove a board"):
            label = {f"{b['company_name']} ({b['ats']}/{b['slug']})": b["id"] for b in boards}
            choice = st.selectbox("Board", list(label))
            if st.button("Remove", key="remove_board"):
                with get_connection() as conn:
                    opp_repo.delete_career_board(conn, label[choice])
                st.rerun()

with tab_found:
    with get_connection() as conn:
        filters = opportunity_filters(conn, key="careers_found", show_source=False)
        rows = opp_repo.list_opportunities(conn, **{**filters, "sources": ["career_page"]})
        opportunity_table(conn, rows, key="careers_found")
