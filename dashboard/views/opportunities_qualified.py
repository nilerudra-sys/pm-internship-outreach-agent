"""Qualified opportunities grouped by company, ordered to keep the 80/20 India/international mix."""

import sqlite3

import streamlit as st

from config.settings import get_settings
from database import opportunities as opp_repo
from database import repository
from database.db import get_connection
from dashboard.discovery_ui import MARKET_LABELS, SOURCE_LABELS, compensation_label, opportunity_filters
from services.discovery.normalize import sector_for_industry as sector_for
from services.discovery.roles import CATEGORY_LABELS
from services.discovery.scoring import mix_by_market

settings = get_settings()
st.title("Qualified Companies")
st.caption(f"Companies with at least one opportunity scoring ≥ {settings.qualify_threshold}, ordered "
           f"{settings.india_ratio:.0%} India / {1 - settings.india_ratio:.0%} international. "
           "Add a company to the outreach pipeline to find contacts and draft emails later.")


with get_connection() as conn:
    filters = opportunity_filters(conn, key="qualified")
    rows = opp_repo.list_opportunities(conn, **filters, statuses=["qualified"])
    sources = opp_repo.sources_for(conn, [r["id"] for r in rows])

companies: dict[str, dict] = {}
for r in rows:
    key = r["company_domain"] or r["company_name_norm"]
    c = companies.setdefault(key, {"name": r["company_name"], "domain": r["company_domain"],
                                   "name_norm": r["company_name_norm"], "market": r["market"],
                                   "company_id": r["company_id"], "industry": r["industry"],
                                   "best": r["relevance_score"] or 0, "roles": []})
    c["roles"].append(r)
    c["best"] = max(c["best"], r["relevance_score"] or 0)
    c["industry"] = c["industry"] or r["industry"]
    c["company_id"] = c["company_id"] or r["company_id"]
    if c["market"] == "unknown":
        c["market"] = r["market"]

ordered = mix_by_market(sorted(companies.values(), key=lambda c: -c["best"]), settings.india_ratio)
india = sum(1 for c in ordered if c["market"] == "india")
st.write(f"**{len(ordered)}** companies · {india} India · {len(ordered) - india} international/unknown")

if not ordered:
    st.info("No qualified opportunities yet. Import listings, then analyse them on **All Opportunities**.")

for c in ordered:
    with st.container(border=True):
        head, action = st.columns([4, 1])
        head.markdown(f"### {c['name']}\n{MARKET_LABELS.get(c['market'], c['market'])} · best score "
                      f"**{c['best']}** · {c['industry'] or 'industry not stated'}"
                      + (f" · `{c['domain']}`" if c["domain"] else ""))
        for r in sorted(c["roles"], key=lambda x: -(x["relevance_score"] or 0)):
            seen = ", ".join(sorted({SOURCE_LABELS.get(s["source"], s["source"]) for s in sources.get(r["id"], [])}))
            link = f" · [Open ↗]({r['job_url']})" if r["job_url"] else ""
            st.markdown(f"- **{r['job_title']}** ({CATEGORY_LABELS.get(r['role_category'], r['role_category'])}, "
                        f"{r['relevance_score']}) · {r['location'] or 'location not stated'} · {r['remote_status']} · "
                        f"{compensation_label(r)} · via {seen}{link}")

        if c["company_id"]:
            action.success("In pipeline")
            continue
        with action.popover("➕ Add to pipeline"):
            with st.form(f"promote_{c['name_norm']}"):
                domain = st.text_input("Company domain*", value=c["domain"] or "", placeholder="company.com")
                market = st.selectbox("Market", ["india", "international"],
                                      index=0 if c["market"] != "international" else 1)
                if st.form_submit_button("Add", type="primary"):
                    if not domain.strip():
                        st.error("Domain is required (the job board's domain is not the company's).")
                    else:
                        best = c["roles"][0]
                        remote = {"remote": "remote_india_ok" if best["india_eligible"] == "yes" else "remote",
                                  "hybrid": "hybrid", "onsite": "onsite"}.get(best["remote_status"], "unknown")
                        try:
                            with get_connection() as conn:
                                existing = repository.find_company_by_domain(conn, domain)
                                company_id = existing["id"] if existing else repository.add_company(
                                    conn, name=c["name"], domain=domain, market=market,
                                    sector=sector_for(c["industry"]), remote_policy=remote, source="discovery",
                                    description=f"Industry: {c['industry'] or 'not stated'}. "
                                                f"Roles: {', '.join(r['job_title'] for r in c['roles'])}",
                                    hiring_signal=best["job_url"],
                                )
                                opp_repo.link_company(conn, c["name_norm"], company_id)
                            st.rerun()
                        except (ValueError, sqlite3.IntegrityError) as exc:
                            st.error(str(exc))
