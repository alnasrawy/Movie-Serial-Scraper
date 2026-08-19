"""Foreign stream provider (PrimeTV engine + EasyPlex) — plain HTTP, no browser.

Two independent, HTTP-only sources power the PrimeTV Android app's playback;
this module replicates them so our backend can offer the same direct links:

1. ``engine.php`` (the app's server-side scraper). A single GET returns ready
   media URLs keyed by TMDB id:

       GET {engine_base_url}engine.php?action=play&tmdb=27205
           &type=movie&title=Inception&year=2010[&imdb=tt1375666]
       GET {engine_base_url}engine.php?action=play&tmdb=1396
           &type=tv&title=Breaking%20Bad&year=2008&se=1&ep=1
       GET {engine_base_url}engine.php?action=detail&id={mbox_id}
       GET {engine_base_url}engine.php?action=play&id={variant_subject_id}&se=..&ep=..

   -> {"success": true, "mbox_id": "...", "streams": [
        {"quality": 1080, "url": "https://sacdn.hakunaymatata.com/dash/.._.._1080_h265_../index_web.mpd",
         "format": "DASH", "cookie": "CloudFront-Policy=..;CloudFront-Signature=..;"}]}

   The signed URLs/cookies are minted for the *server's* IP, so clients must
   stream them through our /stream proxy (which sends the cookie on segments).

2. ``easyplex`` JSON API (a separate aggregator the app also queries in
   parallel and merges into the result):

       GET {easyplex_base_url}/sources/movie?tmdb_id=27205&title=Inception
       GET {easyplex_base_url}/sources/tv?tmdb_id=1396&title=Breaking%20Bad&season=1&episode=1

   -> {"success": true, "provider": "easyplex", "videos": [
        {"server": "Server VIP EGY", "link": "https://s1.egybestvid.com/hls2/.._n/master.m3u8?t=..",
         "header": "https://egybestvid.com/", "useragent": "..", "hd": 0, "hls": 0, "resolved": true}]}

   Videos with ``resolved: true`` are ready media URLs; the rest are embed
   pages whose HTML we scan with the same regex set the app's
   ``EmbedLinkExtractor`` uses (``file=/src=/url=`` assignments and bare
   ``.m3u8``/``.mp4`` URLs).

Neither source needs a browser or JavaScript execution. Config is in
``configs/providers.json`` -> the ``primetv`` block.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus, urljoin

import requests as _fallback_http

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome131"
except Exception:  # pragma: no cover - curl_cffi optional
    _http = _fallback_http
    _IMPERSONATE = None

log = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "primetv": {
        "enabled": True,
        "engine_base_url": "https://primeott.sytes.net/engine/",
        "easyplex_base_url": "http://144.91.94.164:8007",
        "timeout": 25,
        "max_servers": 6,
        "embed_attempts": 3,
        "label": "سيرفر برايم",
        "cache_ttl": 300,
        "verify_live": False,
        "verify_timeout": 8,
    }
}

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_QUALITY_RE = re.compile(r"(2160|1440|1080|720|480|360|240)")

# Regex set ported from PrimeTV's EmbedLinkExtractor (ordered, first wins).
_EMBED_PATTERNS = (
    re.compile(
        r"(?:file|src|source|hls|url|video_url|videoUrl)\s*[:=]\s*['\"]?(https?://[^'\"\s]+\.m3u8[^'\"\s]*)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:file|src|source|url|video_url|videoUrl)\s*[:=]\s*['\"]?(https?://[^'\"\s]+\.mp4[^'\"\s]*)",
        re.IGNORECASE,
    ),
    re.compile(r"sources\s*:\s*\[\s*['\"]?(https?://[^'\"\s]+(?:\.m3u8|\.mp4)[^'\"\s]*)", re.IGNORECASE),
    re.compile(r"['\"](https?://[^'\"]{10,}\.(?:m3u8|mp4)[^'\"]*)['\"]"),
)
_FALLBACK_HTML_RE = re.compile(r"https?://[^\s'\"<>\\]+(?:m3u8|mp4)[^\s'\"<>\\]*", re.IGNORECASE)

_primetv_cache: dict[tuple, tuple[float, "PrimetvResult"]] = {}


def _cfg() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG["primetv"])
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as fh:
            stored = json.load(fh).get("primetv") or {}
        cfg.update({k: v for k, v in stored.items() if v is not None})
    except (OSError, ValueError):
        pass
    return cfg


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


@dataclass
class PrimetvResult:
    servers: list[dict] = field(default_factory=list)  # {"url", "referer", "cookie", "user_agent", "quality", "format", "source"}


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-tested without network)
# ---------------------------------------------------------------------------

def _kind_for(url: str, format_label: str = "") -> str:
    """Media kind from the URL extension (format labels are unreliable)."""
    low = url.lower()
    if low.endswith(".m3u8") or "m3u8" in low:
        return "hls"
    if low.endswith(".mpd") or low.endswith("/manifest") or "application/dash" in format_label.lower():
        return "dash"
    return "mp4"


def is_direct_video_url(value: str) -> bool:
    """A candidate .m3u8/.mp4/.mpd URL that is actually media (not a thumbnail)."""
    low = value.lower()
    if len(value) < 10:
        return False
    if not any(ext in low for ext in (".m3u8", ".mp4", ".mpd")):
        return False
    for junk in ("thumb", "banner", "preview", "poster", "/ads/", "sprite"):
        if junk in low:
            return False
    return True


def _is_playable_media(url: str) -> bool:
    """A URL a player (VLC/ExoPlayer) can open directly.

    Accepts direct .m3u8/.mp4/.mpd URLs plus EarnVids-family playlists served
    as master.txt. Drops embed pages (e.g. mp4plus.org/embed-*.html) which are
    HTML, not media — those make the server list show links that fail in VLC.
    """
    if is_direct_video_url(url):
        return True
    low = url.lower()
    return (".txt" in low) and ("master" in low or "urlset" in low)


def _unescape(html: str) -> str:
    for a, b in (
        ("&amp;", "&"), ("\\/", "/"), ("\\u002F", "/"), ("\\u003A", ":"),
        ("\\u003D", "="), ("\\u0026", "&"), ("\\u003F", "?"),
        ("\\x3a", ":"), ("\\x2f", "/"), ("\\x3d", "="), ("\\x26", "&"),
        ("\\x3f", "?"),
    ):
        html = html.replace(a, b)
    return html


def _normalize(raw: str, base_url: str) -> str:
    value = _unescape((raw or "").strip().strip("\"'`"))
    if not value:
        return ""
    if value.startswith("//"):
        return "https:" + value
    if value.lower().startswith(("http://", "https://")):
        return value
    try:
        return urljoin(base_url, value)
    except ValueError:
        return value


def extract_embed_links(html: str, base_url: str, limit: int = 4) -> list[str]:
    """Scan an embed page for direct media URLs (port of EmbedLinkExtractor)."""
    cleaned = _unescape(html or "")
    found: list[str] = []
    for pattern in _EMBED_PATTERNS:
        for m in pattern.finditer(cleaned):
            url = _normalize(m.group(1), base_url)
            if is_direct_video_url(url) and url not in found:
                found.append(url)
                if len(found) >= limit:
                    return found
    if found:
        return found
    fallback = _unescape(html or "")
    for m in _FALLBACK_HTML_RE.finditer(fallback):
        url = _normalize(m.group(0), base_url)
        if is_direct_video_url(url) and url not in found:
            found.append(url)
            if len(found) >= limit:
                break
    return found


def _pick_quality(video: dict, url: str) -> int:
    server = (video.get("server") or "") + " " + url
    m = _QUALITY_RE.search(server)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return 720 if video.get("hd") == 1 else 360


def parse_engine_streams(data: dict | None) -> list[dict]:
    """engine.php `play` response -> [{url, cookie, quality, format, kind, source}]."""
    if not isinstance(data, dict) or not data.get("success"):
        return []
    out = []
    for st in data.get("streams") or []:
        url = (st.get("url") or "").strip()
        if not url or not is_direct_video_url(url):
            continue
        fmt = st.get("format") or "MP4"
        try:
            quality = int(st.get("quality") or 0)
        except (TypeError, ValueError):
            quality = 0
        out.append({
            "url": url,
            "cookie": st.get("cookie") or "",
            "quality": quality,
            "format": fmt,
            "kind": _kind_for(url, fmt),
            "source": "engine",
        })
    return out


def parse_easyplex_videos(data: dict | None) -> list[dict]:
    """easyplex /sources response -> [{url, referer, user_agent, quality, hls, resolved}]."""
    if not isinstance(data, dict) or not data.get("success"):
        return []
    out = []
    for vid in data.get("videos") or []:
        url = (vid.get("link") or "").strip()
        if not url:
            continue
        out.append({
            "url": url,
            "referer": vid.get("header") or "",
            "user_agent": vid.get("useragent") or "",
            "quality": _pick_quality(vid, url),
            "hls": int(vid.get("hls") or 0),
            "resolved": bool(vid.get("resolved")),
            "server": vid.get("server") or "",
            "source": "easyplex",
        })
    return out


# ---------------------------------------------------------------------------
# Network steps
# ---------------------------------------------------------------------------

def _get(url: str, referer: str | None, timeout: float, headers: dict | None = None, **kw):
    hdrs = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        hdrs["Referer"] = referer
    if headers:
        hdrs.update({k: v for k, v in headers.items() if v})
    if _IMPERSONATE:
        try:
            return _http.get(url, impersonate=_IMPERSONATE, headers=hdrs, timeout=timeout, **kw)
        except Exception:
            pass
    return _fallback_http.get(url, headers=hdrs, timeout=timeout, **kw)


def _verify_live(
    url: str,
    referer: str = "",
    cookie: str = "",
    user_agent: str = "",
    timeout: float = 8.0,
) -> bool:
    """Quick probe that a direct media URL currently serves content.

    Uses an aborted ranged GET so mp4/mpd checks don't download the whole file;
    playlists (m3u8) are small. Returns False on 429/403/502/404/timeouts so
    /watch only lists servers that actually play right now.
    """
    hdrs = {"User-Agent": user_agent or UA, "Accept": "*/*", "Range": "bytes=0-2047"}
    if referer:
        hdrs["Referer"] = referer
    if cookie:
        hdrs["Cookie"] = cookie
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


def fetch_engine(
    tmdb_id: int | str,
    media_type: str = "movie",
    title: str = "",
    year: int | None = None,
    imdb_id: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    subject_id: str | None = None,
    timeout: float | None = None,
) -> dict | None:
    """GET engine.php?action=play and return the JSON body (or None)."""
    cfg = _cfg()
    timeout = timeout or float(cfg.get("timeout", 25))
    base = str(cfg.get("engine_base_url", "")).rstrip("/")
    if not base:
        return None
    params: dict[str, Any] = {"action": "play"}
    if subject_id:
        params["id"] = subject_id
    else:
        params["tmdb"] = int(tmdb_id)
        params["type"] = "tv" if media_type == "tv" else "movie"
        if imdb_id:
            params["imdb"] = imdb_id
        if title:
            params["title"] = title
        if year:
            params["year"] = int(year)
        if season is not None:
            params["se"] = int(season)
        if episode is not None:
            params["ep"] = int(episode)
    from urllib.parse import urlencode

    url = base + "/engine.php?" + urlencode(params)
    try:
        resp = _get(url, base + "/", timeout)
        if resp.status_code >= 400:
            log.info("primetv engine failed: %s -> %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        log.warning("primetv engine error: %s", exc)
        return None


def fetch_easyplex(
    tmdb_id: int | str,
    media_type: str = "movie",
    title: str = "",
    season: int | None = None,
    episode: int | None = None,
    timeout: float | None = None,
) -> dict | None:
    """GET easyplex /sources/{movie|tv} and return the JSON body (or None)."""
    cfg = _cfg()
    timeout = timeout or float(cfg.get("timeout", 25))
    base = str(cfg.get("easyplex_base_url", "")).rstrip("/")
    if not base:
        return None
    kind = "tv" if media_type == "tv" else "movie"
    params: dict[str, Any] = {"tmdb_id": int(tmdb_id), "title": title}
    if kind == "tv":
        if season is not None:
            params["season"] = int(season)
        if episode is not None:
            params["episode"] = int(episode)
    from urllib.parse import urlencode

    url = base + "/sources/{kind}?{qs}".format(kind=kind, qs=urlencode(params))
    try:
        resp = _get(url, base + "/", timeout)
        if resp.status_code >= 400:
            log.info("primetv easyplex failed: %s -> %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        log.warning("primetv easyplex error: %s", exc)
        return None


def _resolve_embed_video(video: dict, timeout: float) -> list[dict]:
    """Fetch an unresolved embed page and extract direct media URLs from its HTML."""
    url = video["url"]
    referer = video.get("referer") or url
    headers = {"User-Agent": video.get("user_agent") or UA}
    try:
        resp = _get(url, referer, timeout, headers=headers)
        if resp.status_code >= 400:
            return []
        links = extract_embed_links(resp.text, url)
    except Exception as exc:
        log.debug("primetv embed fetch failed %s: %s", url, exc)
        return []
    out = []
    for link in links:
        out.append({
            "url": link,
            "referer": referer,
            "user_agent": video.get("user_agent") or UA,
            "quality": video.get("quality", 360),
            "hls": 1 if ".m3u8" in link.lower() else video.get("hls", 0),
            "resolved": True,
            "server": video.get("server") or "",
            "source": "easyplex",
        })
    return out


# ---------------------------------------------------------------------------
# Top-level resolve
# ---------------------------------------------------------------------------

def _cache_key(tmdb_id, media_type, title, year, season, episode) -> tuple:
    return (str(tmdb_id), media_type, title, year, season, episode)


def _to_server(video: dict) -> dict:
    return {
        "url": video["url"],
        "referer": video.get("referer") or "",
        "cookie": video.get("cookie") or "",
        "user_agent": video.get("user_agent") or "",
        "quality": video.get("quality", 0),
        "format": video.get("format") or ("hls" if video.get("hls") else _kind_for(video["url"])),
        "kind": _kind_for(video["url"], video.get("format") or ""),
        "source": video.get("source") or "easyplex",
    }


def resolve(
    tmdb_id: int | str,
    media_type: str = "movie",
    title: str = "",
    year: int | None = None,
    imdb_id: str | None = None,
    season: int | None = None,
    episode: int | None = None,
    timeout: float | None = None,
) -> PrimetvResult:
    """Resolve a TMDB id to direct media URLs via engine + easyplex.

    Streams come pre-signed/expiring: never cache longer than `cache_ttl`.
    """
    cfg = _cfg()
    timeout = timeout or float(cfg.get("timeout", 25))
    key = _cache_key(tmdb_id, media_type, title, year, season, episode)
    cached = _primetv_cache.get(key)
    if cached is not None and time.monotonic() <= cached[0]:
        return cached[1]

    result = PrimetvResult()
    engine, easy = None, None
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        f_engine = pool.submit(
            fetch_engine, tmdb_id, media_type, title=title, year=year,
            imdb_id=imdb_id, season=season, episode=episode, timeout=timeout,
        )
        f_easy = pool.submit(
            fetch_easyplex, tmdb_id, media_type, title=title,
            season=season, episode=episode, timeout=timeout,
        )
        engine = f_engine.result(timeout=timeout + 5)
        easy = f_easy.result(timeout=timeout + 5)

    for st in sorted(parse_engine_streams(engine), key=lambda s: -s["quality"]):
        result.servers.append(_to_server(st))
    embed_budget = int(cfg.get("embed_attempts", 3))
    for vid in parse_easyplex_videos(easy):
        if vid["resolved"] or is_direct_video_url(vid["url"]):
            result.servers.append(_to_server(vid))
        elif embed_budget > 0:
            embed_budget -= 1
            for extra in _resolve_embed_video(vid, timeout):
                result.servers.append(_to_server(extra))

    seen: set[str] = set()
    deduped: list[dict] = []
    for sv in sorted(result.servers, key=lambda s: -s["quality"]):
        if sv["url"] not in seen:
            seen.add(sv["url"])
            deduped.append(sv)

    # Only keep URLs a player can open directly — drop embed pages (HTML) that
    # would surface as broken "mp4" servers in VLC/ExoPlayer.
    deduped = [sv for sv in deduped if _is_playable_media(sv["url"])]

    if cfg.get("verify_live", False) and deduped:
        verify_timeout = min(float(cfg.get("verify_timeout", 8)), timeout)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(deduped))) as pool:
            futures = [
                pool.submit(
                    _verify_live,
                    sv["url"],
                    sv.get("referer") or "",
                    sv.get("cookie") or "",
                    sv.get("user_agent") or "",
                    verify_timeout,
                )
                for sv in deduped
            ]
            alive = [bool(fut.result()) for fut in futures]
        deduped = [sv for sv, ok in zip(deduped, alive) if ok]

    max_servers = int(cfg.get("max_servers", 6))
    result.servers = deduped[:max_servers]
    if result.servers:
        _primetv_cache[key] = (time.monotonic() + float(cfg.get("cache_ttl", 300)), result)
    return result
