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
    assert url.startswith("/stream/abc123?url=")
    assert "https%3A%2F%2Fcdn.example%2Fx%2Fy%2Fmaster.m3u8" in url


def test_proxy_url_ext_hints_media_type_for_exoplayer():
    from middleware.server import _ext_for, _proxy_url

    assert _ext_for("hls") == ".m3u8"
    assert _ext_for("dash") == ".mpd"
    assert _ext_for("mp4") == ".mp4"
    assert _ext_for("unknown") == ".mp4"
    # ExoPlayer infers HLS/DASH from the URL's last path-segment extension.
    assert _proxy_url("abc", "https://c/x.m3u8", ext=_ext_for("hls")).startswith("/stream/abc.m3u8?")
    assert _proxy_url("abc", "https://c/x.mpd", ext=_ext_for("dash")).startswith("/stream/abc.mpd?")
    assert _proxy_url("abc", "https://c/x.mp4", ext=_ext_for("mp4")).startswith("/stream/abc.mp4?")


def test_rewrite_m3u8_wraps_segment_uris():
    m3u8 = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nmaster.m3u8\nseg-1.ts\n"
    out = _rewrite(m3u8, "https://cdn.test/path/master.m3u8", "sid1")
    lines = out.splitlines()
    assert lines[0] == "#EXTM3U"
    assert lines[1].startswith("#EXT-X-STREAM-INF")
    assert lines[2].startswith("/stream/sid1?url=")
    assert "path%2Fmaster.m3u8" in lines[2]
    assert lines[3].startswith("/stream/sid1?url=")
    assert "path%2Fseg-1.ts" in lines[3]


def test_rewrite_m3u8_rewrites_uri_attribute():
    m3u8 = '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\nseg.m3u8\n'
    out = _rewrite(m3u8, "https://cdn.test/p/pl.m3u8", "s")
    assert "/stream/s?url=" in out
    assert 'URI="/stream/s?url=' in out


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


def test_index_page_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "فاحص السيرفرات" in r.text


def test_cors_headers(client):
    r = client.get("/health", headers={"Origin": "https://example.com"})
    assert r.headers.get("access-control-allow-origin") == "*"


def test_stream_rewrites_playlist(client):
    r = client.get("/stream", params={"sid": "test-sid", "url": "https://cdn.test/p/master.m3u8"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    body = r.text
    assert body.startswith("#EXTM3U")
    assert "/stream/test-sid?url=" in body
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
    assert first["proxy_url"].startswith("http://testserver/stream/")
    assert ".m3u8?" in first["proxy_url"]


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
        assert "/stream/http-sid?url=" in r.text
        assert "seg-1.ts" in r.text
    finally:
        server.http_sessions.pop("http-sid", None)


def test_stream_self_heals_when_session_missing(monkeypatch):
    """A /stream link with `ref` re-resolves the embed when the sid is gone.

    Copied links keep working after a server restart (in-memory sessions are
    wiped) — /stream must re-mint a session from the embed URL.
    """
    from fastapi.testclient import TestClient

    from middleware import server

    seen = {}

    def fake_resolve_http(embed_url, referer=None):
        seen["args"] = (embed_url, referer)
        return {"kind": "hls", "url": "https://cdn.test/p/fresh.m3u8", "referer": embed_url}

    def fake_http_fetch(sid, url):
        assert url == "https://cdn.test/p/fresh.m3u8"
        return 200, "application/vnd.apple.mpegurl", b"#EXTM3U\nseg-1.ts\nseg-2.ts\n"

    monkeypatch.setattr(server, "resolve_http", fake_resolve_http)
    monkeypatch.setattr(server, "_http_fetch", fake_http_fetch)
    try:
        c = TestClient(server.app)
        r = c.get(
            "/stream/missing-sid.m3u8",
            params={
                "url": "https://cdn.test/p/old.m3u8",
                "ref": "https://embed.test/e/1",
                "site_ref": "https://tv10.egydead.live/movie/x",
            },
        )
        assert r.status_code == 200
        assert "seg-1.ts" in r.text
        # the embed host serves its full page only to the site referer
        assert seen["args"] == ("https://embed.test/e/1", "https://tv10.egydead.live/movie/x")
        assert server.http_sessions.get("missing-sid", {}).get("url") == "https://cdn.test/p/fresh.m3u8"
    finally:
        server.http_sessions.pop("missing-sid", None)


def test_watch_requires_query_or_tmdb_id(monkeypatch):
    from fastapi.testclient import TestClient

    from middleware import server

    c = TestClient(server.app)
    r = c.post("/watch", json={})
    assert r.status_code == 400


def test_path_lower_ignores_query_token():
    """Media-type decisions use the URL path only — CDN URLs carry signed
    tokens in the query (master.m3u8?t=..&s=..), so endswith() on the raw URL
    is wrong."""
    from middleware.server import _path_lower

    assert _path_lower("https://cdn.test/p/master.m3u8?t=abc&s=1786747113") == "/p/master.m3u8"
    assert _path_lower("https://cdn.test/video.mp4?sign=xyz&e=50") == "/video.mp4"
    assert _path_lower("https://cdn.test/dash/idx/index_web.mpd?x=1") == "/dash/idx/index_web.mpd"


def test_stream_ranges_mp4_with_query_token(monkeypatch):
    """An mp4 CDN URL carrying a query token must still take the ranged-stream
    path (path-based detection), not the full-buffer fetch."""
    from fastapi.testclient import TestClient

    from middleware import server

    server.http_sessions["mp4-sid"] = {
        "url": "https://cdn.test/movie.mp4?token=xyz",
        "referer": "https://embed.test/e/1",
    }
    calls = {}

    def fake_stream(sid, url, range_header):
        calls["url"] = url
        calls["range"] = range_header
        return 206, "video/mp4", {"Content-Range": "bytes 0-99/1000"}, iter([b"x"] * 100)

    monkeypatch.setattr(server, "_http_stream", fake_stream)
    try:
        c = TestClient(server.app)
        r = c.get(
            "/stream/mp4-sid.mp4",
            params={"url": "https://cdn.test/movie.mp4?token=xyz"},
            headers={"Range": "bytes=0-99"},
        )
        assert r.status_code == 206
        assert calls["url"] == "https://cdn.test/movie.mp4?token=xyz"
        assert calls["range"] == "bytes=0-99"
    finally:
        server.http_sessions.pop("mp4-sid", None)


def test_stream_sniffs_mp2t_for_text_plain_ts(monkeypatch):
    """CDNs sometimes label TS segments text/plain; ExoPlayer prefers an
    explicit video/mp2t for HLS chunks, so sniff the sync bytes."""
    from fastapi.testclient import TestClient

    from middleware import server

    ts_packet = b"\x47" + b"\x00" * 187
    ts = ts_packet * 3
    server.http_sessions["ts-sid"] = {
        "url": "https://cdn.test/p/seg.ts",
        "referer": "https://embed.test/e/1",
    }

    def fake_fetch(sid, url):
        return 200, "text/plain", ts

    monkeypatch.setattr(server, "_http_fetch", fake_fetch)
    try:
        c = TestClient(server.app)
        r = c.get("/stream", params={"sid": "ts-sid", "url": "https://cdn.test/p/seg.ts"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp2t"
        assert r.content.startswith(b"\x47")
    finally:
        server.http_sessions.pop("ts-sid", None)


def test_direct_endpoint_returns_raw_media_urls(monkeypatch):
    """POST /direct returns CDN URLs without proxy wrapping, with caching."""
    from fastapi.testclient import TestClient

    from middleware import server

    server._direct_cache.clear()

    def fake_direct_servers(req):
        return "Inception", [{
            "site": "akwams",
            "name": "سيرفر 5",
            "kind": "hls",
            "url": "https://cdn.test/hls2/x/y/master.m3u8?t=abc",
        }]

    monkeypatch.setattr(server, "_direct_servers", fake_direct_servers)
    c = TestClient(server.app)

    r1 = c.post("/direct", json={"query": "Inception", "sites": ["akwams"]})
    r2 = c.post("/direct", json={"query": "Inception", "sites": ["akwams"]})
    assert r1.status_code == 200
    data = r1.json()
    assert data["query"] == "Inception"
    assert len(data["servers"]) == 1
    assert data["servers"][0]["url"].startswith("https://cdn.test/hls2")
    assert "/stream?" not in data["servers"][0]["url"]
    assert r2.json().get("cached") is True
    server._direct_cache.clear()


def test_watch_second_call_served_from_cache(monkeypatch):
    """Repeat /watch for the same title must not scrape or resolve again."""
    from fastapi.testclient import TestClient

    from middleware import server

    server._watch_cache.clear()

    async def fake_resolve_embed(url, referer=None):
        return {"sid": f"sid-{hash(url)}", "kind": "hls", "url": "https://cdn.test/master.m3u8"}

    calls = {"scrape": 0}

    def fake_scrape_all(query, sites):
        calls["scrape"] += 1
        return [{
            "source": "akwams",
            "title": "X",
            "detail_url": "https://akwams.org/x",
            "watch_servers": [{"name": "سيرفر 1", "url": "https://embed1.test/e/1"}],
        }]

    monkeypatch.setattr(server, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(server, "_resolve_embed", fake_resolve_embed)

    c = TestClient(server.app)
    r1 = c.post("/watch", json={"query": "inception", "sites": ["akwams"]})
    r2 = c.post("/watch", json={"query": "inception", "sites": ["akwams"]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("cached") is True
    assert r1.json()["servers"] == r2.json()["servers"]
    assert calls["scrape"] == 1
    server._watch_cache.clear()


def test_watch_tries_multiple_query_candidates_and_merges(monkeypatch):
    """akwams ignores the Arabic title; watch must fall back to the original."""
    from fastapi.testclient import TestClient

    from middleware import server

    def fake_tmdb_title(tmdb_id, key=None, media_type="movie"):
        return {
            "tmdb_id": str(tmdb_id),
            "media_type": "movie",
            "title": "استهلال",
            "original_title": "Inception",
            "overview": "",
        }

    def fake_scrape_all(query, sites):
        if query == "استهلال":
            return []
        if query == "Inception":
            return [{
                "source": "akwams",
                "title": "Inception",
                "detail_url": "https://akwams.org/x",
                "watch_servers": [{"name": "سيرفر 1", "url": "https://embed1.test/e/1"}],
            }]
        return []

    async def fake_resolve_embed(url, referer=None):
        return {"sid": f"sid-{hash(url)}", "kind": "hls", "url": "https://cdn.test/master.m3u8"}

    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    monkeypatch.setattr("scraper.tmdb.tmdb_title", fake_tmdb_title)
    monkeypatch.setattr(server, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(server, "_resolve_embed", fake_resolve_embed)

    async def fake_foreign(req, base, servers):
        return "", []

    monkeypatch.setattr(server, "_add_foreign_servers", fake_foreign)

    c = TestClient(server.app)
    r = c.post("/watch", json={"tmdb_id": 27205, "type": "movie", "sites": ["akwams"]})
    assert r.status_code == 200
    data = r.json()
    assert len(data["servers"]) == 1
    assert data["servers"][0]["site"] == "akwams"


def test_tmdb_popular_proxies_results(monkeypatch):
    """/tmdb/popular calls TMDB with the server key and returns Arabic items."""
    from fastapi.testclient import TestClient

    from middleware import server

    def fake_get(url, params=None, timeout=None):
        assert "api.themoviedb.org/3/movie/popular" in url
        assert params["api_key"] == "test-key"
        assert params["language"] == "ar-SA"
        return SimpleNamespace(
            raise_for_status=lambda: None,
            encoding="utf-8",
            json=lambda: {
                "page": 1,
                "total_pages": 5,
                "results": [{
                    "id": 27205,
                    "title": "استهلال",
                    "original_title": "Inception",
                    "overview": "نص",
                    "poster_path": "/abc.jpg",
                    "vote_average": 8.3,
                }],
            },
        )

    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    monkeypatch.setattr("requests.get", fake_get)

    c = TestClient(server.app)
    r = c.get("/tmdb/popular?type=movie")
    assert r.status_code == 200
    data = r.json()
    assert data["page"] == 1
    assert len(data["items"]) == 1
    it = data["items"][0]
    assert it["tmdb_id"] == 27205
    assert it["title"] == "استهلال"
    assert it["poster"].startswith("https://image.tmdb.org/t/p/w500")


def test_tmdb_search_multi_carries_media_type(monkeypatch):
    """/tmdb/search uses search/multi so both movies and shows come back."""
    from fastapi.testclient import TestClient

    from middleware import server

    def fake_get(url, params=None, timeout=None):
        assert "/search/multi" in url
        assert params["query"] == "inception"
        return SimpleNamespace(
            raise_for_status=lambda: None,
            encoding="utf-8",
            json=lambda: {
                "results": [
                    {"id": 27205, "media_type": "movie", "title": "Inception"},
                    {"id": 1, "media_type": "tv", "name": "Show", "poster_path": "/p.jpg"},
                ]
            },
        )

    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    monkeypatch.setattr("requests.get", fake_get)

    c = TestClient(server.app)
    r = c.get("/tmdb/search?q=inception")
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["media_type"] for i in items] == ["movie", "tv"]
    assert items[1]["poster"].startswith("https://image.tmdb.org/t/p/w500")


def test_rewrite_mpd_wraps_baseurl_and_templates():
    from middleware.server import _rewrite_mpd

    mpd = (
        '<MPD>\n'
        '  <BaseURL>segments/</BaseURL>\n'
        '  <SegmentTemplate media="v$Number$.m4s" initialization="init.mp4"/>\n'
        '  <BaseURL>https://other.test/x/</BaseURL>\n'
        '</MPD>\n'
    )
    out = _rewrite_mpd(mpd, "https://cdn.test/dash/idx/index_web.mpd", "s")
    assert "<BaseURL>/stream/s?url=https%3A%2F%2Fcdn.test%2Fdash%2Fidx%2Fsegments%2F</BaseURL>" in out
    assert 'media="/stream/s?url=https%3A%2F%2Fcdn.test%2Fdash%2Fidx%2Fv$Number$.m4s"' in out
    assert 'initialization="/stream/s?url=https%3A%2F%2Fcdn.test%2Fdash%2Fidx%2Finit.mp4"' in out
    assert "https%3A%2F%2Fother.test%2Fx%2F" in out
    assert out.startswith("<MPD>")
    assert "$Number$" in out  # template vars preserved for the player to fill


def test_rewrite_mpd_keeps_printf_template_tokens_literal():
    """Engine DASH templates use $Number%05d$ — the % and $ must stay literal."""
    from middleware.server import _rewrite_mpd

    mpd = (
        '<MPD>\n'
        '  <SegmentTemplate media="chunk-stream$RepresentationID$-$Number%05d$.m4s" '
        'initialization="init-stream$RepresentationID$.m4s"/>\n'
        "</MPD>\n"
    )
    out = _rewrite_mpd(mpd, "https://cdn.test/dash/idx/index_web.mpd", "s")
    assert 'media="/stream/s?url=https%3A%2F%2Fcdn.test%2Fdash%2Fidx%2Fchunk-stream$RepresentationID$-$Number%05d$.m4s"' in out
    assert 'initialization="/stream/s?url=https%3A%2F%2Fcdn.test%2Fdash%2Fidx%2Finit-stream$RepresentationID$.m4s"' in out
    assert "%25" not in out  # no double-encoded %


def test_http_headers_includes_cookie_and_ua():
    from middleware.server import _http_headers

    ep = {
        "referer": "https://embed.test/e/1",
        "cookie": "CloudFront-Policy=eyJZ;CloudFront-Signature=abc;",
        "user_agent": "TestUA/1.0",
    }
    headers = _http_headers(ep, "https://cdn.test/x")
    assert headers["Cookie"] == ep["cookie"]
    assert headers["User-Agent"] == "TestUA/1.0"
    assert headers["Referer"] == "https://embed.test/e/1"


def test_stream_rewrites_dash_manifest(monkeypatch):
    """A DASH manifest fetched through the proxy gets segment URLs rewritten."""
    from fastapi.testclient import TestClient

    from middleware import server

    session = SimpleNamespace(active=True, new_url="")

    async def fake_fetch(sid: str, url: str):
        if url.endswith(".mpd"):
            mpd = (
                "<MPD>\n"
                '  <SegmentTemplate media="v$Number$.m4s" initialization="init.mp4"/>\n'
                "</MPD>\n"
            )
            return 200, "application/dash+xml", mpd.encode("utf-8")
        return 200, "video/mp2t", b"x"

    mgr = server.get_manager()
    monkeypatch.setitem(mgr.sessions, "dash-sid", session)
    monkeypatch.setattr(mgr, "fetch", fake_fetch)
    c = TestClient(server.app)
    r = c.get("/stream", params={"sid": "dash-sid", "url": "https://cdn.test/dash/idx/index_web.mpd"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/dash+xml")
    assert 'media="/stream/dash-sid?url=' in r.text
    assert "v$Number$.m4s" in r.text


def test_watch_adds_primetv_foreign_servers(monkeypatch):
    """/watch resolves primetv engine/easyplex streams and proxies them."""
    from fastapi.testclient import TestClient

    from middleware import primetv
    from middleware import server
    from middleware import vidsrc

    def fake_tmdb_title(tmdb_id, key=None, media_type="movie"):
        return {
            "tmdb_id": str(tmdb_id),
            "media_type": "movie",
            "title": "استهلال",
            "original_title": "Inception",
            "overview": "",
            "year": 2010,
        }

    def fake_scrape_all(query, sites):
        return []

    async def fake_resolve_embed(url, referer=None):
        return {"sid": f"sid-{hash(url)}", "kind": "hls", "url": "https://cdn.test/master.m3u8"}

    class _Res:
        servers = [
            {
                "url": "https://sacdn.hakunaymatata.com/dash/6207982430134357800_1_1_1080/index_web.mpd",
                "kind": "dash",
                "quality": 1080,
                "cookie": "CloudFront-Policy=eyJZ;",
                "referer": "https://primeott.sytes.net/engine/",
                "user_agent": "",
            }
        ]

    captured = {}

    def fake_resolve(tmdb_id, kind, title="", year=None, season=None, episode=None):
        captured["args"] = (tmdb_id, kind, title, year)
        return _Res()

    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    monkeypatch.setattr("scraper.tmdb.tmdb_title", fake_tmdb_title)
    monkeypatch.setattr(server, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(server, "_resolve_embed", fake_resolve_embed)
    monkeypatch.setattr(primetv, "is_enabled", lambda: True)
    monkeypatch.setattr(primetv, "resolve", fake_resolve)
    monkeypatch.setattr(primetv, "_cfg", lambda: {"label": "سيرفر برايم"})
    monkeypatch.setattr(vidsrc, "is_enabled", lambda: False)

    server.http_sessions.clear()
    server._watch_cache.clear()
    try:
        c = TestClient(server.app)
        r = c.post("/watch", json={"tmdb_id": 27205, "type": "movie"})
        assert r.status_code == 200
        data = r.json()
        pt = [s for s in data["servers"] if s["site"] == "primetv"]
        assert len(pt) == 1
        assert pt[0]["name"] == "سيرفر برايم 1"
        assert pt[0]["kind"] == "dash"
        assert pt[0]["proxy_url"].startswith("http://testserver/stream/")
        assert ".mpd?" in pt[0]["proxy_url"]
        sessions = [ep for ep in server.http_sessions.values() if ep.get("kind") == "primetv"]
        assert len(sessions) == 1
        assert sessions[0]["cookie"] == "CloudFront-Policy=eyJZ;"
        assert captured["args"] == (27205, "movie", "Inception", 2010)
    finally:
        server.http_sessions.clear()


def test_watch_movie_defaults_to_our_arabic_sites(monkeypatch):
    """A movie with no `sites` scrapes our own Arabic sites (akwams, egydead)."""
    from fastapi.testclient import TestClient

    from middleware import server
    from middleware import primetv, subtitles, vidsrc

    calls = {"sites": None}

    def fake_scrape_all(query, sites):
        calls["sites"] = list(sites)
        return []

    async def fake_resolve_embed(url, referer=None):
        return {"sid": "s", "kind": "hls", "url": "https://cdn.test/master.m3u8"}

    class _Res:
        servers = [
            {"url": "https://cdn.test/master.m3u8", "kind": "hls", "quality": 720,
             "cookie": "", "referer": "", "user_agent": ""}
        ]

    def fake_resolve(tmdb_id, kind, title="", year=None, season=None, episode=None):
        return _Res()

    def fake_tmdb_title(tmdb_id, key=None, media_type="movie"):
        return {"tmdb_id": str(tmdb_id), "media_type": "movie", "title": "استهلال",
                "original_title": "Inception", "overview": "", "year": 2010}

    monkeypatch.setenv("TMDB_API_KEY", "test-key")
    monkeypatch.setattr("scraper.tmdb.tmdb_title", fake_tmdb_title)
    monkeypatch.setattr(server, "_scrape_all", fake_scrape_all)
    monkeypatch.setattr(server, "_resolve_embed", fake_resolve_embed)
    monkeypatch.setattr(primetv, "is_enabled", lambda: True)
    monkeypatch.setattr(primetv, "resolve", fake_resolve)
    monkeypatch.setattr(primetv, "_cfg", lambda: {"label": "سيرفر برايم"})
    monkeypatch.setattr(vidsrc, "is_enabled", lambda: False)
    monkeypatch.setattr(subtitles, "is_enabled", lambda: False)
    monkeypatch.setattr(server, "_tmdb_external_imdb", lambda tmdb_id, media_type: "")

    server.http_sessions.clear()
    server._watch_cache.clear()
    try:
        c = TestClient(server.app)
        r = c.post("/watch", json={"tmdb_id": 27205, "type": "movie"})
        assert r.status_code == 200
        # our own Arabic engine runs by default for movies
        assert calls["sites"] == ["akwams", "egydead"]

        # TV keeps the fast path: no Arabic scrape unless sites are explicit
        r3 = c.post("/watch", json={"tmdb_id": 1396, "type": "tv", "season": 1, "episode": 1})
        assert r3.status_code == 200
        assert calls["sites"] == ["akwams", "egydead"]  # unchanged -> no tv scrape
    finally:
        server.http_sessions.clear()
