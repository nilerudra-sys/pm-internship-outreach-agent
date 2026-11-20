import sqlite3

import pandas as pd
import streamlit as st

from database import repository
from database.db import get_connection
from database.models import COMPANY_STATUSES, MARKETS, REMOTE_POLICIES, ROLE_CATEGORIES, SECTORS

st.title("Companies & contacts")

f1, f2, f3 = st.columns(3)
market = f1.selectbox("Market", ["All", *MARKETS])
sector = f2.selectbox("Sector", ["All", *SECTORS])
status = f3.selectbox("Status", ["All", *COMPANY_STATUSES])

with get_connection() as conn:
    companies = repository.list_companies(
        conn,
        market=None if market == "All" else market,
        sector=None if sector == "All" else sector,
        status=None if status == "All" else status,
    )

if companies:
    cols = ["id", "name", "domain", "market", "sector", "remote_policy", "status", "fit_score", "city", "source"]
    st.dataframe(pd.DataFrame(companies)[cols], width="stretch", hide_index=True)
else:
    st.info("No companies match. Add one below or run discovery once it's built.")

with st.expander("➕ Add a company manually"):
    with st.form("add_company", clear_on_submit=True):
        a1, a2 = st.columns(2)
        name = a1.text_input("Name*")
        domain = a2.text_input("Website / domain*", placeholder="example.com")
        b1, b2, b3 = st.columns(3)
        new_market = b1.selectbox("Market*", MARKETS)
        new_sector = b2.selectbox("Sector*", SECTORS)
        remote = b3.selectbox("Remote policy", REMOTE_POLICIES, index=REMOTE_POLICIES.index("unknown"))
        description = st.text_area("Notes / description")
        if st.form_submit_button("Add company", type="primary"):
            if not name or not domain:
                st.error("Name and domain are required.")
            else:
                try:
                    with get_connection() as conn:
                        repository.add_company(
                            conn, name=name, domain=domain, market=new_market, sector=new_sector,
                            remote_policy=remote, description=description or None,
                        )
                    st.success(f"Added {name}.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("A company with that domain already exists.")
                except ValueError as exc:
                    st.error(str(exc))

st.divider()
st.subheader("Contacts")

with get_connection() as conn:
    all_companies = repository.list_companies(conn)
    contacts = repository.list_contacts(conn)

if contacts:
    cols = ["company_name", "full_name", "role_title", "role_category", "email", "email_status", "source"]
    st.dataframe(pd.DataFrame(contacts)[cols], width="stretch", hide_index=True)
else:
    st.caption("No contacts yet.")

if all_companies:
    with st.expander("➕ Add a contact manually"):
        with st.form("add_contact", clear_on_submit=True):
            options = {f"{c['name']} ({c['domain']})": c["id"] for c in all_companies}
            company_label = st.selectbox("Company*", list(options))
            c1, c2 = st.columns(2)
            full_name = c1.text_input("Full name*")
            role_title = c2.text_input("Title", placeholder="Co-founder & CEO")
            d1, d2 = st.columns(2)
            role_category = d1.selectbox("Role category", ROLE_CATEGORIES)
            email = d2.text_input("Email")
            if st.form_submit_button("Add contact", type="primary"):
                if not full_name:
                    st.error("Full name is required.")
                else:
                    try:
                        with get_connection() as conn:
                            repository.add_contact(
                                conn, company_id=options[company_label], full_name=full_name,
                                first_name=full_name.split()[0], role_title=role_title or None,
                                role_category=role_category, email=email or None,
                            )
                        st.success(f"Added {full_name}.")
                        st.rerun()
                    except sqlite3.IntegrityError:
                        st.error("That email already exists for this company.")
