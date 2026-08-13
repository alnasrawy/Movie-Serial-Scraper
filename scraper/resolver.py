"""Resolve video-host embed pages to direct media URLs where possible.

Video hosts (EarnVids, Mixdrop, StreamHG, DoodStream, ...) hide the real
video URL behind JavaScript on the embed page. This module tries, per host,
to reconstruct that URL.

Important: these sites fight scraping and change their schemes often, and the
CDNs frequently reject non-browser requests (HTTP 403) even with the correct
URL. Treat ``direct_url`` as best-effort; the original ``embed`` page is the
reliable fallback (plays in any WebView-based player).
"""

from __future__ import annotations

import logging
import re

from .fetcher import Fetcher

log = logging.getLogger(__name__)

_PACKER_RE = re.compile(
    r"}\('(.*?)',\s*(\d+),\s*(\d+),\s*'((?:[^'\\]|\\.)*)'\.split\('\|'\)",
    re.S,
)
_TOKEN_RE = re.compile(r"\b([A-Za-z0-9_$]+)\b")
_M3U8_RE = re.compile(r'https?://[^\s"\'\\<>]+?\.m3u8[^\s"\'\\<>]*')
_MP4_RE = re.compile(r'https?://[^\s"\'\\<>]+?\.(?:mp4|webm)[^\s"\'\\<>]*')
_SRC_RE = re.compile(r'(?:src|file|source)\s*[:=]\s*["\']([^"\']+?(?:\.m3u8|\.mp4|\.webm)[^"\']*)["\']', re.I)


def unpack_packer(text: str) -> str | None:
    """Decode a 'Dean Edwards' style packed JS snippet, if present."""
    m = _PACKER_RE.search(text)
    if not m:
        return None
    payload, radix, count, table = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4).split("|")
    if len(table) != count:
        return None

    def repl(mo):
        tok = mo.group(1)
        try:
            n = int(tok, radix)
        except ValueError:
            return tok
        if 0 <= n < count:
            return table[n]
        return tok

    return _TOKEN_RE.sub(repl, payload)


def _best_hls(text: str) -> str | None:
    for pat in (_M3U8_RE, _SRC_RE):
        m = pat.search(text)
        if m:
            return m.group(1) if pat is _SRC_RE else m.group(0)
    m = _MP4_RE.search(text)
    return m.group(0) if m else None


def _fetch_text(fetcher: Fetcher, url: str, referer: str | None = None) -> str | None:
    if not referer:
        from urllib.parse import urlparse

        referer = f"{urlparse(url).scheme}://{urlparse(url).netloc}/"
    headers = {"Referer": referer}
    try:
        page = fetcher.get_soup(url, page_budget=False, headers=headers)
    except Exception as exc:
        log.debug("Resolve fetch failed for %s: %s", url, exc)
        return None
    return str(page.soup) if page.soup is not None else None


def resolve_embed(url: str, fetcher: Fetcher, referer: str | None = None) -> dict:
    """Try to find a direct media URL for an embed page.

    Returns {"direct_url": str, "method": str} on success, {} otherwise.
    """
    host = (url or "").lower()
    if any(h in host for h in ("morencius.com", "vidhide.com", "earnvids.com", "smoothpre.com", "minochinos.com")):
        return _resolve_earnvids(url, fetcher, referer)

    text = _fetch_text(fetcher, url, referer)
    if not text:
        return {}
    direct = _best_hls(text)
    if not direct:
        decoded = unpack_packer(text)
        if decoded:
            direct = _best_hls(decoded)
    if direct:
        return {"direct_url": direct, "method": "regex"}
    return {}


def _resolve_earnvids(url: str, fetcher: Fetcher, referer: str | None = None) -> dict:
    text = _fetch_text(fetcher, url, referer)
    if not text:
        return {}
    decoded = unpack_packer(text)
    direct = _best_hls(decoded) if decoded else None
    if not direct:
        return {}
    log.info("Resolved EarnVids embed to direct URL")
    return {"direct_url": direct, "method": "packer"}
