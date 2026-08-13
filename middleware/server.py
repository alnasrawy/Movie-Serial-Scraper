"""FastAPI service that turns embed pages into native-player media streams.

Endpoints:
    POST /resolve  {"url": embed, "referer": optional}
                   -> {"sid", "kind", "url", "proxy_url", ...}
    GET  /stream   {sid, url} -> HLS playlist (rewritten) or media bytes,
                   transparently proxied through the embed's browser session.
    GET  /health

Run:  python -m uvicorn middleware.server:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from urllib.parse import urljoin, urlencode

from fastapi import FastAPI, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from .player import BrowserManager

log = logging.getLogger(__name__)
app = FastAPI(title="Embed resolver middleware")
manager = BrowserManager()


class ResolveRequest(BaseModel):
    url: str
    referer: str | None = None


def _proxy_url(sid: str, media_url: str, name: str = "") -> str:
    return "/stream?" + urlencode({"sid": sid, "url": media_url})


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


@app.post("/resolve")
async def resolve(req: ResolveRequest, request: Request) -> dict:
    result = await manager.open_session(req.url, req.referer)
    if result.get("kind") != "none":
        base = str(request.base_url).rstrip("/")
        result["proxy_url"] = base + _proxy_url(result["sid"], result["url"])
    else:
        result["proxy_url"] = None
    return result


@app.get("/stream")
async def stream(sid: str = Query(...), url: str = Query(...)) -> Response:
    status, content_type, body = None, "", b""
    if manager.sessions.get(sid) and manager.sessions[sid].active:
        got = await manager.fetch(sid, url)
        if got:
            status, content_type, body = got

    is_playlist = bool(
        status is not None
        and 200 <= status < 400
        and ("mpegurl" in content_type or "m3u8" in content_type or url.lower().endswith(".m3u8"))
    )

    # token died: refresh the embed session and retry once for playlists
    if status is not None and status >= 400 and (url.lower().endswith((".m3u8", ".txt")) or "mpegurl" in content_type):
        if await manager.refresh_session(sid):
            new_url = getattr(manager.sessions.get(sid), "new_url", url)
            got = await manager.fetch(sid, new_url)
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
    await manager.close_session(sid)
    return {"ok": True}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "sessions": len(manager.sessions)}
