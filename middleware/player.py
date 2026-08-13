"""Browser-backed embed resolver and media proxy.

Video hosts (acek-cdn, vibuxer, hgcloud...) protect streams with JS-generated
short-lived tokens and block plain HTTP clients.  A real Chromium executes the
page, and Playwright's APIRequestContext (which shares the session cookie jar
but not the detectable headless TLS headers) fetches the actual HLS playlists
and segments.  Because the URL tokens die within minutes, the proxy refreshes
the session (reloads the embed) and re-writes the playlist on demand, which is
exactly what keeps playback alive during a full movie.
"""

from __future__ import annotations

import logging
import time
import uuid

from playwright.async_api import async_playwright

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

SESSION_TTL = 1800  # seconds a session lives after last use

_VIDEO_PATH_HINTS = (".m3u8", ".mp4", ".webm", "master.txt", ".urlset")
_VIDEO_CT_HINTS = ("mpegurl", "video/", "application/octet-stream", "mp4")
_AD_HINTS = (".js", ".css", ".png", ".jpeg", ".jpg", ".gif", ".svg", ".woff",
             "google", "doubleclick", "adservice", "googletag", "adsystem",
             "imasdk", "greatdexchange", "popunder", "analytics", "gtag",
             "fonts", "favicon", "hotjar", "sentry", "dns-prefetch")

_PLAY_SELECTORS = [
    "button.play", ".play-button", "#play-btn", "#btnplay", ".play_btn",
    "[data-player-button-play]", ".vjs-big-play-button",
    "button[aria-label*='play' i]",
    ".jw-display-icon-container", ".jw-icon-display", ".jw-icon-play",
    ".jw-svg-icon-play", ".jw-button-color", ".jw-playback-rate-container button",
]


class Session:
    def __init__(self, sid: str, context, page, url: str, referer: str) -> None:
        self.sid = sid
        self.context = context
        self.page = page
        self.url = url
        self.referer = referer or ""
        self.created = time.time()
        self.last_use = time.time()
        self.active = True

    def is_expired(self) -> bool:
        return time.time() - self.last_use > SESSION_TTL


def _is_media_candidate(url: str, content_type: str) -> bool:
    low = url.lower()
    ct = (content_type or "").lower()
    if any(h in low for h in _AD_HINTS):
        return False
    return any(h in low for h in _VIDEO_PATH_HINTS) or any(h in ct for h in _VIDEO_CT_HINTS)


def _kind_of(url: str) -> str:
    low = url.lower()
    if ".m3u8" in low or "mpegurl" in low or "master.txt" in low or ".urlset" in low or "hls3" in low:
        return "hls"
    if low.endswith((".mp4", ".webm")):
        return "mp4"
    return "unknown"


class BrowserManager:
    """One long-lived Chromium that owns short-lived browser contexts."""

    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self.sessions: dict[str, Session] = {}
        self._lock = None

    async def _ensure(self):
        if self._browser is None:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--mute-audio",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
        return self._browser

    def _cleanup(self) -> None:
        for sid in [s for s in self.sessions if self.sessions[s].is_expired()]:
            s = self.sessions.pop(sid)
            log.info("closing expired session %s", sid)
            try:
                s.context.close()
            except Exception:
                pass

    async def open_session(self, url: str, referer: str | None = None, *, timeout: float = 40.0) -> dict:
        """Load ``url`` in a headless browser and register a streaming session."""
        self._cleanup()
        browser = await self._ensure()
        context = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720})
        page = await context.new_page()
        candidates: list[dict] = []
        error: str | None = None

        def on_response(resp) -> None:
            ct = resp.headers.get("content-type", "")
            if _is_media_candidate(resp.url, ct):
                candidates.append({"url": resp.url, "status": resp.status, "ct": ct})

        page.on("response", on_response)
        try:
            await page.goto(url, referer=referer or url, wait_until="domcontentloaded", timeout=25000)
        except Exception as exc:
            error = str(exc)[:200]
            log.info("goto failed: %s", error)
        try:
            await page.evaluate(
                """() => {
                    const sels = %s;
                    for (const s of sels) {
                        const el = document.querySelector(s);
                        if (el) { el.click(); return; }
                    }
                    const v = document.querySelector('video');
                    if (v) { v.muted = true; v.play().catch(() => {}); }
                }""" % _PLAY_SELECTORS
            )
        except Exception:
            pass

        waited = 0
        while waited < timeout * 1000:
            if candidates:
                break
            try:
                src = await page.evaluate("document.querySelector('video')?.src || ''")
                if src and src.startswith("http"):
                    candidates.append({"url": src, "status": 200, "ct": ""})
                    break
            except Exception:
                pass
            await page.wait_for_timeout(500)
            waited += 500

        if not candidates:
            sid = uuid.uuid4().hex
            self.sessions[sid] = Session(sid, context, page, url, referer or "")
            return {"sid": sid, "kind": "none", "url": url, "error": error or "no media request captured"}

        best = candidates[0]
        for c in candidates:
            if c["status"] == 200 and not best["status"] == 200:
                best = c
        sid = uuid.uuid4().hex
        self.sessions[sid] = Session(sid, context, page, best["url"], referer or "")
        return {
            "sid": sid,
            "kind": _kind_of(best["url"]),
            "url": best["url"],
            "status": best["status"],
            "candidates": [c["url"] for c in candidates],
            "error": error,
        }

    async def refresh_session(self, sid: str, timeout: float = 30.0) -> bool:
        """Reload the embed to mint a fresh token/URL. Returns True on success."""
        s = self.sessions.get(sid)
        if not s or not s.active:
            return False
        try:
            await s.page.goto(s.url, wait_until="domcontentloaded", timeout=20000)
            await s.page.wait_for_timeout(3000)
            new = await self.open_session(s.url, s.referer, timeout=timeout)
            if new.get("kind") == "none":
                return False
            s.new_url = new["url"]
            s.last_use = time.time()
            return True
        except Exception as exc:
            log.info("refresh %s failed: %s", sid, exc)
            return False

    async def fetch(self, sid: str, url: str) -> tuple[int, str, bytes] | None:
        """Fetch a playlist/segment through the session's own cookie jar."""
        s = self.sessions.get(sid)
        if not s or not s.active:
            return None
        s.last_use = time.time()
        try:
            # the CDN validates the embed page referer; ctx.request does not
            # set one automatically, so replay the session's own referer.
            referer = s.url
            headers = {"Referer": referer}
            resp = await s.context.request.get(url, headers=headers, timeout=30000)
            body = await resp.body()
            return resp.status, resp.headers.get("content-type", ""), body
        except Exception as exc:
            log.info("fetch %s failed: %s", sid, exc)
            return None

    async def close_session(self, sid: str) -> None:
        s = self.sessions.pop(sid, None)
        if s:
            s.active = False
            try:
                await s.context.close()
            except Exception:
                pass
