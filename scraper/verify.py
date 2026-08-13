"""Post-processing for scraped items: verify links are alive, label servers.

These ad-driven hosts protect their streams with client-side JS, so the
reliable playback link is usually the host's embed page (which plays inside a
WebView player).  ``verify_item`` does a live HTTP check of every server link
(and any ``direct_url`` the resolver produced) and drops links that are dead.
``label_servers`` then renames the survivors to "سيرفر 1", "سيرفر 2", ...
"""

from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)
_MAX_SAMPLE = 65536


def check_link(url: str, timeout: float = 10.0) -> dict:
    """Return {"alive", "status", "content_type", "body"} for a URL."""
    try:
        with requests.get(
            url,
            headers={"User-Agent": _UA},
            timeout=timeout,
            stream=True,
            allow_redirects=True,
        ) as resp:
            status = resp.status_code
            content_type = (resp.headers.get("content-type") or "").lower()
            body = b""
            for chunk in resp.iter_content(8192):
                body += chunk
                if len(body) >= _MAX_SAMPLE:
                    break
        alive = 200 <= status < 400
        return {
            "alive": alive,
            "status": status,
            "content_type": content_type,
            "body": body,
        }
    except Exception as exc:
        return {"alive": False, "status": None, "content_type": "", "body": b"", "error": str(exc)[:120]}


def _is_media(result: dict) -> bool:
    ct = result.get("content_type", "")
    if any(t in ct for t in ("video/", "audio/", "mpegurl", "m3u8", "octet-stream", "x-mpegurl")):
        return True
    body = result.get("body", b"")
    if body.lstrip().startswith(b"#EXTM3U") or b"#EXT-X-STREAM-INF" in body or b"#EXTINF" in body:
        return True
    return ct.startswith("text/plain") and len(body) > 0


def verify_server(server: dict[str, Any], delay: float = 0.4) -> bool:
    """Verify one server dict; returns True if a playable link survived."""
    url = server.get("url")
    direct = server.get("direct_url")
    if delay:
        time.sleep(delay)

    if direct:
        result = check_link(direct)
        if result["alive"] and _is_media(result):
            server["checked"] = True
            server["status"] = result["status"]
            server["playable"] = "direct"
            return True
        server.pop("direct_url", None)
        server.pop("resolved_by", None)
        log.info("Direct link dead for %s (%s); falling back to embed", url, result.get("status"))

    if not url:
        return False
    result = check_link(url)
    if result["alive"]:
        server["checked"] = True
        server["status"] = result["status"]
        server["playable"] = "embed"
        return True
    log.info("Dead server link dropped: %s (%s)", url, result.get("status"))
    return False


def verify_item(item: dict[str, Any], delay: float = 0.4) -> dict[str, Any]:
    """Verify every server list on an item and drop dead links."""
    for key in ("watch_servers", "download_servers"):
        servers = item.get(key)
        if not isinstance(servers, list):
            continue
        survivors = [s for s in servers if verify_server(s, delay=delay)]
        if survivors:
            item[key] = survivors
        else:
            item.pop(key, None)
    return item


def label_servers(
    item: dict[str, Any],
    watch_label: str = "سيرفر {n}",
    download_label: str = "تحميل {n}",
) -> dict[str, Any]:
    """Rename server lists to numbered labels, keeping the original as metadata."""
    for key, label in (("watch_servers", watch_label), ("download_servers", download_label)):
        servers = item.get(key)
        if not isinstance(servers, list):
            continue
        for i, server in enumerate(servers, 1):
            server.setdefault("original_name", server.get("name") or "")
            server["server_number"] = i
            server["name"] = label.format(n=i)
    return item
