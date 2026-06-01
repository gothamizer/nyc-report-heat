from __future__ import annotations

import random
import time
from dataclasses import dataclass

import requests
from bs4 import BeautifulSoup


# A browser-like UA is materially more reliable against public endpoints
# (notably Google News RSS) that reject obvious bot agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Status codes worth retrying: transient rate limiting / availability.
RETRY_STATUS = {429, 500, 502, 503, 504}


def _cache_key(url: str, params) -> tuple:
    if not params:
        return (url, ())
    return (url, tuple(sorted((str(k), str(v)) for k, v in params.items())))


@dataclass
class HttpClient:
    timeout: int = 30
    sleep_seconds: float = 0.0
    max_retries: int = 3
    backoff_base: float = 0.6

    def __post_init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        # Per-client response cache. Each candidate gets a fresh client, so this
        # de-duplicates the identical provider query issued once per heat window.
        self._cache: dict[tuple, requests.Response] = {}

    def get(self, url: str, **kwargs) -> requests.Response:
        key = _cache_key(url, kwargs.get("params"))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(url, timeout=self.timeout, **kwargs)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                self._wait(attempt)
                continue

            if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._wait(attempt, response)
                continue

            response.raise_for_status()
            if response.encoding is None or response.encoding.lower() == "iso-8859-1":
                response.encoding = response.apparent_encoding
            self._cache[key] = response
            return response

        # Exhausted retries on a retryable status: surface the final error.
        if last_exc is not None:
            raise last_exc
        response.raise_for_status()
        return response

    def post_json(self, url: str, payload: dict, headers: dict | None = None) -> requests.Response:
        """POST a JSON body with the same retry/backoff and per-client cache as get()."""
        key = ("POST", url, json.dumps(payload, sort_keys=True))
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        if self.sleep_seconds:
            time.sleep(self.sleep_seconds)

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.post(url, json=payload, headers=headers, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                self._wait(attempt)
                continue
            if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                self._wait(attempt, response)
                continue
            response.raise_for_status()
            self._cache[key] = response
            return response

        if last_exc is not None:
            raise last_exc
        response.raise_for_status()
        return response

    def _wait(self, attempt: int, response: requests.Response | None = None) -> None:
        delay = self.backoff_base * (2 ** attempt) + random.uniform(0, 0.5)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(delay, float(retry_after))
                except ValueError:
                    pass
        time.sleep(min(delay, 20.0))

    def soup(self, url: str) -> BeautifulSoup:
        return BeautifulSoup(self.get(url).text, "lxml")
