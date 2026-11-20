import pytest

from database import opportunities as opp_repo
from integrations.ats import detect_board, fetch_jobs, parse_ashby, parse_greenhouse, parse_lever
from services.discovery.career_pages import check_all_boards

PM_DESC = "Own the roadmap, run user research and write PRDs. Track product metrics in Mixpanel."

GREENHOUSE = {"jobs": [
    {"title": "Product Management Intern", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
     "location": {"name": "Bengaluru, India"}, "content": "&lt;p&gt;" + PM_DESC + "&lt;/p&gt;",
     "updated_at": "2026-11-01T10:00:00-04:00"},
    {"title": "Senior Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2",
     "location": {"name": "Remote"}, "content": "Go and Kafka"},
    {"title": "Strategy Intern", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3",
     "location": {"name": "Remote - India"}, "content": PM_DESC},
]}

LEVER = [
    {"text": "Product Analyst", "hostedUrl": "https://jobs.lever.co/globex/abc",
     "categories": {"location": "London, UK", "commitment": "Intern"}, "descriptionPlain": PM_DESC,
     "lists": [{"text": "You will", "content": "<li>Analyse funnels</li>"}], "workplaceType": "remote",
     "createdAt": 1762000000000},
    {"text": "Account Executive", "hostedUrl": "https://jobs.lever.co/globex/def",
     "categories": {"location": "London", "commitment": "Full-time"}, "descriptionPlain": "Sell."},
]

ASHBY = {"jobs": [
    {"title": "APM Intern", "jobUrl": "https://jobs.ashbyhq.com/initech/1", "location": "Remote",
     "isRemote": True, "employmentType": "Intern", "descriptionPlain": PM_DESC,
     "publishedAt": "2026-11-03T00:00:00Z", "compensation": {"compensationTierSummary": "$2K/month"}},
    {"title": "Hidden", "jobUrl": "x", "isListed": False},
]}


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get_json(self, url, params=None):
        self.calls.append((url, params))
        for key, value in self.routes.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError(f"unexpected URL {url}")


@pytest.mark.parametrize("url, expected", [
    ("https://boards.greenhouse.io/acme/jobs/123", ("greenhouse", "acme")),
    ("https://job-boards.greenhouse.io/acme", ("greenhouse", "acme")),
    ("jobs.lever.co/globex", ("lever", "globex")),
    ("https://jobs.ashbyhq.com/initech/abc", ("ashby", "initech")),
    ("https://acme.com/careers", None),
])
def test_detect_board(url, expected):
    assert detect_board(url) == expected


def test_parsers():
    gh = parse_greenhouse(GREENHOUSE)
    assert gh[0].title == "Product Management Intern" and gh[0].posted_date == "2026-11-01"
    lv = parse_lever(LEVER)
    assert lv[0].employment_type == "Intern" and lv[0].workplace == "remote" and "Analyse funnels" in lv[0].description
    ab = parse_ashby(ASHBY)
    assert len(ab) == 1 and ab[0].workplace == "remote" and ab[0].compensation == "$2K/month"


def test_fetch_jobs_uses_official_endpoints():
    http = FakeHttp({"greenhouse": GREENHOUSE, "lever": LEVER, "ashby": ASHBY})
    fetch_jobs("greenhouse", "acme", http)
    fetch_jobs("lever", "globex", http)
    fetch_jobs("ashby", "initech", http)
    assert http.calls[0] == ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", {"content": "true"})
    assert http.calls[1] == ("https://api.lever.co/v0/postings/globex", {"mode": "json"})
    assert http.calls[2][0] == "https://api.ashbyhq.com/posting-api/job-board/initech"
    with pytest.raises(ValueError):
        fetch_jobs("greenhouse", "../etc", http)


def test_check_boards_imports_only_product_internships(conn):
    opp_repo.add_career_board(conn, company_name="Acme", ats="greenhouse", slug="acme",
                              website="https://acme.in", market_hint="india")
    opp_repo.add_career_board(conn, company_name="Globex", ats="lever", slug="globex")
    opp_repo.add_career_board(conn, company_name="Initech", ats="ashby", slug="initech", market_hint="international")
    http = FakeHttp({"greenhouse": GREENHOUSE, "lever": LEVER, "ashby": ASHBY})

    summary = check_all_boards(conn, http=http)
    by_board = {b.board.split(" ")[0]: b for b in summary.boards}
    assert (by_board["Acme"].fetched, by_board["Acme"].matched) == (3, 2)  # PM intern + strategy intern w/ PM duties
    assert by_board["Globex"].matched == 1                                  # Product Analyst (Intern commitment)
    assert by_board["Initech"].matched == 1

    opps = {o["job_title"]: o for o in opp_repo.list_opportunities(conn)}
    assert set(opps) == {"Product Management Intern", "Strategy Intern", "Product Analyst", "APM Intern"}
    assert opps["Product Management Intern"]["company_domain"] == "acme.in"
    assert opps["Product Management Intern"]["description"].startswith("Own the roadmap")
    assert opps["APM Intern"]["market"] == "international"   # 'Remote' alone -> board hint
    assert opps["APM Intern"]["compensation"] == "paid"
    assert opps["Product Analyst"]["market"] == "international"

    # Second run within 6 hours is skipped; forced run merges instead of duplicating.
    again = check_all_boards(conn, http=http)
    assert all(b.skipped_recent for b in again.boards)
    forced = check_all_boards(conn, http=http, force=True)
    assert sum(b.created for b in forced.boards) == 0
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 4


def test_board_error_is_recorded(conn):
    opp_repo.add_career_board(conn, company_name="Acme", ats="greenhouse", slug="acme")
    summary = check_all_boards(conn, http=FakeHttp({"greenhouse": RuntimeError("404 Not Found")}))
    assert summary.boards[0].error
    assert opp_repo.list_career_boards(conn)[0]["last_result"].startswith("error")
