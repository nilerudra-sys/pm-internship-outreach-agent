from database import opportunities as opp_repo
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead


def _lead(**kw):
    base = dict(company_name="Acme Technologies Pvt Ltd", job_title="Product Management Intern",
                source="linkedin", job_url="https://www.linkedin.com/jobs/view/pm-intern-at-acme-4011112222/?trk=x",
                location="Bengaluru, India", discovered_at="2026-11-10")
    base.update(kw)
    return Lead(**base)


def test_new_lead_is_inserted_with_source(conn):
    res = ingest_lead(conn, _lead())
    assert res.created
    opp = opp_repo.get_opportunity(conn, res.opportunity_id)
    assert opp["title_category"] == "core_pm"
    assert opp["market"] == "india"
    assert opp["job_url_norm"] == "linkedin.com/jobs/view/4011112222"
    srcs = opp_repo.sources_for(conn, [res.opportunity_id])[res.opportunity_id]
    assert [s["source"] for s in srcs] == ["linkedin"]


def test_same_job_url_merges(conn):
    first = ingest_lead(conn, _lead())
    second = ingest_lead(conn, _lead(source="google",
                                     job_url="https://in.linkedin.com/jobs/view/4011112222",
                                     description="Own the roadmap and run user research."))
    assert not second.created and second.opportunity_id == first.opportunity_id
    assert "description" in second.filled_fields
    srcs = opp_repo.sources_for(conn, [first.opportunity_id])[first.opportunity_id]
    # Both original URLs are preserved.
    assert {s["url"] for s in srcs} == {
        "https://www.linkedin.com/jobs/view/pm-intern-at-acme-4011112222/?trk=x",
        "https://in.linkedin.com/jobs/view/4011112222",
    }
    assert conn.execute("SELECT COUNT(*) FROM opportunities").fetchone()[0] == 1


def test_same_company_name_and_title_merges_across_sites(conn):
    first = ingest_lead(conn, _lead())
    second = ingest_lead(conn, _lead(company_name="ACME Technologies", source="wellfound",
                                     job_url="https://wellfound.com/jobs/3123456-product-management-intern",
                                     job_title="Product Management Internship"))
    assert second.opportunity_id == first.opportunity_id


def test_same_domain_and_title_merges(conn):
    first = ingest_lead(conn, _lead(company_name="Acme", website="https://acme.io", job_url=None))
    second = ingest_lead(conn, _lead(company_name="Acme Labs (India)", website="https://www.acme.io/about",
                                     job_url="https://careers.acme.io/pm-intern", source="career_page"))
    assert second.opportunity_id == first.opportunity_id
    opp = opp_repo.get_opportunity(conn, first.opportunity_id)
    assert opp["job_url"] == "https://careers.acme.io/pm-intern"  # filled because it was empty


def test_different_role_same_company_is_separate(conn):
    a = ingest_lead(conn, _lead())
    b = ingest_lead(conn, _lead(job_title="Product Analyst Intern", job_url="https://acme.io/jobs/analyst"))
    assert a.opportunity_id != b.opportunity_id


def test_merge_never_overwrites(conn):
    first = ingest_lead(conn, _lead(location="Bengaluru, India", stipend="₹20,000/month"))
    ingest_lead(conn, _lead(location="Mumbai, India", stipend="₹5,000/month", source="google"))
    opp = opp_repo.get_opportunity(conn, first.opportunity_id)
    assert opp["location"] == "Bengaluru, India" and opp["stipend"] == "₹20,000/month"


def test_repeat_of_same_source_url_is_not_double_counted(conn):
    first = ingest_lead(conn, _lead())
    again = ingest_lead(conn, _lead())
    assert not again.created and not again.new_source
    assert len(opp_repo.sources_for(conn, [first.opportunity_id])[first.opportunity_id]) == 1


def test_filters(conn):
    ingest_lead(conn, _lead())
    ingest_lead(conn, _lead(company_name="Globex", job_title="Founder's Office Intern",
                            job_url="https://globex.com/jobs/fo", location="Berlin, Germany",
                            source="csv", discovered_at="2026-10-01"))
    assert len(opp_repo.list_opportunities(conn, sources=["csv"])) == 1
    assert len(opp_repo.list_opportunities(conn, countries=["Germany"])) == 1
    assert len(opp_repo.list_opportunities(conn, categories=["adjacent"])) == 1
    assert len(opp_repo.list_opportunities(conn, date_from="2026-11-01")) == 1
    assert len(opp_repo.list_opportunities(conn, remote_statuses=["remote"])) == 0
    assert len(opp_repo.list_opportunities(conn, min_score=50, include_unscored=False)) == 0
    assert "Germany" in opp_repo.distinct_countries(conn)
