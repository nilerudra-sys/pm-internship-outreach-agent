import hashlib

import pandas as pd
import streamlit as st

from database.db import get_connection
from services.discovery.csv_import import COLUMNS, OPTIONAL_COLUMNS, import_preview, preview_csv, template_csv

st.title("CSV Import")
st.markdown(
    "Import listings collected from Google, LinkedIn, Wellfound or anywhere else. Rows are validated, "
    "duplicates are detected (by job URL, company domain + title, or company name + title), and "
    "duplicates of existing opportunities are **merged**, which adds their source URL rather than "
    "creating a copy."
)

with st.expander("Expected columns"):
    st.markdown(
        "Required: `company_name`, `job_title`, and `job_url` or `website`.  \n"
        f"Supported: {', '.join(f'`{c}`' for c in COLUMNS + OPTIONAL_COLUMNS)}.  \n"
        "`remote_status`: remote / hybrid / onsite / unknown · `platform_source`: google / linkedin / "
        "wellfound / career_page / csv · `discovery_date`: YYYY-MM-DD (blank = today).  \n"
        "Common header names such as *Company*, *Title*, *URL*, *Source* are recognised automatically."
    )
    st.download_button("⬇️ Download template", template_csv(), "internship_import_template.csv", "text/csv")

upload = st.file_uploader("Upload CSV", type=["csv"])
if upload is None:
    st.stop()

data = upload.getvalue()
digest = hashlib.sha256(data).hexdigest()
if st.session_state.get("csv_digest") != digest:
    st.session_state.pop("csv_done", None)
    try:
        with get_connection() as conn:
            st.session_state["csv_preview"] = preview_csv(conn, data)
        st.session_state["csv_digest"] = digest
    except ValueError as exc:
        st.error(str(exc))
        st.stop()

preview = st.session_state["csv_preview"]

if preview.missing_columns:
    st.error(f"Missing required columns: {', '.join(preview.missing_columns)}")
if preview.unknown_columns:
    st.warning(f"Ignored columns: {', '.join(preview.unknown_columns)}")

c1, c2, c3 = st.columns(3)
c1.metric("New", preview.count("new"))
c2.metric("Duplicates", preview.count("duplicate"))
c3.metric("Invalid", preview.count("invalid"))

table = pd.DataFrame([{
    "row": r.row_number,
    "status": r.status,
    "company": r.raw.get("company_name", ""),
    "title": r.raw.get("job_title", ""),
    "source": r.lead.source if r.lead else r.raw.get("platform_source", ""),
    "market": r.lead.market if r.lead else "",
    "details": "; ".join(r.errors) if r.errors else (f"duplicate of {r.duplicate_of}" if r.duplicate_of else ""),
} for r in preview.rows])

tabs = st.tabs(["All rows", "Invalid", "Duplicates"])
for tab, status in zip(tabs, (None, "invalid", "duplicate")):
    with tab:
        view = table if status is None else table[table["status"] == status]
        if view.empty:
            st.caption("None.")
        else:
            st.dataframe(view, width="stretch", hide_index=True)

if st.session_state.get("csv_done"):
    st.success(st.session_state["csv_done"])
elif st.button("Import valid rows", type="primary",
               disabled=preview.count("new") + preview.count("duplicate") == 0 or bool(preview.missing_columns)):
    with get_connection() as conn:
        summary = import_preview(conn, preview)
    st.session_state["csv_done"] = (
        f"Imported {summary.created} new · merged {summary.merged} into existing opportunities · "
        f"skipped {summary.skipped_invalid} invalid and {summary.skipped_in_file_duplicates} repeated rows."
    )
    st.rerun()
