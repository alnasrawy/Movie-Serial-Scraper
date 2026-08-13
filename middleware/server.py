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
import uuid
from typing import Literal
from urllib.parse import urljoin, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from .http_resolver import resolve_http

log = logging.getLogger(__name__)
app = FastAPI(title="Embed resolver middleware")

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


def _proxy_url(sid: str, media_url: str, name: str = "") -> str:
    return "/stream?" + urlencode({"sid": sid, "url": media_url})


def _scrape_all(query: str, sites: list[str]) -> list[dict]:
    """Scrape watch servers for a query from the given site configs."""
    from scraper.fetcher import FetchSettings
    from scraper.sites import build_scraper, find_config

    items: list[dict] = []
    for name in sites:
        config = find_config(name)
        if config is None:
            log.warning("Unknown site %r skipped", name)
            continue
        config.custom["resolve_servers"] = False
        config.custom["verify_servers"] = False
        config.custom["label_servers"] = True
        scraper = build_scraper(config, FetchSettings(delay=0.4, timeout=20))
        items.extend(scraper.scrape(query, with_details=True, watch_only=True))
    return items


async def _resolve_embed(url: str, referer: str | None) -> dict:
    """Resolve one embed, HTTP-first, browser as fallback.

    Returns a dict like BrowserManager.open_session: {sid, kind, url, error}.
    """
    got = await asyncio.to_thread(resolve_http, url, referer)
    if got:
        sid = _new_sid()
        http_sessions[sid] = {"url": got["url"], "referer": url}
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
    Returns: {"tmdb_id", "type", "query", "servers": [{name, site, kind, proxy_url}]}
    """
    sites = [s.strip() for s in (req.sites or ["akwams", "egydead"]) if s.strip()]
    if not sites:
        raise HTTPException(400, "No sites selected")

    if req.query:
        query = req.query
        info = {"title": query}
    else:
        if not req.tmdb_id:
            raise HTTPException(400, "Provide 'tmdb_id' or 'query'")
        from scraper.tmdb import api_key, search_query, tmdb_title

        key = api_key()
        if not key:
            raise HTTPException(400, "TMDB_API_KEY is not set on the server")
        info = await asyncio.to_thread(tmdb_title, req.tmdb_id, key=key, media_type=req.type)
        query = search_query(info)

    items = await asyncio.to_thread(_scrape_all, query, sites)
    base = str(request.base_url).rstrip("/")

    servers: list[dict] = []
    for item in items:
        for sv in item.get("watch_servers") or []:
            url = sv.get("url")
            if not url:
                continue
            res = await _resolve_embed(url, referer=item.get("detail_url"))
            if res.get("kind") == "none":
                continue
            servers.append({
                "site": item.get("source"),
                "name": sv.get("name"),
                "original_name": sv.get("original_name"),
                "kind": res["kind"],
                "proxy_url": base + _proxy_url(res["sid"], res["url"]),
            })

    return {
        "tmdb_id": req.tmdb_id,
        "type": req.type,
        "query": query,
        "servers": servers,
    }


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
            # HTTP sessions: re-resolve the embed and mint a fresh URL
            re_got = await asyncio.to_thread(resolve_http, http_ep["referer"], None)
            if re_got:
                http_ep["url"] = re_got["url"]
                new_url = re_got["url"]
                refreshed = True
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


@app.get("/health")
async def health() -> dict:
    mgr = get_manager()
    n_browser = len(mgr.sessions) if mgr is not None else 0
    return {"ok": True, "sessions": n_browser + len(http_sessions)}
