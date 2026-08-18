"""CSS-selector driven scraper: no code needed per site, just a config.

Example config (see configs/):
    {
      "name": "ExampleMovies",
      "base_url": "https://example.com/",
      "search_url": "https://example.com/search/{query}",
      "item_selector": "article.movie",
      "fields": {
        "title": "h2",
        "year": ".year",
        "rating": ".rating",
        "detail_url": "a@href"
      }
    }

Field selector syntax:
  - ".class" / "#id" / "h2"   -> text of first match
  - "tag@attr"                -> attribute of first match (e.g. "a@href")
  - "@attr"                   -> attribute of the current element (e.g. "@data-link")
  - special key "detail_url"  -> used to follow into the item page

Detail-page extras (via custom):
  - "watch_servers"/"download_servers": {"item_selector", "fields"} -> list of dicts
  - "extra_detail_pages": [{"suffix", "servers"}] -> fetch {detail_url}/{suffix} and
    apply each server spec (a key in custom) to that page
  - "." as a field selector -> text of the matched element itself
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import Tag

from .base import BaseScraper, SiteConfig, absolute
from .fetcher import FetchedPage

log = logging.getLogger(__name__)

_ATTR = re.compile(r"^(.+)@([\w-]+)$")
_SELF_ATTR = re.compile(r"^@([\w-]+)$")


def extract_text(node: Tag, selector: str) -> str | None:
    if selector == ".":
        return " ".join(node.get_text(" ", strip=True).split())
    self_attr = _SELF_ATTR.match(selector)
    if self_attr:
        return node.get(self_attr.group(1))
    match = _ATTR.match(selector)
    if match:
        css, attr = match.groups()
        el = node.select_one(css)
        return el.get(attr) if el else None
    el = node.select_one(selector)
    if el is None:
        return None
    return " ".join(el.get_text(" ", strip=True).split())


def extract_server_list(node, spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract a list of {field: value} dicts from repeated sub-nodes.

    spec: {"item_selector": str, "fields": {field: selector}}
    """
    items: list[dict[str, Any]] = []
    selector = spec.get("item_selector")
    if not selector:
        return items
    for sub in node.select(selector):
        item: dict[str, Any] = {}
        for field, field_selector in (spec.get("fields") or {}).items():
            value = extract_text(sub, field_selector)
            if value is not None:
                item[field] = value
        if item:
            items.append(item)
    return items


class GenericScraper(BaseScraper):
    """Scrapes any site described by a SiteConfig of CSS selectors."""

    def parse_listing(self, page: FetchedPage) -> list[dict[str, Any]]:
        soup = page.soup
        items: list[dict[str, Any]] = []
        if soup is None or not self.config.item_selector:
            return items

        for card in soup.select(self.config.item_selector):
            item: dict[str, Any] = {"source": self.config.name}
            for field, selector in self.config.fields.items():
                value = extract_text(card, selector)
                if value is not None:
                    item[field] = value
            if item.get("detail_url"):
                item["detail_url"] = absolute(page.url, item["detail_url"])
            items.append(item)
        return items

    def parse_detail(self, page: FetchedPage, item: dict[str, Any]) -> dict[str, Any]:
        if page.soup is None:
            return item
        detail_fields = self.config.custom.get("detail_fields", {})
        for field, selector in detail_fields.items():
            value = extract_text(page.soup, selector)
            if value is not None:
                item[field] = value
        for key in ("watch_servers", "download_servers"):
            spec = self.config.custom.get(key)
            if not spec:
                continue
            servers = extract_server_list(page.soup, spec)
            if servers:
                item[key] = servers
        for sub in self.config.custom.get("extra_detail_pages", []):
            self._fetch_and_parse_sub(item, sub)
        if self.config.custom.get("resolve_servers"):
            self._resolve_servers(item)
        if self.config.custom.get("verify_servers"):
            self._verify_servers(item)
        if self.config.custom.get("label_servers"):
            self._label_servers(item)
        return item

    def _fetch_and_parse_sub(self, item: dict[str, Any], sub: dict[str, Any]) -> None:
        """Fetch an extra detail sub-page (e.g. /watch) and parse server specs from it.

        sub: {"suffix": "watch", "servers": ["watch_servers", ...]} — the suffix is
        appended to the item's detail_url; each name in "servers" is a key in
        config.custom holding an {"item_selector", "fields"} spec.

        Alternatively a sub may use {"url": "play.php?vid={vid}", ...} — a path
        template filled from the detail_url's query parameters (e.g. a phpVibe
        site whose server page is play.php but whose detail page is video.php).
        """
        detail_url = item.get("detail_url") or item.get("url")
        url = None
        if "url" in sub:
            tpl = sub["url"]
            if not tpl:
                return
            from urllib.parse import parse_qs, urlparse

            params = {
                k: v[0]
                for k, v in parse_qs(urlparse(detail_url or "").query).items()
            }
            try:
                url = tpl.format(**params)
            except (KeyError, IndexError) as exc:
                log.info("Sub-page url template failed for %s: %s", tpl, exc)
                return
            url = absolute(self.config.base_url, url)
        else:
            suffix = sub.get("suffix")
            if not detail_url or not suffix:
                return
            url = f"{detail_url.rstrip('/')}/{suffix.lstrip('/')}"
        try:
            sub_page = self.fetcher.get_soup(url)
        except Exception as exc:
            log.info("Sub-page fetch failed for %s: %s", url, exc)
            return
        if sub_page.soup is None:
            return
        for key in sub.get("servers", []):
            spec = self.config.custom.get(key)
            if not spec:
                continue
            servers = extract_server_list(sub_page.soup, spec)
            if servers:
                item[key] = servers

    def _resolve_servers(self, item: dict[str, Any]) -> None:
        """Try to replace watch embed URLs with direct media URLs."""
        from .resolver import resolve_embed

        servers = item.get("watch_servers")
        if not isinstance(servers, list):
            return
        referer = item.get("detail_url") or self.config.base_url
        for server in servers:
            url = server.get("url")
            if not url:
                continue
            result = resolve_embed(url, self.fetcher, referer=referer)
            if result:
                server["direct_url"] = result["direct_url"]
                server["resolved_by"] = result["method"]

    def _verify_servers(self, item: dict[str, Any]) -> None:
        """Drop server links that fail a live HTTP check."""
        from .verify import verify_item

        verify_item(item)

    def _label_servers(self, item: dict[str, Any]) -> None:
        """Rename servers to numbered labels (سيرفر 1, ...)."""
        from .verify import label_servers

        label_servers(item)
