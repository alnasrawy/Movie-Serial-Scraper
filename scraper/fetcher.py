"""HTTP layer: politeness, rate limiting, retries."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.8",
}


@dataclass
class FetchSettings:
    """Control how HTTP requests are made.

    Attributes:
        delay:  base seconds to wait between requests (politeness).
        jitter: random extra delay 0..jitter seconds.
        timeout: per-request timeout in seconds.
        retries: number of retries on failure.
        retry_backoff: seconds to wait before the first retry (doubles each time).
        max_pages: cap on total pages fetched (-1 = unlimited).
    """

    delay: float = 1.5
    jitter: float = 0.5
    timeout: float = 15.0
    retries: int = 3
    retry_backoff: float = 2.0
    max_pages: int = -1

    def sleep(self) -> None:
        time.sleep(self.delay + random.random() * self.jitter)


@dataclass
class FetchedPage:
    url: str
    soup: BeautifulSoup | None = None
    status_code: int = 0
    headers: dict = field(default_factory=dict)


class RateLimitedError(RuntimeError):
    """Raised when the site blocks us (403 / 429)."""


class Fetcher:
    """Fetch pages with politeness, retries and caching of last-response."""

    def __init__(self, settings: FetchSettings | None = None, session: requests.Session | None = None) -> None:
        self.settings = settings or FetchSettings()
        self.session = session or requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self._pages_fetched = 0
        self.last_response: requests.Response | None = None

    def _check_page_budget(self) -> None:
        if self.settings.max_pages > 0 and self._pages_fetched >= self.settings.max_pages:
            raise RuntimeError(f"Reached max_pages budget ({self.settings.max_pages})")

    def _handle_status(self, resp: requests.Response) -> None:
        if resp.status_code in (429, 503):
            raise RateLimitedError(f"Blocked by site: HTTP {resp.status_code} for {resp.url}")
        if resp.status_code >= 400:
            raise RuntimeError(f"HTTP {resp.status_code} for {resp.url}")

    def get_soup(self, url: str, *, page_budget: bool = True, headers: dict | None = None) -> FetchedPage:
        """Fetch a URL (GET) and parse it into a BeautifulSoup document."""
        return self._fetch_soup(url, page_budget=page_budget, headers=headers)

    def post_soup(
        self,
        url: str,
        data: dict | None = None,
        *,
        page_budget: bool = True,
        headers: dict | None = None,
    ) -> FetchedPage:
        """Fetch a URL (POST) and parse the response into a BeautifulSoup document."""
        return self._fetch_soup(url, data=data, page_budget=page_budget, headers=headers)

    def _fetch_soup(
        self,
        url: str,
        data: dict | None = None,
        *,
        page_budget: bool = True,
        headers: dict | None = None,
    ) -> FetchedPage:
        """Fetch a URL (GET or POST) and parse it into a BeautifulSoup document."""
        if page_budget:
            self._check_page_budget()

        last_err: Exception | None = None
        backoff = self.settings.retry_backoff
        for attempt in range(self.settings.retries + 1):
            if attempt:
                time.sleep(backoff)
                backoff *= 2
            try:
                if data is None:
                    resp = self.session.get(url, timeout=self.settings.timeout, headers=headers)
                else:
                    resp = self.session.post(url, data=data, timeout=self.settings.timeout, headers=headers)
                self.last_response = resp
                self._handle_status(resp)
                resp.encoding = resp.encoding or "utf-8"
                soup = BeautifulSoup(resp.text, "lxml")
                self._pages_fetched += 1
                return FetchedPage(url=url, soup=soup, status_code=resp.status_code, headers=dict(resp.headers))
            except RateLimitedError:
                raise  # do not retry on explicit blocks
            except Exception as exc:  # network / timeout / http errors
                last_err = exc
                log.warning("Attempt %d failed for %s: %s", attempt + 1, url, exc)
                self.settings.sleep()

        raise RuntimeError(f"Failed to fetch {url}: {last_err}")
