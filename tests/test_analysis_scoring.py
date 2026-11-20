import json

import pytest

from database import opportunities as opp_repo
from services.discovery.analysis import (
    OpportunityAnalysis,
    SubScores,
    analyse_opportunity,
    analyse_pending,
    build_prompt,
)
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead
from services.discovery.scoring import final_score, mix_by_market, qualify, weighted_score

DESC = ("Acme builds payroll software for SMEs. As a Product Intern you will write PRDs, run user "
        "interviews, prioritise the roadmap with engineering, and track product analytics. "
        "Stipend ₹20,000 per month. Duration 6 months. Open to students; no experience required.")


class FakeGemini:
    model = "fake-gemini"

    def __init__(self, result=None, error=None):
        self.result, self.error, self.prompts = result, error, []

    def generate_json(self, prompt, schema, *, system=None, temperature=0.1):
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        return self.result


def make_result(**kw):
    data = dict(
        role_category="core_pm", is_internship="yes", industry="SaaS", company_product="Payroll software for SMEs",
        location="Bengaluru", country="India", remote_status="remote", india_eligible="yes",
        stipend="₹20,000 per month", compensation="paid", duration="6 months",
        experience_required="None", product_responsibilities=["Write PRDs", "Run user interviews"],
        scores=SubScores(product_responsibilities=9, internship_eligibility=9, engineering_fit=8,
                         location_fit=10, experience_fit=10),
        insufficient_information=False, reasoning="Core PM duties are listed.",
    )
    data.update(kw)
    return OpportunityAnalysis(**data)


def _opp(conn, **kw):
    base = dict(company_name="Acme", job_title="Product Intern", source="csv",
                job_url="https://acme.io/jobs/1", description=DESC)
    base.update(kw)
    res = ingest_lead(conn, Lead(**base))
    return opp_repo.get_opportunity(conn, res.opportunity_id)


# ------------------------------------------------------------------ scoring


def test_weighted_score_bounds():
    assert weighted_score({k: 10 for k in ("product_responsibilities", "internship_eligibility",
                                            "engineering_fit", "location_fit", "experience_fit")}) == 100
    assert weighted_score({}) == 0
    assert weighted_score({"product_responsibilities": 10}) == 35


@pytest.mark.parametrize("kwargs, max_score", [
    (dict(role_category="not_relevant", is_internship="yes", market="india", india_eligible="yes"), 20),
    (dict(role_category="core_pm", is_internship="no", market="india", india_eligible="yes"), 30),
    (dict(role_category="core_pm", is_internship="yes", market="international", india_eligible="no"), 25),
    (dict(role_category="core_pm", is_internship="yes", market="international", india_eligible="unknown"), 70),
    (dict(role_category="core_pm", is_internship="yes", market="india", india_eligible="yes"), 100),
])
def test_caps(kwargs, max_score):
    full = {k: 10 for k in ("product_responsibilities", "internship_eligibility", "engineering_fit",
                            "location_fit", "experience_fit")}
    score, _ = final_score(full, **kwargs)
    assert score == max_score


def test_adjacent_needs_product_duties():
    sub = {"product_responsibilities": 3, "internship_eligibility": 10, "engineering_fit": 10,
           "location_fit": 10, "experience_fit": 10}
    score, notes = final_score(sub, role_category="adjacent", is_internship="yes", market="india",
                               india_eligible="yes")
    assert score == 40 and notes


def test_qualify():
    assert qualify(75, "core_pm", False)[0] == "qualified"
    assert qualify(59, "core_pm", False)[0] == "rejected"
    assert qualify(90, "not_relevant", False)[0] == "rejected"
    assert qualify(90, "core_pm", True)[0] == "new"


def test_mix_by_market_80_20():
    items = [{"market": "india", "n": i} for i in range(20)] + [{"market": "international", "n": i} for i in range(20)]
    ordered = mix_by_market(items, 0.8)
    first10 = [i["market"] for i in ordered[:10]]
    assert first10.count("india") == 8
    assert len(ordered) == 40


# ------------------------------------------------------------------ analysis


def test_prompt_contains_listing_and_rules(conn):
    opp = _opp(conn)
    prompt = build_prompt(opp, profile="CS student")
    assert "Do not invent" in prompt and "Product Intern" in prompt and "payroll software" in prompt
    assert "{" not in prompt.split("CANDIDATE PROFILE")[1]  # every placeholder filled


def test_analysis_qualifies_and_fills_only_grounded_fields(conn):
    opp = _opp(conn)
    fake = FakeGemini(make_result(location="Bengaluru", duration="6 months"))
    out = analyse_opportunity(conn, opp, fake, threshold=60, profile="CS student")
    # 0.35*9 + 0.20*9 + 0.15*8 + 0.20*10 + 0.10*10 = 9.15 -> 92
    assert out["status"] == "qualified" and out["relevance_score"] == 92
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert saved["role_category"] == "core_pm"
    assert saved["duration"] == "6 months"           # appears in the description
    assert saved["stipend"] == "₹20,000 per month"   # appears in the description
    assert saved["location"] is None                 # "Bengaluru" is not in the listing text -> not invented
    assert saved["country"] is None
    assert saved["industry"] == "SaaS"
    assert json.loads(saved["product_responsibilities"]) == ["Write PRDs", "Run user interviews"]
    assert saved["analysis_model"] == "fake-gemini"


def test_analysis_never_overwrites_source_fields(conn):
    opp = _opp(conn, location="Pune, India", remote_status="onsite", stipend="₹15,000/month")
    analyse_opportunity(conn, opp, FakeGemini(make_result()), profile="x")
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert saved["location"] == "Pune, India"
    assert saved["remote_status"] == "onsite"
    assert saved["stipend"] == "₹15,000/month"
    assert saved["company_name"] == "Acme" and saved["job_url"] == "https://acme.io/jobs/1"


def test_not_relevant_is_rejected(conn):
    opp = _opp(conn, job_title="Product Development Intern")
    analyse_opportunity(conn, opp, FakeGemini(make_result(role_category="not_relevant")), profile="x")
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert saved["status"] == "rejected" and saved["relevance_score"] <= 20


def test_international_without_india_eligibility_rejected(conn):
    opp = _opp(conn, location="Berlin, Germany")
    analyse_opportunity(conn, opp, FakeGemini(make_result(india_eligible="no")), profile="x")
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert saved["market"] == "international" and saved["status"] == "rejected"


def test_missing_description_is_skipped_without_calling_gemini(conn):
    opp = _opp(conn, description=None)
    fake = FakeGemini(make_result())
    assert analyse_opportunity(conn, opp, fake) == {"skipped": True}
    assert fake.prompts == []
    assert opp_repo.get_opportunity(conn, opp["id"])["status"] == "new"


def test_gemini_error_is_recorded(conn):
    opp = _opp(conn)
    analyse_opportunity(conn, opp, FakeGemini(error=RuntimeError("quota exceeded")), profile="x")
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert "quota exceeded" in saved["analysis_error"] and saved["analyzed_at"] is None


def test_manual_status_survives_reanalysis(conn):
    opp = _opp(conn)
    opp_repo.set_status(conn, opp["id"], "qualified", "I like it")
    analyse_opportunity(conn, opp, FakeGemini(make_result(role_category="not_relevant")), profile="x")
    saved = opp_repo.get_opportunity(conn, opp["id"])
    assert saved["status"] == "qualified" and saved["role_category"] == "not_relevant"


def test_analyse_pending_india_first(conn):
    intl = _opp(conn, company_name="Globex", job_url="https://globex.com/j", location="London, UK")
    india = _opp(conn, company_name="Initech", job_url="https://initech.in/j", location="Mumbai")
    fake = FakeGemini(make_result())
    summary = analyse_pending(conn, fake, limit=1)
    assert summary["analysed"] == 1
    assert opp_repo.get_opportunity(conn, india["id"])["analyzed_at"] is not None
    assert opp_repo.get_opportunity(conn, intl["id"])["analyzed_at"] is None


def test_schema_rejects_bad_category():
    with pytest.raises(Exception):
        make_result(role_category="ai_only")
