import sqlite3

import pytest

from database import repository
from database.db import SCHEMA_VERSION, connect, init_db
from database.models import normalize_domain
from database.repository import InvalidTransition


def test_init_creates_tables_and_version(tmp_path):
    path = tmp_path / "x.db"
    init_db(path)
    init_db(path)  # idempotent
    conn = connect(path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"companies", "contacts", "emails", "activity_log", "api_usage",
            "opportunities", "opportunity_sources", "career_boards",
            "company_pages", "company_research"} <= tables
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("https://www.Example.com/about", "example.com"),
        ("example.in", "example.in"),
        ("  sub.example.co.in/path ", "sub.example.co.in"),
    ],
)
def test_normalize_domain(raw, expected):
    assert normalize_domain(raw) == expected


def test_normalize_domain_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_domain("localhost")


def test_company_domain_is_normalised_and_unique(conn, company_with_contact):
    company_id, _ = company_with_contact
    assert repository.get_company(conn, company_id)["domain"] == "testco.example"
    with pytest.raises(sqlite3.IntegrityError):
        repository.add_company(conn, name="Dup", domain="testco.example", market="india")


def test_company_validation(conn):
    with pytest.raises(ValueError):
        repository.add_company(conn, name="X", domain="x.example", market="mars")
    with pytest.raises(ValueError):
        repository.add_company(conn, name="X", domain="x.example", market="india", sector="Crypto")


def test_contact_email_lowercased(conn, company_with_contact):
    company_id, _ = company_with_contact
    contacts = repository.list_contacts(conn, company_id)
    assert contacts[0]["email"] == "person@testco.example"


def test_cascade_delete(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    repository.create_email_draft(conn, company_id=company_id, contact_id=contact_id, subject="s", body="b")
    conn.execute("DELETE FROM companies WHERE id = ?", (company_id,))
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_draft_starts_pending_and_cannot_skip_approval(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    email_id = repository.create_email_draft(
        conn, company_id=company_id, contact_id=contact_id, subject="Hi", body="Body"
    )
    assert repository.get_email(conn, email_id)["status"] == "pending_review"
    with pytest.raises(InvalidTransition):
        repository.set_email_status(conn, email_id, "sent")


def test_approval_then_send_flow(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    email_id = repository.create_email_draft(
        conn, company_id=company_id, contact_id=contact_id, subject="Hi", body="Body"
    )
    repository.set_email_status(conn, email_id, "approved")
    assert repository.get_email(conn, email_id)["approved_at"]
    repository.set_email_status(conn, email_id, "sent", gmail_message_id="m1")
    email = repository.get_email(conn, email_id)
    assert email["status"] == "sent" and email["sent_at"] and email["gmail_message_id"] == "m1"
    with pytest.raises(InvalidTransition):
        repository.update_email_content(conn, email_id, subject="x", body="y")


def test_editing_approved_email_requires_reapproval(conn, company_with_contact):
    company_id, contact_id = company_with_contact
    email_id = repository.create_email_draft(
        conn, company_id=company_id, contact_id=contact_id, subject="Hi", body="Body"
    )
    repository.set_email_status(conn, email_id, "approved")
    repository.update_email_content(conn, email_id, subject="Hi v2", body="Body v2")
    email = repository.get_email(conn, email_id)
    assert email["status"] == "pending_review"
    assert email["approved_at"] is None


def test_pipeline_stats(conn, company_with_contact):
    stats = repository.pipeline_stats(conn)
    assert stats["companies"] == 1
    assert stats["contacts"] == 1
    assert stats["company_market"] == {"india": 1}
