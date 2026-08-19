"""Direct m3u8 video source provider — Chinese CMS APIs.

MacCMS V10 API format: GET {base}/api.php/provide/vod/?ac=detail&wd={query}
Returns JSON with direct m3u8 HLS links — no embed/proxy needed.
ExoPlayer plays these natively.

Tested working sources (2026-08):
    - hongniuzy2.com   → hn.bfvvs.com
    - jinyingzy.com    → hd.ijycnd.com
    - ikunzy.com       → bfikuncdn.com / kkzycdn.com
    - 360zy.com        → vod1/vod2.maowushi.com
    - guangsu (光速)    → v.gsuus.com
    - subo (速播)       → play.xluuss.com
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urlencode

import requests as _fallback_http

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome131"
except Exception:
    _http = _fallback_http
    _IMPERSONATE = None

log = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "cnsource": {
        "enabled": True,
        "sources": [
            {
                "name": "ikunzy",
                "base_url": "https://ikunzy.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
            {
                "name": "360zy",
                "base_url": "https://360zy.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
            {
                "name": "hongniuzy2",
                "base_url": "https://hongniuzy2.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
            {
                "name": "jinyingzy",
                "base_url": "http://jinyingzy.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
            {
                "name": "guangsu",
                "base_url": "https://api.guangsuapi.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
            {
                "name": "subo",
                "base_url": "https://subocaiji.com",
                "api_path": "/api.php/provide/vod/",
                "timeout": 10,
            },
        ],
        "tmdb_languages": ["zh-CN", "zh-TW", "zh"],
        "timeout": 15,
        "max_servers": 12,
        "label": "سورس صيني",
        "cache_ttl": 3600,
        "verify_live": False,
        "verify_timeout": 5,
    }
}

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_m3u8_re = re.compile(r"https?://[^\s$#]+\.m3u8(?:\?[^\s$#]*)?")
_mp4_re = re.compile(r"https?://[^\s$#]+\.mp4(?:\?[^\s$#]*)?")

_cache: dict[tuple, tuple[float, "CnSourceResult"]] = {}


def _cfg() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG["cnsource"])
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as fh:
            stored = json.load(fh).get("cnsource") or {}
        if "enabled" in stored:
            cfg["enabled"] = stored["enabled"]
        if "sources" in stored and isinstance(stored["sources"], list):
            cfg["sources"] = stored["sources"]
        for k in ("timeout", "max_servers", "label", "cache_ttl",
                   "verify_live", "verify_timeout", "tmdb_languages"):
            if k in stored and stored[k] is not None:
                cfg[k] = stored[k]
    except (OSError, ValueError):
        pass
    return cfg


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


@dataclass
class CnSourceResult:
    servers: list[dict] = field(default_factory=list)


def _get(url: str, timeout: float, referer: str | None = None):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        hdrs["Referer"] = referer
    if _IMPERSONATE:
        try:
            return _http.get(url, impersonate=_IMPERSONATE, headers=hdrs, timeout=timeout)
        except Exception:
            pass
    return _fallback_http.get(url, headers=hdrs, timeout=timeout)


def fetch_cn_title(
    tmdb_id: int | str,
    media_type: str = "movie",
    languages: list[str] | None = None,
    timeout: float = 10.0,
) -> str:
    """Get the Chinese title for a TMDB id. Tries zh-CN, zh-TW, zh."""
    import requests as _req

    languages = languages or ["zh-CN", "zh-TW", "zh"]
    kind = "movie" if media_type in ("movie",) else "tv"
    api_key = os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_API")
    if not api_key:
        return ""

    for lang in languages:
        try:
            resp = _req.get(
                f"https://api.themoviedb.org/3/{kind}/{tmdb_id}",
                params={"api_key": api_key, "language": lang},
                timeout=timeout,
            )
            resp.raise_for_status()
            resp.encoding = "utf-8"
            data = resp.json()
            title = data.get("title") or data.get("name") or ""
            if title and any("\u4e00" <= c <= "\u9fff" for c in title):
                return title
        except Exception:
            continue
    return ""


def _parse_play_url(vod_play_from: str, vod_play_url: str, prefer_formats: list[str]) -> list[dict]:
    """Parse MacCMS vod_play_from/vod_play_url into [{name, url, source}].

    vod_play_from: "hnm3u8$$$hnyun"  (source names separated by $$$)
    vod_play_url: "name$url#name$url$$$name$url"  (episodes within a source, then sources)
    """
    sources = [s.strip() for s in (vod_play_from or "").split("$$$")]
    segments = (vod_play_url or "").split("$$$")

    servers = []
    for i, seg in enumerate(segments):
        source_name = sources[i] if i < len(sources) else f"source_{i}"
        is_preferred = not prefer_formats or source_name in prefer_formats

        for ep in seg.split("#"):
            ep = ep.strip()
            if "$" not in ep:
                continue
            name, url = ep.rsplit("$", 1)
            url = url.strip()
            name = name.strip()
            if not url:
                continue
            is_m3u8 = bool(_m3u8_re.search(url))
            is_mp4 = bool(_mp4_re.search(url))
            if not (is_m3u8 or is_mp4):
                continue
            kind = "hls" if is_m3u8 else "mp4"
            servers.append({
                "name": name or "عالي",
                "url": url,
                "kind": kind,
                "source_name": source_name,
                "preferred": is_preferred,
            })
    return servers


def _extract_unique_m3u8(vod_play_url: str) -> list[str]:
    """Extract unique m3u8 URLs from vod_play_url, preserving order."""
    seen = set()
    out = []
    for m in _m3u8_re.finditer(vod_play_url or ""):
        url = m.group(0)
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _verify_live(url: str, timeout: float = 5.0) -> bool:
    hdrs = {"User-Agent": UA, "Accept": "*/*", "Range": "bytes=0-2047"}
    try:
        if _IMPERSONATE:
            resp = _http.get(url, impersonate=_IMPERSONATE, headers=hdrs, stream=True, timeout=timeout)
        else:
            resp = _fallback_http.get(url, headers=hdrs, stream=True, timeout=timeout)
        status = resp.status_code
        resp.close()
        return status in (200, 206)
    except Exception:
        return False


def _search_one(query: str, src: dict, timeout: float) -> list[dict]:
    base = src.get("base_url", "").rstrip("/")
    api_path = src.get("api_path", "/api.php/provide/vod/")
    src_timeout = float(src.get("timeout", timeout))
    if not base:
        return []
    url = f"{base}{api_path}?ac=detail&wd={quote_plus(query)}"
    try:
        resp = _get(url, src_timeout, referer=base + "/")
        if resp.status_code >= 400:
            log.info("cnsource %s failed: %s", src.get("name"), resp.status_code)
            return []
        data = resp.json()
    except Exception as exc:
        log.warning("cnsource %s error: %s", src.get("name"), exc)
        return []
    items = data.get("list") or []
    if not items:
        return []
    src_servers = []
    for item in items[:3]:
        m3u8_urls = _extract_unique_m3u8(item.get("vod_play_url") or "")
        for m3u8 in m3u8_urls[:2]:
            src_servers.append({
                "url": m3u8,
                "kind": "hls",
                "quality": 0,
                "source": f"cn_{src.get('name', 'unknown')}",
                "source_name": "hnm3u8",
            })
    return src_servers


def search(
    query: str,
    sources: list[dict] | None = None,
    prefer_formats: list[str] | None = None,
    timeout: float = 10.0,
) -> CnSourceResult:
    """Search Chinese CMS sources and return direct m3u8/mp4 links.

    All sources are queried in parallel so the slowest one alone decides the
    wall-clock time (6 sources used to run sequentially = up to 60s delay).
    """
    import concurrent.futures

    cfg = _cfg()
    sources = sources or cfg.get("sources", [])
    prefer = prefer_formats or []
    result = CnSourceResult()

    per_source: list[list[dict]] = []
    workers = min(6, len(sources) or 1)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_search_one, query, src, timeout): src for src in sources}
        for fut in concurrent.futures.as_completed(futures):
            src = futures[fut]
            got = fut.result()
            if got:
                log.info("cnsource %s: %d links", src.get("name"), len(got))
            per_source.append(got)

    seen_urls: set[str] = set()
    deduped = []
    max_per = 2
    for i in range(max_per):
        for group in per_source:
            if i < len(group) and group[i]["url"] not in seen_urls:
                seen_urls.add(group[i]["url"])
                deduped.append(group[i])

    max_servers = int(cfg.get("max_servers", 12))
    result.servers = deduped[:max_servers]
    return result
    return result


def resolve(
    tmdb_id: int | str,
    media_type: str = "movie",
    title: str = "",
    season: int | None = None,
    episode: int | None = None,
    timeout: float | None = None,
) -> CnSourceResult:
    """Resolve a TMDB id to direct m3u8 links via Chinese CMS APIs.

    Uses TMDB to get the Chinese title, then searches CMS sources.
    """
    cfg = _cfg()
    timeout = timeout or float(cfg.get("timeout", 12))

    key = (str(tmdb_id), media_type, title, season, episode)
    cached = _cache.get(key)
    if cached is not None and time.monotonic() <= cached[0]:
        return cached[1]

    cn_title = fetch_cn_title(
        tmdb_id, media_type,
        languages=cfg.get("tmdb_languages"),
        timeout=min(5.0, timeout),
    )
    if not cn_title:
        log.info("cnsource: no Chinese title for TMDB %s", tmdb_id)
        return CnSourceResult()

    result = search(cn_title, timeout=timeout)

    if cfg.get("verify_live") and result.servers:
        verify_timeout = min(float(cfg.get("verify_timeout", 5)), timeout)
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(result.servers))) as pool:
            futures = [
                pool.submit(_verify_live, sv["url"], verify_timeout)
                for sv in result.servers
            ]
            alive = [bool(fut.result()) for fut in futures]
        result.servers = [sv for sv, ok in zip(result.servers, alive) if ok]

    if result.servers:
        _cache[key] = (time.monotonic() + float(cfg.get("cache_ttl", 3600)), result)
    return result
