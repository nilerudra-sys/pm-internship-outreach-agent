from types import SimpleNamespace

import httpx2 as httpx  # the HTTP library google-genai 3.x uses
import pytest
from google.genai import errors
from pydantic import BaseModel

from integrations.gemini_client import GeminiClient, GeminiError
from integrations.http import PoliteSession

PRIMARY, FALLBACK = "primary-model", "fallback-model"


class Out(BaseModel):
    value: int


def api_error(code, status="UNAVAILABLE", retry_delay=None):
    body = {"error": {"code": code, "message": f"{status} message", "status": status}}
    if retry_delay:
        body["error"]["details"] = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}]
    return errors.APIError(code, body)


class FakeModels:
    """Scripted responses per model; records which models were called, in order."""

    def __init__(self, script):
        self.script = {m: list(r) for m, r in script.items()}
        self.calls = []

    def generate_content(self, model, contents, config):
        self.calls.append(model)
        r = self.script[model].pop(0)
        if isinstance(r, Exception):
            raise r
        return SimpleNamespace(text=r)


def make(script, fallback=FALLBACK, max_retries=1):
    sleeps = []
    models = FakeModels(script)
    client = GeminiClient("", PRIMARY, min_interval=0, max_retries=max_retries, fallback_model=fallback,
                          client=SimpleNamespace(models=models), sleep=sleeps.append)
    return client, models, sleeps


# ------------------------------------------------------------------ basics


def test_structured_output_config():
    client, models, _ = make({PRIMARY: ['{"value": 7}']})
    assert client.generate_json("p", Out).value == 7
    assert client.last_model_used == PRIMARY


def test_requires_api_key():
    with pytest.raises(GeminiError):
        GeminiClient("", "m")


def test_fallback_equal_to_primary_is_ignored():
    client, _, _ = make({PRIMARY: []}, fallback=PRIMARY)
    assert client.fallback_model is None


# ------------------------------------------------------------------ primary / fallback


def test_primary_success_does_not_touch_fallback():
    client, models, _ = make({PRIMARY: ['{"value": 1}'], FALLBACK: []})
    assert client.generate_json("p", Out).value == 1
    assert models.calls == [PRIMARY]
    assert client.successes == {PRIMARY: 1}


def test_primary_recovers_after_one_retry():
    client, models, sleeps = make({PRIMARY: [api_error(503), '{"value": 2}'], FALLBACK: []})
    assert client.generate_json("p", Out).value == 2
    assert models.calls == [PRIMARY, PRIMARY]
    assert client.last_model_used == PRIMARY
    assert sleeps == [3.0]


def test_fallback_after_primary_overloaded():
    client, models, _ = make({PRIMARY: [api_error(503), api_error(503)], FALLBACK: ['{"value": 3}']})
    assert client.generate_json("p", Out).value == 3
    assert models.calls == [PRIMARY, PRIMARY, FALLBACK]   # 1 retry, then switch: 3 calls total
    assert client.last_model_used == FALLBACK


def test_fallback_after_rate_limit_honours_retry_delay_with_cap():
    client, models, sleeps = make({
        PRIMARY: [api_error(429, "RESOURCE_EXHAUSTED", "40s"), api_error(429, "RESOURCE_EXHAUSTED")],
        FALLBACK: ['{"value": 4}'],
    })
    assert client.generate_json("p", Out).value == 4
    assert sleeps == [15.0]   # server asked for 40s; capped
    assert client.last_model_used == FALLBACK


def test_network_timeout_is_transient():
    client, models, _ = make({PRIMARY: [httpx.ReadTimeout("slow"), httpx.ConnectError("down")],
                              FALLBACK: ['{"value": 5}']})
    assert client.generate_json("p", Out).value == 5
    assert models.calls == [PRIMARY, PRIMARY, FALLBACK]


def test_overloaded_primary_is_skipped_for_next_requests():
    client, models, _ = make({PRIMARY: [api_error(503), api_error(503), '{"value": 9}'],
                              FALLBACK: ['{"value": 6}', '{"value": 7}']})
    client.generate_json("p", Out)
    client.generate_json("p", Out)                 # within cooldown: straight to fallback
    assert models.calls == [PRIMARY, PRIMARY, FALLBACK, FALLBACK]
    client._primary_down_until = 0                 # cooldown over: primary is tried again
    assert client.generate_json("p", Out).value == 9
    assert client.last_model_used == PRIMARY


def test_both_models_failing():
    client, models, _ = make({PRIMARY: [api_error(503)] * 2, FALLBACK: [api_error(503)] * 2})
    with pytest.raises(GeminiError) as exc:
        client.generate_json("p", Out)
    assert models.calls == [PRIMARY, PRIMARY, FALLBACK, FALLBACK]   # bounded: 4 calls
    assert PRIMARY in str(exc.value) and FALLBACK in str(exc.value)
    assert client.last_model_used is None


def test_no_fallback_configured():
    client, models, _ = make({PRIMARY: [api_error(503)] * 2}, fallback=None)
    with pytest.raises(GeminiError):
        client.generate_json("p", Out)
    assert models.calls == [PRIMARY, PRIMARY]


# ------------------------------------------------------------------ non-transient: no fallback


@pytest.mark.parametrize("code, status", [
    (401, "UNAUTHENTICATED"), (403, "PERMISSION_DENIED"), (400, "INVALID_ARGUMENT"), (404, "NOT_FOUND"),
])
def test_non_transient_errors_fail_fast_without_fallback(code, status):
    client, models, sleeps = make({PRIMARY: [api_error(code, status)], FALLBACK: ['{"value": 1}']})
    with pytest.raises(GeminiError) as exc:
        client.generate_json("p", Out)
    assert models.calls == [PRIMARY]
    assert sleeps == []
    if code in (401, 403):
        assert "GEMINI_API_KEY" in str(exc.value)


def test_malformed_output_retries_once_then_fails_without_fallback():
    client, models, _ = make({PRIMARY: ["not json", '{"value": "x"}'], FALLBACK: ['{"value": 1}']})
    with pytest.raises(GeminiError):
        client.generate_json("p", Out)
    assert models.calls == [PRIMARY, PRIMARY]


def test_malformed_then_valid():
    client, models, _ = make({PRIMARY: ["not json", '{"value": 8}']})
    assert client.generate_json("p", Out).value == 8


# ------------------------------------------------------------------ analysis records the model


def test_analysis_records_fallback_model(conn):
    from database import opportunities as opp_repo
    from services.discovery.analysis import analyse_opportunity
    from services.discovery.leads import ingest_lead
    from services.discovery.normalize import Lead

    desc = "Write PRDs, run user interviews and own the roadmap. Stipend 10000 per month. 3 months. Students."
    opp_id = ingest_lead(conn, Lead(company_name="Acme", job_title="Product Intern", source="csv",
                                    job_url="https://acme.io/j", description=desc)).opportunity_id
    payload = ('{"role_category":"core_pm","is_internship":"yes","remote_status":"unknown","india_eligible":"unknown",'
               '"compensation":"paid","product_responsibilities":["Write PRDs"],"scores":{"product_responsibilities":9,'
               '"internship_eligibility":9,"engineering_fit":7,"location_fit":6,"experience_fit":10},'
               '"insufficient_information":false,"reasoning":"PM duties listed."}')
    client, _, _ = make({PRIMARY: [api_error(503), api_error(503)], FALLBACK: [payload]})
    out = analyse_opportunity(conn, opp_repo.get_opportunity(conn, opp_id), client, profile="x")
    assert out["model"] == FALLBACK
    assert opp_repo.get_opportunity(conn, opp_id)["analysis_model"] == FALLBACK
    log = conn.execute("SELECT details FROM activity_log WHERE action = 'analysed'").fetchone()[0]
    assert f"model {FALLBACK}" in log


# ------------------------------------------------------------------ http session (unchanged)


class FakeResp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code, self.payload, self.headers = status, payload, headers or {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_polite_session_honours_retry_after():
    sleeps = []
    responses = [FakeResp(429, headers={"Retry-After": "3"}), FakeResp(200, {"ok": True})]
    session = SimpleNamespace(headers={}, get=lambda url, params=None, timeout=None: responses.pop(0))
    http = PoliteSession(min_interval=0, session=session, sleep=sleeps.append)
    assert http.get_json("https://api.lever.co/x") == {"ok": True}
    assert 3 in sleeps
    assert "PMInternshipOutreach" in session.headers["User-Agent"]
