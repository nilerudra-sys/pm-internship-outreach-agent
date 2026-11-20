import streamlit as st

from dashboard.discovery_ui import source_page

st.title("LinkedIn Discoveries")
source_page("linkedin", (
    "LinkedIn Jobs searches filtered to the **Internship** level, for India, remote-in-India and "
    "worldwide remote roles.  \n"
    "**How it works:** the links open LinkedIn in your own browser, where you are signed in. "
    "This app never logs in to, fetches from or automates LinkedIn. Copy the public job URL, title, "
    "location and description of listings you like into **Add a listing** or a CSV."
))
