from config.settings import HARD_DAILY_CAP, Settings, load_profile


def test_daily_limit_clamped_to_hard_cap():
    assert Settings(daily_email_limit=50).daily_email_limit == HARD_DAILY_CAP == 15
    assert Settings(daily_email_limit=-3).daily_email_limit == 0
    assert Settings(daily_email_limit=10).daily_email_limit == 10


def test_india_ratio_clamped():
    assert Settings(india_ratio=1.7).india_ratio == 1.0
    assert Settings(india_ratio=-0.2).india_ratio == 0.0


def test_integration_status_without_keys(tmp_path):
    s = Settings(gmail_credentials_file=tmp_path / "none.json", gmail_token_file=tmp_path / "none2.json")
    assert not any(s.integration_status().values())


def test_profile_loads():
    profile = load_profile()
    assert profile["applicant"]["target_role"] == "Product Management Intern"
    assert profile["targeting"]["india_ratio"] == 0.8
    assert {p["name"] for p in profile["applicant"]["projects"]} == {"SignalAI"}
