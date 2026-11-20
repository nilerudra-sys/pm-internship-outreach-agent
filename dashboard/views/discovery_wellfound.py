import streamlit as st

from dashboard.discovery_ui import source_page

st.title("Wellfound Discoveries")
source_page("wellfound", (
    "Wellfound role pages for Indian cities and remote roles, intern listings (searched through "
    "Google, since Wellfound has no intern page), and early-stage companies that may consider an "
    "unsolicited internship application.  \n"
    "**How it works:** Wellfound has no public API and blocks automated access, so the app only "
    "builds links. Add listings or companies you find with **Add a listing** or **CSV Import**."
))
