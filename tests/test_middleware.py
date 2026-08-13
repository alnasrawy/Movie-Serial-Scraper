"""Unit tests for the middleware: pure helpers and HTTP endpoints.

These tests run without a browser: they cover URL rewriting, PNG-wrapper
stripping, media classification and the FastAPI streaming pipeline with a
stubbed session/fetch layer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest


def _rewrite(m3u8: str, base_url: str, sid: str) -> str:
    from middleware.server import _rewrite_m3u8

    return _rewrite_m3u8(m3u8, base_url, sid)


def test_proxy_url_encodes_media_url():
    from middleware.server import _proxy_url

    url = _proxy_url("abc123", "https://cdn.example/x/y/master.m3u8")
    assert url.startswith("/stream?sid=abc123&url=")
    assert "https%3A%2F%2Fcdn.example%2Fx%2Fy%2Fmaster.m3u8" in url


def test_rewrite_m3u8_wraps_segment_uris():
    m3u8 = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nmaster.m3u8\nseg-1.ts\n"
    out = _rewrite(m3u8, "https://cdn.test/path/master.m3u8", "sid1")
    lines = out.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXT-X-STREAM-INF")
    assert lines[2].startswith("/stream?sid=sid1&url=")
    assert "path%2Fmaster.m3u8" in lines[2]
    assert lines[3].startswith("/stream?sid=sid1&url=")
    assert "path%2Fseg-1.ts" in lines[3]


def test_rewrite_m3u8_rewrites_uri_attribute():
    m3u8 = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nseg.m3u8\n'
    out = _rewrite(m3u8, "https://cdn.test/p/pl.m3u8", "s")
    assert "/stream?sid=s&url=" in out
    assert 'URI="/stream?sid=s&url=' in out


def test_rewrite_m3u8_joins_relative_to_base():
    out = _rewrite("seg.ts\n", "https://cdn.test/a/b/index.m3u8", "s")
    assert "https%3A%2F%2Fcdn.test%2Fa%2Fb%2Fseg.ts" in out


def test_strip_png_wrapper_returns_ts_payload():
    from middleware.server import _strip_png_wrapper

    ts_packet = b"\x47" + b"\x00" * 187  # one MPEG-TS packet
    ts = ts_packet * 4
    png = b"\x89PNG\r\n\x1a\n" + ts  # payload starts right after the 8-byte PNG header
    out = _strip_png_wrapper(png)
    assert out == ts
    assert out.startswith(b"\x47")
    assert len(out) % 188 == 0


def test_strip_png_wrapper_ignores_header_g_byte():
    from middleware.server import _strip_png_wrapper

    ts_packet = b"\x47" + b"\x00" * 187
    ts = ts_packet * 3
    png = b"\x89PNG\r\n\x1a\n" + b"IDAT" + b"\x47" + ts  # stray 0x47 inside the chunk data
    out = _strip_png_wrapper(png)
    assert out == ts


def test_strip_png_wrapper_passes_non_png_through():
    from middleware.server import _strip_png_wrapper

    data = b"#EXTM3U\n#EXTINF:4,\nseg.ts\n"
    assert _strip_png_wrapper(data) is data


def test_kind_of():
    from middleware.player import _kind_of

    assert _kind_of("https://x/master.m3u8") == "hls"
    assert _kind_of("https://x/master.txt") == "hls"
    assert _kind_of("https://x/hls3/stream.urlset") == "hls"
    assert _kind_of("https://x/movie.mp4") == "mp4"
    assert _kind_of("https://x/video.webm") == "mp4"
    assert _kind_of("https://x/page") == "unknown"


def test_is_media_candidate_filters_ads():
    from middleware.player import _is_media_candidate

    assert _is_media_candidate("https://cdn/x/master.m3u8", "application/vnd.apple.mpegurl")
    assert _is_media_candidate("https://cdn/x/seg.ts", "video/mp2t")
    assert not _is_media_candidate("https://cdn/x/ads.js", "text/javascript")
    assert not _is_media_candidate("https://googletagmanager.com/gtm.js", "text/javascript")
    assert not _is_media_candidate("https://cdn/x/pixel.png", "image/png")


@pytest.fixture
def client(monkeypatch):
    """FastAPI TestClient with a stubbed manager that serves fixed media."""
    from fastapi.testclient import TestClient

    from middleware import server

    session = SimpleNamespace(active=True, new_url="")

    async def fake_fetch(sid: str, url: str):
        if url.endswith(".m3u8") or "m3u8" in url:
            return 200, "application/vnd.apple.mpegurl", b"#EXTM3U\nseg-1.ts\nseg-2.ts\n"
        ts_packet = b"\x47" + b"\x00" * 187
        return 200, "image/png", b"\x89PNG\r\n\x1a\n" + ts_packet * 3

    mgr = server.get_manager()
    monkeypatch.setitem(mgr.sessions, "test-sid", session)
    monkeypatch.setattr(mgr, "fetch", fake_fetch)
    return TestClient(server.app)


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_stream_rewrites_playlist(client):
    r = client.get("/stream", params={"sid": "test-sid", "url": "https://cdn.test/p/master.m3u8"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    body = r.text
    assert body.startswith("#EXTM3U")
    assert "/stream?sid=test-sid&url=" in body
    assert "seg-1.ts" in body


def test_stream_strips_png_wrapper(client):
    r = client.get("/stream", params={"sid": "test-sid", "url": "https://cdn.test/p/seg-1.ts"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "video/mp2t"
    assert r.content.startswith(b"\x47")
    assert len(r.content) % 188 == 0


def test_stream_missing_session_returns_503(client):
    r = client.get("/stream", params={"sid": "nope", "url": "https://cdn.test/p/master.m3u8"})
    assert r.status_code == 503


def test_watch_endpoint_returns_server_list(monkeypatch):
    from fastapi.testclient import TestClient

    from middleware import server

    async def fake_resolve_embed(url, referer=None):
        return {"sid": f"sid-{hash(url)}", "kind": "hls", "url": "https://cdn.test/master.m3u8"}

    def fake_scrape_all(query, sites):
        return [{
            "source": "akwams",
            "title": "X",
            "detail_url": "https://akwams.org/x",
            "watch_servers": [
                {"name": "سيرفر 1", "url": "https://embed1.test/e/1"},
                {"name": "سيرفر 2", "url": "https://embed2.test/e/2"},
            ],
        }]

    monkeypatch.setattr(server, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(server, "_resolve_embed", fake_resolve_embed)

    c = TestClient(server.app)
    r = c.post("/watch", json={"query": "inception", "sites": ["akwams"]})
    assert r.status_code == 200
    data = r.json()
    assert data["query"] == "inception"
    assert len(data["servers"]) == 2
    first = data["servers"][0]
    assert first["name"] == "سيرفر 1"
    assert first["site"] == "akwams"
    assert first["kind"] == "hls"
    assert first["proxy_url"].startswith("http://testserver/stream?sid=")


def test_stream_serves_http_session_without_browser(monkeypatch):
    """A sid created by the HTTP resolver streams through the requests proxy."""
    from fastapi.testclient import TestClient

    from middleware import server

    server.http_sessions["http-sid"] = {
        "url": "https://cdn.test/p/master.m3u8",
        "referer": "https://embed.test/e/1",
    }

    def fake_http_fetch(sid, url):
        return 200, "application/vnd.apple.mpegurl", b"#EXTM3U\nseg-1.ts\nseg-2.ts\n"

    monkeypatch.setattr(server, "_http_fetch", fake_http_fetch)
    try:
        c = TestClient(server.app)
        r = c.get("/stream", params={"sid": "http-sid", "url": "https://cdn.test/p/master.m3u8"})
        assert r.status_code == 200
        assert "/stream?sid=http-sid&url=" in r.text
        assert "seg-1.ts" in r.text
    finally:
        server.http_sessions.pop("http-sid", None)


def test_watch_requires_query_or_tmdb_id(monkeypatch):
    from fastapi.testclient import TestClient

    from middleware import server

    c = TestClient(server.app)
    r = c.post("/watch", json={})
    assert r.status_code == 400
