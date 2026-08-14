"""Abstract scraper: defines the contract every site adapter implements."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from .fetcher import FetchedPage

log = logging.getLogger(__name__)


@dataclass
class SiteConfig:
    """Static description of one website's scraping rules."""

    name: str
    base_url: str
    search_url: str | None = None        # template, use {query} placeholder
    item_selector: str | None = None     # CSS selector for each result card
    fields: dict[str, str] = field(default_factory=dict)  # field -> css selector
    detail_url_selector: str | None = None
    max_pages: int = 1
    encoding: str | None = None
    detail_method: str = "get"  # "get" or "post"
    detail_data: dict[str, Any] = field(default_factory=dict)
    custom: dict[str, Any] = field(default_factory=dict)


class BaseScraper:
    """Subclass this to scrape a specific site.

    Override :meth:`parse_listing` and optionally :meth:`parse_detail`.
    """

    def __init__(self, config: SiteConfig, fetcher=None) -> None:
        self.config = config
        self.fetcher = fetcher

    def parse_listing(self, page: FetchedPage) -> list[dict[str, Any]]:
        raise NotImplementedError

    def parse_detail(self, page: FetchedPage, item: dict[str, Any]) -> dict[str, Any]:
        """Default: leave the item unchanged."""
        return item

    def scrape(
        self,
        query: str | None = None,
        *,
        with_details: bool = True,
        watch_only: bool = False,
        max_items: int | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch search/listing page(s) and return structured items.

        ``watch_only`` drops download links from the result and skips fetching
        any download sub-pages (watch servers only). ``max_items`` caps how many
        listing results get detail pages fetched (a search for a specific title
        usually matches its first few cards; fetching every card is wasted work).
        """
        results: list[dict[str, Any]] = []
        for page in self.pages(query):
            results.extend(self.parse_listing(page))
        if max_items is not None and max_items > 0:
            results = results[:max_items]
        if with_details:
            saved_extra = None
            if watch_only:
                saved_extra = self.config.custom.get("extra_detail_pages", [])
                self.config.custom["extra_detail_pages"] = [
                    sub
                    for sub in saved_extra
                    if not any(k == "download_servers" for k in sub.get("servers", []))
                ]
            try:
                results = [self.parse_detail(self._fetch_detail(item), item) for item in results]
            finally:
                if saved_extra is not None:
                    self.config.custom["extra_detail_pages"] = saved_extra
        if watch_only:
            for item in results:
                item.pop("download_servers", None)
        return results

    def pages(self, query: str | None) -> list[FetchedPage]:
        """Return listing pages. Default: a single search (or home) page."""
        url = self.search_url(query) if query else self.config.base_url
        return [self._fetch(url)]

    def search_url(self, query: str) -> str:
        if not self.config.search_url:
            raise ValueError(f"{self.config.name} has no search_url; pass query=None")
        return self.config.search_url.format(query=_quote(query))

    def _fetch(self, url: str) -> FetchedPage:
        page = self.fetcher.get_soup(url)
        if page.soup is not None and self.config.encoding:
            page.soup.original_encoding = self.config.encoding
        return page

    def _fetch_detail(self, item: dict[str, Any]) -> FetchedPage:
        url = item.get("detail_url") or item.get("url")
        if not url:
            return FetchedPage(url="", soup=None)
        if self.config.detail_method == "post":
            return self.fetcher.post_soup(url, data=self.config.detail_data)
        return self.fetcher.get_soup(url)


def _quote(query: str) -> str:
    from urllib.parse import quote

    return quote(query.strip(), safe="")


def absolute(base_url: str, href: str) -> str:
    from urllib.parse import urljoin

    return urljoin(base_url, href)


def same_site(page_url: str, link_url: str) -> bool:
    from urllib.parse import urlparse

    return urlparse(page_url).netloc == urlparse(link_url).netloc


def strip_query(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url)._replace(query="", fragment="").geturl()
