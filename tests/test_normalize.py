import pytest

from services.discovery.normalize import (
    Lead,
    clean_text,
    detect_compensation,
    detect_market,
    detect_remote,
    is_job_board,
    normalize_company_name,
    normalize_job_url,
    normalize_title,
    registrable_domain,
    sector_for_industry,
)


@pytest.mark.parametrize("raw, expected", [
    ("Razorpay Software Pvt. Ltd.", "razorpay software"),
    ("Acme, Inc.", "acme"),
    ("Zeta & Co", "zeta"),
    ("  CRED  ", "cred"),
    ("Navi Technologies Private Limited", "navi technologies"),
])
def test_company_name(raw, expected):
    assert normalize_company_name(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("Product Management Internship (Remote) - 2026", "product management intern"),
    ("Founder's Office Intern", "founders office intern"),
    ("APM Intern | Summer 2026", "apm intern summer"),
])
def test_title(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("https://www.linkedin.com/jobs/view/product-intern-at-acme-4012345678/?trk=abc&refId=x",
     "linkedin.com/jobs/view/4012345678"),
    ("https://in.linkedin.com/jobs/view/4012345678", "linkedin.com/jobs/view/4012345678"),
    ("https://www.linkedin.com/jobs/search/?currentJobId=4012345678&keywords=pm",
     "linkedin.com/jobs/view/4012345678"),
    ("https://wellfound.com/jobs/3123456-product-intern?utm_source=x", "wellfound.com/jobs/3123456"),
    ("https://jobs.lever.co/Acme/ABC-123/?lever-source=LinkedIn", "jobs.lever.co/acme/abc-123"),
    ("https://acme.com/careers/pm-intern/?utm_campaign=x&id=7#apply", "acme.com/careers/pm-intern?id=7"),
    ("", None),
    ("not a url", None),
])
def test_job_url(raw, expected):
    assert normalize_job_url(raw) == expected


def test_job_board_and_domain():
    assert is_job_board("https://in.linkedin.com/jobs/view/1")
    assert is_job_board("boards.greenhouse.io")
    assert not is_job_board("https://careers.acme.in/jobs")
    assert registrable_domain("https://careers.acme.co.in/x") == "acme.co.in"
    assert registrable_domain("https://jobs.example.com") == "example.com"


@pytest.mark.parametrize("location, market, country", [
    ("Bengaluru, Karnataka", "india", "India"),
    ("Remote - India", "india", "India"),
    ("Gurugram", "india", "India"),
    ("Berlin, Germany", "international", "Germany"),
    ("Remote", "unknown", None),
    ("Anywhere", "unknown", None),
    ("", "unknown", None),
    ("San Francisco, CA (Remote)", "international", None),
])
def test_market(location, market, country):
    assert detect_market(location) == (market, country)


def test_remote_and_compensation():
    assert detect_remote("Remote (India)") == "remote"
    assert detect_remote("Hybrid - Pune") == "hybrid"
    assert detect_remote("On-site, Mumbai") == "onsite"
    assert detect_remote("Mumbai") == "unknown"
    assert detect_compensation("Stipend: ₹15,000/month") == "paid"
    assert detect_compensation("This is an unpaid internship") == "unpaid"
    assert detect_compensation("Great learning opportunity") == "unknown"


def test_clean_text_strips_html():
    assert clean_text("&lt;p&gt;Own the &lt;b&gt;roadmap&lt;/b&gt;&lt;/p&gt;") == "Own the roadmap"


def test_lead_derives_fields():
    lead = Lead(company_name="Acme Pvt Ltd", job_title="Product Intern", source="linkedin",
                job_url="https://www.linkedin.com/jobs/view/123456789", location="Remote, India",
                stipend="₹10k/month")
    assert lead.market == "india" and lead.country == "India"
    assert lead.remote_status == "remote"
    assert lead.company_domain is None  # LinkedIn is not the employer's domain
    assert lead.compensation == "paid"
    assert lead.company_name_norm == "acme"


def test_lead_domain_from_careers_url():
    lead = Lead(company_name="Acme", job_title="APM Intern", source="career_page",
                job_url="https://careers.acme.io/apm-intern")
    assert lead.company_domain == "acme.io"


def test_lead_requires_fields():
    with pytest.raises(ValueError):
        Lead(company_name="", job_title="PM Intern", source="csv")
    with pytest.raises(ValueError):
        Lead(company_name="Acme", job_title="PM Intern", source="indeed")


@pytest.mark.parametrize("industry, sector", [
    ("Retail", "Consumer Tech"), ("AI and ML", "AI"), ("Banking and financial services", "Fintech"),
    ("Developer tools", "SaaS"), ("Logistics", "Other"), (None, "Other"),
])
def test_sector_for_industry(industry, sector):
    assert sector_for_industry(industry) == sector
