"""Phase 4: find founders, product leaders, hiring managers and recruiters at pipeline companies."""

import hashlib
import sqlite3

import pandas as pd
import streamlit as st

from config.settings import get_settings
from database import contacts as contacts_repo
from database.db import get_connection
from database.models import ROLE_CATEGORIES, VERIFICATION_STATUSES
from dashboard.discovery_ui import MARKET_LABELS
from services.contacts.csv_import import import_contacts, preview_contacts_csv, template_csv
from services.contacts.emails import validate_email
from services.contacts.roles import ROLE_LABELS, classify_designation
from services.contacts.search_links import google_people_searches, linkedin_people_searches
from services.discovery.normalize import is_valid_url

settings = get_settings()
VERIFY_LABELS = {"manually_verified": "✅ Verified by you", "evidence_found": "📄 Evidence found",
                 "unverified": "⚪ Unverified"}
EMAIL_LABELS = {"public_verified": "Published on official page", "hunter": "Hunter.io (found on web)",
                "unverified": "Unverified", "none": "No email"}

st.title("Contact Discovery")
st.caption("Founders, product leaders, hiring managers and recruiters, never product interns. Every contact keeps "
           "the source that supports its name and role. Emails are only ever taken from a source, never guessed.")

with get_connection() as conn:
    companies = [dict(r) for r in conn.execute("SELECT * FROM companies ORDER BY name")]
if not companies:
    st.info("No companies in the pipeline yet. Add them from **Qualified Companies** or **Companies & contacts**.")
    st.stop()
by_label = {f"{c['name']} ({c['domain']})": c for c in companies}

tabs = st.tabs(["🔍 Discover", "🔗 Search links", "👥 Contacts", "➕ Add contact", "📥 CSV import", "🚫 No contacts"])

# ------------------------------------------------------------------ discover
with tabs[0]:
    with get_connection() as conn:
        waiting = contacts_repo.companies_for_discovery(conn)
        runs = contacts_repo.last_runs(conn)
    st.write(f"**{len(waiting)}** companies waiting (not discovered in the last 14 days; failed ones are retried).")
    c1, c2, c3 = st.columns([1, 1, 2])
    limit = c1.number_input("Process up to", 1, 50, min(5, max(len(waiting), 1)))
    force = c2.checkbox("Re-run already discovered", help="Ignores the 14-day skip and the page cache")
    use_hunter = False
    if settings.hunter_api_key:
        use_hunter = c3.checkbox("Also use Hunter.io (1 credit per company, max once per 30 days)")
    else:
        c3.caption("Hunter.io: no `HUNTER_API_KEY` in .env, so only websites and listings are used.")
    picked = st.multiselect("Only these companies (optional)", list(by_label))
    st.caption("Order: companies with product internship vacancies first, then "
               f"{settings.india_ratio:.0%} India / {1 - settings.india_ratio:.0%} international. "
               "Up to 5 pages per company (home, team, leadership, about, careers), robots.txt respected, "
               "2 s between requests, pages cached 7 days.")
    if st.button("🔍 Discover contacts", type="primary", disabled=not waiting and not picked and not force):
        from agents.contact_agent import ContactAgent

        hunter = None
        if use_hunter:
            from integrations.hunter import HunterClient

            hunter = HunterClient(settings.hunter_api_key)
        bar = st.progress(0.0, text="Starting…")
        with get_connection() as conn:
            agent = ContactAgent(conn, settings, hunter=hunter)
            summary, details = agent.run(int(limit), [by_label[p]["id"] for p in picked] or None,
                                         force or bool(picked),
                                         lambda i, n, c: bar.progress((i - 0.5) / n, text=f"{i}/{n} {c['name']}"))
        bar.progress(1.0, text="Done")
        st.success(f"Processed {summary.processed} companies · {summary.created} new contacts.")
        st.dataframe(pd.DataFrame([{"company": d.company_name, "status": d.status, "pages": d.pages_checked,
                                    "people found": d.people_found, "new": d.added, "updated": d.updated,
                                    "Hunter": "yes" if d.hunter_used else "", "issues": "; ".join(d.errors)[:200]}
                                   for d in details]), width="stretch", hide_index=True)

    if runs:
        st.markdown("**Last discovery per company**")
        st.dataframe(pd.DataFrame([{"company": next((c["name"] for c in companies if c["id"] == cid), cid),
                                    "status": r["status"], "when": (r["started_at"] or "")[:16],
                                    "pages": r["pages_checked"], "people": r["people_found"],
                                    "new": r["contacts_added"], "issues": (r["error"] or "")[:160]}
                                   for cid, r in runs.items()]), width="stretch", hide_index=True)
        failed = [c for c in companies if runs.get(c["id"], {}).get("status") == "failed"]
        if failed:
            st.caption(f"{len(failed)} failed: select them above and run again to retry.")

# ------------------------------------------------------------------ search links
with tabs[1]:
    label = st.selectbox("Company", list(by_label), key="links_company")
    company = by_label[label]
    st.caption("These open in your own browser. LinkedIn needs you to be signed in; the app never opens or reads "
               "LinkedIn. Add people you find on **Add contact** with their profile URL as the source.")
    g, li = st.columns(2)
    with g:
        st.markdown("**Google**")
        st.dataframe(pd.DataFrame([{"search": s.text, "open": s.url}
                                   for s in google_people_searches(company["name"], company["domain"])]),
                     width="stretch", hide_index=True,
                     column_config={"open": st.column_config.LinkColumn("Open", display_text="Search ↗")})
    with li:
        st.markdown("**LinkedIn people search**")
        st.dataframe(pd.DataFrame([{"search": s.text, "open": s.url}
                                   for s in linkedin_people_searches(company["name"])]),
                     width="stretch", hide_index=True,
                     column_config={"open": st.column_config.LinkColumn("Open", display_text="Search ↗")})

# ------------------------------------------------------------------ contacts
with tabs[2]:
    f1, f2, f3, f4 = st.columns(4)
    f_companies = f1.multiselect("Company", list(by_label), key="f_company")
    f_roles = f2.multiselect("Designation", [r for r in ROLE_CATEGORIES], format_func=ROLE_LABELS.get, key="f_role")
    f_verify = f3.multiselect("Verification", list(VERIFICATION_STATUSES), format_func=VERIFY_LABELS.get,
                              key="f_verify")
    f_score = f4.slider("Minimum score", 0, 100, 0, step=5, key="f_score")
    search = st.text_input("Search name or title", key="f_search")
    with get_connection() as conn:
        rows = contacts_repo.list_contacts(conn, company_ids=[by_label[c]["id"] for c in f_companies] or None,
                                           categories=f_roles or None, verification=f_verify or None,
                                           min_score=f_score or None, search=search or None)
        evidence = contacts_repo.evidence_for(conn, [r["id"] for r in rows])

    if not rows:
        st.info("No contacts match. Run discovery, add contacts manually, or import a CSV.")
    grouped: dict[str, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["company_name"], []).append(r)
    for company_name, items in grouped.items():
        first = items[0]
        with st.expander(f"{company_name} · {MARKET_LABELS.get(first['market'], first['market'])} · "
                         f"{len(items)} contact(s)", expanded=len(grouped) <= 3):
            st.dataframe(pd.DataFrame([{
                "priority": r["outreach_priority"], "name": r["full_name"], "designation": r["role_title"],
                "role": ROLE_LABELS.get(r["role_category"], r["role_category"]),
                "score": r["score_total"], "role pts": r["score_role"], "authority": r["score_authority"],
                "association": r["score_association"], "contact pts": r["score_contact"],
                "verification": VERIFY_LABELS.get(r["verification_status"]),
                "email": r["email"] or "", "email type": EMAIL_LABELS.get(r["email_type"]),
                "source": r["source"], "source url": r["source_url"], "LinkedIn": r["linkedin_url"],
            } for r in items]), width="stretch", hide_index=True, column_config={
                "score": st.column_config.ProgressColumn("Score", min_value=0, max_value=100, format="%d"),
                "source url": st.column_config.LinkColumn("Source", display_text="Open ↗"),
                "LinkedIn": st.column_config.LinkColumn("LinkedIn", display_text="Profile ↗"),
            })
            for r in items:
                st.caption(f"**{r['full_name']}** evidence: “{(r['evidence'] or 'none')[:220]}”"
                           + (f" · seen {len(evidence[r['id']])}×" if len(evidence[r["id"]]) > 1 else ""))

    if rows:
        st.divider()
        st.markdown("**Edit or delete a contact**")
        options = {f"{r['full_name']} · {r['role_title'] or ''} · {r['company_name']}": r for r in rows}
        choice = st.selectbox("Contact", list(options), key="edit_pick")
        r = options[choice]
        with st.form(f"edit_{r['id']}"):
            e1, e2 = st.columns(2)
            name = e1.text_input("Full name", r["full_name"])
            title = e2.text_input("Designation", r["role_title"] or "")
            e3, e4 = st.columns(2)
            category = e3.selectbox("Role", ROLE_CATEGORIES, index=ROLE_CATEGORIES.index(r["role_category"]),
                                    format_func=ROLE_LABELS.get)
            email = e4.text_input("Email", r["email"] or "")
            e5, e6 = st.columns(2)
            linkedin = e5.text_input("LinkedIn URL", r["linkedin_url"] or "")
            source_url = e6.text_input("Source URL", r["source_url"] or "")
            email_public = st.checkbox("This email is published on an official company page",
                                       value=r["email_type"] == "public_verified")
            notes = st.text_area("Notes", r["notes"] or "", height=70)
            dnc = st.checkbox("Do not contact", value=bool(r["do_not_contact"]))
            verify = st.checkbox("I have checked this person's name, role and company myself",
                                 value=r["verification_status"] == "manually_verified")
            s1, s2 = st.columns([1, 1])
            save = s1.form_submit_button("💾 Save", type="primary")
            delete = s2.form_submit_button("🗑️ Delete contact")
        if save:
            check = validate_email(email, r["company_domain"]) if email else None
            if email and not check.ok:
                st.error("That email address is not valid.")
            elif source_url and not is_valid_url(source_url):
                st.error("Source URL must be a full http(s) link.")
            else:
                etype = "none" if not email else ("public_verified" if email_public else
                                                  (r["email_type"] if email.lower() == (r["email"] or "")
                                                   and r["email_type"] != "none" else "unverified"))
                try:
                    with get_connection() as conn:
                        contacts_repo.edit_contact(conn, r["id"], {
                            "full_name": name, "role_title": title, "role_category": category,
                            "email": email or None, "email_type": etype, "linkedin_url": linkedin or None,
                            "source_url": source_url or None, "notes": notes or None, "do_not_contact": int(dnc)},
                            mark_verified=verify)
                        contacts_repo.rescore_company(conn, r["company_id"])
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Another contact at this company already has that name or email.")
        if delete:
            with get_connection() as conn:
                contacts_repo.delete_contact(conn, r["id"])
                contacts_repo.rescore_company(conn, r["company_id"])
            st.rerun()

# ------------------------------------------------------------------ add contact
with tabs[3]:
    st.caption("For people you found yourself (e.g. via the LinkedIn or Google links). A source URL is required "
               "so every contact has evidence.")
    with st.form("add_contact_p4", clear_on_submit=True):
        label = st.selectbox("Company*", list(by_label))
        a1, a2 = st.columns(2)
        name = a1.text_input("Full name*")
        title = a2.text_input("Current designation*", placeholder="Head of Product")
        b1, b2 = st.columns(2)
        source_url = b1.text_input("Source URL*", placeholder="LinkedIn profile or company page")
        linkedin = b2.text_input("LinkedIn profile URL")
        c1, c2 = st.columns(2)
        email = c1.text_input("Professional email (only if published)")
        email_public = c2.checkbox("Email is published on an official company page")
        notes = st.text_area("Notes", height=70)
        verified = st.checkbox("I have checked this person's name, role and company myself")
        submitted = st.form_submit_button("Add contact", type="primary")
    if submitted:
        company = by_label[label]
        errors = []
        if len(name.split()) < 1 or len(name.strip()) < 3:
            errors.append("Full name is required.")
        if not title.strip():
            errors.append("Designation is required.")
        if not is_valid_url(source_url):
            errors.append("Source URL must be a full http(s) link.")
        if linkedin and not is_valid_url(linkedin):
            errors.append("LinkedIn URL must be a full http(s) link.")
        check = validate_email(email, company["domain"]) if email else None
        if email and not check.ok:
            errors.append("Email address is not valid.")
        category = classify_designation(title)
        if "intern" in title.lower():
            errors.append("Interns are not outreach targets.")
        if errors:
            for e in errors:
                st.error(e)
        else:
            if check and check.problems:
                st.warning("Email note: " + ", ".join(check.problems))
            try:
                with get_connection() as conn:
                    _, action = contacts_repo.upsert_contact(
                        conn, company["id"], full_name=name.strip(), role_title=title.strip(),
                        role_category=category, source="linkedin" if "linkedin.com" in source_url else "manual",
                        source_url=source_url.strip(), evidence=f"Added by you from {source_url.strip()}",
                        email=check.email if check else None,
                        email_type=("public_verified" if email_public else "unverified") if check else "none",
                        linkedin_url=linkedin.strip() or None, notes=notes or None,
                        verification_status="manually_verified" if verified else None)
                    contacts_repo.rescore_company(conn, company["id"])
                st.success(f"Contact {action}." + ("" if category != "other" else
                                                    " Its role is not a target role, so it scores low."))
            except sqlite3.IntegrityError:
                st.error("Another contact at this company already uses that email.")

# ------------------------------------------------------------------ csv import
with tabs[4]:
    st.caption("Columns: company, full_name, current_role, professional_email, linkedin_url, source_url, notes. "
               "The company must already be in your pipeline. source_url or linkedin_url is required as evidence.")
    st.download_button("⬇️ Download template", template_csv(), "contacts_template.csv", "text/csv")
    upload = st.file_uploader("Upload contacts CSV", type=["csv"], key="contacts_csv")
    if upload is not None:
        data = upload.getvalue()
        digest = hashlib.sha256(data).hexdigest()
        if st.session_state.get("contacts_csv_digest") != digest:
            st.session_state.pop("contacts_csv_done", None)
            try:
                with get_connection() as conn:
                    st.session_state["contacts_csv_preview"] = preview_contacts_csv(conn, data)
                st.session_state["contacts_csv_digest"] = digest
            except ValueError as exc:
                st.error(str(exc))
                st.stop()
        preview = st.session_state["contacts_csv_preview"]
        if preview.missing_columns:
            st.error(f"Missing columns: {', '.join(preview.missing_columns)}")
        if preview.unknown_columns:
            st.warning(f"Ignored columns: {', '.join(preview.unknown_columns)}")
        m1, m2, m3 = st.columns(3)
        m1.metric("New", preview.count("new"))
        m2.metric("Duplicates", preview.count("duplicate"))
        m3.metric("Invalid", preview.count("invalid"))
        st.dataframe(pd.DataFrame([{
            "row": r.row_number, "status": r.status, "company": r.raw.get("company"), "name": r.raw.get("full_name"),
            "role": r.raw.get("current_role"), "details": "; ".join(r.errors + r.warnings)
            + (f" duplicate of {r.duplicate_of}" if r.duplicate_of else "")} for r in preview.rows]),
            width="stretch", hide_index=True)
        if st.session_state.get("contacts_csv_done"):
            st.success(st.session_state["contacts_csv_done"])
        elif st.button("Import valid rows", type="primary",
                       disabled=bool(preview.missing_columns) or preview.count("new") + preview.count("duplicate") == 0):
            with get_connection() as conn:
                s = import_contacts(conn, preview)
            st.session_state["contacts_csv_done"] = (
                f"Imported {s.created} new · merged {s.merged} · skipped {s.skipped_invalid} invalid and "
                f"{s.skipped_repeats} repeated rows.")
            st.rerun()

# ------------------------------------------------------------------ no contacts
with tabs[5]:
    with get_connection() as conn:
        empty = contacts_repo.companies_without_contacts(conn)
    if not empty:
        st.success("Every pipeline company has at least one contact.")
    else:
        st.dataframe(pd.DataFrame([{"company": c["name"], "domain": c["domain"],
                                    "market": MARKET_LABELS.get(c["market"], c["market"]), "status": c["status"],
                                    "last discovery": c["last_run_status"] or "never run"} for c in empty]),
                     width="stretch", hide_index=True)
        st.caption("Try the **Search links** tab for these, then add people manually.")
