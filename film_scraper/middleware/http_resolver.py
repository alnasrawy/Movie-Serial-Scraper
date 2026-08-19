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

# Hosts known to be impossible to resolve over plain HTTP (browser-only JS,
# reCAPTCHA, dead origins). Skipping them avoids a wasted 20s GET+timeout
# during /watch resolve, which is the main latency driver on the free tier.
# Each entry is a host fragment matched against the embed URL's netloc.
_HTTP_UNRESOLVABLE = (
    "hgcloud.to",
    "vibuxer",
    "acek-cdn",
    "bibplayer",
    "mixdrop",
    "playmogo",
    "dsvplay",
    "koramaup",
    "uqload",
    "okru",
    "voe.",
    "stmruby",
)


def _http_hopeless(embed_url: str) -> bool:
    netloc = urlparse(embed_url).netloc.lower()
    return any(frag in netloc for frag in _HTTP_UNRESOLVABLE)


# Hosts that can't be resolved even with a real browser: mixdrop (reCAPTCHA),
# playmogo/dsvplay (never start media), koramaup (obfuscated JS), minochinos
# (no packer). Distinct from _HTTP_UNRESOLVABLE, which lists HTTP-only hosts
# that the browser CAN resolve (vibuxer/hgcloud/okru).
_BROWSER_HOPELESS = (
    "mixdrop",
    "playmogo",
    "dsvplay",
    "koramaup",
    "minochinos",
)


def _browser_hopeless(embed_url: str) -> bool:
    netloc = urlparse(embed_url).netloc.lower()
    return any(frag in netloc for frag in _BROWSER_HOPELESS)


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


def _resolve_vidaraa(embed_url: str, referer: str | None = None) -> dict | None:
    """Vidaraa embeds (``vidaraa.cc/e/<code>``): POST the SPA's stream API.

    The embed page blocks foreign Referers (``akwams.org`` -> 403
    "Embedding not allowed"), so a matching site referer is required —
    exactly what the resolver gets from the host site's detail page. The
    returned ``streaming_url`` carries an IP-bound token, so it must be
    played through the /stream proxy (the caller always does).
    """
    try:
        m = re.search(r"/e/([^/?#]+)", embed_url)
        if not m:
            return None
        code = m.group(1)
        base = "{}://{}".format(urlparse(embed_url).scheme, urlparse(embed_url).netloc)
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})
        refs = [referer, embed_url] if referer else [embed_url]
        for ref in refs:
            try:
                r = sess.post(
                    base + "/api/stream",
                    headers={"Referer": ref, "Content-Type": "application/json"},
                    json={"filecode": code, "device": "web"},
                    timeout=_EMBED_TIMEOUT,
                )
            except requests.RequestException:
                continue
            if r.status_code >= 400:
                continue
            try:
                data = r.json()
            except ValueError:
                continue
            url = (data or {}).get("streaming_url")
            if url:
                kind = _kind_of(url)
                if kind != "unknown":
                    log.info("http-resolve OK (vidaraa): %s -> %s", embed_url, url[:120])
                    return {"url": url, "kind": kind}
        log.info("http-resolve no playable candidate for %s", embed_url)
    except Exception:
        log.exception("http-resolve vidaraa error for %s", embed_url)
    return None


def _resolve_bysekoze(embed_url: str) -> dict | None:
    """Bysekoze embeds (``bysekoze.com/e/<code>``): fetch the SPA's video API.

    The SPA (a React bundle) loads ``/api/videos/<code>/`` which returns an
    AES-256-GCM encrypted payload split across many base64url ``key_parts``.
    The key is assembled from the two parts indexed by ``[version, 31-version]``
    (see the minified ``Ea`` in the bundle) — no browser needed. The decrypted
    JSON holds the real HLS master URL.
    """
    try:
        m = re.search(r"/e/([^/?#]+)", embed_url)
        if not m:
            return None
        code = m.group(1)
        base = "{}://{}".format(urlparse(embed_url).scheme, urlparse(embed_url).netloc)
        sess = requests.Session()
        sess.headers.update({"User-Agent": UA, "Accept-Language": "ar,en;q=0.9"})

        r = sess.get("{}/api/videos/{}/".format(base, code), timeout=_EMBED_TIMEOUT)
        if r.status_code >= 400:
            log.info("http-resolve bysekoze api failed: %s -> %s", embed_url, r.status_code)
            return None
        data = r.json()
        pb = data.get("playback") or {}
        parts = pb.get("key_parts") or []
        if not parts or not pb.get("payload"):
            return None

        def b64url(s: str) -> bytes:
            s = s.replace("-", "+").replace("_", "/")
            s += "=" * ((4 - len(s) % 4) % 4)
            return __import__("base64").b64decode(s)

        ver = int(pb.get("version") or 0)
        idx = [ver, 31 - ver]
        sel = [parts[i - 1] for i in idx if 1 <= i <= len(parts)]
        key = b"".join(b64url(p) for p in sel)
        iv = b64url(pb["iv"])
        ct = b64url(pb["payload"])

        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plain = AESGCM(key).decrypt(iv, ct, None).decode("utf-8", "replace")
        decoded = __import__("json").loads(plain)
        for src in decoded.get("sources") or []:
            url = src.get("url")
            if url:
                kind = _kind_of(url)
                if kind != "unknown":
                    log.info("http-resolve OK (bysekoze): %s -> %s", embed_url, url[:120])
                    return {"url": url, "kind": kind}
        log.info("http-resolve no playable source in bysekoze payload for %s", embed_url)
    except Exception:
        log.exception("http-resolve bysekoze error for %s", embed_url)
    return None


def resolve_http(embed_url: str, referer: str | None = None) -> dict | None:
    """Try to resolve ``embed_url`` to a media URL using plain HTTP.

    Returns ``{"url": str, "kind": str}`` on success, ``None`` otherwise.
    """
    try:
        embed_url = embed_url.strip()
        netloc = urlparse(embed_url).netloc.lower()
        if _http_hopeless(embed_url):
            log.info("http-resolve skip (known hopeless): %s", embed_url)
            return None
        if "vidaraa" in netloc:
            return _resolve_vidaraa(embed_url, referer)
        if "bysekoze" in netloc:
            return _resolve_bysekoze(embed_url)
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
