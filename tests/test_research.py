from types import SimpleNamespace

import pytest

from agents.research_agent import ResearchAgent
from config.settings import Settings
from database import repository
from database import research as research_repo
from integrations.http import FetchedPage, PoliteSession
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead
from services.research.company_research import (
    CompanyResearch,
    Fact,
    ResearchScores,
    Source,
    build_prompt,
    build_sources,
    fit_score,
    is_grounded,
    research_company,
    validate_facts,
)
from services.research.html_text import extract
from services.research.website import RobotsCache, candidate_links, fetch_company_pages

HOME = """<html><head><title>Acme Freight</title>
<meta name="description" content="Acme Freight matches truckers with shippers across India.">
<script>var x = "ignore me";</script><style>.a{}</style></head>
<body><nav><a href="/">Home</a><a href="/about-us">About us</a><a href="/careers">Careers</a>
<a href="/product/platform">Platform</a><a href="https://twitter.com/acme">Twitter</a>
<a href="/privacy">Privacy policy</a><a href="mailto:hi@acme.in">Email</a></nav>
<h1>Freight matching for Indian truckers</h1>
<p>Acme Freight runs a mobile app that matches 20,000 truck drivers with shippers in Pune and Mumbai.</p>
<p>Drivers see loads near them and get paid within 24 hours.</p></body></html>"""
ABOUT = """<html><head><title>About Acme</title></head><body><h2>Our story</h2>
<p>Founded in 2022 in Pune, Acme Freight raised a seed round led by Example Ventures.</p>
<p>We are a remote-first team of 25 people across India.</p></body></html>"""
CAREERS = """<html><body><h2>Careers at Acme</h2><p>We hire product interns every summer to work on the driver app.</p>
<p>Interns run user interviews with drivers and write feature specs.</p></body></html>"""
JS_ONLY = "<html><body><div id='root'></div><script>render()</script></body></html>"


class FakeHttp:
    """Serves canned pages; records requested URLs."""

    def __init__(self, pages, robots="User-agent: *\nAllow: /\n"):
        self.pages = dict(pages)
        self.robots = robots
        self.requested = []
        self.host_intervals = {}

    def get_page(self, url, max_bytes=1_500_000):
        self.requested.append(url)
        if url.endswith("/robots.txt"):
            if isinstance(self.robots, int):
                return FetchedPage(url, url, self.robots, "text/plain", "")
            if isinstance(self.robots, Exception):
                raise self.robots
            return FetchedPage(url, url, 200, "text/plain", self.robots)
        if url not in self.pages:
            return FetchedPage(url, url, 404, "text/html", "not found")
        body = self.pages[url]
        if isinstance(body, tuple):          # (final_url, html) for redirects
            return FetchedPage(url, body[0], 200, "text/html; charset=utf-8", body[1])
        return FetchedPage(url, url, 200, "text/html; charset=utf-8", body)


SITE = {
    "https://acme.in/": HOME,
    "https://acme.in/about-us": ABOUT,
    "https://acme.in/careers": CAREERS,
    "https://acme.in/product/platform": ("<html><body><p>The Acme platform lets shippers post loads and track "
                                         "every truck in real time.</p><p>Shippers pay only when a load is "
                                         "delivered on time to the warehouse.</p></body></html>"),
}


@pytest.fixture
def company(conn):
    cid = repository.add_company(conn, name="Acme Freight", domain="acme.in", market="india", sector="Other")
    return repository.get_company(conn, cid)


def make_result(**kw):
    data = dict(
        summary="Acme Freight runs an app matching truck drivers with shippers in India.",
        product="mobile app that matches truck drivers with shippers",
        target_customers="truck drivers and shippers",
        business_model=None,
        industry="Logistics",
        product_facts=[Fact(text="Mobile app matches 20,000 truck drivers with shippers in Pune and Mumbai", source="P1")],
        stage_signals=[Fact(text="Founded in 2022 in Pune and raised a seed round", source="P2")],
        hiring_signals=[Fact(text="Hires product interns every summer to work on the driver app", source="P3")],
        remote_signals=[Fact(text="Remote-first team of 25 people across India", source="P2")],
        outreach_hooks=[
            Fact(text="Drivers get paid within 24 hours", source="P1"),
            Fact(text="Partnership with Tata Motors announced last month", source="P1"),   # not in source
            Fact(text="Acme operates in Pune", source="P9"),                               # unknown source
        ],
        candidate_angle="Cloud background fits a real-time matching app.",
        scores=ResearchScores(product_depth=8, pm_opportunity=9, remote_india_fit=10, background_fit=7,
                              personalisation_potential=8),
        insufficient_information=False,
        reasoning="Sources describe the product, team and internships.",
    )
    data.update(kw)
    return CompanyResearch(**data)


class FakeGemini:
    model = "fake-primary"

    def __init__(self, result=None, error=None, used="fake-fallback"):
        self.result, self.error, self.used, self.prompts = result, error, used, []
        self.last_model_used = None

    def generate_json(self, prompt, schema, *, system=None, temperature=0.1):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        self.last_model_used = self.used
        return self.result


# ------------------------------------------------------------------ html


def test_extract_text_title_description_links():
    page = extract(HOME)
    assert page.title == "Acme Freight"
    assert page.description.startswith("Acme Freight matches truckers")
    assert "ignore me" not in page.text and ".a{}" not in page.text
    assert "20,000 truck drivers" in page.text
    assert "Freight matching for Indian truckers" in page.headings
    assert ("/careers", "Careers") in page.links
    assert "Home" not in page.text.split("\n")   # one-word menu items dropped


def test_extract_survives_broken_html():
    assert "hello world from acme" in extract("<p>hello world from acme<div><span>").text.lower() + " "


# ------------------------------------------------------------------ links + robots


def test_candidate_links_same_site_and_ranked():
    links = extract(HOME).links + [("https://careers.acme.in/jobs", "Jobs"), ("https://evil.com/about", "About")]
    picked = dict(candidate_links("https://acme.in/", links, "acme.in", 4))
    assert picked["about"] == "https://acme.in/about-us"
    assert picked["careers"] == "https://acme.in/careers"       # shallower than careers.acme.in/jobs
    assert picked["product"] == "https://acme.in/product/platform"
    assert not any("twitter" in u or "privacy" in u or "evil" in u for u in picked.values())


@pytest.mark.parametrize("robots, url, allowed", [
    ("User-agent: *\nDisallow: /careers\n", "https://acme.in/careers", False),
    ("User-agent: *\nDisallow: /careers\n", "https://acme.in/about-us", True),
    ("User-agent: PMInternshipOutreach\nDisallow: /\n", "https://acme.in/", False),
    (404, "https://acme.in/", True),                    # RFC 9309: missing robots.txt -> allowed
    (503, "https://acme.in/", False),                   # server error -> assume disallowed
    (ConnectionError("down"), "https://acme.in/", False),
])
def test_robots(robots, url, allowed):
    assert RobotsCache(FakeHttp({}, robots=robots)).allowed(url) is allowed


def test_robots_crawl_delay_sets_host_interval():
    http = FakeHttp({}, robots="User-agent: *\nCrawl-delay: 30\n")
    RobotsCache(http).allowed("https://acme.in/")
    assert http.host_intervals["acme.in"] == 10.0   # capped


# ------------------------------------------------------------------ website fetch


def test_fetch_company_pages_and_cache(conn, company):
    http = FakeHttp(SITE)
    report = fetch_company_pages(conn, company, http, RobotsCache(http))
    types = [p["page_type"] for p in report.pages]
    assert types[0] == "home" and {"about", "careers", "product"} <= set(types)
    assert len(report.pages) <= 5
    assert not any("twitter" in u for u in http.requested)

    http2 = FakeHttp(SITE)
    cached = fetch_company_pages(conn, company, http2, RobotsCache(http2))
    assert cached.from_cache and http2.requested == []
    forced = fetch_company_pages(conn, company, http2, RobotsCache(http2), force=True)
    assert not forced.from_cache and http2.requested


def test_robots_blocked_pages_are_not_fetched(conn, company):
    http = FakeHttp(SITE, robots="User-agent: *\nDisallow: /careers\nDisallow: /about-us\n")
    report = fetch_company_pages(conn, company, http, RobotsCache(http))
    assert report.blocked == 2
    assert "https://acme.in/careers" not in http.requested
    errors = {p["url"]: p["error"] for p in research_repo.all_pages(conn, company["id"])}
    assert errors["https://acme.in/careers"] == "blocked by robots.txt"


def test_www_fallback_and_offsite_redirect(conn, company):
    site = {"https://www.acme.in/": HOME, "https://acme.in/about-us": ("https://other.com/x", ABOUT)}
    http = FakeHttp(site)
    report = fetch_company_pages(conn, company, http, RobotsCache(http))
    assert report.pages[0]["url"] == "https://www.acme.in/"
    assert any("redirected off-site" in e for e in report.errors) or \
        "https://acme.in/about-us" not in [p["url"] for p in report.pages]


def test_javascript_only_site_has_no_pages(conn, company):
    http = FakeHttp({"https://acme.in/": JS_ONLY})
    report = fetch_company_pages(conn, company, http, RobotsCache(http))
    assert report.pages == []
    assert "too little readable text" in research_repo.all_pages(conn, company["id"])[0]["error"]


# ------------------------------------------------------------------ grounding + scoring


def test_is_grounded():
    src = "Acme Freight runs a mobile app that matches 20,000 truck drivers with shippers."
    assert is_grounded("App matches 20,000 truck drivers with shippers", src)
    assert not is_grounded("Partnership with Tata Motors announced last month", src)
    assert not is_grounded("", src)


def test_validate_facts_drops_unknown_and_unsupported():
    sources = build_sources([
        {"url": "https://acme.in/", "page_type": "home", "title": "Acme", "text": extract(HOME).text},
        {"url": "https://acme.in/about-us", "page_type": "about", "title": "About", "text": extract(ABOUT).text},
        {"url": "https://acme.in/careers", "page_type": "careers", "title": "Careers", "text": extract(CAREERS).text},
    ], [])
    facts, dropped = validate_facts(make_result(), sources)
    assert dropped == 2
    assert [f["text"] for f in facts["outreach_hooks"]] == ["Drivers get paid within 24 hours"]
    assert facts["outreach_hooks"][0]["url"] == "https://acme.in/"
    assert facts["hiring_signals"][0]["source_id"] == "P3"


def test_strip_inline_citations():
    from services.research.company_research import strip_citations

    assert strip_citations("Serves 1.8 crore clients [P1, P2].") == "Serves 1.8 crore clients."
    assert strip_citations("Kite [P3] and Coin [L1]") == "Kite and Coin"
    assert strip_citations(None) is None


def _only_hooks(*hooks):
    return make_result(outreach_hooks=list(hooks), product_facts=[], stage_signals=[], hiring_signals=[],
                       remote_signals=[])


def test_multi_source_fact_needs_every_cited_source():
    sources = [Source("P1", "page", "https://acme.in/", "home", "Drivers get paid within 24 hours.", "2026-11-17"),
               Source("P2", "page", "https://acme.in/about", "about", "We are a remote-first team of 25 people.")]
    combined = Fact(text="Drivers get paid within 24 hours and the team is remote-first [P1, P2]", source="P1, P2")
    facts, dropped = validate_facts(_only_hooks(combined), sources)
    hook = facts["outreach_hooks"][0]
    assert dropped == 0 and hook["text"] == "Drivers get paid within 24 hours and the team is remote-first"
    assert [s["source_id"] for s in hook["sources"]] == ["P1", "P2"]
    assert hook["sources"][0]["fetched_at"] == "2026-11-17" and hook["verified"] is True
    # Any unknown cited id, or a cited source that supports nothing, drops the fact.
    assert validate_facts(_only_hooks(Fact(text=combined.text, source="P1, P9")), sources)[1] == 1
    unrelated = Fact(text="Drivers get paid within 24 hours", source="P1, P2")
    assert validate_facts(_only_hooks(unrelated), sources)[1] == 1


def test_fit_score_and_caps():
    full = {k: 10 for k in ("product_depth", "pm_opportunity", "remote_india_fit", "background_fit",
                            "personalisation_potential")}
    assert fit_score(full, insufficient=False, hooks=2) == (100, [])
    assert fit_score(full, insufficient=True, hooks=2)[0] == 40
    assert fit_score(full, insufficient=False, hooks=0)[0] == 60


def test_prompt_lists_sources_and_rules(conn, company):
    sources = [Source("P1", "page", "https://acme.in/", "home: Acme", "Acme text"),
               Source("L1", "listing", "https://acme.in/jobs/1", "job listing: PM Intern", "Listing text")]
    prompt = build_prompt(company, sources, profile="CS student")
    assert "[P1] home: Acme (https://acme.in/)" in prompt and "[L1]" in prompt
    assert "Do not invent" in prompt and "acme.in" in prompt
    assert "{" not in prompt.split("COMPANY")[1]


# ------------------------------------------------------------------ research end to end


def _pages(conn, company):
    http = FakeHttp(SITE)
    return fetch_company_pages(conn, company, http, RobotsCache(http)).pages


def test_research_company_saves_cited_facts_and_updates_company(conn, company):
    pages = _pages(conn, company)
    fake = FakeGemini(make_result())
    data = research_company(conn, company, pages, fake, profile="x")
    assert data["model"] == "fake-fallback"          # model that actually answered
    saved = research_repo.get_research(conn, company["id"])
    assert saved["fit_score"] == 86 and saved["dropped_facts"] == 2
    assert {s["id"] for s in saved["sources"]} == {f"P{i}" for i in range(1, len(pages) + 1)}
    assert saved["industry"] == "Logistics"
    co = repository.get_company(conn, company["id"])
    assert co["status"] == "researched" and co["fit_score"] == 86
    # The company record gets only verified text, never Gemini's free-form summary.
    assert co["research_summary"] == saved["verified_summary"]
    assert saved["verified_summary"].startswith("Mobile app matches 20,000 truck drivers")
    assert saved["summary"].startswith("Acme Freight runs an app")          # interpretation kept separately
    hook = saved["facts"]["outreach_hooks"][0]
    assert hook["quote"] == "Drivers see loads near them and get paid within 24 hours."
    assert hook["sources"][0]["fetched_at"]                                 # provenance stored
    assert all(s["fetched_at"] for s in saved["sources"])
    assert "product interns" in co["hiring_signal"]
    log = conn.execute("SELECT details FROM activity_log WHERE action='researched'").fetchone()[0]
    assert "model fake-fallback" in log


def test_unsupported_free_text_is_dropped(conn, company):
    pages = _pages(conn, company)
    research_company(conn, company, pages, FakeGemini(make_result(business_model="Enterprise SaaS subscription")),
                     profile="x")
    assert research_repo.get_research(conn, company["id"])["business_model"] is None


def test_linked_listing_is_used_as_source(conn, company):
    ingest_lead(conn, Lead(company_name="Acme Freight Pvt Ltd", job_title="Product Intern", source="linkedin",
                           job_url="https://www.linkedin.com/jobs/view/4000000001",
                           description="Work on the driver app roadmap with the CEO."))
    fake = FakeGemini(make_result())
    research_company(conn, company, [], fake, profile="x")
    assert "[L1] job listing: Product Intern" in fake.prompts[0]


def test_no_sources_skips_gemini(conn, company):
    fake = FakeGemini(make_result())
    out = research_company(conn, company, [], fake, profile="x")
    assert "no readable sources" in out["error"] and fake.prompts == []


def test_gemini_failure_is_recorded_and_company_unchanged(conn, company):
    pages = _pages(conn, company)
    out = research_company(conn, company, pages, FakeGemini(error=RuntimeError("All Gemini models unavailable")),
                           profile="x")
    assert "unavailable" in out["error"]
    assert repository.get_company(conn, company["id"])["status"] == "discovered"
    assert research_repo.companies_needing_research(conn)[0]["id"] == company["id"]


def test_status_is_never_downgraded(conn, company):
    conn.execute("UPDATE companies SET status = 'contacted' WHERE id = ?", (company["id"],))
    company = repository.get_company(conn, company["id"])
    research_company(conn, company, _pages(conn, company), FakeGemini(make_result()), profile="x")
    assert repository.get_company(conn, company["id"])["status"] == "contacted"


# ------------------------------------------------------------------ agent


def test_agent_researches_india_first_and_skips_recent(conn):
    for i in range(4):
        repository.add_company(conn, name=f"India {i}", domain=f"india{i}.in", market="india")
    repository.add_company(conn, name="Intl", domain="intl.com", market="international")
    pages = {f"https://india{i}.in/": HOME for i in range(4)} | {"https://intl.com/": HOME}
    http = FakeHttp(pages)
    agent = ResearchAgent(conn, Settings(india_ratio=0.8), FakeGemini(make_result()), http=http)

    order = [c["name"] for c in agent.targets(5)]
    assert order == ["India 0", "India 1", "India 2", "India 3", "Intl"]
    result = agent.run(limit=2)
    assert (result.processed, result.created, result.errors) == (2, 2, [])
    assert len(research_repo.companies_needing_research(conn)) == 3   # researched ones are skipped
    assert agent.targets(5, force=True, company_ids=[1])[0]["id"] == 1


def test_schema_v3_tables(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"company_pages", "company_research"} <= tables


def test_v3_database_upgrades_to_v4(tmp_path):
    import sqlite3

    from database.db import SCHEMA_VERSION, init_db

    # Build a genuine v3 database: the current schema minus the columns v4 added.
    path = tmp_path / "old.db"
    init_db(path)
    old = sqlite3.connect(path)
    old.execute("ALTER TABLE company_research DROP COLUMN verified_summary")
    old.execute("ALTER TABLE company_research DROP COLUMN unreadable_pages")
    old.execute("INSERT INTO companies (name, domain, market) VALUES ('Acme', 'acme.in', 'india')")
    old.execute("INSERT INTO company_research (company_id, summary) VALUES (1, 'kept')")
    old.execute("PRAGMA user_version = 3")
    old.commit()
    old.close()
    init_db(path)
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(company_research)")}
    assert {"verified_summary", "unreadable_pages"} <= cols
    assert conn.execute("SELECT summary FROM company_research").fetchone()[0] == "kept"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 4


def test_unreadable_careers_page_is_unknown_not_negative(conn, company):
    site = dict(SITE)
    site["https://acme.in/careers"] = JS_ONLY
    http = FakeHttp(site)
    pages = fetch_company_pages(conn, company, http, RobotsCache(http)).pages
    fake = FakeGemini(make_result(hiring_signals=[], scores=ResearchScores(
        product_depth=8, pm_opportunity=2, remote_india_fit=10, background_fit=7, personalisation_potential=8)))
    research_company(conn, company, pages, fake, profile="x")
    assert "careers: https://acme.in/careers (too little readable text" in fake.prompts[0]
    saved = research_repo.get_research(conn, company["id"])
    assert saved["score_breakdown"]["pm_opportunity"] == 5
    assert "hiring treated as unknown" in saved["score_breakdown"]["notes"][0]
    assert saved["unreadable_pages"][0]["page_type"] == "careers"


def test_cookie_banner_page_is_not_content():
    from services.research.website import has_real_content

    banner = extract("<html><body><div>We use cookies to improve your experience. Accept all cookies or manage "
                     "preferences.</div><div>Please enable JavaScript to view this website properly today.</div>"
                     "<nav>Home About Careers</nav></body></html>")
    assert not has_real_content(banner.text)
    assert has_real_content(extract(ABOUT).text)


def test_get_page_decodes_utf8_without_charset_header():
    body = "<p>Stipend ₹15,000 per month</p>".encode("utf-8")

    class Resp:
        status_code, url, encoding = 200, "https://acme.in/", "ISO-8859-1"   # requests' default guess
        headers = {"Content-Type": "text/html"}

        def iter_content(self, n):
            yield body

        def close(self):
            pass

    session = SimpleNamespace(headers={}, get=lambda url, **kw: Resp())
    page = PoliteSession(min_interval=0, session=session).get_page("https://acme.in/")
    assert "₹15,000" in page.text


def test_polite_session_get_page_caps_body():
    class Resp:
        status_code, url, encoding = 200, "https://acme.in/", "utf-8"
        headers = {"Content-Type": "text/html"}

        def iter_content(self, n):
            yield b"a" * 70000
            yield b"b" * 70000

        def close(self):
            pass

    session = SimpleNamespace(headers={}, get=lambda url, **kw: Resp())
    page = PoliteSession(min_interval=0, session=session).get_page("https://acme.in/", max_bytes=100000)
    assert len(page.text) == 100000 and page.status == 200
