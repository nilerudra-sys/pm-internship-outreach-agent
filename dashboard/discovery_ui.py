"""Shared Streamlit components for the discovery and opportunity pages."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

from config.settings import get_settings
from database import opportunities as opp_repo
from database.db import get_connection
from services.discovery import queries as q
from services.discovery.analysis import parse_json_field
from services.discovery.leads import ingest_lead
from services.discovery.normalize import REMOTE_STATUSES, SOURCES, Lead, is_valid_url
from services.discovery.roles import CATEGORY_LABELS

SOURCE_LABELS = {
    "google": "Google", "linkedin": "LinkedIn", "wellfound": "Wellfound",
    "career_page": "Career page", "csv": "CSV import", "manual": "Manual",
}
FILTER_CATEGORIES = ("core_pm", "related_product", "adjacent", "not_relevant", "unmatched")
MARKET_LABELS = {"india": "🇮🇳 India", "international": "🌍 International", "unknown": "❔ Unknown"}


# --------------------------------------------------------------------------- formatting


def compensation_label(opp: dict) -> str:
    stipend = opp.get("stipend")
    if opp.get("compensation") == "unpaid":
        return "Unpaid"
    if opp.get("compensation") == "paid":
        return f"Paid · {stipend}" if stipend else "Paid (amount not stated)"
    return stipend or "Not stated"


def category_label(opp: dict) -> str:
    if opp.get("role_category") and opp["role_category"] != "unclassified":
        return CATEGORY_LABELS[opp["role_category"]]
    guess = opp.get("title_category", "unmatched")
    return f"{CATEGORY_LABELS[guess]} (title only)" if guess != "unmatched" else "Not analysed"


def opportunities_frame(rows: list[dict], sources: dict[int, list[dict]] | None = None) -> pd.DataFrame:
    sources = sources or {}
    records = []
    for o in rows:
        seen = sorted({s["source"] for s in sources.get(o["id"], [])} | {o["primary_source"]})
        records.append({
            "id": o["id"],
            "score": o["relevance_score"],
            "category": category_label(o),
            "title": o["job_title"],
            "company": o["company_name"],
            "location": o["location"] or "",
            "market": MARKET_LABELS.get(o["market"], o["market"]),
            "remote": o["remote_status"],
            "India OK": o["india_eligible"],
            "compensation": compensation_label(o),
            "sources": ", ".join(SOURCE_LABELS.get(s, s) for s in seen),
            "discovered": o["discovered_at"],
            "status": o["status"],
            "link": o["job_url"],
        })
    return pd.DataFrame(records)


COLUMN_CONFIG = {
    "id": st.column_config.NumberColumn("#", width="small"),
    "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
    "link": st.column_config.LinkColumn("Job link", display_text="Open ↗"),
}


# --------------------------------------------------------------------------- filters + table


def opportunity_filters(conn: sqlite3.Connection, key: str, *, show_source: bool = True) -> dict:
    with st.expander("Filters", expanded=False):
        c1, c2, c3 = st.columns(3)
        sources = c1.multiselect("Source", SOURCES, format_func=SOURCE_LABELS.get, key=f"{key}_src") \
            if show_source else None
        countries = c2.multiselect("Country", opp_repo.distinct_countries(conn), key=f"{key}_country")
        remote = c3.multiselect("Remote status", REMOTE_STATUSES, key=f"{key}_remote")
        d1, d2, d3 = st.columns(3)
        categories = d1.multiselect("Job category", FILTER_CATEGORIES,
                                    format_func=CATEGORY_LABELS.get, key=f"{key}_cat")
        dates = d2.date_input("Discovered between", value=(), key=f"{key}_dates", format="YYYY-MM-DD")
        min_score = d3.slider("Minimum relevance score", 0, 100, 0, step=5, key=f"{key}_score")
        include_unscored = d3.checkbox("Include not-yet-scored", value=True, key=f"{key}_unscored")
    date_from = date_to = None
    if isinstance(dates, (list, tuple)) and dates:
        date_from = dates[0].isoformat()
        date_to = (dates[1] if len(dates) > 1 else dates[0]).isoformat()
    return {
        "sources": sources or None,
        "countries": countries or None,
        "remote_statuses": remote or None,
        "categories": categories or None,
        "date_from": date_from,
        "date_to": date_to,
        "min_score": min_score or None,
        "include_unscored": include_unscored,
    }


def opportunity_table(conn: sqlite3.Connection, rows: list[dict], key: str, *, actions: bool = True) -> None:
    """Table with row selection, bulk status actions, and a detail panel."""
    if not rows:
        st.info("No opportunities match.")
        return
    srcs = opp_repo.sources_for(conn, [r["id"] for r in rows])
    df = opportunities_frame(rows, srcs)
    event = st.dataframe(
        df, width="stretch", hide_index=True, column_config=COLUMN_CONFIG,
        on_select="rerun", selection_mode="multi-row", key=f"{key}_table",
    )
    selected = [int(df.iloc[i]["id"]) for i in event.selection.rows] if event and event.selection else []
    st.caption(f"{len(rows)} opportunities · select rows for actions and details")

    if actions and selected:
        a1, a2, a3 = st.columns(3)
        if a1.button(f"✅ Mark qualified ({len(selected)})", key=f"{key}_q"):
            _bulk_status(selected, "qualified", "marked qualified manually")
        if a2.button(f"🚫 Reject ({len(selected)})", key=f"{key}_r"):
            _bulk_status(selected, "rejected", "rejected manually")
        if a3.button(f"↺ Reset to automatic ({len(selected)})", key=f"{key}_n"):
            _bulk_status(selected, "new", "", lock=False)

    if selected:
        by_id = {r["id"]: r for r in rows}
        for opp_id in selected[:5]:
            opportunity_detail(by_id[opp_id], srcs.get(opp_id, []))
        if len(selected) > 5:
            st.caption("Details shown for the first 5 selected rows.")


def _bulk_status(ids: list[int], status: str, reason: str, lock: bool = True) -> None:
    with get_connection() as conn:
        for i in ids:
            opp_repo.set_status(conn, i, status, reason, lock=lock)
    st.rerun()


def opportunity_detail(opp: dict, sources: list[dict]) -> None:
    with st.container(border=True):
        st.markdown(f"**{opp['job_title']}** · {opp['company_name']}  \n"
                    f"{category_label(opp)} · score **{opp['relevance_score'] if opp['relevance_score'] is not None else '—'}** "
                    f"· status **{opp['status']}**" + (" (manual)" if opp["status_locked"] else ""))
        if opp.get("status_reason"):
            st.caption(f"Reason: {opp['status_reason']}")
        if opp.get("analysis_error"):
            st.warning(f"Analysis: {opp['analysis_error']}")
        cols = st.columns(4)
        cols[0].markdown(f"**Location**  \n{opp['location'] or 'Not stated'}")
        cols[1].markdown(f"**Remote**  \n{opp['remote_status']} · India OK: {opp['india_eligible']}")
        cols[2].markdown(f"**Compensation**  \n{compensation_label(opp)}")
        cols[3].markdown(f"**Duration / experience**  \n{opp['duration'] or '—'} / {opp['experience_required'] or '—'}")
        if opp.get("industry") or opp.get("company_product"):
            st.markdown(f"**Industry:** {opp.get('industry') or '—'} · **Product:** {opp.get('company_product') or '—'}")
        if opp.get("ai_reasoning"):
            st.markdown(f"**Gemini:** {opp['ai_reasoning']}")
        resp = parse_json_field(opp.get("product_responsibilities"))
        if resp:
            st.markdown("**Product responsibilities in the listing:**\n" + "\n".join(f"- {r}" for r in resp))
        breakdown = parse_json_field(opp.get("score_breakdown"))
        if breakdown:
            st.caption("Sub-scores (0-10): " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in breakdown.items()))
        st.markdown("**Seen on:**")
        for s in sources:
            label = SOURCE_LABELS.get(s["source"], s["source"])
            st.markdown(f"- {label} · {s['discovered_at']} · " + (f"[{s['url']}]({s['url']})" if s["url"] else "no URL"))
        if opp.get("description"):
            with st.expander("Full description"):
                st.text(opp["description"])


# --------------------------------------------------------------------------- query links


def query_links(platform: str, key: str) -> None:
    settings = get_settings()
    c1, c2, c3, c4 = st.columns(4)
    recency = c1.selectbox("Recency", list(q.GOOGLE_RECENCY), index=2, key=f"{key}_recency")
    market = c2.selectbox("Market", ["80/20 plan", "India only", "International only"], key=f"{key}_market")
    groups = c3.multiselect("Role group", ["core_pm", "related_product", "adjacent", "industry", "company"],
                            default=[], format_func=lambda g: CATEGORY_LABELS.get(g, g.title()), key=f"{key}_grp")
    limit = c4.number_input("How many", 5, 300, 30, step=5, key=f"{key}_limit")

    queries = q.all_queries(platform, recency)
    if groups:
        queries = [x for x in queries if x.category in groups]
    if market == "India only":
        planned = [x for x in queries if x.market == "india"][: int(limit)]
    elif market == "International only":
        planned = [x for x in queries if x.market == "international"][: int(limit)]
    else:
        planned = q.plan_queries(queries, int(limit), settings.india_ratio)

    india = sum(1 for x in planned if x.market == "india")
    st.caption(f"{len(queries)} queries available · showing {len(planned)} "
               f"({india} India / {len(planned) - india} international)")
    df = pd.DataFrame([{
        "market": MARKET_LABELS[x.market], "role": x.role or "—",
        "group": CATEGORY_LABELS.get(x.category, x.category.title()), "intent": x.intent,
        "query": x.text, "open": x.url,
    } for x in planned])
    st.dataframe(df, width="stretch", hide_index=True,
                 column_config={"open": st.column_config.LinkColumn("Search", display_text="Open ↗")})

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["platform", "market", "role", "group", "intent", "query", "url"])
    for x in planned:
        writer.writerow([x.platform, x.market, x.role or "", x.category, x.intent, x.text, x.url])
    st.download_button("⬇️ Download these queries (CSV)", buf.getvalue(), f"{platform}_queries.csv",
                       "text/csv", key=f"{key}_dl")


# --------------------------------------------------------------------------- manual add


def quick_add_form(source: str, key: str) -> None:
    """Add one listing you found on a site. Paste the description so Gemini can analyse it."""
    with st.form(f"{key}_add", clear_on_submit=True):
        a1, a2 = st.columns(2)
        company = a1.text_input("Company*")
        title = a2.text_input("Job title*")
        b1, b2 = st.columns(2)
        job_url = b1.text_input("Job URL*", placeholder="https://...")
        website = b2.text_input("Company website", placeholder="https://company.com")
        c1, c2, c3 = st.columns(3)
        location = c1.text_input("Location", placeholder="Bengaluru, India / Remote")
        remote = c2.selectbox("Remote status", REMOTE_STATUSES, index=REMOTE_STATUSES.index("unknown"))
        stipend = c3.text_input("Stipend", placeholder="₹15,000/month, Unpaid, ...")
        d1, d2 = st.columns(2)
        duration = d1.text_input("Duration", placeholder="3 months")
        discovered = d2.date_input("Discovery date", value=date.today(), max_value=date.today())
        description = st.text_area("Job description (paste from the listing)", height=180)
        submitted = st.form_submit_button("Add listing", type="primary")

    if not submitted:
        return
    errors = []
    if not company.strip() or not title.strip():
        errors.append("Company and job title are required.")
    if not is_valid_url(job_url):
        errors.append("Job URL must be a full http(s) link.")
    if website and not is_valid_url(website if "://" in website else "https://" + website):
        errors.append("Company website is not a valid URL.")
    if errors:
        for e in errors:
            st.error(e)
        return
    lead = Lead(
        company_name=company, job_title=title, source=source, job_url=job_url.strip(),
        website=(website if "://" in website else f"https://{website}") if website else None,
        description=description or None, location=location or None,
        remote_status=remote if remote != "unknown" else None,
        stipend=stipend or None, duration=duration or None, discovered_at=discovered.isoformat(),
    )
    with get_connection() as conn:
        result = ingest_lead(conn, lead)
    if result.created:
        st.success(f"Added as opportunity #{result.opportunity_id}.")
    else:
        extra = f" Filled: {', '.join(result.filled_fields)}." if result.filled_fields else ""
        st.info(f"Duplicate of opportunity #{result.opportunity_id}; source recorded.{extra}")


# --------------------------------------------------------------------------- analysis


def analysis_panel(key: str) -> None:
    from integrations.gemini_client import GeminiClient, GeminiError
    from services.discovery.analysis import analyse_pending

    settings = get_settings()
    with get_connection() as conn:
        stats = opp_repo.discovery_stats(conn)
        ready = len(opp_repo.pending_analysis(conn, limit=10000))
    no_desc = stats["unanalysed"] - ready
    st.write(f"**{ready}** listings ready for Gemini analysis"
             + (f" · {no_desc} need a description first" if no_desc > 0 else ""))
    c1, c2 = st.columns([1, 3])
    limit = c1.number_input("Analyse up to", 1, 100, min(10, max(ready, 1)), key=f"{key}_n")
    fallback = f", fallback `{settings.gemini_fallback_model}`" if settings.gemini_fallback_model else ""
    c2.caption(f"Model `{settings.gemini_model}`{fallback}, one call every {settings.gemini_min_interval:g}s. "
               "Gemini reads only the listing text you imported.")
    if st.button("🤖 Analyse with Gemini", disabled=ready == 0, key=f"{key}_go", type="primary"):
        try:
            client = GeminiClient(settings.gemini_api_key, settings.gemini_model, settings.gemini_min_interval,
                                  fallback_model=settings.gemini_fallback_model or None)
        except GeminiError as exc:
            st.error(str(exc))
            return
        bar = st.progress(0.0, text="Starting…")

        def progress(i: int, n: int, opp: dict) -> None:
            bar.progress(i / n, text=f"{i}/{n}: {opp['job_title']} @ {opp['company_name']}")

        with get_connection() as conn:
            summary = analyse_pending(conn, client, int(limit), settings.qualify_threshold, progress)
        models = ", ".join(f"{m}: {n}" for m, n in summary.get("by_model", {}).items())
        st.success(f"Analysed {summary['analysed']} · qualified {summary.get('qualified', 0)} · "
                   f"rejected {summary.get('rejected', 0)} · errors {summary['errors']}"
                   + (f" · models used ({models})" if models else ""))


def source_page(source: str, intro: str) -> None:
    """Common layout for the Google / LinkedIn / Wellfound pages."""
    st.markdown(intro)
    tab_links, tab_add, tab_found = st.tabs(["🔎 Search links", "➕ Add a listing", "📋 Discovered here"])
    with tab_links:
        query_links(source, key=source)
    with tab_add:
        quick_add_form(source, key=source)
        st.caption("Found many? Put them in a spreadsheet and use **CSV Import**.")
    with tab_found:
        with get_connection() as conn:
            filters = opportunity_filters(conn, key=f"{source}_found", show_source=False)
            rows = opp_repo.list_opportunities(conn, **{**filters, "sources": [source]})
            opportunity_table(conn, rows, key=f"{source}_found")
