"""Foreign stream provider (vidsrc family) — plain HTTP + WebAssembly, no browser.

Resolving a TMDB id to playable HLS playlists is a five-step HTTP flow:

    1. GET https://vidsrcme.ru/embed/{movie|tv}/{tmdb_id}
       -> parse the cloudorchestranova <iframe src> (it carries the `vs` token
          as static text; no JS execution needed).
    2. GET that iframe URL -> read the inline `window.CONFIG` JSON. For movies
       it carries `api` (the stream-data endpoint); for TV it carries
       `streamBase` + a season/episode map.
    3. GET the api URL -> `data.stream_urls` is either an array (plain path) or
       a single base64 ChaCha20(nonce||ciphertext) string protected by a
       per-5-minute-window WebAssembly decryptor at `vs.wasm_url`.
    4. Decrypt via wasmtime (pure Python WASM runtime; tiny, transient memory).
    5. GET {stream_host}/generate.php -> an IP-bound JWT (valid ~4h). Append it
       to the master playlist URL as `?token=` (variants/segments need none).

The JWT is bound to the server's IP, so clients must play these URLs through
our `/stream` proxy (server-side fetch). This module is config-driven via
`configs/providers.json` -> the `vidsrc` block.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

import requests as _fallback_http

try:
    from curl_cffi import requests as _http
    _IMPERSONATE = "chrome131"
except Exception:  # pragma: no cover - curl_cffi optional
    _http = _fallback_http
    _IMPERSONATE = None

log = logging.getLogger(__name__)

_DEFAULT_CONFIG = {
    "vidsrc": {
        "enabled": True,
        "embed_base": "https://vidsrcme.ru",
        "referer": "https://vidsrcme.ru/",
        "player_referer": "https://cloudorchestranova.com/",
        "timeout": 25,
        "max_servers": 3,
        "label": "سيرفر أجنبي",
        "doh_url": "https://cloudflare-dns.com/dns-query",
    }
}

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json"
)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

_IFRAME_RE = re.compile(r'<iframe[^>]*\bsrc="([^"]+)"')
_CONFIG_RE = re.compile(r"window\.CONFIG\s*=\s*(\{[^;]+\})\s*;")

# Cache the decrypted stream list + host JWT briefly (the JWT lives ~4h, but the
# host list rotates per request, so 10 minutes is a safe middle ground).
_VS_CACHE_TTL = float(os.environ.get("VIDSRC_CACHE_TTL", "600"))
_vs_cache: dict[tuple, tuple[float, dict]] = {}


def _cfg() -> dict[str, Any]:
    cfg = dict(_DEFAULT_CONFIG["vidsrc"])
    try:
        with open(_CONFIG_FILE, encoding="utf-8") as fh:
            stored = json.load(fh).get("vidsrc") or {}
        cfg.update({k: v for k, v in stored.items() if v is not None})
    except (OSError, ValueError):
        pass
    return cfg


def is_enabled() -> bool:
    return bool(_cfg().get("enabled", True))


@dataclass
class VidsrcResult:
    servers: list[dict] = field(default_factory=list)  # {"url", "host"}
    imdb_id: str = ""
    title: str = ""
    thumbnails: str = ""


# ---------------------------------------------------------------------------
# Parsing helpers (pure functions, unit-tested without network)
# ---------------------------------------------------------------------------

def extract_iframe_src(html: str) -> str | None:
    """Pull the player iframe src (carries the `vs` token) from the embed page."""
    m = _IFRAME_RE.search(html)
    return m.group(1) if m else None


def extract_config(html: str) -> dict:
    """Parse the inline ``window.CONFIG = {...}`` JSON from the player page."""
    m = _CONFIG_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1))
    except ValueError:
        return {}


def api_url_for(config: dict, media_type: str = "movie", season=None, episode=None) -> str:
    """The stream-data endpoint for a CONFIG dict.

    Movies: CONFIG.api already ends with ``&stream_urls``. TV: CONFIG.streamBase
    is the bare endpoint; the player appends ``&season=&episode=&stream_urls``.
    """
    if media_type != "tv":
        return config.get("api") or ""
    base = config.get("streamBase") or ""
    if not base:
        return ""
    return "{}&season={}&episode={}&stream_urls".format(
        base, int(season) if season else 1, int(episode) if episode else 1
    )


def parse_token(text: str) -> str:
    """Interpret a /generate.php response: raw string or JSON with .token/.data."""
    t = (text or "").strip()
    if not t:
        return ""
    if t[:1] in ("{", "["):
        try:
            j = json.loads(t)
            if isinstance(j, str):
                return j
            if isinstance(j, dict):
                return str(j.get("token") or j.get("data") or j.get("string") or j.get("result") or t)
        except ValueError:
            pass
    return t


def stamp_token(master_url: str, token: str) -> str:
    """Append/replace the `token` query param on a master playlist URL."""
    if not token:
        return master_url
    sep = "&" if "?" in master_url else "?"
    return master_url + sep + "token=" + token


# ---------------------------------------------------------------------------
# Network steps
# ---------------------------------------------------------------------------

def _get(url: str, referer: str | None, timeout: float, accept: str = "*/*", **kw):
    headers = {"User-Agent": UA, "Accept": accept}
    if referer:
        headers["Referer"] = referer
    if _IMPERSONATE:
        try:
            return _http.get(url, impersonate=_IMPERSONATE, headers=headers, timeout=timeout, **kw)
        except Exception:
            pass
        doh = _cfg().get("doh_url")
        if doh:
            try:
                return _http.get(
                    url, impersonate=_IMPERSONATE, headers=headers, timeout=timeout,
                    doh_url=doh, **kw,
                )
            except Exception:
                pass
    return _fallback_http.get(url, headers=headers, timeout=timeout, **kw)


def get_iframe_url(embed_url: str, timeout: float) -> str | None:
    resp = _get(embed_url, _cfg().get("referer"), timeout)
    if resp.status_code >= 400:
        log.info("vidsrc embed failed: %s -> %s", embed_url, resp.status_code)
        return None
    return extract_iframe_src(resp.text)


def get_config(iframe_url: str, timeout: float) -> dict:
    referer = urljoin(_cfg().get("embed_base", "") + "/", "/")
    resp = _get(iframe_url, referer, timeout)
    if resp.status_code >= 400:
        log.info("vidsrc player page failed: %s -> %s", iframe_url, resp.status_code)
        return {}
    return extract_config(resp.text)


def fetch_stream_data(api_url: str, timeout: float) -> dict | None:
    resp = _get(api_url, _cfg().get("player_referer"), timeout, accept="application/json")
    if resp.status_code >= 400:
        log.info("vidsrc data api failed: %s -> %s", api_url, resp.status_code)
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _decrypt_via_wasm(enc_b64: str, wasm_url: str | None, wasm_b64: str | None) -> list[str]:
    """Decrypt `stream_urls` using the per-window ChaCha20 wasm (vsdec protocol).

    The wasm exports `alloc(len)` and `decrypt(ptr, len)`; the ciphertext begins
    with a 12-byte nonce that `decrypt` consumes but keeps in the first 12 bytes.
    """
    import wasmtime  # lazy: only needed when protection is enabled

    if wasm_url:
        wasm_bytes = _get(wasm_url, None, _cfg().get("timeout", 25)).content
    elif wasm_b64:
        wasm_bytes = base64.b64decode(wasm_b64)
    else:
        return []

    store = wasmtime.Store()
    module = wasmtime.Module(store.engine, wasm_bytes)
    instance = wasmtime.Instance(store, module, [])
    ex = instance.exports(store)
    memory = ex["memory"]

    raw = base64.b64decode(enc_b64)
    ptr = ex["alloc"](store, len(raw))
    memory.write(store, raw, ptr)
    out_len = ex["decrypt"](store, ptr, len(raw))
    out = memory.read(store, ptr + 12, ptr + 12 + out_len)
    text = out.decode("utf-8", errors="replace")
    return [line for line in text.split("\n") if line.strip()]


def parse_stream_urls(data: dict) -> list[str]:
    """Return the decrypted stream URLs for an api.php response.

    Accepts the full JSON body (``{"data": {...}, "vs": {...}}``) or the inner
    ``data`` object. ``stream_urls`` is either a plain array (protection off)
    or an encrypted base64 string; the ``vs`` block — always at the top level
    of the response — carries the wasm decryptor.
    """
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    value = payload.get("stream_urls")
    if isinstance(value, list):
        return [u for u in value if u]
    if not isinstance(value, str) or not value:
        return []
    vs = data.get("vs") or payload.get("vs") or {}
    try:
        return _decrypt_via_wasm(value, vs.get("wasm_url"), vs.get("wasm"))
    except Exception as exc:
        log.warning("vidsrc wasm decrypt failed: %s", exc)
        return []


def mint_host_token(host: str, timeout: float) -> str:
    resp = _get(host + "/generate.php", _cfg().get("player_referer"), timeout)
    if resp.status_code >= 400:
        return ""
    return parse_token(resp.text)


def restamp_master(base_url: str, host: str, timeout: float | None = None) -> str | None:
    """Re-mint a fresh host token and return a freshly stamped master URL."""
    token = mint_host_token(host, timeout or _cfg().get("timeout", 25))
    if not token:
        return None
    return stamp_token(base_url, token)


# ---------------------------------------------------------------------------
# Top-level resolve
# ---------------------------------------------------------------------------

def _cache_key(tmdb_id, media_type, season, episode) -> tuple:
    return (tmdb_id, media_type, season, episode)


def resolve(
    tmdb_id: int | str,
    media_type: str = "movie",
    season: int | None = None,
    episode: int | None = None,
    timeout: float | None = None,
) -> VidsrcResult:
    """Resolve a TMDB id to proxied HLS master URLs (with host tokens stamped).

    Returns VidsrcResult(servers=[{"url", "host"}], imdb_id, title, thumbnails).
    """
    cfg = _cfg()
    timeout = timeout or float(cfg.get("timeout", 25))
    key = _cache_key(tmdb_id, media_type, season, episode)
    cached = _vs_cache.get(key)
    if cached is not None and time.monotonic() <= cached[0]:
        return cached[1]

    result = VidsrcResult()
    kind = "tv" if media_type == "tv" else "movie"
    base = str(cfg.get("embed_base", "")).rstrip("/")
    embed_url = "{}/embed/{}/{}".format(base, kind, tmdb_id)

    iframe = get_iframe_url(embed_url, timeout)
    if not iframe:
        log.info("vidsrc: no iframe for %s", embed_url)
        return result
    config = get_config(iframe, timeout)
    api = api_url_for(config, media_type, season, episode)
    if not api:
        log.info("vidsrc: no api/streamBase in config for %s", embed_url)
        return result
    data = fetch_stream_data(api, timeout)
    if not data:
        return result

    payload = data.get("data") or {}
    result.imdb_id = str(payload.get("imdb_id") or "")
    result.title = str(payload.get("title") or "")
    result.thumbnails = str(data.get("thumbnails_url") or "")

    urls = parse_stream_urls(data)
    for raw in urls[: int(cfg.get("max_servers", 3))]:
        host = raw.split("/pl/", 1)[0]
        if not host:
            continue
        token = mint_host_token(host, timeout)
        if not token:
            log.info("vidsrc: no token for host %s", host)
            continue
        result.servers.append({
            "url": stamp_token(raw, token),
            "base": raw,
            "host": host,
        })

    if result.servers:
        _vs_cache[key] = (time.monotonic() + _VS_CACHE_TTL, result)
    return result
