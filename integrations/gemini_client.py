"""Gemini via the official Google Gen AI SDK (google-genai). The Anthropic API is never used.

Retry policy (keeps API use low):
- Transient errors (429, 500, 502, 503, 504, timeouts, connection errors): retry the same model
  `max_retries` times with a short backoff, then switch to the fallback model.
- After the primary fails over, it is skipped for `primary_cooldown` seconds, so a batch of
  listings does not keep retrying an overloaded model.
- Non-transient errors (400 invalid request, 401/403 auth, 404 unknown model): fail immediately,
  no retry and no fallback, because another model would fail the same way.
- Malformed JSON from the model: one retry on the same model, then fail (no fallback).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections import Counter
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

TRANSIENT_CODES = frozenset({429, 500, 502, 503, 504})


class GeminiError(RuntimeError):
    """Gemini call failed after retries, or returned output that did not match the schema."""


def _network_errors() -> tuple[type[BaseException], ...]:
    """Timeout/connection exception classes of whichever HTTP library google-genai uses
    (httpx2 in google-genai 3.x, httpx in older versions)."""
    found: list[type[BaseException]] = []
    for name in ("httpx2", "httpx"):
        try:
            mod = __import__(name)
        except ImportError:
            continue
        found += [mod.TimeoutException, mod.TransportError]
    return tuple(found)


class _Transient(Exception):
    def __init__(self, label: str, delay: float | None = None):
        super().__init__(label)
        self.label, self.delay = label, delay


class GeminiClient:
    """Structured-JSON wrapper with rate limiting, bounded retries and a fallback model."""

    def __init__(self, api_key: str, model: str, min_interval: float = 6.0, max_retries: int = 1,
                 client=None, fallback_model: str | None = None, timeout: float = 60.0,
                 primary_cooldown: float = 300.0, sleep=None):
        if not api_key and client is None:
            raise GeminiError("GEMINI_API_KEY is not set in .env")
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout * 1000)))
        self._client = client
        self.model = model
        self.fallback_model = fallback_model if fallback_model and fallback_model != model else None
        self.min_interval = min_interval
        self.max_retries = max(0, max_retries)
        self.primary_cooldown = primary_cooldown
        self.last_model_used: str | None = None
        self.calls: Counter[str] = Counter()       # requests sent, per model
        self.successes: Counter[str] = Counter()   # successful results, per model
        self._sleep = sleep or time.sleep
        self._primary_down_until = 0.0
        self._last_call = 0.0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ internals

    def _wait_turn(self) -> None:
        with self._lock:
            delay = self._last_call + self.min_interval - time.monotonic()
            if delay > 0:
                self._sleep(delay)
            self._last_call = time.monotonic()

    def _models_to_try(self) -> list[str]:
        if self.fallback_model and time.monotonic() < self._primary_down_until:
            log.info("Skipping %s (recently unavailable); using %s", self.model, self.fallback_model)
            return [self.fallback_model]
        return [self.model] + ([self.fallback_model] if self.fallback_model else [])

    @staticmethod
    def _retry_delay(exc) -> float | None:
        """Server-suggested delay from a RetryInfo detail, e.g. '13s'."""
        try:
            for d in (exc.details or {}).get("error", {}).get("details", []):
                m = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(d.get("retryDelay", "")))
                if m:
                    return float(m.group(1))
        except AttributeError:
            pass
        return None

    def _call(self, model: str, prompt: str, config):
        from google.genai import errors

        network_errors = _network_errors()
        self._wait_turn()
        self.calls[model] += 1
        try:
            return self._client.models.generate_content(model=model, contents=prompt, config=config)
        except errors.APIError as exc:
            if exc.code in TRANSIENT_CODES:
                raise _Transient(f"{model}: {exc.code} {exc.status or ''}".strip(), self._retry_delay(exc)) from exc
            raise GeminiError(
                f"{model}: {exc.code} {exc.status or ''} {exc.message or ''}".strip()
                + (" (check GEMINI_API_KEY)" if exc.code in (401, 403) else "")
            ) from exc
        except network_errors as exc:
            raise _Transient(f"{model}: {type(exc).__name__}") from exc

    # ------------------------------------------------------------------ public

    def generate_json(self, prompt: str, schema: type[T], *, system: str | None = None,
                      temperature: float = 0.1) -> T:
        from google.genai import types

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=schema.model_json_schema(),
            temperature=temperature,
            system_instruction=system,
        )
        failures: list[str] = []
        models = self._models_to_try()
        for index, model in enumerate(models):
            transient_attempts = 0
            malformed_retried = False
            while True:
                try:
                    response = self._call(model, prompt, config)
                except _Transient as t:
                    failures.append(t.label)
                    transient_attempts += 1
                    if transient_attempts > self.max_retries:
                        break  # give up on this model
                    delay = min(t.delay if t.delay is not None else 3.0 * transient_attempts, 15.0)
                    log.warning("%s; retrying %s in %.0fs", t.label, model, delay)
                    self._sleep(delay)
                    continue
                try:
                    result = schema.model_validate_json(response.text or "")
                except ValidationError as exc:
                    if malformed_retried:
                        raise GeminiError(f"{model} returned output that does not match the schema") from exc
                    malformed_retried = True
                    log.warning("%s returned malformed JSON; retrying once", model)
                    continue
                self.last_model_used = model
                self.successes[model] += 1
                log.info("Gemini request processed by %s", model)
                return result

            if model == self.model and self.fallback_model and index + 1 < len(models):
                self._primary_down_until = time.monotonic() + self.primary_cooldown
                log.warning("%s unavailable; switching to fallback %s", model, self.fallback_model)

        self.last_model_used = None
        raise GeminiError("All Gemini models unavailable: " + "; ".join(failures))
