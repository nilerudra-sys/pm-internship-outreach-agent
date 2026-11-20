import streamlit as st

from dashboard.discovery_ui import source_page

st.title("Google Discoveries")
source_page("google", (
    "Targeted Google searches for every product role variation, across all industries, planned "
    "80% India / 20% international.  \n"
    "**How it works:** open a search, then add listings you find with **Add a listing** or "
    "**CSV Import**. No search API is called: Google's Custom Search JSON API is closed to new "
    "customers, and Gemini's Google Search grounding is billed, so neither is used without your OK."
))
