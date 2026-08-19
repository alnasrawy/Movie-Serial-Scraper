"""Site registry: load adapters from config files and construct scrapers."""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .base import BaseScraper, SiteConfig
from .fetcher import Fetcher, FetchSettings
from .generic import GenericScraper

log = logging.getLogger(__name__)

_sites_cache: dict[str, tuple[float, list[SiteConfig]]] = {}
_SITES_CACHE_TTL = float(os.environ.get("SITES_CACHE_TTL", "60"))


def config_dir() -> Path:
    """Absolute path to the site configs directory (package-relative)."""
    return Path(__file__).resolve().parent.parent / "configs"


def _parse_config(raw: dict[str, Any]) -> SiteConfig:
    fields = dict(raw.get("fields", {}))
    if raw.get("item_selector") and raw.get("detail_url_selector"):
        fields.setdefault("detail_url", raw["detail_url_selector"])
    return SiteConfig(
        name=raw.get("name") or raw.get("base_url", "site"),
        base_url=raw.get("base_url", ""),
        search_url=raw.get("search_url"),
        item_selector=raw.get("item_selector"),
        fields=fields,
        max_pages=raw.get("max_pages", 1),
        encoding=raw.get("encoding"),
        detail_method=raw.get("detail_method", "get"),
        detail_data=raw.get("detail_data", {}),
        custom=raw.get("custom", {}),
        enabled=bool(raw.get("enabled", True)),
    )


def load_config(path: str | Path) -> SiteConfig:
    path = Path(path)
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and "sites" in raw:
        raise ValueError("config is a bundle file; load_sites() expects a single site config")
    return _parse_config(raw)


def load_sites(directory: str | Path = "configs") -> list[SiteConfig]:
    """Load every *.json config from a directory.

    ``providers.json`` is the middleware's provider bundle (subtitles, ...),
    not a site config — it is skipped.

    Results are cached in-memory for SITES_CACHE_TTL seconds to avoid
    re-reading JSON files on every call.
    """
    dir_key = str(Path(directory).resolve())
    now = time.monotonic()
    cached = _sites_cache.get(dir_key)
    if cached is not None:
        ts, sites = cached
        if (now - ts) < _SITES_CACHE_TTL:
            return list(sites)
    directory = Path(directory)
    if not directory.is_dir():
        log.warning("Config directory %s not found", directory)
        return []
    sites: list[SiteConfig] = []
    for path in sorted(directory.glob("*.json")):
        if path.name == "providers.json":
            continue
        try:
            config = load_config(path)
        except Exception as exc:
            log.error("Skipping %s: %s", path, exc)
            continue
        if not config.enabled:
            log.info("Skipping disabled site %s (%s)", config.name, path.name)
            continue
        sites.append(config)
    _sites_cache[dir_key] = (now, sites)
    return list(sites)


def build_scraper(
    config: SiteConfig,
    settings: FetchSettings | None = None,
    session=None,
) -> BaseScraper:
    fetcher = Fetcher(settings, session)
    return GenericScraper(config, fetcher)


def find_config(name: str, directory: str | Path = "configs") -> SiteConfig | None:
    """Find a config by site name (case-insensitive, prefix match allowed)."""
    target = name.lower()
    for config in load_sites(directory):
        if config.name.lower() == target:
            return config
    for config in load_sites(directory):
        if config.name.lower().startswith(target):
            return config
    return None


def available_sites(directory: str | Path = "configs") -> list[str]:
    return [c.name for c in load_sites(directory)]


def invalidate_sites_cache() -> None:
    """Force re-read of config files on next load_sites() call."""
    _sites_cache.clear()
