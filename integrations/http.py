"""Polite HTTP session: identifies itself, rate-limits per host, honours Retry-After."""

from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

import requests

USER_AGENT = "PMInternshipOutreach/0.3 (personal internship search; low volume)"
ROBOTS_AGENT = "PMInternshipOutreach"   # token matched against robots.txt User-agent lines


@dataclass
class FetchedPage:
    url: str
    final_url: str
    status: int
    content_type: str
    text: str


class PoliteSession:
    def __init__(self, min_interval: float = 1.5, timeout: float = 20.0, max_retries: int = 2,
                 session: requests.Session | None = None, sleep=time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self.min_interval = min_interval
        self.timeout = timeout
        self.max_retries = max_retries
        self.host_intervals: dict[str, float] = {}   # e.g. robots.txt Crawl-delay per host
        self._last: dict[str, float] = {}
        self._sleep = sleep

    def _wait(self, host: str) -> None:
        interval = max(self.min_interval, self.host_intervals.get(host, 0.0))
        wait = self._last.get(host, 0) + interval - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        self._last[host] = time.monotonic()

    def get_json(self, url: str, params: dict | None = None):
        host = urlparse(url).hostname or ""
        for attempt in range(self.max_retries + 1):
            self._wait(host)
            resp = self.session.get(url, params=params, timeout=self.timeout)
            if resp.status_code in (429, 502, 503, 504) and attempt < self.max_retries:
                retry_after = resp.headers.get("Retry-After", "")
                delay = min(int(retry_after), 60) if retry_after.isdigit() else 5 * (attempt + 1)
                self._sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def get_page(self, url: str, max_bytes: int = 1_500_000) -> FetchedPage:
        """GET one web page (no retries; callers record failures). Body is capped at max_bytes."""
        host = urlparse(url).hostname or ""
        self._wait(host)
        resp = self.session.get(
            url, timeout=self.timeout, stream=True, allow_redirects=True,
            headers={"Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.8,*/*;q=0.1"},
        )
        try:
            chunks, size = [], 0
            for chunk in resp.iter_content(65536):
                chunks.append(chunk)
                size += len(chunk)
                if size >= max_bytes:
                    break
            body = b"".join(chunks)[:max_bytes]
        finally:
            resp.close()
        # requests assumes ISO-8859-1 when the header has no charset, which garbles '₹' and other
        # UTF-8 text. Use the declared charset only if the server sent one; otherwise try UTF-8 first.
        declared = "charset=" in resp.headers.get("Content-Type", "").lower()
        text = None
        for encoding in ([resp.encoding] if declared and resp.encoding else []) + ["utf-8"]:
            try:
                text = body.decode(encoding)
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if text is None:
            text = body.decode(resp.encoding or "utf-8", errors="replace")
        return FetchedPage(url, resp.url or url, resp.status_code, resp.headers.get("Content-Type", ""), text)
