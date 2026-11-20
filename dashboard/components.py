"""Reusable UI pieces for the dashboard views."""

from __future__ import annotations

import streamlit as st

from config.settings import get_settings
from database.db import get_connection
from services import quota

STATUS_LABELS = {
    "pending_review": "🟡 Pending review",
    "approved": "🟢 Approved",
    "rejected": "⚪ Rejected",
    "sent": "📨 Sent",
    "failed": "🔴 Failed",
    "replied": "💬 Replied",
}

MARKET_LABELS = {"india": "🇮🇳 India", "international": "🌍 International"}


def sidebar_status() -> None:
    """Daily quota and integration health, visible on every page."""
    settings = get_settings()
    with get_connection() as conn:
        sent = quota.sent_today(conn, settings.timezone)
    limit = settings.daily_email_limit

    with st.sidebar:
        st.markdown("### Today")
        st.metric("Emails sent", f"{sent} / {limit}")
        st.progress(min(sent / limit, 1.0) if limit else 0.0)
        st.caption(f"Mode: hybrid approval · TZ {settings.timezone}")

        st.markdown("### Integrations")
        for name, ok in settings.integration_status().items():
            st.markdown(f"{'✅' if ok else '⚠️'} {name}")
