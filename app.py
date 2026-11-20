"""Streamlit entry point.  Run with:  streamlit run app.py"""

import streamlit as st

from database.db import init_db
from dashboard.components import sidebar_status

st.set_page_config(page_title="PM Internship Outreach", page_icon="🎯", layout="wide")


@st.cache_resource
def _bootstrap() -> str:
    return str(init_db())


_bootstrap()

pages = {
    "Outreach": [
        st.Page("dashboard/views/overview.py", title="Overview", icon="📊", default=True),
        st.Page("dashboard/views/email_drafts.py", title="Draft Emails", icon="✍️"),
        st.Page("dashboard/views/review_queue.py", title="Review queue", icon="✅"),
        st.Page("dashboard/views/companies.py", title="Companies & contacts", icon="🏢"),
        st.Page("dashboard/views/company_research.py", title="Company Research", icon="🔬"),
        st.Page("dashboard/views/contact_discovery.py", title="Contact Discovery", icon="👥"),
    ],
    "Discovery": [
        st.Page("dashboard/views/discovery_google.py", title="Google Discoveries", icon="🔎"),
        st.Page("dashboard/views/discovery_linkedin.py", title="LinkedIn Discoveries", icon="💼"),
        st.Page("dashboard/views/discovery_wellfound.py", title="Wellfound Discoveries", icon="🚀"),
        st.Page("dashboard/views/discovery_careers.py", title="Career Pages", icon="🏢"),
        st.Page("dashboard/views/csv_import.py", title="CSV Import", icon="📥"),
    ],
    "Opportunities": [
        st.Page("dashboard/views/opportunities_all.py", title="All Opportunities", icon="📋"),
        st.Page("dashboard/views/opportunities_qualified.py", title="Qualified Companies", icon="⭐"),
        st.Page("dashboard/views/opportunities_rejected.py", title="Rejected Opportunities", icon="🗂️"),
    ],
    "System": [
        st.Page("dashboard/views/settings.py", title="Settings", icon="⚙️"),
    ],
}

nav = st.navigation(pages)
sidebar_status()
nav.run()
