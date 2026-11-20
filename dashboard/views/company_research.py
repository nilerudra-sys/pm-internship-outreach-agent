"""Phase 3: research pipeline companies from their own websites and discovered listings."""

import pandas as pd
import streamlit as st

from config.settings import get_settings
from database import research as research_repo
from database.db import get_connection
from dashboard.discovery_ui import MARKET_LABELS

settings = get_settings()

st.title("Company Research")
st.markdown(
    "Researches companies in your outreach pipeline from **their own public website** (up to 5 pages: home, "
    "about, product, careers, pricing; `robots.txt` respected, 2 s between requests, pages cached 7 days) and "
    "any **discovered listings** linked to them. Gemini must cite a source for every fact; facts it cannot "
    "support are dropped."
)

with get_connection() as conn:
    overview = research_repo.research_overview(conn)
    todo = research_repo.companies_needing_research(conn)

if not overview:
    st.info("No companies in the pipeline yet. Add them from **Qualified Companies** or **Companies & contacts**.")
    st.stop()

done = sum(1 for c in overview if c["researched_at"] and not c["research_error"])
c1, c2, c3 = st.columns(3)
c1.metric("Pipeline companies", len(overview))
c2.metric("Researched", done)
c3.metric("Waiting", len(todo))

# ------------------------------------------------------------------ run
with st.container(border=True):
    r1, r2, r3 = st.columns([1, 1, 2])
    limit = r1.number_input("Research up to", 1, 20, min(3, max(len(todo), 1)))
    force = r2.checkbox("Refresh already researched", help="Re-fetch pages and re-run Gemini")
    fallback = f", fallback `{settings.gemini_fallback_model}`" if settings.gemini_fallback_model else ""
    r3.caption(f"Model `{settings.gemini_model}`{fallback}. About 30-60 s per company. "
               f"Order keeps {settings.india_ratio:.0%} India first.")
    names = {f"{c['name']} ({c['domain']})": c["id"] for c in overview}
    picked = st.multiselect("Only these companies (optional)", list(names))
    if st.button("🔬 Research companies", type="primary", disabled=not todo and not force and not picked):
        from agents.research_agent import ResearchAgent
        from integrations.gemini_client import GeminiClient, GeminiError

        try:
            client = GeminiClient(settings.gemini_api_key, settings.gemini_model, settings.gemini_min_interval,
                                  fallback_model=settings.gemini_fallback_model or None)
        except GeminiError as exc:
            st.error(str(exc))
            st.stop()
        bar = st.progress(0.0, text="Starting…")

        def progress(i, n, company, stage):
            bar.progress((i - 0.5) / n, text=f"{i}/{n} {company['name']}: {stage}")

        with get_connection() as conn:
            agent = ResearchAgent(conn, settings, client)
            result = agent.run(int(limit), [names[p] for p in picked] or None, force or bool(picked), progress)
        bar.progress(1.0, text="Done")
        st.success(f"Researched {result.created} of {result.processed} companies.")
        for err in result.errors:
            st.warning(err)

# ------------------------------------------------------------------ overview table
df = pd.DataFrame([{
    "id": c["id"], "company": c["name"], "domain": c["domain"], "market": MARKET_LABELS.get(c["market"], c["market"]),
    "status": c["status"], "fit": c["fit_score"], "pages": c["pages"],
    "researched": (c["researched_at"] or "")[:10],
    "note": c["research_error"] or ("thin sources" if c["insufficient"] else ""),
} for c in overview])
event = st.dataframe(
    df, width="stretch", hide_index=True, on_select="rerun", selection_mode="single-row", key="research_table",
    column_config={"fit": st.column_config.ProgressColumn("Fit", min_value=0, max_value=100, format="%d")},
)

# ------------------------------------------------------------------ detail
rows = event.selection.rows if event and event.selection else []
if not rows:
    st.caption("Select a company to see its research.")
    st.stop()

company_id = int(df.iloc[rows[0]]["id"])
with get_connection() as conn:
    research = research_repo.get_research(conn, company_id)
    pages = research_repo.all_pages(conn, company_id)
company = next(c for c in overview if c["id"] == company_id)

st.subheader(company["name"])
if not research or not research.get("researched_at"):
    if research and research.get("error"):
        st.error(f"Last attempt failed: {research['error']}")
    else:
        st.info("Not researched yet.")
else:
    st.markdown(f"**Fit {research['fit_score']}** · researched {research['researched_at'][:16]} with "
                f"`{research['model']}`" + (" · ⚠️ sources were thin" if research["insufficient"] else ""))

    # ---- verified: safe to use in outreach
    with st.container(border=True):
        st.markdown("#### ✅ Verified facts")
        st.caption("Each fact was checked sentence by sentence against the page it cites. Only these may be "
                   "used in emails. Quotes are shown so you can check them yourself.")
        if research.get("verified_summary"):
            st.write(research["verified_summary"])
        facts = research.get("facts") or {}
        labels = {"outreach_hooks": "🎯 Outreach hooks", "product_facts": "📦 Product",
                  "hiring_signals": "🧑‍💼 Hiring", "remote_signals": "🌍 Remote / location", "stage_signals": "📈 Stage"}
        any_fact = False
        for key, label in labels.items():
            items = facts.get(key) or []
            if items:
                any_fact = True
                st.markdown(f"**{label}**")
                for f in items:
                    cited = f.get("sources") or [{"source_id": f.get("source_id"), "url": f.get("url"),
                                                  "quote": f.get("quote")}]
                    links = ", ".join(f"[{c['source_id']}]({c['url']})" if c.get("url") else c["source_id"]
                                      for c in cited)
                    st.markdown(f"- {f['text']} · {links}")
                    for c in cited:
                        if c.get("quote"):
                            st.caption(f"    “{c['quote']}”" + (f" — fetched {c['fetched_at'][:10]}"
                                                                if c.get("fetched_at") else ""))
        if not any_fact:
            st.info("No facts could be verified. Do not personalise an email from this research.")
        if research.get("dropped_facts"):
            st.caption(f"{research['dropped_facts']} fact(s) dropped: citation missing, or the cited sentence "
                       "did not support the wording, numbers or negation.")

    # ---- interpretation: context only
    with st.container(border=True):
        st.markdown("#### 🤖 Gemini interpretation (not verified)")
        st.caption("Gemini's reading of the sources. Use as context only; do not state these in an email as fact.")
        st.write(research.get("summary") or "—")
        cols = st.columns(4)
        for col, (label, key) in zip(cols, [("Product", "product"), ("Customers", "target_customers"),
                                            ("Business model", "business_model"), ("Industry", "industry")]):
            col.markdown(f"**{label}**  \n{research.get(key) or '—'}")
        if research.get("candidate_angle"):
            st.markdown(f"**Your angle:** {research['candidate_angle']}")
        breakdown = research.get("score_breakdown") or {}
        st.caption("Sub-scores (0-10): " + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in breakdown.items()
                                                     if k not in ("caps", "notes"))
                   + (f" · adjusted: {'; '.join(breakdown.get('notes') or breakdown.get('caps'))}"
                      if breakdown.get("notes") or breakdown.get("caps") else ""))
        if research.get("reasoning"):
            st.caption(f"Reasoning: {research['reasoning']}")

    unreadable = research.get("unreadable_pages") or []
    if unreadable:
        st.warning("Pages that could not be read (treated as unknown):  \n" + "  \n".join(
            f"- {u.get('page_type') or 'page'}: {u['url']} ({u['reason']})" for u in unreadable))

with st.expander(f"Pages fetched ({len(pages)})"):
    for p in pages:
        state = p["error"] or f"HTTP {p['http_status']}, {len(p['text'] or '')} chars"
        st.markdown(f"- `{p['page_type']}` [{p['url']}]({p['url']}) · {state} · {p['fetched_at'][:16]}")
