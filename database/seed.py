"""Load clearly-fictional demo data so the dashboard can be explored before APIs are connected.

Usage:  python -m database.seed            (adds demo rows if the DB has no companies)
        python -m database.seed --force    (adds them even if data exists; duplicates are skipped)
"""

from __future__ import annotations

import sqlite3
import sys

from database import repository
from database.db import get_connection, init_db

DEMO_COMPANIES = [
    dict(name="Demo AI Labs", domain="demo-ai-labs.example", market="india", sector="AI",
         remote_policy="remote", city="Bengaluru", country="India", funding_stage="Seed",
         description="Fictional GenAI copilot startup (demo data)."),
    dict(name="Sample Pay", domain="samplepay.example", market="india", sector="Fintech",
         remote_policy="remote_india_ok", city="Mumbai", country="India", funding_stage="Series A",
         description="Fictional UPI payments startup (demo data)."),
    dict(name="LearnLoop Demo", domain="learnloop.example", market="india", sector="EdTech",
         remote_policy="hybrid", city="Pune", country="India", funding_stage="Seed",
         description="Fictional adaptive-learning platform (demo data)."),
    dict(name="Stackly Demo", domain="stackly.example", market="international", sector="SaaS",
         remote_policy="remote_india_ok", city="Remote", country="USA", funding_stage="Series A",
         description="Fictional B2B workflow SaaS hiring remotely (demo data)."),
]

DEMO_CONTACTS = {
    "demo-ai-labs.example": dict(full_name="Asha Demo", first_name="Asha", role_title="Co-founder & CEO",
                                 role_category="cofounder", email="asha@demo-ai-labs.example"),
    "samplepay.example": dict(full_name="Rohan Sample", first_name="Rohan", role_title="Head of Product",
                              role_category="head_of_product", email="rohan@samplepay.example"),
    "stackly.example": dict(full_name="Jordan Example", first_name="Jordan", role_title="Talent Partner",
                            role_category="recruiter", email="jordan@stackly.example"),
}


def seed(conn: sqlite3.Connection) -> int:
    added = 0
    for data in DEMO_COMPANIES:
        if repository.find_company_by_domain(conn, data["domain"]):
            continue
        company_id = repository.add_company(conn, source="seed", **data)
        added += 1
        contact = DEMO_CONTACTS.get(data["domain"])
        if contact:
            contact_id = repository.add_contact(conn, company_id=company_id, source="seed", **contact)
            repository.create_email_draft(
                conn,
                company_id=company_id,
                contact_id=contact_id,
                subject=f"Product internship - ideas for {data['name']}",
                body=(
                    f"Hi {contact['first_name']},\n\n"
                    "[DEMO DRAFT] This placeholder will be replaced by a Gemini-written email "
                    "once the writer agent is built.\n\nBest regards"
                ),
                model="demo",
            )
    return added


def main() -> None:
    init_db()
    with get_connection() as conn:
        has_data = conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] > 0
        if has_data and "--force" not in sys.argv:
            print("Database already has companies; use --force to add demo rows anyway.")
            return
        print(f"Added {seed(conn)} demo companies.")


if __name__ == "__main__":
    main()
