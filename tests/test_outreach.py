import json
import sqlite3

import pytest

from agents.writer_agent import WriterAgent
from config.settings import Settings
from database import contacts as contacts_repo
from database import repository
from database import research as research_repo
from database.db import connect, init_db
from services.discovery.leads import ingest_lead
from services.discovery.normalize import Lead
from services.outreach.checks import check_draft
from services.outreach.writer import (
    EmailDraft,
    build_material,
    build_prompt,
    draft_candidates,
    draft_for_contact,
    recheck_email,
)
from services.contacts.outreach_context import get_outreach_context

FACTS = {
    "outreach_hooks": [{"text": "Drivers get paid within 24 hours of delivery", "quote": "Drivers get paid within 24 hours.",
                        "url": "https://demofreight.in/"}],
    "product_facts": [{"text": "A mobile app matches 20,000 truck drivers with shippers in Pune and Mumbai",
                       "quote": "Our app matches 20,000 truck drivers with shippers in Pune and Mumbai.",
                       "url": "https://demofreight.in/"}],
}

FIRST_PARA = (
    "Paying truck drivers within 24 hours of delivery stood out to me, especially with Demo Freight's app now "
    "matching 20,000 drivers with shippers in Pune and Mumbai. Fast, predictable payouts feel like the kind of "
    "product decision that keeps a two-sided marketplace healthy."
)
GOOD_BODY = (
    "Hi Asha,\n\n" + FIRST_PARA + "\n\n"
    "I built and launched SignalAI, and I study Cloud Computing as part of my "
    "B.Tech in Computer Science, so I enjoy thinking about how a product works end to end.\n\n"
    "Would you consider a product intern on your team this year? I would be grateful for a short 15-minute call "
    "whenever convenient.\n\n"
    "Best regards,\nRiya Sharma"
)


class FakeGemini:
    model = "fake-primary"

    def __init__(self, drafts):
        self.drafts, self.prompts, self.last_model_used = list(drafts), [], None

    def generate_json(self, prompt, schema, *, system=None, temperature=0.1):
        self.prompts.append(prompt)
        d = self.drafts.pop(0)
        if isinstance(d, Exception):
            raise d
        self.last_model_used = "fake-fallback"
        return d


def good_draft(**kw):
    data = dict(subject="Product internship idea for Demo Freight", body=GOOD_BODY, facts_used=["F1", "F2"],
                projects_mentioned=["SignalAI"])
    data.update(kw)
    return EmailDraft(**data)


def make_ready_company(conn, name="Demo Freight", domain="demofreight.in", market="india", email=True,
                       facts=FACTS):
    cid = repository.add_company(conn, name=name, domain=domain, market=market, funding_stage="Seed")
    research_repo.save_research(conn, cid, {"summary": "INTERPRETATION ONLY: Series D unicorn", "facts": facts,
                                            "verified_summary": "x", "researched_at": "2026-11-17T00:00:00+00:00"})
    contact_id, _ = contacts_repo.upsert_contact(
        conn, cid, full_name="Asha Rao", role_title="Co-founder & CEO", role_category="cofounder", source="website",
        source_url=f"https://{domain}/team", evidence="Asha Rao — Co-founder & CEO",
        email=f"asha@{domain}" if email else None, email_type="public_verified" if email else "none")
    contacts_repo.rescore_company(conn, cid)
    return cid, contact_id


def material_for(conn, contact_id, signature="Riya Sharma"):
    return build_material(get_outreach_context(conn, contact_id), signature)


# ------------------------------------------------------------------ material + prompt


def test_material_uses_only_verified_data(conn):
    _, contact_id = make_ready_company(conn)
    m = material_for(conn, contact_id)
    assert list(m["facts"]) == ["F1", "F2"] and m["facts"]["F1"]["category"] == "outreach_hooks"
    prompt = build_prompt(m)
    assert "[F1] Drivers get paid within 24 hours of delivery" in prompt
    assert "INTERPRETATION ONLY" not in prompt and "Series D" not in prompt     # Gemini interpretation excluded
    assert "SPECULATIVE" in prompt and "Do not mention or imply an opening" in prompt
    assert "First name: Asha" in prompt and "SignalAI" in prompt and "Riya Sharma" in prompt
    assert "{" not in prompt.split("RECIPIENT")[1]


def test_vacancy_prompt_mentions_listing(conn):
    cid, contact_id = make_ready_company(conn)
    ingest_lead(conn, Lead(company_name="Demo Freight", job_title="Product Intern", source="linkedin",
                           job_url="https://www.linkedin.com/jobs/view/4000000011", location="Pune, India"))
    m = material_for(conn, contact_id)
    assert m["outreach_type"] == "vacancy"
    p = build_prompt(m)
    assert "VACANCY" in p and "Title: Product Intern" in p and "linkedin.com/jobs/view/4000000011" in p


# ------------------------------------------------------------------ checks


def test_good_draft_passes(conn):
    _, contact_id = make_ready_company(conn)
    c = check_draft("Product internship idea for Demo Freight", GOOD_BODY, ["F1", "F2"], material_for(conn, contact_id))
    assert c.ok, c.errors
    assert c.facts_verified == ["F1", "F2"]


@pytest.mark.parametrize("change, expected", [
    (lambda b: b.replace("20,000", "50,000"), "number not in the provided facts"),
    (lambda b: b.replace("15-minute", "15-minute") + "\nWe raised $5M recently.", "number not in"),
    (lambda b: b.replace("I built and launched SignalAI", "I built SignalAI, used by Google"), "names not in"),
    (lambda b: b.replace("Hi Asha", "Hi there"), "does not greet Asha"),
    (lambda b: b.replace("Best regards", "Best regards [Your Name]"), "placeholder"),
    (lambda b: "Hi Asha, quick note. Best regards, Riya Sharma", "body has"),
    (lambda b: b.replace("Would you consider a product intern on your team this year?",
                         "I saw your internship posting and would like to apply."), "no internship is advertised"),
])
def test_bad_drafts_fail(conn, change, expected):
    _, contact_id = make_ready_company(conn)
    c = check_draft("Product internship idea", change(GOOD_BODY), ["F1", "F2"], material_for(conn, contact_id))
    assert not c.ok
    assert any(expected in e for e in c.errors), c.errors


def test_draft_must_use_a_verified_fact(conn):
    _, contact_id = make_ready_company(conn)
    body = GOOD_BODY.replace(FIRST_PARA, "Your team clearly cares about the people who use the product, and that "
                                         "matters a lot to a student who wants to learn how products get built.")
    c = check_draft("Hello", body, ["F1"], material_for(conn, contact_id))
    assert "does not use any verified company fact" in c.errors
    assert any("cites unknown fact F9" in e for e in check_draft("Hi", GOOD_BODY, ["F9"], material_for(conn, contact_id)).errors)


# ------------------------------------------------------------------ style


STIFF_BODY = (
    "Dear Asha,\n\n"
    "I am a B.Tech Computer Science student specialising in Cloud Computing, with skills in OpenAI API, Gemini API, "
    "Cloud Computing and Product thinking. I built and launched SignalAI.\n\n"
    "I have followed Demo Freight closely. It is impressive that a mobile app matches 20,000 truck drivers with "
    "shippers in Pune and Mumbai, and drivers get paid within 24 hours of delivery.\n\n"
    "I am writing to ask whether you would consider a product intern. Would you be open to a 15-minute call?\n\n"
    "Best regards,\nRiya Sharma"
)


def test_stiff_cover_letter_style_is_rejected(conn):
    _, contact_id = make_ready_company(conn)
    errors = check_draft("Internship", STIFF_BODY, ["F1", "F2"], material_for(conn, contact_id)).errors
    joined = " | ".join(errors)
    assert "opens with the student" in joined
    assert "words in a row from fact F2; paraphrase it" in joined
    assert "lists 4 skills like a CV" in joined
    assert "“i have followed”" in joined and "“impressive”" in joined and "“i am writing to”" in joined


@pytest.mark.parametrize("phrase", ["I hope this email finds you well.", "I came across your company.",
                                    "I am passionate about products.", "Kindly let me know.",
                                    "I wanted to reach out.", "It would be a great opportunity."])
def test_cliches_are_rejected(conn, phrase):
    _, contact_id = make_ready_company(conn)
    body = GOOD_BODY.replace("whenever convenient.", "whenever convenient. " + phrase)
    assert any("cliché" in e for e in check_draft("x", body, ["F1"], material_for(conn, contact_id)).errors)


def test_inline_greeting_opening_is_detected(conn):
    _, contact_id = make_ready_company(conn)
    body = GOOD_BODY.replace("Hi Asha,\n\nPaying", "Hi Asha, I'm a student. Paying")
    assert any("opens with the student" in e for e in check_draft("x", body, ["F1"], material_for(conn, contact_id)).errors)


@pytest.mark.parametrize("sentence", [
    # Taken from real fallback-model drafts: claims about SignalAI that the profile never makes.
    "My work building SignalAI helped me understand how to structure features for specific user needs.",
    "My experience building SignalAI involves similar technical integration and product thinking.",
    "SignalAI now serves thousands of traders every day.",
    # Real live draft: refers back with "this project" instead of the name.
    "I built and launched SignalAI. Developing this project strengthened my commitment to solving clear user "
    "problems through thoughtful product design.",
])
def test_invented_project_claims_are_rejected(conn, sentence):
    _, contact_id = make_ready_company(conn)
    body = GOOD_BODY.replace("I built and launched SignalAI, and",
                             sentence + " I")
    errors = check_draft("x", body, ["F1"], material_for(conn, contact_id)).errors
    assert any("not in your profile" in e for e in errors), errors


def test_profile_wording_about_projects_is_allowed(conn):
    _, contact_id = make_ready_company(conn)
    c = check_draft("x", GOOD_BODY, ["F1", "F2"], material_for(conn, contact_id))
    assert not [e for e in c.errors if "profile" in e]


@pytest.mark.parametrize("body_change, expected", [
    (lambda b: b.replace("Hi Asha,\n\n", "Asha, "), "start with “Hi Asha,”"),
    (lambda b: b.replace("\n\n", " "), "3 short paragraphs"),
    (lambda b: b.replace("\n\nBest regards,\nRiya Sharma", " Best regards, Riya Sharma"), "own line"),
])
def test_format_rules(conn, body_change, expected):
    _, contact_id = make_ready_company(conn)
    errors = check_draft("x", body_change(GOOD_BODY), ["F1"], material_for(conn, contact_id)).errors
    assert any(expected in e for e in errors), errors


@pytest.mark.parametrize("raw", [
    "Asha, Paying drivers fast matters.\n\nMiddle.\n\nAsk? Best regards,",          # real fallback-model shape
    "Dear Asha,\n\nPaying drivers fast matters.\n\nMiddle.\n\nAsk?\n\nWarm regards,\nRiya Sharma",
    "Hi Asha,\n\nPaying drivers fast matters.\n\nMiddle.\n\nAsk?",
    "Hi there,\n\nPaying drivers fast matters.\n\nMiddle.\n\nAsk?\nBest regards",
])
def test_normalize_format_fixes_greeting_and_signoff_only(raw):
    from services.outreach.writer import normalize_format

    out = normalize_format(raw, "Asha", "Riya Sharma")
    assert out.startswith("Hi Asha,\n\nPaying drivers fast matters.")
    assert out.endswith("\n\nBest regards,\nRiya Sharma")
    assert "Middle." in out and out.count("regards") == 1


def test_rejected_attempts_are_recorded(conn):
    _, contact_id = make_ready_company(conn)
    stiff = good_draft(body=STIFF_BODY)
    out = draft_for_contact(conn, contact_id, FakeGemini([stiff, good_draft()]), "Riya Sharma")
    checks = json.loads(repository.get_email(conn, out.email_id)["checks"])
    assert out.attempts == 2 and len(checks["rejected_attempts"]) == 1
    assert any("opens with the student" in e for e in checks["rejected_attempts"][0])


# ------------------------------------------------------------------ generation + storage


def test_draft_is_stored_pending_review_with_evidence(conn):
    cid, contact_id = make_ready_company(conn)
    out = draft_for_contact(conn, contact_id, FakeGemini([good_draft()]), "Riya Sharma")
    assert out.email_id and out.attempts == 1 and out.model == "fake-fallback"
    e = repository.get_email(conn, out.email_id)
    assert e["status"] == "pending_review" and e["outreach_type"] == "speculative" and e["model"] == "fake-fallback"
    used = json.loads(e["facts_used"])
    assert [f["id"] for f in used] == ["F1", "F2"] and used[0]["quote"] and used[0]["url"]
    assert json.loads(e["checks"])["errors"] == []


def test_failed_check_is_retried_with_feedback(conn):
    _, contact_id = make_ready_company(conn)
    bad = good_draft(body=GOOD_BODY.replace("20,000", "50,000"))
    fake = FakeGemini([bad, good_draft()])
    out = draft_for_contact(conn, contact_id, fake, "Riya Sharma")
    assert out.email_id and out.attempts == 2
    assert "number not in the provided facts or profile: 50,000" in fake.prompts[1]


def test_two_failures_store_nothing(conn):
    _, contact_id = make_ready_company(conn)
    bad = good_draft(body=GOOD_BODY.replace("20,000", "70,000"))
    out = draft_for_contact(conn, contact_id, FakeGemini([bad, bad]), "Riya Sharma")
    assert out.email_id is None and "failed checks twice" in out.error
    assert conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0] == 0


def test_gemini_error_is_reported(conn):
    _, contact_id = make_ready_company(conn)
    out = draft_for_contact(conn, contact_id, FakeGemini([RuntimeError("All Gemini models unavailable")]), None)
    assert out.email_id is None and "unavailable" in out.error


def test_not_ready_contacts_are_skipped_without_calling_gemini(conn):
    _, contact_id = make_ready_company(conn, email=False)
    fake = FakeGemini([good_draft()])
    out = draft_for_contact(conn, contact_id, fake, None)
    assert out.skipped.startswith("not ready") and fake.prompts == []


def test_no_verified_facts_means_no_draft(conn):
    _, contact_id = make_ready_company(conn, facts={})
    out = draft_for_contact(conn, contact_id, FakeGemini([good_draft()]), None)
    assert out.skipped == "no verified company facts to personalise with"


def test_one_active_email_per_company(conn):
    cid, contact_id = make_ready_company(conn)
    draft_for_contact(conn, contact_id, FakeGemini([good_draft()]), "Riya Sharma")
    again = draft_for_contact(conn, contact_id, FakeGemini([good_draft()]), "Riya Sharma")
    assert again.skipped == "company already has a draft or sent email"
    assert draft_candidates(conn) == []


def test_recheck_flags_edits(conn):
    _, contact_id = make_ready_company(conn)
    out = draft_for_contact(conn, contact_id, FakeGemini([good_draft()]), "Riya Sharma")
    e = repository.get_email(conn, out.email_id)
    assert recheck_email(conn, e, "Riya Sharma").ok
    repository.update_email_content(conn, out.email_id, subject=e["subject"],
                                    body=e["body"].replace("20,000", "90,000"))
    assert not recheck_email(conn, repository.get_email(conn, out.email_id), "Riya Sharma").ok


# ------------------------------------------------------------------ agent ordering


def test_writer_agent_vacancy_first_and_india_first(conn):
    ids = {}
    for i in range(5):
        ids[f"in{i}"] = make_ready_company(conn, name=f"India Co {i}", domain=f"indiaco{i}.in")[1]
    for i in range(2):
        ids[f"w{i}"] = make_ready_company(conn, name=f"World Co {i}", domain=f"worldco{i}.com", market="international")[1]
    ingest_lead(conn, Lead(company_name="World Co 1", job_title="Product Intern", source="csv",
                           job_url="https://worldco1.com/jobs/pm", website="https://worldco1.com"))
    agent = WriterAgent(conn, Settings(india_ratio=0.8), FakeGemini([]))
    order = [t["company_name"] for t in agent.targets(7)]
    assert order.index("World Co 1") < order.index("World Co 0")
    assert sum(n.startswith("India") for n in order[:5]) == 4


def test_writer_agent_batch_survives_failures(conn):
    _, a = make_ready_company(conn, name="Alpha Freight", domain="alphafreight.in")
    _, b = make_ready_company(conn, name="Beta Freight", domain="betafreight.in")
    body_b = GOOD_BODY.replace("Demo Freight's", "Beta Freight's")
    fake = FakeGemini([RuntimeError("quota"), good_draft(body=body_b, subject="Product internship idea for Beta Freight")])
    summary, outcomes = WriterAgent(conn, Settings(sender_name="Riya Sharma"), fake).run(limit=5)
    assert summary.processed == 2 and summary.created == 1 and len(summary.errors) == 1


# ------------------------------------------------------------------ migration


def test_v5_emails_gain_draft_columns(tmp_path):
    path = tmp_path / "v5.db"
    init_db(path)
    c = sqlite3.connect(path)
    for col in ("outreach_type", "opportunity_id", "facts_used", "checks"):
        c.execute(f"ALTER TABLE emails DROP COLUMN {col}")
    c.execute("PRAGMA user_version = 5")
    c.commit()
    c.close()
    init_db(path)
    conn = connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    assert {"outreach_type", "opportunity_id", "facts_used", "checks"} <= cols
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 6
