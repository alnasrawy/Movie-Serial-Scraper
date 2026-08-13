"""Pure-HTTP embed resolver: turn an embed page into a playable HLS URL
without any browser. This is the cheap/fast path used on low-memory hosts
(free Render tier); the Playwright-based BrowserManager remains the fallback.

Proven flow (EarnVids family: smoothpre.com):
    GET  embed page          -> page contains a Dean-Edwards-packed JS blob
    unpack_packer(page)      -> links.hls3 = "...zzw3dy72jlnh_,l,n,.urlset/master.txt"
    GET  master.txt          -> 200, application/vnd.apple.mpegurl   (Referer = embed)
The token inside the page is static text, so no JS execution is required.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlparse

import requests

from scraper.resolver import unpack_packer

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_URLSET_RE = re.compile(r'https?://[^"\'\s\)]+\.urlset[^"\'\s\)]*')
_M3U8_RE = re.compile(r'https?://[^\s"\'\\<>]+?\.m3u8[^\s"\'\\<>]*')
_SRC_RE = re.compile(
    r'(?:src|file|source)\s*[:=]\s*["\']([^"\']+?(?:\.m3u8|\.mp4|\.webm)[^"\']*)["\']',
    re.I,
)

_EMBED_TIMEOUT = 20.0
_MEDIA_TIMEOUT = 25.0


def _kind_of(url: str) -> str:
    low = url.lower()
    if any(k in low for k in (".m3u8", ".txt", ".urlset")) and (
        "m3u8" in low or "urlset" in low or low.endswith(".txt")
    ):
        return "hls"
    if re.search(r"\.(mp4|webm)(\?|$)", low):
        return "mp4"
    return "unknown"


def _candidates(text: str) -> list[str]:
    """Collect plausible media URLs from an embed page (HTML or decoded JS)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str):
        u = u.replace("\\", "")
        if u not in seen:
            seen.add(u)
            out.append(u)

    for pat in (_URLSET_RE, _M3U8_RE, _SRC_RE):
        for m in pat.finditer(text):
            add(m.group(0))
    return out


def resolve_http(embed_url: str, referer: str | None = None) -> dict | None:
    """Try to resolve ``embed_url`` to a media URL using plain HTTP.

    Returns ``{"url": str, "kind": str}`` on success, ``None`` otherwise.
    """
    try:
        embed_url = embed_url.strip()
        page_ref = referer or "{}://{}/".format(
            urlparse(embed_url).scheme, urlparse(embed_url).netloc
        )
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})

        r = sess.get(embed_url, headers={"Referer": page_ref}, timeout=_EMBED_TIMEOUT)
        if r.status_code >= 400:
            log.info("http-resolve embed failed: %s -> %s", embed_url, r.status_code)
            return None
        text = r.text

        decoded = unpack_packer(text) if unpack_packer(text) else None
        raw = decoded or text
        cands = _candidates(raw)
        log.debug("http-resolve candidates for %s: %s", embed_url, cands)

        for u in cands:
            try:
                mr = sess.get(u, headers={"Referer": embed_url}, timeout=_MEDIA_TIMEOUT)
            except requests.RequestException:
                continue
            if mr.status_code < 400:
                kind = _kind_of(mr.url)
                if kind != "unknown":
                    log.info("http-resolve OK: %s -> %s", embed_url, mr.url[:120])
                    return {"url": mr.url, "kind": kind}
        log.info("http-resolve no playable candidate for %s", embed_url)
    except requests.RequestException as exc:
        log.debug("http-resolve request error for %s: %s", embed_url, exc)
    except Exception:
        log.exception("http-resolve unexpected error for %s", embed_url)
    return None
