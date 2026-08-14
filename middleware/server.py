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
from typing import Literal
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


def _proxy_url(sid: str, media_url: str, name: str = "") -> str:
    return "/stream?" + urlencode({"sid": sid, "url": media_url})


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
    delay = float(os.environ.get("SCRAPE_DELAY", "0.25"))
    scraper = build_scraper(config, FetchSettings(delay=delay, timeout=20))
    try:
        return scraper.scrape(query, with_details=True, watch_only=True)
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


async def _resolve_many(servers: list[tuple[dict, dict]], limit: int = 6) -> list[tuple[dict, dict, dict]]:
    """Resolve embed URLs concurrently, with a concurrency cap."""
    sem = asyncio.Semaphore(limit)

    async def one(item: dict, sv: dict) -> tuple[dict, dict, dict]:
        async with sem:
            res = await _resolve_embed(sv.get("url") or "", referer=item.get("detail_url"))
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


def _http_fetch(sid: str, url: str) -> tuple[int, str, bytes] | None:
    """Plain-requests fetch for HTTP-resolved sessions (no browser)."""
    import requests

    ep = http_sessions.get(sid)
    if not ep:
        return None
    try:
        resp = requests.get(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
                ),
                "Referer": ep["referer"],
            },
            timeout=30,
        )
        return resp.status_code, resp.headers.get("content-type", ""), resp.content
    except requests.RequestException as exc:
        log.debug("http-proxy fetch failed for %s: %s", url, exc)
        return None


@app.post("/resolve")
async def resolve(req: ResolveRequest, request: Request) -> dict:
    result = await _resolve_embed(req.url, req.referer)
    if result.get("kind") != "none":
        base = str(request.base_url).rstrip("/")
        result["proxy_url"] = base + _proxy_url(result["sid"], result["url"])
    else:
        result["proxy_url"] = None
    return result


@app.post("/watch")
async def watch(req: WatchRequest, request: Request) -> dict:
    """Main-app contract: TMDB id (or raw query) -> list of playable servers.

    Body: {"tmdb_id": 27205, "type": "movie", "sites": ["akwams", "egydead"]}
    Returns: {"tmdb_id", "type", "query", "imdb_id", "subtitles",
              "servers": [{name, site, kind, proxy_url}]}
    """
    sites = [s.strip() for s in (req.sites or ["akwams", "egydead"]) if s.strip()]
    if not sites:
        raise HTTPException(400, "No sites selected")

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
    for cand in candidates:
        for item in await asyncio.to_thread(_scrape_all, cand, sites):
            key = (item.get("source"), item.get("detail_url") or item.get("id") or item.get("title"))
            if key not in seen:
                seen.add(key)
                merged_items.append(item)

    items = merged_items
    base = str(request.base_url).rstrip("/")

    to_resolve = []
    for item in items:
        for sv in item.get("watch_servers") or []:
            if sv.get("url"):
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
            "proxy_url": base + _proxy_url(res["sid"], res["url"]),
        })

    imdb_id, subtitles_list = await _add_foreign_servers(req, base, servers)
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


async def _add_foreign_servers(req: WatchRequest, base: str, servers: list[dict]) -> tuple[str, list[dict]]:
    """Resolve foreign (vidsrc) streams for a TMDB id and append proxied servers.

    Returns (imdb_id, subtitle languages) — the former is needed by the app to
    fetch subtitles later via /subtitle.
    """
    if not req.tmdb_id:
        return "", []
    from . import subtitles as subs
    from . import vidsrc

    imdb_id, sub_langs = "", []
    try:
        if vidsrc.is_enabled():
            res = await asyncio.to_thread(
                vidsrc.resolve, req.tmdb_id, req.type, season=req.season, episode=req.episode
            )
            label = vidsrc._cfg().get("label", "سيرفر أجنبي")
            for i, sv in enumerate(res.servers, 1):
                sid = _new_sid()
                http_sessions[sid] = {
                    "url": sv["url"],
                    "referer": vidsrc._cfg().get("player_referer", "https://cloudorchestranova.com/"),
                    "kind": "vidsrc",
                    "base_url": sv["base"],
                    "host": sv["host"],
                    "created": time.monotonic(),
                }
                servers.append({
                    "site": "vidsrc",
                    "name": "{} {}".format(label, i),
                    "kind": "hls",
                    "proxy_url": base + _proxy_url(sid, sv["url"]),
                    "foreign": True,
                })
            imdb_id = res.imdb_id
            if imdb_id and subs.is_enabled():
                found = await asyncio.to_thread(subs.search, imdb_id)
                sub_langs = subs.available_languages(found)
    except Exception as exc:
        log.warning("foreign/subtitle providers failed (%s); Arabic servers unaffected", exc)
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
        for item in _scrape_all(cand, req.sites or ["akwams", "egydead"]):
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
    sites = tuple(s.strip() for s in (req.sites or ["akwams", "egydead"]) if s.strip())
    if not sites:
        raise HTTPException(400, "No sites selected")
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
async def stream(sid: str = Query(...), url: str = Query(...)) -> Response:
    status, content_type, body = None, "", b""
    mgr = get_manager()
    browser_session = mgr.sessions.get(sid) if mgr is not None else None
    http_ep = http_sessions.get(sid)

    if browser_session is not None and browser_session.active:
        got = await mgr.fetch(sid, url)
        if got:
            status, content_type, body = got
    elif http_ep:
        got = await asyncio.to_thread(_http_fetch, sid, url)
        if got:
            status, content_type, body = got

    is_playlist = bool(
        status is not None
        and 200 <= status < 400
        and ("mpegurl" in content_type or "m3u8" in content_type or url.lower().endswith(".m3u8"))
    )

    # token died: refresh the session and retry once for playlists
    if status is not None and status >= 400 and (url.lower().endswith((".m3u8", ".txt")) or "mpegurl" in content_type):
        new_url = url
        refreshed = False
        if browser_session is not None:
            refreshed = await mgr.refresh_session(sid)
            new_url = getattr(browser_session, "new_url", url)
        elif http_ep:
            # HTTP sessions: re-resolve the embed and mint a fresh URL. For
            # vidsrc sessions the stream data is static — just re-mint the
            # host JWT and restamp the (token-less) master playlist URL.
            from . import vidsrc

            if http_ep.get("kind") == "vidsrc":
                new_url = await asyncio.to_thread(
                    vidsrc.restamp_master, http_ep.get("base_url") or url, http_ep.get("host") or ""
                )
                refreshed = bool(new_url)
            else:
                re_got = await asyncio.to_thread(resolve_http, http_ep["referer"], None)
                if re_got:
                    http_ep["url"] = re_got["url"]
                    new_url = re_got["url"]
                    refreshed = True
            if refreshed:
                http_ep["url"] = new_url
        if refreshed:
            got = await mgr.fetch(sid, new_url) if browser_session is not None else await asyncio.to_thread(_http_fetch, sid, new_url)
            if got:
                status, content_type, body = got
                url = new_url
                is_playlist = 200 <= status < 400 and "mpegurl" in content_type or url.lower().endswith(".m3u8")

    if status is None:
        return Response(status_code=503, content="session unavailable")

    if status >= 400:
        return Response(status_code=status, content=body[:2048], media_type=content_type or "text/plain")

    if is_playlist:
        rewritten = _rewrite_m3u8(body.decode("utf-8", "replace"), url, sid)
        return Response(content=rewritten, media_type="application/vnd.apple.mpegurl")

    stripped = _strip_png_wrapper(body)
    media_type = "video/mp2t" if stripped is not body else (content_type or "application/octet-stream")
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
    return FileResponse(page, media_type="text/html")


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


@app.get("/debug-subs")
async def debug_subs(imdb_id: str = "1375666") -> dict:
    """TEMP diagnostic: raw search response from Render (remove after diagnosis)."""
    import re as _re

    from . import subtitles as subs

    out = {"curl_cffi": bool(getattr(subs, "_IMPERSONATE", None))}
    imdb = _re.sub(r"^[tT]+", "", (imdb_id or "").strip())
    url = subs._cfg()["search_base"].format(imdb=imdb)
    headers = {
        "User-Agent": subs._ua(), "Accept": "application/json",
        "X-User-Agent": subs._cfg().get("x_user_agent") or "trailers.to-UA",
    }
    try:
        from curl_cffi import requests as cr
        r = cr.get(url, impersonate="chrome131", headers=headers, timeout=8)
        out["status"] = r.status_code
        out["ct"] = r.headers.get("content-type", "")[:50]
        out["len"] = len(r.content)
        out["head"] = r.text[:150]
    except Exception as exc:
        out["error"] = str(exc)[:160]
    return out


@app.get("/health")
async def health() -> dict:
    from . import subtitles as subs
    from . import vidsrc

    mgr = get_manager()
    n_browser = len(mgr.sessions) if mgr is not None else 0
    providers = {}
    for name, mod in (("vidsrc", vidsrc), ("subtitles", subs)):
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
