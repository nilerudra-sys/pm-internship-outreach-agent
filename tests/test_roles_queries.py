from urllib.parse import parse_qs, urlparse

import pytest

from services.discovery import queries as q
from services.discovery.roles import (
    ADJACENT_ROLES,
    ALL_ROLES,
    CORE_ROLES,
    INDUSTRIES,
    RELATED_ROLES,
    classify_title,
    is_candidate_listing,
    is_internship,
    product_signal_count,
)


@pytest.mark.parametrize("role", CORE_ROLES)
def test_core_titles(role):
    assert classify_title(role) == "core_pm"


@pytest.mark.parametrize("role", RELATED_ROLES)
def test_related_titles(role):
    assert classify_title(role) == "related_product"


@pytest.mark.parametrize("role", ADJACENT_ROLES)
def test_adjacent_titles(role):
    assert classify_title(role) == "adjacent"


@pytest.mark.parametrize("title, expected", [
    ("Software Engineering Intern", "unmatched"),
    ("Associate Product Manager - Intern (2026)", "core_pm"),
    ("Intern, Product Analytics", "related_product"),
    ("Founders Office Internship", "adjacent"),
    ("Marketing Intern", "unmatched"),
])
def test_title_variants(title, expected):
    assert classify_title(title) == expected


def test_internship_detection():
    assert is_internship("Product Trainee")
    assert is_internship("Product Manager", employment_type="Intern")
    assert not is_internship("Senior Product Manager")


def test_candidate_listing_uses_responsibilities():
    desc = "You will own the roadmap, run user research and write PRDs with A/B test plans."
    assert product_signal_count(desc) >= 3
    # Non-product title but product responsibilities -> kept.
    assert is_candidate_listing("Strategy Intern", desc)
    # Non-product title, no product work -> dropped.
    assert not is_candidate_listing("Sales Intern", "Cold calling and lead generation.")
    # Not an internship -> dropped even with a product title.
    assert not is_candidate_listing("Senior Product Manager", desc)
    # Other functions with generic overlap are dropped (live Lever data showed these slipping through).
    eng_desc = "Ship features, write specs, work with stakeholders in sprints, own the roadmap, user research."
    assert not is_candidate_listing("Forward Deployed Software Engineer, Internship", eng_desc)
    assert not is_candidate_listing("Product Designer, Internship", eng_desc)
    assert not is_candidate_listing("Applied Scientist Intern", eng_desc)
    # Generic engineering words alone do not count as product signals.
    assert product_signal_count("Build features, write specs, join sprints, talk to stakeholders.") == 0


def test_all_22_roles_present():
    assert len(ALL_ROLES) == 22 and len(set(ALL_ROLES)) == 22
    assert len(INDUSTRIES) == 15


def test_google_queries_cover_every_role_and_industry():
    queries = q.google_queries()
    roles_searched = {x.role for x in queries if x.role}
    assert roles_searched == set(ALL_ROLES)
    for role in ALL_ROLES:
        intents = {x.intent for x in queries if x.role == role}
        assert {"India", "Remote (India)", "Remote, accepts India"} <= intents, role
    industry_text = " ".join(x.text for x in queries if x.category == "industry")
    for industry in INDUSTRIES:
        assert industry in industry_text
    # No query restricts the search to AI.
    assert not any("AI" in x.text.split() for x in queries if x.category != "industry")


def test_google_url_has_recency():
    url = q.google_url('"Product Intern" India', "Past week")
    params = parse_qs(urlparse(url).query)
    assert params["q"] == ['"Product Intern" India'] and params["tbs"] == ["qdr:w"]
    assert "tbs" not in parse_qs(urlparse(q.google_url("x", "Any time")).query)


def test_linkedin_urls_are_internship_filtered():
    queries = q.linkedin_queries("Past week")
    assert {x.role for x in queries} == set(ALL_ROLES)
    for x in queries:
        params = parse_qs(urlparse(x.url).query)
        assert params["f_E"] == ["1"]
        assert params["f_TPR"] == ["r604800"]
        if x.market == "international":
            assert params["location"] == ["Worldwide"] and params["f_WT"] == ["2"]


def test_wellfound_queries():
    queries = q.wellfound_queries()
    urls = [x.url for x in queries]
    assert "https://wellfound.com/role/l/product-manager/india" in urls
    assert "https://wellfound.com/role/r/product-manager" in urls
    assert any(x.category == "company" for x in queries)
    assert {x.role for x in queries if x.role} == set(ALL_ROLES)


@pytest.mark.parametrize("platform", q.PLATFORMS)
def test_plan_keeps_80_20_and_spreads_roles(platform):
    queries = q.all_queries(platform)
    plan = q.plan_queries(queries, 50, 0.8)
    india = sum(1 for x in plan if x.market == "india")
    assert len(plan) == 50
    assert 38 <= india <= 42
    if platform != "wellfound":
        # Round-robin: the first 22 India queries are 22 different roles/groups.
        india_roles = [x.role or x.intent for x in plan if x.market == "india"][:22]
        assert len(set(india_roles)) == len(india_roles)


def test_plan_handles_one_sided_pool():
    queries = [x for x in q.google_queries() if x.market == "international"]
    assert len(q.plan_queries(queries, 10)) == 10
