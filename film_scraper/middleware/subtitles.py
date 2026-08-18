"""Subtitle provider: OpenSubtitles legacy search + download + srt->vtt.

The classic ``rest.opensubtitles.org/search/imdbid-<imdb>`` endpoint is still
alive and keyed only by IMDb id — no API key, no login. Each search returns a
JSON array of subtitles (language, name, download-count, signed download URL).

Design:
    * ``search(imdb_id)``  -> list of matches (fresh every call; the signed
      ``vrf`` download links are short-lived).
    * ``available_languages(subs)`` -> ordered {code: count} for the player UI.
    * ``best_for_language(subs, code)`` -> highest-downloaded match for a lang.
    * ``fetch(url)`` -> gzip/zip/plain subtitle bytes, decompressed + decoded.
    * ``srt_to_vtt`` -> the WebVTT the native players expect.

Config: ``configs/providers.json`` -> the ``subtitles`` block.
"""

from __future__ import annotations

import gzip
import io
import json
import logging
import os
import re
import time
import zipfile
from typing import Any

import requests as _fallback_http

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome131"
except Exception:  # pragma: no cover - curl_cffi optional
    _http = _fallback_http
    _IMPERSONATE = None

log = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "subtitles": {
        "opensubtitles": {
            "enabled": True,
            "search_base": "https://rest.opensubtitles.org/search/imdbid-{imdb}",
            "max_results": 100,
            "user_agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "x_user_agent": "trailers.to-UA",
            "doh_url": "https://cloudflare-dns.com/dns-query",
            "retries": 2,
        }
    }
}

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json"
)

# The legacy API 403s non-browser user agents; a Chrome UA is required even
# though the endpoint is plain JSON.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_SEARCH_CACHE_TTL = float(os.environ.get("SUBS_CACHE_TTL", "1800"))
_search_cache: dict[str, tuple[float, list[dict]]] = {}


def _cfg() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG["subtitles"]["opensubtitles"])
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as fh:
            stored = json.load(fh).get("subtitles", {}).get("opensubtitles") or {}
        cfg.update({k: v for k, v in stored.items() if v is not None})
    except (OSError, ValueError):
        pass
    return cfg


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


def _ua() -> str:
    return _cfg().get("user_agent") or _DEFAULT_UA


def _get(url: str, headers: dict, timeout: float):
    """GET through curl_cffi (browser TLS fingerprint) with a requests fallback.

    The OpenSubtitles WAF rate-limits plain ``requests`` fingerprints (it can
    even poison DNS for them); a real browser fingerprint keeps the API happy.
    If the normal resolver is down (DNS poisoning blips), retry once over
    DNS-over-HTTPS before falling back to plain requests.
    """
    if _IMPERSONATE:
        try:
            return _http.get(url, impersonate=_IMPERSONATE, headers=headers, timeout=timeout)
        except Exception:
            pass
        doh = _cfg().get("doh_url")
        if doh:
            try:
                return _http.get(
                    url, impersonate=_IMPERSONATE, headers=headers, timeout=timeout, doh_url=doh
                )
            except Exception:
                pass
    return _fallback_http.get(url, headers=headers, timeout=timeout)


def search(imdb_id: str, timeout: float = 20.0, lang: str | None = None) -> list[dict]:
    """Return raw OpenSubtitles matches for an IMDb id (cached briefly).

    ``lang`` (optional) appends ``sublanguageid-<lang>`` so a language that is
    outside the default result window can be fetched precisely.
    """
    imdb = (imdb_id or "").strip()
    if not imdb:
        return []
    # The legacy API only accepts the numeric IMDb id; the `tt` prefix makes its
    # WAF redirect the request into a sinkhole (`_` host) that fails DNS.
    imdb = re.sub(r"^[tT]+", "", imdb)
    if not imdb.isdigit():
        return []
    lang = (lang or "").strip().lower() or None
    cache_key = (imdb, lang)
    cached = _search_cache.get(cache_key)
    if cached is not None and time.monotonic() <= cached[0]:
        return cached[1]

    url = _cfg().get("search_base", "").format(imdb=imdb)
    if lang:
        url += "/sublanguageid-" + lang
    headers = {"User-Agent": _ua(), "Accept": "application/json"}
    x_ua = _cfg().get("x_user_agent")
    if x_ua:
        # The legacy API blocks unknown user agents / flagged IPs but honours a
        # registered X-User-Agent (their own player sends trailers.to-UA).
        headers["X-User-Agent"] = x_ua
    retries = int(_cfg().get("retries", 2))
    for attempt in range(retries + 1):
        try:
            resp = _get(url, headers, timeout)
            if resp.status_code >= 400:
                log.info("subtitle search failed: %s -> %s", url, resp.status_code)
            else:
                items = (resp.json() or [])[: int(_cfg().get("max_results", 100))]
                _search_cache[cache_key] = (time.monotonic() + _SEARCH_CACHE_TTL, items)
                return items
        except Exception as exc:
            log.debug("subtitle search error for %s: %s", imdb, exc)
        if attempt < retries:
            time.sleep(0.7 * (attempt + 1))
    return []


def available_languages(subs: list[dict]) -> list[dict]:
    """Languages present in a search result, most-matched first."""
    counts: dict[str, int] = {}
    for s in subs:
        code = (s.get("SubLanguageID") or "").strip() or (s.get("ISO639") or "")
        if code:
            counts[code] = counts.get(code, 0) + 1
    names = {
        "ara": "العربية", "arb": "العربية", "eng": "English", "fre": "Français",
        "spa": "Español", "tur": "Türkçe", "per": "فارسی", "ger": "Deutsch",
        "ita": "Italiano", "por": "Português", "rus": "Русский", "pol": "Polski",
        "nld": "Nederlands", "hin": "हिन्दी", "heb": "עברית", "ukr": "Українська",
    }
    return [
        {"code": c, "name": names.get(c, c), "count": n}
        for c, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]


def best_for_language(subs: list[dict], lang: str) -> dict | None:
    """Best (most downloaded) subtitle for a language code, or None."""
    lang = (lang or "").strip().lower()
    matches = [s for s in subs if (s.get("SubLanguageID") or "").strip().lower() == lang]
    if not matches:
        return None
    return max(matches, key=lambda s: int(s.get("SubDownloadsCnt") or 0))


def _decompress(data: bytes) -> tuple[bytes, str]:
    """Decompress gzip/zip subtitle payloads; return (bytes, label)."""
    if data[:2] == b"\x1f\x8b":
        return gzip.decompress(data), "gzip"
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            name = zf.namelist()[0]
            return zf.read(name), "zip"
    return data, "plain"


def _decode(data: bytes) -> str:
    """Best-effort text decoding (Arabic subs are cp1256/utf-8)."""
    for enc in ("utf-8-sig", "utf-8", "cp1256", "latin-1"):
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, ValueError):
            continue
    return data.decode("latin-1", errors="replace")


def fetch_subtitle(url: str, timeout: float = 20.0) -> tuple[str, str] | None:
    """Download + decompress + decode a subtitle; returns (text, format)."""
    try:
        resp = _get(
            url,
            {"User-Agent": _ua(), "Referer": "https://rest.opensubtitles.org/"},
            timeout,
        )
        if resp.status_code >= 400:
            log.info("subtitle download failed: %s -> %s", url, resp.status_code)
            return None
        data, _ = _decompress(resp.content)
        return _decode(data), (resp.headers.get("content-type") or "")
    except Exception as exc:  # provider failure must never break /watch
        log.debug("subtitle download error for %s: %s", url, exc)
        return None


_TS_RE = re.compile(r"(\d{2}:\d{2}:\d{2})[,.](\d{1,3})")


def srt_to_vtt(text: str) -> str:
    """Convert SRT text to WebVTT (dialogue lines only, safe for Arabic)."""
    lines = []
    for line in (text or "").splitlines():
        if "-->" in line:
            lines.append(_TS_RE.sub(lambda m: m.group(1) + "." + m.group(2).ljust(3, "0"), line))
        elif not line.strip() or line.strip().isdigit():
            lines.append("")
        else:
            lines.append(line)
    vtt = []
    seen_dialogue = False
    for line in lines:
        if line.strip():
            seen_dialogue = True
        if not seen_dialogue:
            continue
        vtt.append(line)
    return "WEBVTT\n\n" + "\n".join(vtt).strip() + "\n"
