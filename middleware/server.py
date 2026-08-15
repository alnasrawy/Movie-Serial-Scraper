"""FastAPI service that turns embed pages into native-player media streams.

Endpoints:
    POST /resolve  {"url": embed, "referer": optional}
                   -> {"sid", "kind", "url", "proxy_url", ...}
    GET  /stream   {sid, url} -> HLS playlist (rewritten) or media bytes,
                   transparently proxied through the embed's session.
    GET  /health

Two resolution paths:
  * HTTP-only (default on low-memory hosts / free tier): resolves EarnVids
    family embeds via plain HTTP (unpacked packer + Referer), no browser.
  * Browser fallback (when BROWSER_ENABLED=1 and Playwright is installed):
    opens a real Chromium session for hosts that need JS (vibuxer/hgcloud).

Run:  python -m uvicorn middleware.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Literal
from urllib.parse import urljoin, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel

from .http_resolver import resolve_http

log = logging.getLogger(__name__)
app = FastAPI(title="Embed resolver middleware")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BROWSER_ENABLED = os.environ.get("BROWSER_ENABLED", "1").lower() in ("1", "true", "yes", "on")

_manager = None


def get_manager():
    """Lazily import/start the browser manager (None when disabled/unavailable)."""
    global _manager
    if not BROWSER_ENABLED:
        return None
    if _manager is None:
        try:
            from .player import BrowserManager
        except Exception as exc:  # playwright not installed (lite image)
            log.warning("Playwright unavailable (%s); browser path disabled", exc)
            return None
        _manager = BrowserManager()
    return _manager


# HTTP-resolved sessions: sid -> {"url": media url, "referer": embed page}
http_sessions: dict[str, dict] = {}


def _new_sid() -> str:
    return uuid.uuid4().hex


class ResolveRequest(BaseModel):
    url: str
    referer: str | None = None


class WatchRequest(BaseModel):
    """The main-app contract: give us a TMDB id (or a raw query) and we return
    a ready-to-play server list."""

    tmdb_id: int | None = None
    type: Literal["movie", "tv"] = "movie"
    query: str | None = None
    sites: list[str] | None = None
    season: int | None = None
    episode: int | None = None


def _ext_for(kind: str) -> str:
    """A URL path extension hinting the media type.

    ExoPlayer (Media3) picks HLS/DASH by the URI's extension and does NOT sniff
    a plain playlist/mpd text. The proxy URL path must therefore end in
    .m3u8/.mpd or ExoPlayer treats the stream as a (sniffable) container and
    fails. Sub-resources inside a playlist are consumed by the already-selected
    HlsMediaSource/DashMediaSource, so only the top-level URL needs the hint.
    """
    if kind == "hls":
        return ".m3u8"
    if kind == "dash":
        return ".mpd"
    return ".mp4"


def _path_lower(url: str) -> str:
    """Lowercased URL path (ignoring query/fragment) — CDN media URLs carry
    signed tokens as query params, so `url.lower().endswith(".m3u8")` fails on
    `master.m3u8?t=...&s=...`. Media-type decisions must use the path only."""
    from urllib.parse import urlparse

    return urlparse(url).path.lower()


def _proxy_url(sid: str, media_url: str, name: str = "", ref: str = "", site_ref: str = "", ext: str = "") -> str:
    q = {"url": media_url}
    if ref:
        q["ref"] = ref
    if site_ref:
        q["site_ref"] = site_ref
    return "/stream/{}{}?{}".format(sid, ext, urlencode(q))


def _scrape_site(name: str, query: str) -> list[dict]:
    """Scrape watch servers for a query from a single site config."""
    from scraper.fetcher import FetchSettings
    from scraper.sites import build_scraper, find_config

    config = find_config(name)
    if config is None:
        log.warning("Unknown site %r skipped", name)
        return []
    config.custom["resolve_servers"] = False
    config.custom["verify_servers"] = False
    config.custom["label_servers"] = True
    delay = float(os.environ.get("SCRAPE_DELAY", "0.1"))
    max_items = int(os.environ.get("MAX_ITEMS_PER_SITE", "3"))
    # A title search matches its first few cards; fetching details for every
    # card costs 2-3 page requests each and is the #1 /watch latency driver.
    # Retries are trimmed too — the /watch path has multiple sites and can
    # afford a quick failure over a 2s/4s backoff.
    scraper = build_scraper(config, FetchSettings(delay=delay, timeout=15, retries=1))
    try:
        return scraper.scrape(query, with_details=True, watch_only=True, max_items=max_items)
    except Exception as exc:
        log.warning("Scrape failed on %s: %s", name, exc)
        return []


def _scrape_all(query: str, sites: list[str]) -> list[dict]:
    """Scrape watch servers for a query from all sites, in parallel."""
    import concurrent.futures

    items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(sites))) as pool:
        futures = [pool.submit(_scrape_site, name, query) for name in sites]
        for fut in concurrent.futures.as_completed(futures):
            items.extend(fut.result())
    return items


async def _resolve_many(servers: list[tuple[dict, dict]], limit: int = 8) -> list[tuple[dict, dict, dict]]:
    """Resolve embed URLs concurrently, with a concurrency cap."""
    sem = asyncio.Semaphore(limit)

    async def one(item: dict, sv: dict) -> tuple[dict, dict, dict]:
        async with sem:
            res = await asyncio.wait_for(
                _resolve_embed(sv.get("url") or "", referer=item.get("detail_url")),
                timeout=35,
            )
            return item, sv, res

    return await asyncio.gather(*(one(item, sv) for item, sv in servers))


# Per-title result cache: key -> (expires_ts, {"servers", "imdb_id", "subtitles"})
_watch_cache: dict[tuple, tuple[float, dict]] = {}
_WATCH_CACHE_TTL = float(os.environ.get("WATCH_CACHE_TTL", "600"))
_MAX_CACHE_ENTRIES = 200


def _cache_get(key: tuple) -> dict | None:
    entry = _watch_cache.get(key)
    if entry is None:
        return None
    expires, payload = entry
    if time.monotonic() > expires:
        _watch_cache.pop(key, None)
        return None
    return payload


def _cache_set(key: tuple, payload: dict) -> None:
    if len(_watch_cache) >= _MAX_CACHE_ENTRIES:
        _watch_cache.clear()
    _watch_cache[key] = (time.monotonic() + _WATCH_CACHE_TTL, payload)


async def _resolve_embed(url: str, referer: str | None) -> dict:
    """Resolve one embed, HTTP-first, browser as fallback.

    Returns a dict like BrowserManager.open_session: {sid, kind, url, error}.
    """
    got = await asyncio.to_thread(resolve_http, url, referer)
    if got:
        sid = _new_sid()
        http_sessions[sid] = {"url": got["url"], "referer": url, "created": time.monotonic()}
        return {"sid": sid, "kind": got["kind"], "url": got["url"], "error": None}

    mgr = get_manager()
    if mgr is not None:
        res = await mgr.open_session(url, referer)
        if res.get("kind") != "none":
            verified = await mgr.fetch(res["sid"], res["url"])
            if verified and verified[0] < 400:
                return res
    return {"kind": "none", "url": url, "error": "not resolvable (http or browser)"}


def _strip_png_wrapper(data: bytes) -> bytes:
    """Some CDNs (tiktokcdn) wrap MPEG-TS segments inside a PNG container.

    The PNG signature itself contains a 0x47 byte ("G"), so we locate the real
    payload by requiring three 188-aligned MPEG-TS sync bytes, not the first
    0x47 in the file.
    """
    if not data.startswith(b"\x89PNG"):
        return data
    i = _ts_start(data)
    if i < 0:
        return data
    end = len(data) - ((len(data) - i) % 188)
    return data[i:end]


def _ts_start(data: bytes) -> int:
    """Index of the first MPEG-TS run: 0x47 at i, i+188 and i+2*188."""
    for i in range(len(data) - 2 * 188):
        if data[i] == 0x47 and data[i + 188] == 0x47 and data[i + 2 * 188] == 0x47:
            return i
    return -1


def _rewrite_m3u8(text: str, base_url: str, sid: str) -> str:
    out_lines = []

    def wrap(uri: str) -> str:
        return _proxy_url(sid, urljoin(base_url, uri))

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            out_lines.append(line)
            continue
        if stripped.startswith("#"):
            new_line = re.sub(r'(URI=")([^"]+)(")', lambda m: m.group(1) + wrap(m.group(2)) + m.group(3), line)
            out_lines.append(new_line)
            continue
        out_lines.append(wrap(stripped))
    return "\n".join(out_lines)


_MPD_ATTR_RE = re.compile(r'\b(?:media|initialization)="([^"]+)"')


def _rewrite_mpd(text: str, base_url: str, sid: str) -> str:
    """Rewrite a DASH manifest so segments go through our proxy.

    Relative <BaseURL> entries and SegmentTemplate `media`/`initialization`
    attribute values are resolved against the MPD URL. The session's cookie
    (e.g. CloudFront signed cookies) is attached server-side on each fetch.
    `$Name$` template tokens stay unencoded so the player can substitute them.
    """

    def wrap(uri: str) -> str:
        # $Name$ template tokens (may include printf specifiers like %05d) must
        # stay literal so the player can substitute them; stash them before
        # urlencoding and restore afterwards.
        tokens: list[str] = []

        def stash(m: re.Match) -> str:
            tokens.append(m.group(0))
            return "\x01{0}\x01".format(len(tokens) - 1)

        protected = re.sub(r"\$[A-Za-z0-9_%.:-]*\$", stash, uri)
        wrapped = _proxy_url(sid, urljoin(base_url, protected))
        for i, tok in enumerate(tokens):
            wrapped = wrapped.replace("%01{0}%01".format(i), tok)
        return wrapped

    def repl_attr(m: re.Match) -> str:
        value = m.group(1).strip()
        if not value or value.startswith(("http://", "https://", "$")):
            return m.group(0)
        return '{}="{}"'.format(m.group(0).split("=", 1)[0].rstrip(), wrap(value))

    def repl_base(m: re.Match) -> str:
        value = m.group(1).strip()
        if not value:
            return m.group(0)
        return "<BaseURL>{}</BaseURL>".format(wrap(value))

    out_lines = []
    for line in text.splitlines():
        line = re.sub(r"<BaseURL>([^<]*)</BaseURL>", repl_base, line)
        line = _MPD_ATTR_RE.sub(repl_attr, line)
        out_lines.append(line)
    return "\n".join(out_lines)


def _http_headers(ep: dict, url: str) -> dict:
    """Request headers for an HTTP-resolved session (referer + optional cookie/UA)."""
    headers = {
        "User-Agent": ep.get("user_agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": ep["referer"],
    }
    cookie = ep.get("cookie")
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _http_fetch(sid: str, url: str) -> tuple[int, str, bytes] | None:
    """Fetch for HTTP-resolved sessions (no browser).

    Prefers curl_cffi with a Chrome TLS fingerprint — the video CDNs bot-block
    plain ``requests`` fingerprints (they return 502/429) but serve the same
    URL fine to an impersonated browser client.

    The CDNs are flaky from datacenter IPs under a player's parallel burst:
    retry transient 5xx/429/408 responses and transport errors before giving
    up, so a single dropped segment doesn't kill playback.
    """
    import time as _t

    import requests as _plain

    try:
        from curl_cffi import requests as _imp
    except Exception:
        _imp = None

    ep = http_sessions.get(sid)
    if not ep:
        return None
    headers = {
        "User-Agent": ep.get("user_agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": ep["referer"],
        "Accept": "*/*",
    }
    cookie = ep.get("cookie")
    if cookie:
        headers["Cookie"] = cookie

    last: tuple[int, str, bytes] | None = None
    for attempt in range(3):
        if attempt:
            _t.sleep(0.3 * attempt)
        ok = False
        try:
            if _imp is not None:
                resp = _imp.get(url, impersonate="chrome131", headers=headers, timeout=30)
                last = (resp.status_code, resp.headers.get("content-type", ""), resp.content)
                ok = resp.status_code < 500
                if ok or attempt == 2:
                    return last
        except Exception as exc:
            log.debug("http-proxy impersonated fetch failed for %s: %s", url, exc)
        try:
            resp = _plain.get(url, headers=headers, timeout=30)
            last = (resp.status_code, resp.headers.get("content-type", ""), resp.content)
            ok = resp.status_code < 500
            if ok or attempt == 2:
                return last
        except _plain.RequestException as exc:
            log.debug("http-proxy fetch failed for %s: %s", url, exc)
    return last


def _http_stream(sid: str, url: str, range_header: str | None) -> tuple[int, str, dict, Any] | None:
    """Open a RANGED upstream stream for media (mp4).

    The video CDNs refuse full (un-ranged) GETs from datacenter IPs with 502 but
    serve the same URL as 206 to a ranged request. Players (VLC/ExoPlayer) seek
    via Range anyway, so /stream must forward the client's Range and stream the
    upstream body back instead of buffering the whole file.
    """
    import time as _t

    import requests as _plain

    try:
        from curl_cffi import requests as _imp
    except Exception:
        _imp = None

    ep = http_sessions.get(sid)
    if not ep:
        return None
    headers = {
        "User-Agent": ep.get("user_agent")
        or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": ep["referer"],
        "Accept": "*/*",
        "Range": range_header or "bytes=0-",
    }
    cookie = ep.get("cookie")
    if cookie:
        headers["Cookie"] = cookie
    last: tuple[int, str, dict, Any] | None = None
    for attempt in range(3):
        if attempt:
            _t.sleep(0.3 * attempt)
        try:
            if _imp is not None:
                resp = _imp.get(url, impersonate="chrome131", headers=headers, timeout=30, stream=True)
                last = (resp.status_code, resp.headers.get("content-type", ""), resp.headers, resp.iter_content(chunk_size=65536))
                if resp.status_code < 500 or attempt == 2:
                    return last
        except Exception as exc:
            log.debug("http-proxy ranged stream failed for %s: %s", url, exc)
        try:
            resp = _plain.get(url, headers=headers, timeout=30, stream=True)
            last = (resp.status_code, resp.headers.get("content-type", ""), resp.headers, resp.iter_content(chunk_size=65536))
            if resp.status_code < 500 or attempt == 2:
                return last
        except _plain.RequestException as exc:
            log.debug("http-proxy ranged stream fallback failed for %s: %s", url, exc)
    return last


def _http_media_stream(sid: str, url: str, range_header: str | None) -> tuple[int, str, dict, Any] | None:
    """Ranged streaming fetch for any media (TS segments, mp4, DASH m4s).

    Buffering a whole 2-4 MB segment before replying makes scrubbing feel like
    minutes — instead stream the upstream body through, forwarding the client's
    Range so the CDN serves only the bytes the player asked for. The first
    chunk is sniffed to fix the media type (PNG wrapper / text/plain TS).
    """
    result = _http_stream(sid, url, range_header)
    if result is None:
        return None
    up_status, up_ct, up_headers, chunks = result
    if up_status >= 400:
        return up_status, up_ct or "", {}, None
    first = b""
    try:
        first = next(chunks)
    except StopIteration:
        pass
    ct = (up_ct or "").lower()
    looks_ts = bool(
        _path_lower(url).endswith((".ts", ".m4s"))
        or ct in ("text/plain", "application/octet-stream")
        or first.startswith(b"\x89PNG")
    )
    media_ct = up_ct or "application/octet-stream"
    if looks_ts:
        off = _ts_start(first)
        if off > 0:
            first = first[off:]
            media_ct = "video/mp2t"

    def gen():
        if first:
            yield first
        yield from chunks

    return up_status, media_ct, up_headers, gen()


@app.post("/resolve")
async def resolve(req: ResolveRequest, request: Request) -> dict:
    result = await _resolve_embed(req.url, req.referer)
    if result.get("kind") != "none":
        base = str(request.base_url).rstrip("/")
        result["proxy_url"] = base + _proxy_url(result["sid"], result["url"], ext=_ext_for(result["kind"]))
    else:
        result["proxy_url"] = None
    return result


@app.post("/watch")
async def watch(req: WatchRequest, request: Request) -> dict:
    """Main-app contract: TMDB id (or raw query) -> list of playable servers.

    Body: {"tmdb_id": 27205, "type": "movie", "sites": ["akwams", "egydead"]}
    Returns: {"tmdb_id", "type", "query", "imdb_id", "subtitles",
              "servers": [{name, site, kind, proxy_url}]}

    Source selection: an explicit `sites` list is used as-is. Movies default to
    our own Arabic sites (akwams, egydead) so the app's list is our engine;
    TV defaults to foreign providers only (fast — the Arabic sites index a
    series by iterating every episode).
    """
    sites = [s.strip() for s in (req.sites or []) if s.strip()]
    if not sites and req.type == "movie":
        sites = ["akwams", "egydead"]
    if not sites and not req.tmdb_id:
        raise HTTPException(400, "Provide 'sites' or 'tmdb_id'")

    if req.query:
        query = req.query
        candidates = [query]
    else:
        if not req.tmdb_id:
            raise HTTPException(400, "Provide 'tmdb_id' or 'query'")
        from scraper.tmdb import api_key, search_query, tmdb_title

        key = api_key()
        if not key:
            raise HTTPException(400, "TMDB_API_KEY is not set on the server")
        info = await asyncio.to_thread(tmdb_title, req.tmdb_id, key=key, media_type=req.type)
        query = search_query(info)
        candidates = []
        for t in (query, info.get("original_title") or ""):
            t = (t or "").strip()
            if t and t not in candidates:
                candidates.append(t)
        if not candidates:
            candidates = [info.get("original_title") or info.get("title") or str(req.tmdb_id)]

    # Some sites index a title under its Arabic name, others under the original
    # (akwams ignores "استهلال" but matches "Inception"). Try every candidate
    # and merge unique items so we keep max server coverage.
    cache_key = (str(request.base_url).rstrip("/"), tuple(candidates), tuple(sites))
    cached = _cache_get(cache_key)
    if cached is not None:
        return {
            "tmdb_id": req.tmdb_id,
            "type": req.type,
            "query": query,
            "imdb_id": cached.get("imdb_id"),
            "subtitles": cached.get("subtitles") or [],
            "cached": True,
            "servers": cached["servers"],
        }

    merged_items: list[dict] = []
    seen: set[tuple] = set()
    if sites:
        # Run every query candidate in parallel (each internally fan-outs across
        # sites) so the Arabic title + original title don't add up serially.
        cand_results = await asyncio.gather(
            *(asyncio.to_thread(_scrape_all, cand, sites) for cand in candidates)
        )
        for items in cand_results:
            for item in items:
                key = (item.get("source"), item.get("detail_url") or item.get("id") or item.get("title"))
                if key not in seen:
                    seen.add(key)
                    merged_items.append(item)

    items = merged_items
    base = str(request.base_url).rstrip("/")

    # Resolve each unique embed once (both candidates can match the same item).
    to_resolve = []
    seen_embed: set[tuple] = set()
    for item in items:
        for sv in item.get("watch_servers") or []:
            if not sv.get("url"):
                continue
            key = (sv["url"], item.get("detail_url") or "")
            if key in seen_embed:
                continue
            seen_embed.add(key)
            to_resolve.append((item, sv))

    servers: list[dict] = []
    for item, sv, res in await _resolve_many(to_resolve):
        if res.get("kind") == "none":
            continue
        servers.append({
            "site": item.get("source"),
            "name": sv.get("name"),
            "original_name": sv.get("original_name"),
            "kind": res["kind"],
            "proxy_url": base + _proxy_url(
                res["sid"], res["url"],
                ref=sv.get("url") or "",
                site_ref=item.get("detail_url") or "",
                ext=_ext_for(res["kind"]),
            ),
        })

    imdb_id, subtitles_list = await _add_foreign_servers(req, base, servers)

    # Our Arabic sites number servers per page, so "سيرفر 2" repeats across
    # sites/items. Renumber all non-foreign servers globally (سيرفر 1..N).
    arabic_n = 0
    for sv in servers:
        if not sv.get("foreign"):
            arabic_n += 1
            sv["name"] = "سيرفر {}".format(arabic_n)

    _prune_sessions()
    _cache_set(cache_key, {"servers": servers, "imdb_id": imdb_id, "subtitles": subtitles_list})

    return {
        "tmdb_id": req.tmdb_id,
        "type": req.type,
        "query": query,
        "imdb_id": imdb_id or None,
        "subtitles": subtitles_list,
        "servers": servers,
    }


def _prune_sessions(max_age: float = 86400.0) -> None:
    """Drop stale HTTP sessions so long-running servers don't leak memory."""
    now = time.monotonic()
    stale = [sid for sid, ep in http_sessions.items() if now - ep.get("created", now) > max_age]
    for sid in stale:
        http_sessions.pop(sid, None)


async def _refresh_primetv(http_ep: dict) -> tuple[str, bool]:
    """Re-resolve a primetv session's stream and return a fresh URL."""
    from . import primetv

    provider = http_ep.get("provider") or {}
    idx = int(http_ep.get("idx") or 0)
    try:
        res = await asyncio.to_thread(
            primetv.resolve,
            provider.get("tmdb_id"),
            provider.get("type") or "movie",
            title=provider.get("title") or "",
            year=provider.get("year"),
            season=provider.get("season"),
            episode=provider.get("episode"),
        )
    except Exception as exc:
        log.warning("primetv refresh failed: %s", exc)
        return http_ep.get("url") or "", False
    servers = res.servers
    if idx >= len(servers):
        return http_ep.get("url") or "", False
    sv = servers[idx]
    http_ep["url"] = sv["url"]
    http_ep["referer"] = sv["referer"] or http_ep.get("referer", "")
    if sv.get("cookie"):
        http_ep["cookie"] = sv["cookie"]
    return sv["url"], True


async def _add_foreign_servers(req: WatchRequest, base: str, servers: list[dict]) -> tuple[str, list[dict]]:
    """Resolve foreign (primetv/easyplex) streams for a TMDB id and append proxied servers.

    Runs in parallel with the TMDB external-id lookup (a few seconds of HTTP);
    the subtitle language list is fetched once afterwards.

    Returns (imdb_id, subtitle languages) — the former is needed by the app to
    fetch subtitles later via /subtitle.
    """
    if not req.tmdb_id:
        return "", []
    from scraper.tmdb import api_key, tmdb_title
    from . import primetv
    from . import subtitles as subs

    async def job_imdb() -> str:
        if not subs.is_enabled():
            return ""
        try:
            return await asyncio.to_thread(_tmdb_external_imdb, req.tmdb_id, req.type)
        except Exception as exc:
            log.warning("tmdb external_ids failed: %s", exc)
            return ""

    async def job_primetv() -> list[dict]:
        if not primetv.is_enabled():
            return []
        key = api_key()
        info = {}
        if key:
            try:
                info = await asyncio.to_thread(tmdb_title, req.tmdb_id, key=key, media_type=req.type)
            except Exception as exc:
                log.warning("tmdb_title for primetv failed: %s", exc)
        ptitle = (info.get("original_title") or info.get("title") or "").strip()
        res = await asyncio.to_thread(
            primetv.resolve,
            req.tmdb_id,
            req.type,
            title=ptitle,
            year=info.get("year"),
            season=req.season,
            episode=req.episode,
        )
        label = primetv._cfg().get("label", "سيرفر برايم")
        provider_args = {
            "tmdb_id": req.tmdb_id,
            "type": req.type,
            "title": ptitle,
            "year": info.get("year"),
            "season": req.season,
            "episode": req.episode,
        }
        built = []
        for i, sv in enumerate(res.servers, 1):
            sid = _new_sid()
            http_sessions[sid] = {
                "url": sv["url"],
                "referer": sv["referer"] or primetv._cfg().get("engine_base_url", ""),
                "cookie": sv["cookie"] or "",
                "user_agent": sv["user_agent"] or "",
                "kind": "primetv",
                "idx": i - 1,
                "provider": provider_args,
                "created": time.monotonic(),
            }
            built.append({
                "site": "primetv",
                "name": "{} {}".format(label, i),
                "kind": sv["kind"],
                "quality": sv["quality"],
                "proxy_url": base + _proxy_url(sid, sv["url"], ext=_ext_for(sv["kind"])),
                "foreign": True,
            })
        return built

    ext_imdb, primetv_servers = await asyncio.gather(job_imdb(), job_primetv())

    imdb_id = ext_imdb or ""
    sub_langs: list[dict] = []
    if imdb_id and subs.is_enabled():
        try:
            found = await asyncio.to_thread(subs.search, imdb_id)
            sub_langs = subs.available_languages(found)
        except Exception as exc:
            log.warning("subtitle search failed: %s", exc)

    for s in primetv_servers:
        servers.append(s)

    return imdb_id, sub_langs


# Same cache namespace pattern but for /direct (raw media URLs, no proxy).
_direct_cache: dict[tuple, tuple[float, list[dict]]] = {}


def _direct_servers(req: WatchRequest) -> tuple[str, list[dict]]:
    """Scrape + resolve to raw media URLs; returns (query, [server dicts])."""
    import concurrent.futures

    if req.query:
        query = req.query
        candidates = [query]
    else:
        from scraper.tmdb import api_key, search_query, tmdb_title

        key = api_key()
        if not key:
            raise HTTPException(400, "TMDB_API_KEY is not set on the server")
        info = tmdb_title(req.tmdb_id, key=key, media_type=req.type)
        query = search_query(info)
        candidates = []
        for t in (query, info.get("original_title") or ""):
            t = (t or "").strip()
            if t and t not in candidates:
                candidates.append(t)
        candidates = candidates or [info.get("original_title") or info.get("title") or str(req.tmdb_id)]

    items: list[dict] = []
    seen: set[tuple] = set()
    for cand in candidates:
        for item in _scrape_all(cand, req.sites or []):
            key = (item.get("source"), item.get("detail_url") or item.get("id") or item.get("title"))
            if key not in seen:
                seen.add(key)
                items.append(item)

    jobs = [(item, sv) for item in items for sv in (item.get("watch_servers") or []) if sv.get("url")]

    servers: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            pool.submit(resolve_http, sv.get("url"), item.get("detail_url")): (item, sv)
            for item, sv in jobs
        }
        for fut in concurrent.futures.as_completed(futures):
            item, sv = futures[fut]
            got = fut.result()
            if not got:
                continue
            servers.append({
                "site": item.get("source"),
                "name": sv.get("name"),
                "kind": got["kind"],
                "url": got["url"],
            })
    return query, servers


@app.post("/direct")
async def direct(req: WatchRequest) -> dict:
    """Like /watch but returns the raw m3u8/media URLs — no proxy, no session.

    Body: {"tmdb_id": 27205, "type": "movie"} or {"query": "Inception"}
    Returns: {"tmdb_id", "type", "query", "servers": [{site, name, kind, url}]}
    The URLs are playable directly in ExoPlayer/VLC (EarnVids hls2 links are
    token-signed for ~36h and do not need a Referer header).
    """
    sites = tuple(s.strip() for s in (req.sites or []) if s.strip())
    if not sites and req.type == "movie":
        sites = ("akwams", "egydead")
    if not sites and not req.tmdb_id:
        raise HTTPException(400, "Provide 'sites' or 'tmdb_id'")
    req.sites = list(sites)

    cache_key = (req.query, req.tmdb_id, req.type, sites)
    entry = _direct_cache.get(cache_key)
    if entry is not None:
        expires, servers = entry
        if time.monotonic() <= expires:
            return {"tmdb_id": req.tmdb_id, "type": req.type, "query": req.query, "cached": True, "servers": servers}
        _direct_cache.pop(cache_key, None)

    query, servers = _direct_servers(req)
    _direct_cache[cache_key] = (time.monotonic() + _WATCH_CACHE_TTL, servers)
    return {"tmdb_id": req.tmdb_id, "type": req.type, "query": query, "servers": servers}


@app.get("/stream")
@app.get("/stream/{tail}")
async def stream(sid: str = Query(""), url: str = Query(...), ref: str = "", site_ref: str = "", tail: str = "", request: Request = None) -> Response:
    # The sid lives in the URL path (/stream/{sid}.m3u8?url=...) so ExoPlayer
    # can infer HLS/DASH from the extension; the query form (?sid=..) is also
    # supported for backwards compatibility and sub-resource URLs.
    if not sid and tail:
        sid = tail.split(".", 1)[0]
    status, content_type, body = None, "", b""
    mgr = get_manager()
    browser_session = mgr.sessions.get(sid) if mgr is not None else None
    http_ep = http_sessions.get(sid)

    # Session gone (e.g. server restart): re-resolve the embed from `ref` and
    # mint a fresh session under the same sid so previously copied links keep
    # working without re-scraping the whole site. The embed host serves the
    # full page only to its referer site (the movie/series page), so pass the
    # site referer through too.
    if http_ep is None and not (browser_session is not None and browser_session.active) and ref:
        re_got = await asyncio.to_thread(resolve_http, ref, site_ref or None)
        if re_got:
            http_sessions[sid] = {"url": re_got["url"], "referer": ref, "created": time.monotonic()}
            http_ep = http_sessions[sid]
            url = re_got["url"]

    if browser_session is not None and browser_session.active:
        got = await mgr.fetch(sid, url)
        if got:
            status, content_type, body = got
    elif http_ep:
        # Playlists/manifests need the full buffered body (we rewrite them).
        # Everything else — mp4, TS segments, DASH m4s — is STREAMED with Range
        # forwarding instead of buffering the whole 2-4 MB segment first, so
        # scrubbing starts instantly instead of after the whole segment
        # downloads from the CDN.
        if _path_lower(url).endswith((".m3u8", ".txt", ".mpd", "/manifest")):
            got = await asyncio.to_thread(_http_fetch, sid, url)
            if got:
                status, content_type, body = got
        else:
            client_range = request.headers.get("range") if request is not None else None
            streamed = await asyncio.to_thread(_http_media_stream, sid, url, client_range)
            if streamed is not None:
                up_status, up_ct, up_headers, chunks = streamed
                if up_status >= 400:
                    status, content_type, body = up_status, up_ct or "", b""
                else:
                    pass_headers = {
                        k: v
                        for k, v in up_headers.items()
                        if k.lower() in ("content-range", "content-length", "accept-ranges")
                    }
                    pass_headers.setdefault("Accept-Ranges", "bytes")
                    return StreamingResponse(chunks, media_type=up_ct, status_code=up_status, headers=pass_headers)
            else:
                got = await asyncio.to_thread(_http_fetch, sid, url)
                if got:
                    status, content_type, body = got

    is_playlist = bool(
        status is not None
        and 200 <= status < 400
        and ("mpegurl" in content_type or "m3u8" in content_type or _path_lower(url).endswith((".m3u8", ".txt")))
    )
    is_manifest = bool(
        status is not None
        and 200 <= status < 400
        and ("dash" in content_type or _path_lower(url).endswith((".mpd", "/manifest")))
    )

    # token died: refresh the session and retry once. Match on the URL path +
    # content-type for playlists (a dead-token response is not a 2xx), and also
    # refresh on 401/403 for any URL — segments share the token too. Cooldown
    # guards against re-resolving on every dropped segment.
    dead_token = bool(
        status is not None
        and status >= 400
        and (
            status in (401, 403)
            or _path_lower(url).endswith((".m3u8", ".txt", ".mpd", "/manifest"))
            or "mpegurl" in content_type
            or "dash" in content_type
        )
    )
    refresh_ok = True
    if http_ep:
        last = http_ep.get("last_refresh") or 0
        refresh_ok = time.monotonic() - last > 60
    if dead_token and refresh_ok:
        new_url = url
        refreshed = False
        if browser_session is not None:
            refreshed = await mgr.refresh_session(sid)
            new_url = getattr(browser_session, "new_url", url)
        elif http_ep:
            # HTTP sessions: re-resolve the embed and mint a fresh URL.
            from . import primetv

            if http_ep.get("kind") == "primetv":
                new_url, refreshed = await _refresh_primetv(http_ep)
            else:
                re_got = await asyncio.to_thread(resolve_http, http_ep["referer"], None)
                if re_got:
                    http_ep["url"] = re_got["url"]
                    new_url = re_got["url"]
                    refreshed = True
            if refreshed:
                http_ep["url"] = new_url
            http_ep["last_refresh"] = time.monotonic()
        if refreshed:
            got = await mgr.fetch(sid, new_url) if browser_session is not None else await asyncio.to_thread(_http_fetch, sid, new_url)
            if got:
                status, content_type, body = got
                url = new_url
                is_playlist = 200 <= status < 400 and ("mpegurl" in content_type or "m3u8" in content_type or _path_lower(url).endswith((".m3u8", ".txt")))
                is_manifest = 200 <= status < 400 and ("dash" in content_type or _path_lower(url).endswith((".mpd", "/manifest")))

    if status is None:
        return Response(status_code=503, content="session unavailable")

    if status >= 400:
        return Response(status_code=status, content=body[:2048], media_type=content_type or "text/plain")

    if is_playlist:
        rewritten = _rewrite_m3u8(body.decode("utf-8", "replace"), url, sid)
        return Response(content=rewritten, media_type="application/vnd.apple.mpegurl")

    if is_manifest:
        rewritten = _rewrite_mpd(body.decode("utf-8", "replace"), url, sid)
        return Response(content=rewritten, media_type="application/dash+xml")

    stripped = _strip_png_wrapper(body)
    if stripped is not body:
        media_type = "video/mp2t"
    elif _path_lower(url).endswith(".ts") and _ts_start(stripped) >= 0:
        # Some CDNs label TS segments as text/plain or octet-stream; ExoPlayer
        # sniffs containers but prefers an explicit video/mp2t for HLS chunks.
        media_type = "video/mp2t"
    else:
        media_type = content_type or "application/octet-stream"
    return StreamingResponse(iter([stripped]), media_type=media_type)


@app.get("/subtitle")
async def subtitle(
    imdb_id: str = Query(...),
    lang: str = Query("ara"),
    fmt: Literal["vtt", "srt"] = Query("vtt"),
) -> Response:
    """Serve a subtitle (converted to WebVTT) for an IMDb id and language.

    Re-searches OpenSubtitles fresh each time (the signed download links are
    short-lived), picks the highest-downloaded match for `lang`, downloads,
    decompresses and converts. Returns ``text/vtt`` for the native players.
    """
    from . import subtitles as subs

    if not subs.is_enabled():
        raise HTTPException(503, "subtitles disabled")
    found = await asyncio.to_thread(subs.search, imdb_id)
    best = subs.best_for_language(found, lang)
    if best is None:
        # A language outside the default result window needs a precise search.
        found = await asyncio.to_thread(subs.search, imdb_id, lang=lang)
        best = subs.best_for_language(found, lang)
    if best is None:
        raise HTTPException(404, "no subtitle for language %r" % lang)
    url = best.get("SubDownloadLink") or best.get("ZipDownloadLink")
    if not url:
        raise HTTPException(404, "subtitle has no download link")
    fetched = await asyncio.to_thread(subs.fetch_subtitle, url)
    if not fetched:
        raise HTTPException(502, "subtitle download failed")
    text, _ = fetched
    if fmt == "srt":
        return Response(content=text, media_type="text/plain; charset=utf-8")
    return Response(content=subs.srt_to_vtt(text), media_type="text/vtt; charset=utf-8")


@app.get("/", include_in_schema=False)
async def index() -> Response:
    """The in-browser tester page (add a movie and see the live servers)."""
    page = os.path.join(os.path.dirname(__file__), "static", "index.html")
    return FileResponse(
        page,
        media_type="text/html",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/stream-video")
async def stream_video(sid: str = Query(...), url: str = Query(...)) -> Response:
    """Alias for /stream (kept for API clarity)."""
    return await stream(sid=sid, url=url)


@app.delete("/session/{sid}")
async def close_session(sid: str) -> dict:
    http_sessions.pop(sid, None)
    mgr = get_manager()
    if mgr is not None and sid in mgr.sessions:
        await mgr.close_session(sid)
    return {"ok": True}


# ---------------------------------------------------------------------------
# TMDB browse endpoints (server-side key; the app never sees the API key)
# ---------------------------------------------------------------------------

_TMDB_API = "https://api.themoviedb.org/3"
_TMDB_IMAGE = "https://image.tmdb.org/t/p/w500"


def _tmdb_key() -> str:
    from scraper.tmdb import api_key

    key = api_key()
    if not key:
        raise HTTPException(400, "TMDB_API_KEY is not set on the server")
    return key


def _tmdb_items(payload: dict) -> list[dict]:
    items = []
    for r in payload.get("results") or []:
        title = r.get("title") or r.get("name") or ""
        original = r.get("original_title") or r.get("original_name") or title
        poster = r.get("poster_path") or ""
        items.append({
            "tmdb_id": r.get("id"),
            "media_type": r.get("media_type") or "movie",
            "title": title,
            "original_title": original,
            "overview": r.get("overview") or "",
            "poster": _TMDB_IMAGE + poster if poster else "",
            "vote_average": r.get("vote_average"),
        })
    return items


def _tmdb_fetch(path: str, params: dict) -> dict:
    """GET a TMDB endpoint with the server key and an Arabic locale."""
    import requests as _requests

    key = _tmdb_key()
    q = dict(params)
    q.setdefault("language", "ar-SA")
    resp = _requests.get(_TMDB_API + path, params={"api_key": key, **q}, timeout=20)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    data = resp.json()
    return {
        "page": data.get("page"),
        "total_pages": data.get("total_pages"),
        "total_results": data.get("total_results"),
        "items": _tmdb_items(data),
    }


def _tmdb_external_imdb(tmdb_id: int, media_type: str) -> str:
    """IMDb id from TMDB external_ids (used for /subtitle)."""
    import requests as _requests

    path = "/{}/{}/external_ids".format("tv" if media_type == "tv" else "movie", tmdb_id)
    resp = _requests.get(_TMDB_API + path, params={"api_key": _tmdb_key()}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("imdb_id") or ""


@app.get("/tmdb/popular")
async def tmdb_popular(type: str = Query("movie"), page: int = Query(1)) -> dict:
    kind = "tv" if type == "tv" else "movie"
    return await asyncio.to_thread(_tmdb_fetch, "/{}/popular".format(kind), {"page": page})


@app.get("/tmdb/trending")
async def tmdb_trending(time: str = Query("week"), page: int = Query(1)) -> dict:
    return await asyncio.to_thread(
        _tmdb_fetch, "/trending/all/{}".format(time), {"page": page}
    )


@app.get("/tmdb/search")
async def tmdb_search(q: str = Query(...), page: int = Query(1)) -> dict:
    return await asyncio.to_thread(
        _tmdb_fetch, "/search/multi", {"query": q.strip(), "page": page}
    )


@app.get("/health")
async def health() -> dict:
    from . import subtitles as subs

    mgr = get_manager()
    n_browser = len(mgr.sessions) if mgr is not None else 0
    providers = {}
    for name, mod in (("subtitles", subs),):
        providers[name] = {
            "enabled": mod.is_enabled(),
            "curl_cffi": bool(getattr(mod, "_IMPERSONATE", None)),
        }
    return {
        "ok": True,
        "sessions": n_browser + len(http_sessions),
        "cache_entries": len(_watch_cache),
        "providers": providers,
    }
