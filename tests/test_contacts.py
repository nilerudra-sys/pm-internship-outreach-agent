import sqlite3
from urllib.parse import parse_qs, urlparse

import pytest

from agents.contact_agent import ContactAgent
from config.settings import Settings
from database import contacts as contacts_repo
from database import repository
from database import research as research_repo
from database.db import connect, init_db
from integrations.http import FetchedPage
from integrations.hunter import HunterClient, HunterError, parse_domain_search
from services.contacts.csv_import import import_contacts, preview_contacts_csv, template_csv
from services.contacts.discovery import discover_company, people_links
from services.contacts.emails import email_matches_name, validate_email
from services.contacts.extract import extract_people
from services.contacts.outreach_context import get_outreach_context
from services.contacts.roles import classify_designation
from services.contacts.scoring import CompanyContext, company_stage, score, score_role
from services.contacts.search_links import google_people_searches, linkedin_people_searches
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead
from services.research.website import RobotsCache

HOME = """<html><head><title>Demo Freight</title></head><body>
<nav><a href="/team">Our Team</a><a href="/about-us">About</a><a href="/careers">Careers</a>
<a href="/login">Login</a><a href="https://twitter.com/demo">Twitter</a></nav>
<p>Demo Freight matches truck drivers with shippers across India every single day of the week.</p>
<p>Founded in 2021 by Asha Rao and Karan Gupta, we are a seed-stage team in Pune building logistics software.</p>
</body></html>"""
TEAM = """<html><body><h1>Meet our team</h1>
<div><h3>Asha Rao</h3><p>Co-founder &amp; CEO</p></div>
<div><h3>Vikram Shah</h3><p>Head of Product</p></div>
<p>Priya Nair - Talent Acquisition Lead</p>
<p>Sneha Iyer (Product Intern)</p>
<div><h3>Arjun Das</h3><p>Advisor</p></div>
<p>Neha Kapoor - Engineering Manager</p>
<a href="mailto:asha@demofreight.in">Email Asha</a><a href="mailto:careers@demofreight.in">Careers</a>
<a href="https://www.linkedin.com/in/vikram-shah-123/">Vikram Shah on LinkedIn</a>
</body></html>"""


class FakeHttp:
    def __init__(self, pages, robots="User-agent: *\nAllow: /\n", fail_hosts=()):
        self.pages, self.robots, self.fail_hosts = dict(pages), robots, set(fail_hosts)
        self.requested, self.host_intervals = [], {}

    def get_page(self, url, max_bytes=1_500_000):
        self.requested.append(url)
        host = urlparse(url).hostname
        if host in self.fail_hosts or host.removeprefix("www.") in self.fail_hosts:
            raise ConnectionError(f"cannot reach {host}")
        if url.endswith("/robots.txt"):
            return FetchedPage(url, url, 200, "text/plain", self.robots)
        if url not in self.pages:
            return FetchedPage(url, url, 404, "text/html", "")
        return FetchedPage(url, url, 200, "text/html; charset=utf-8", self.pages[url])

    def get_json(self, url, params=None):
        raise AssertionError("no JSON expected")


SITE = {"https://demofreight.in/": HOME, "https://demofreight.in/team": TEAM}


@pytest.fixture
def company(conn):
    cid = repository.add_company(conn, name="Demo Freight", domain="demofreight.in", market="india",
                                 funding_stage="Seed")
    return repository.get_company(conn, cid)


def _discover(conn, company, site=SITE, hunter=None, force=False):
    http = FakeHttp(site)
    return discover_company(conn, company, http, RobotsCache(http), hunter, force), http


# ------------------------------------------------------------------ search links


def test_google_and_linkedin_search_urls():
    g = google_people_searches("Demo Freight", "demofreight.in")
    texts = [s.text for s in g]
    assert '"Demo Freight" Head of Product' in texts
    assert 'site:linkedin.com/in "Demo Freight" recruiter' in texts
    assert "site:demofreight.in (team OR leadership OR founders OR about)" in texts
    q = parse_qs(urlparse(g[0].url).query)["q"][0]
    assert q == '"Demo Freight" founder' and g[0].url.startswith("https://www.google.com/search?")
    li = linkedin_people_searches("Demo Freight")
    assert li[0].url.startswith("https://www.linkedin.com/search/results/people/?")
    assert parse_qs(urlparse(li[3].url).query)["keywords"] == ["Demo Freight Head of Product"]


# ------------------------------------------------------------------ roles


@pytest.mark.parametrize("title, category", [
    ("Co-founder & CEO", "cofounder"), ("Founder", "founder"), ("Chief Executive Officer", "ceo"),
    ("Chief Product Officer", "head_of_product"), ("VP, Product", "head_of_product"),
    ("Head of Product", "head_of_product"), ("Group Product Manager", "product_lead"),
    ("Senior Product Manager", "product_manager"), ("Associate Product Manager", "product_manager"),
    ("Hiring Manager", "hiring_manager"), ("Talent Acquisition Partner", "talent_acquisition"),
    ("Senior Recruiter", "recruiter"), ("CTO", "executive"),
    ("Product Intern", "other"), ("Product Management Intern", "other"), ("APM Trainee", "other"),
    ("Advisor", "other"), ("Investor", "other"), ("Engineering Manager", "other"),
    ("Product Marketing Manager", "other"), ("", "other"),
])
def test_role_classification(title, category):
    assert classify_designation(title) == category


# ------------------------------------------------------------------ extraction


def test_extract_team_page_with_evidence():
    from services.research.html_text import extract

    page = extract(TEAM)
    people = {p.full_name: p for p in extract_people(page.people_text, "https://demofreight.in/team",
                                                     {"demo", "freight"}, page.links, "demofreight.in")}
    assert set(people) == {"Asha Rao", "Vikram Shah", "Priya Nair"}          # no intern, advisor, eng manager
    assert people["Asha Rao"].role_category == "cofounder"
    assert people["Asha Rao"].evidence == "Asha Rao — Co-founder & CEO"
    assert people["Asha Rao"].email == "asha@demofreight.in"                  # published, matches the name
    assert people["Vikram Shah"].email is None                                # careers@ is never attached
    assert people["Vikram Shah"].linkedin_url == "https://www.linkedin.com/in/vikram-shah-123/"


def test_extract_sentences_and_listings():
    people = extract_people("Demo Freight was founded in 2021 by Asha Rao and Karan Gupta.", "u", {"demo"})
    assert {p.full_name for p in people} == {"Asha Rao", "Karan Gupta"}
    listing = "You will report to Vikram Shah, our Head of Product. Questions? Contact Kavya Menon, Talent Acquisition Partner."
    found = {p.full_name: p.role_category for p in extract_people(listing, "u", {"demo"})}
    assert found == {"Vikram Shah": "head_of_product", "Kavya Menon": "talent_acquisition"}


@pytest.mark.parametrize("text", ["Product Manager Intern\nApply Now", "Join Our Team - Careers",
                                  "Bengaluru, Karnataka", "Read More - Founder Stories", "Head of Product Design"])
def test_extract_no_false_positives(text):
    assert extract_people(text, None, {"demo"}) == []


# ------------------------------------------------------------------ emails


def test_email_validation():
    assert validate_email("Asha@DemoFreight.in", "demofreight.in").email == "asha@demofreight.in"
    assert not validate_email("asha@@demo", "demofreight.in").ok
    assert validate_email("careers@demofreight.in", "demofreight.in").generic
    assert not validate_email("asha@gmail.com", "demofreight.in").domain_matches
    assert not validate_email("asha@other.com", "demofreight.in").domain_matches
    assert validate_email("asha@mail.demofreight.in", "demofreight.in").domain_matches
    assert email_matches_name("asha@x.in", "Asha Rao") and email_matches_name("arao@x.in", "Asha Rao")
    assert not email_matches_name("karan@x.in", "Asha Rao")


# ------------------------------------------------------------------ discovery, sources, multiple contacts


def test_discovery_stores_contacts_with_sources(conn, company):
    result, http = _discover(conn, company)
    assert result.status == "ok" and result.added == 4             # 3 on team page + Karan (founded by)
    assert "https://demofreight.in/login" not in http.requested and not any("twitter" in u for u in http.requested)
    rows = {c["full_name"]: c for c in contacts_repo.list_contacts(conn, company_ids=[company["id"]])}
    assert set(rows) == {"Asha Rao", "Vikram Shah", "Priya Nair", "Karan Gupta"}   # multiple per company
    asha = rows["Asha Rao"]
    assert asha["source"] == "website" and asha["source_url"] == "https://demofreight.in/team"
    assert asha["verification_status"] == "evidence_found" and asha["evidence"]
    assert asha["email"] == "asha@demofreight.in" and asha["email_type"] == "public_verified"
    assert rows["Vikram Shah"]["email_type"] == "none"
    assert all(r["score_total"] is not None and r["outreach_priority"] for r in rows.values())
    # Early-stage company without a vacancy: founders first.
    assert min(rows.values(), key=lambda r: r["outreach_priority"])["role_category"] in ("cofounder", "founder")
    assert repository.get_company(conn, company["id"])["status"] == "contacts_found"


def test_rediscovery_preserves_history_without_duplicates(conn, company):
    _discover(conn, company)
    second, http2 = _discover(conn, company, force=True)
    assert second.added == 0
    assert conn.execute("SELECT COUNT(*) FROM contacts").fetchone()[0] == 4
    asha = contacts_repo.find_duplicate(conn, company["id"], "Asha Rao")
    assert len(contacts_repo.evidence_for(conn, [asha["id"]])[asha["id"]]) >= 2      # every sighting kept


def test_pages_are_cached_between_runs(conn, company):
    _discover(conn, company)
    contacts_repo.finish_run(conn, contacts_repo.start_run(conn, company["id"]), status="failed")
    again, http = _discover(conn, company)                 # not forced
    assert http.requested == []                            # all pages from the 7-day cache


def test_listing_contacts_are_linked_to_the_vacancy(conn, company):
    res = ingest_lead(conn, Lead(company_name="Demo Freight", job_title="Product Intern", source="linkedin",
                                 job_url="https://www.linkedin.com/jobs/view/4000000009",
                                 description="You will report to Meera Pillai, our Head of Product."))
    _discover(conn, company)
    meera = contacts_repo.find_duplicate(conn, company["id"], "Meera Pillai")
    assert meera["source"] == "listing" and meera["opportunity_id"] == res.opportunity_id
    assert meera["score_role"] == 40 and meera["outreach_priority"] == 1   # vacancy contact first


def test_duplicate_detection(conn, company):
    a, act = contacts_repo.upsert_contact(conn, company["id"], full_name="Asha Rao", role_title="CEO",
                                          role_category="ceo", source="website", source_url="u", evidence="e")
    b, act2 = contacts_repo.upsert_contact(conn, company["id"], full_name="Dr. ASHA  rao", role_title=None,
                                           role_category="other", source="website", source_url="u2", evidence="e2")
    contacts_repo.edit_contact(conn, a, {"email": "asha@demofreight.in", "email_type": "hunter"})
    d, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Asha R.", role_title="CEO",
                                        role_category="ceo", source="hunter", source_url="u4", evidence="e4",
                                        email="ASHA@demofreight.in", email_type="hunter")
    e, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Asha (Rao)", role_title="CEO",
                                        role_category="ceo", source="csv", source_url="u5", evidence="e5",
                                        linkedin_url="https://in.linkedin.com/in/asha/")
    contacts_repo.edit_contact(conn, a, {"linkedin_url": "https://www.linkedin.com/in/asha"})
    f, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Asha Rao Ji", role_title="CEO",
                                        role_category="ceo", source="csv", source_url="u6", evidence="e6",
                                        linkedin_url="https://www.linkedin.com/in/asha?trk=x")
    assert a == b == d == f                               # same name, same email, same LinkedIn profile
    assert act2 == "unchanged" or act2 == "updated"


# ------------------------------------------------------------------ manual verification protection


def test_manual_data_is_never_overwritten_by_automation(conn, company):
    cid, _ = contacts_repo.upsert_contact(
        conn, company["id"], full_name="Vikram Shah", role_title="VP Product", role_category="head_of_product",
        source="manual", source_url="https://www.linkedin.com/in/vikram", evidence="Added by you",
        email="vikram.s@demofreight.in", email_type="unverified", verification_status="manually_verified")
    _discover(conn, company, force=True)
    contacts_repo.upsert_contact(conn, company["id"], full_name="Vikram Shah", role_title="Product Manager",
                                 role_category="product_manager", source="hunter", source_url="h", evidence="h",
                                 email="vikram@demofreight.in", email_type="hunter")
    v = contacts_repo.get_contact(conn, cid)
    assert v["role_title"] == "VP Product" and v["email"] == "vikram.s@demofreight.in"
    assert v["verification_status"] == "manually_verified"
    assert len(contacts_repo.evidence_for(conn, [cid])[cid]) >= 3          # but every sighting is recorded


def test_explicit_title_replaces_founded_by_placeholder_but_not_manual(conn, company):
    cid, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Karan Gupta",
                                          role_title='Founder ("founded by")', role_category="founder",
                                          source="website", source_url="https://demofreight.in/", evidence="founded by")
    contacts_repo.upsert_contact(conn, company["id"], full_name="Karan Gupta", role_title="Co-founder & CTO",
                                 role_category="cofounder", source="website", source_url="https://demofreight.in/team",
                                 evidence="Karan Gupta — Co-founder & CTO")
    k = contacts_repo.get_contact(conn, cid)
    assert (k["role_title"], k["role_category"], k["source_url"]) == \
        ("Co-founder & CTO", "cofounder", "https://demofreight.in/team")
    m, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Meena Joshi",
                                        role_title='Founder ("founded by")', role_category="founder", source="manual",
                                        source_url="https://x.com", evidence="you")
    contacts_repo.upsert_contact(conn, company["id"], full_name="Meena Joshi", role_title="CEO", role_category="ceo",
                                 source="website", source_url="u", evidence="e")
    assert contacts_repo.get_contact(conn, m)["role_title"] == 'Founder ("founded by")'


def test_stronger_automated_email_upgrades_unprotected_contact(conn, company):
    cid, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Priya Nair", role_title="TA Lead",
                                          role_category="talent_acquisition", source="website", source_url="u",
                                          evidence="e", email="priya@demofreight.in", email_type="hunter")
    contacts_repo.upsert_contact(conn, company["id"], full_name="Priya Nair", role_title=None,
                                 role_category="other", source="website", source_url="u", evidence="e",
                                 email="priya.nair@demofreight.in", email_type="public_verified")
    p = contacts_repo.get_contact(conn, cid)
    assert p["email"] == "priya.nair@demofreight.in" and p["email_type"] == "public_verified"
    contacts_repo.upsert_contact(conn, company["id"], full_name="Priya Nair", role_title=None, role_category="other",
                                 source="hunter", source_url="h", evidence="h", email="pn@demofreight.in",
                                 email_type="hunter")
    assert contacts_repo.get_contact(conn, cid)["email"] == "priya.nair@demofreight.in"   # weaker never replaces


# ------------------------------------------------------------------ scoring


def test_score_components_and_priority_tables():
    early = CompanyContext("early", False, [])
    established = CompanyContext("established", True, [7])
    founder = {"role_category": "founder", "source": "website", "evidence": "e", "verification_status": "evidence_found",
               "email": "a@demofreight.in", "email_type": "public_verified"}
    s = score(founder, early, "demofreight.in")
    assert (s.role, s.authority, s.association, s.contact, s.total) == (40, 25, 18, 15, 98)
    assert s.confidence == 90
    assert score_role("hiring_manager", established, False) == 40
    assert score_role("founder", established, False) < score_role("recruiter", established, False)
    assert score_role("recruiter", established, True) == 40
    assert score_role("recruiter", early, False) < score_role("founder", early, False)
    speculative_big = CompanyContext("established", False, [])
    assert score_role("head_of_product", speculative_big, False) > score_role("hiring_manager", speculative_big, False)
    weak = {**founder, "email": "a@gmail.com", "email_type": "unverified", "source": "csv", "evidence": None,
            "source_url": None}
    s2 = score(weak, early, "demofreight.in")
    assert s2.association == 0 and s2.contact == 4


def test_company_stage_from_verified_data():
    assert company_stage({"funding_stage": "Seed"}, None) == "early"
    assert company_stage({"employee_count": "1,200 employees"}, None) == "established"
    research = {"facts": {"stage_signals": [{"text": "Serves 1.8 crore customers"}]}}
    assert company_stage({}, research) == "established"
    assert company_stage({}, {"summary": "Series D unicorn"}) == "unknown"     # interpretation is ignored


# ------------------------------------------------------------------ Hunter


def test_missing_hunter_key_is_harmless(conn, company):
    with pytest.raises(HunterError):
        HunterClient("")
    assert Settings().hunter_api_key == ""
    result, _ = _discover(conn, company, hunter=None)
    assert result.status == "ok" and not result.hunter_used


HUNTER_PAYLOAD = {"data": {"pattern": "{first}", "emails": [
    {"value": "meera@demofreight.in", "type": "personal", "confidence": 94, "first_name": "Meera",
     "last_name": "Pillai", "position": "Head of Product", "linkedin": None,
     "sources": [{"uri": "https://demofreight.in/blog/launch"}], "verification": {"status": "valid"}},
    {"value": "guess@demofreight.in", "first_name": "Ravi", "last_name": "K", "position": "CEO",
     "provenance": "inferred", "sources": [{"uri": "x"}]},
    {"value": "nosrc@demofreight.in", "first_name": "No", "last_name": "Source", "position": "CTO", "sources": []},
    {"value": "x@demofreight.in", "first_name": "Only", "last_name": "", "position": "", "sources": [{"uri": "y"}]},
]}}


def test_hunter_parsing_rejects_inferred_and_unsourced():
    r = parse_domain_search(HUNTER_PAYLOAD)
    assert [p.full_name for p in r.people] == ["Meera Pillai"]
    assert (r.discarded_inferred, r.discarded_no_source, r.discarded_incomplete) == (1, 1, 1)


class FakeHunterHttp:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.calls = payload, error, []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        if self.error:
            raise self.error
        return self.payload


def test_hunter_used_in_discovery_and_key_not_leaked(conn, company):
    http = FakeHunterHttp(HUNTER_PAYLOAD)
    hunter = HunterClient("SECRET123", http=http)
    result, _ = _discover(conn, company, hunter=hunter)
    assert result.hunter_used and http.calls[0][1]["required_field"] == "full_name,position"
    meera = contacts_repo.find_duplicate(conn, company["id"], "Meera Pillai")
    assert meera["email_type"] == "hunter" and meera["email_confidence"] == 94 and meera["source"] == "hunter"
    assert contacts_repo.find_duplicate(conn, company["id"], "Ravi K") is None
    assert conn.execute("SELECT calls FROM api_usage WHERE provider='hunter'").fetchone()[0] == 1
    # Not searched again within 30 days unless forced.
    _discover(conn, company, hunter=hunter)
    assert len(http.calls) == 1
    bad = HunterClient("SECRET123", http=FakeHunterHttp(error=RuntimeError("401 for url ...api_key=SECRET123")))
    with pytest.raises(HunterError) as exc:
        bad.domain_search("demofreight.in")
    assert "SECRET123" not in str(exc.value)


# ------------------------------------------------------------------ CSV


def test_contacts_csv_import(conn, company):
    other = repository.add_company(conn, name="Globex", domain="globex.com", market="international")
    csv_text = (
        "Company,Name,Title,Email,LinkedIn,Source URL,Notes\n"
        "Demo Freight,Asha Rao,Co-founder & CEO,asha@demofreight.in,https://www.linkedin.com/in/asha,,met at event\n"
        "demofreight.in,Ravi Kumar,Talent Acquisition Partner,,https://www.linkedin.com/in/ravi,,\n"
        "Globex,Jane Doe,Head of Product,jane@gmail.com,,https://globex.com/team,\n"
        "Unknown Co,John Roe,CEO,,https://www.linkedin.com/in/john,,\n"
        "Demo Freight,Sneha Iyer,Product Intern,,https://www.linkedin.com/in/sneha,,\n"
        "Demo Freight,Karan Gupta,Founder,,,,\n"
        "Demo Freight,Bad Email,CEO,not-an-email,https://www.linkedin.com/in/bad,,\n"
        "Demo Freight,ASHA RAO,CEO,,https://www.linkedin.com/in/asha2,,\n"
    )
    preview = preview_contacts_csv(conn, csv_text)
    status = {r.raw["full_name"]: r for r in preview.rows}
    assert status["Asha Rao"].status == "new" and status["Ravi Kumar"].status == "new"
    assert status["Jane Doe"].status == "new" and "webmail address" in status["Jane Doe"].warnings
    assert "not in your pipeline" in status["John Roe"].errors[0]
    assert "interns are not outreach targets" in status["Sneha Iyer"].errors
    assert "evidence" in status["Karan Gupta"].errors[0]
    assert "invalid email" in status["Bad Email"].errors[0]
    assert status["ASHA RAO"].duplicate_of == "row 1"
    s = import_contacts(conn, preview)
    assert (s.created, s.skipped_invalid, s.skipped_repeats) == (3, 4, 1)
    asha = contacts_repo.find_duplicate(conn, company["id"], "Asha Rao")
    assert asha["source"] == "csv" and asha["verification_status"] == "unverified" and asha["notes"] == "met at event"
    again = preview_contacts_csv(conn, csv_text)
    assert {r.raw["full_name"]: r.status for r in again.rows}["Asha Rao"] == "duplicate"
    assert preview_contacts_csv(conn, template_csv()).rows[0].errors     # template company isn't in pipeline
    assert other


# ------------------------------------------------------------------ migration


def test_v4_contacts_migrate_to_v5_without_data_loss(tmp_path):
    path = tmp_path / "v4.db"
    old = sqlite3.connect(path)
    old.executescript("""
        CREATE TABLE companies (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, domain TEXT NOT NULL UNIQUE,
            website TEXT, description TEXT, market TEXT NOT NULL, sector TEXT NOT NULL DEFAULT 'Other', country TEXT,
            city TEXT, remote_policy TEXT NOT NULL DEFAULT 'unknown', funding_stage TEXT, employee_count TEXT,
            hiring_signal TEXT, research_summary TEXT, fit_score INTEGER, status TEXT NOT NULL DEFAULT 'discovered',
            source TEXT NOT NULL DEFAULT 'manual', source_url TEXT, created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01');
        CREATE TABLE contacts (id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE, full_name TEXT NOT NULL,
            first_name TEXT, role_title TEXT, role_category TEXT NOT NULL DEFAULT 'other'
            CHECK (role_category IN ('founder', 'cofounder', 'head_of_product', 'recruiter', 'other')),
            email TEXT, email_confidence INTEGER, email_status TEXT NOT NULL DEFAULT 'unverified',
            linkedin_url TEXT, source TEXT NOT NULL DEFAULT 'manual', do_not_contact INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT '2026-01-01', UNIQUE (company_id, email));
        CREATE INDEX idx_contacts_company ON contacts (company_id);
        CREATE TABLE emails (id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE,
            contact_id INTEGER NOT NULL REFERENCES contacts (id) ON DELETE CASCADE,
            kind TEXT NOT NULL DEFAULT 'initial', subject TEXT NOT NULL, body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending_review', model TEXT, reviewer_notes TEXT, gmail_message_id TEXT,
            gmail_thread_id TEXT, error TEXT, created_at TEXT NOT NULL DEFAULT '2026-01-01',
            updated_at TEXT NOT NULL DEFAULT '2026-01-01', approved_at TEXT, sent_at TEXT);
        INSERT INTO companies (id, name, domain, market) VALUES (1, 'Acme', 'acme.in', 'india');
        INSERT INTO contacts (id, company_id, full_name, first_name, role_title, role_category, email, source)
            VALUES (7, 1, 'Asha Rao', 'Asha', 'Co-founder & CEO', 'cofounder', 'asha@acme.in', 'manual');
        INSERT INTO emails (company_id, contact_id, subject, body) VALUES (1, 7, 'Hi', 'Body');
        PRAGMA user_version = 4;
    """)
    old.close()
    init_db(path)
    conn = connect(path)
    c = dict(conn.execute("SELECT * FROM contacts WHERE id = 7").fetchone())
    assert (c["full_name"], c["role_category"], c["email"], c["email_type"], c["name_norm"]) == \
        ("Asha Rao", "cofounder", "asha@acme.in", "unverified", "asha rao")
    assert conn.execute("SELECT contact_id FROM emails").fetchone()[0] == 7
    assert "REFERENCES contacts" in conn.execute("SELECT sql FROM sqlite_master WHERE name='emails'").fetchone()[0]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 5
    assert (tmp_path / "v4.backup-v4.db").exists()
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"contact_evidence", "contact_discovery_runs"} <= tables and "contacts_v4" not in tables
    conn.execute("DELETE FROM contacts WHERE id = 7")           # cascade still works on the new table
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0
    conn.close()
    init_db(path)                                               # idempotent


# ------------------------------------------------------------------ batch


def _companies(conn, n_india, n_intl):
    ids = []
    for i in range(n_india):
        ids.append(repository.add_company(conn, name=f"India Co {i}", domain=f"indiaco{i}.in", market="india"))
    for i in range(n_intl):
        ids.append(repository.add_company(conn, name=f"World Co {i}", domain=f"worldco{i}.com",
                                          market="international"))
    return ids


def test_batch_is_india_first_and_vacancy_first(conn):
    _companies(conn, 5, 2)
    ingest_lead(conn, Lead(company_name="World Co 1", job_title="Product Intern", source="csv",
                           job_url="https://worldco1.com/jobs/pm", website="https://worldco1.com"))
    agent = ContactAgent(conn, Settings(india_ratio=0.8), http=FakeHttp({}))
    order = [c["name"] for c in agent.targets(7)]
    assert order[0] == "India Co 0"
    assert order.index("World Co 1") < order.index("World Co 0")       # vacancy before no vacancy
    first5 = order[:5]
    assert sum(n.startswith("India") for n in first5) == 4


def test_batch_continues_after_failures_and_retries(conn):
    ids = _companies(conn, 2, 0)
    site = {"https://indiaco1.in/": HOME.replace("Demo Freight", "India Co 1"),
            "https://indiaco1.in/team": TEAM}
    http = FakeHttp(site, fail_hosts={"indiaco0.in"})
    agent = ContactAgent(conn, Settings(), http=http)
    summary, details = agent.run(limit=5)
    status = {d.company_name: d.status for d in details}
    assert status == {"India Co 0": "failed", "India Co 1": "ok"}
    assert summary.processed == 2 and len(summary.errors) == 1
    # Failed company is offered again; the successful one is skipped for 14 days.
    assert [c["id"] for c in contacts_repo.companies_for_discovery(conn)] == [ids[0]]
    assert contacts_repo.last_runs(conn)[ids[0]]["status"] == "failed"


def test_people_links_rank_same_site_people_pages():
    links = [("/team", "Our Team"), ("/blog/news", "Blog"), ("https://other.com/team", "Team"),
             ("/about-us", "About"), ("/login", "Login"), ("/careers", "Careers"), ("/leadership", "Leadership")]
    picked = dict(people_links("https://demofreight.in/", links, "demofreight.in"))
    assert picked["team"] == "https://demofreight.in/team" and picked["leadership"].endswith("/leadership")
    assert "other.com" not in str(picked) and "login" not in str(picked)


# ------------------------------------------------------------------ phase 5 context


def test_outreach_context(conn, company):
    _discover(conn, company)
    research_repo.save_research(conn, company["id"], {
        "summary": "AI says things", "verified_summary": "Matches truck drivers with shippers.",
        "facts": {"outreach_hooks": [{"text": "Matches truck drivers with shippers", "quote": "q", "url": "u"}]},
        "researched_at": "2026-11-17T00:00:00+00:00"})
    asha = contacts_repo.find_duplicate(conn, company["id"], "Asha Rao")
    ctx = get_outreach_context(conn, asha["id"])
    assert ctx["ready"] and ctx["outreach_type"] == "speculative" and ctx["company_stage"] == "early"
    assert ctx["contact"]["full_name"] == "Asha Rao" and ctx["contact"]["email"] == "asha@demofreight.in"
    assert ctx["company"]["verified_facts"]["outreach_hooks"][0]["quote"] == "q"
    assert ctx["company"]["interpretation_not_verified"]["summary"] == "AI says things"
    assert ctx["contact"]["sources"][0]["url"] == "https://demofreight.in/team"
    assert ctx["applicant_profile"]["target_role"] == "Product Management Intern"
    # An unverified CSV contact with an unverified email is not ready, and its name is withheld.
    cid, _ = contacts_repo.upsert_contact(conn, company["id"], full_name="Ravi Kumar", role_title="Recruiter",
                                          role_category="recruiter", source="csv", source_url="https://x.com/r",
                                          evidence="csv", email="ravi@demofreight.in", email_type="unverified")
    ctx2 = get_outreach_context(conn, cid)
    assert not ctx2["ready"] and ctx2["contact"]["full_name"] is None and ctx2["contact"]["email"] is None
    assert len(ctx2["blockers"]) == 2
