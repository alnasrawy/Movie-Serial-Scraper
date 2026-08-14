"""Unit tests for the PrimeTV foreign provider (engine + easyplex, HTTP-only).

Parsing is pure (no network); the resolve-level test stubs `_get`.
"""

from __future__ import annotations

import json
import os

import pytest


def _engine_tv_response():
    return {
        "success": True,
        "mbox_id": "6207982430134357800",
        "cached": True,
        "streams": [
            {
                "quality": 1080,
                "url": "https://sacdn.hakunaymatata.com/dash/6207982430134357800_1_1_1080_h265_136/index_web.mpd",
                "format": "DASH",
                "cookie": "CloudFront-Policy=eyJZ;CloudFront-Signature=abc;CloudFront-Key-Pair-Id=KMHN;",
            }
        ],
    }


def _engine_movie_response():
    return {
        "success": True,
        "mbox_id": "6391474290696802080",
        "cached": False,
        "streams": [
            {"quality": 1080, "url": "https://bcdn.hakunaymatata.com/resource/a.mp4?sign=1&t=1786715434", "format": "MP4", "cookie": ""},
            {"quality": 360, "url": "https://bcdn.hakunaymatata.com/bt/b.mp4?sign=2&t=1786716028", "format": "DASH", "cookie": ""},
        ],
    }


def _easyplex_response():
    return {
        "success": True,
        "provider": "easyplex",
        "movie": {"easyplex_id": 31385, "tmdb_id": 27205, "title": "Inception"},
        "videos": [
            {
                "server": "Server VIP EGY",
                "link": "https://s1.egybestvid.com/hls2/02/00000/aj1c9sw0ire8_n/master.m3u8?t=x&s=1",
                "header": "https://egybestvid.com/",
                "useragent": "Mozilla/5.0 (Android 14)",
                "hd": 0,
                "hls": 0,
                "resolved": True,
            },
            {
                "server": "Server Shahed 1080p",
                "link": "https://b2.shahidtv.net/files/Movies/hollywood/Inception-2010-bluray-1080p.mp4",
                "header": "https://downloader.disk.yandex.ru/",
                "useragent": "Mozilla/5.0 (Android 16)",
                "hd": 1,
                "hls": 0,
                "resolved": True,
            },
            {
                "server": "VIP Fast",
                "link": "https://www.fasel-hd.com/?p=7055",
                "header": "https://faselhd.center/",
                "useragent": "Mozilla/5.0 (Windows)",
                "hd": 1,
                "hls": 1,
                "resolved": False,
            },
        ],
    }


class _FakeResp:
    def __init__(self, body, status=200):
        self.status_code = status
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.content = self.text.encode("utf-8")

    def json(self):
        return json.loads(self.text)


class _Router:
    def __init__(self):
        self.calls = []

    def __call__(self, url, referer=None, timeout=25, headers=None, **kw):
        self.calls.append(url)
        if "engine.php" in url:
            if "&se=" in url or "&ep=" in url:
                return _FakeResp(_engine_tv_response())
            return _FakeResp(_engine_movie_response())
        if "/sources/movie" in url:
            return _FakeResp(_easyplex_response())
        if "fasel-hd.com" in url:
            return _FakeResp('<html><video src="https://cdn.fasel.test/hls/master.m3u8"></video></html>')
        return _FakeResp({})


def test_parse_engine_streams_tv_carries_cookie():
    from middleware.primetv import parse_engine_streams

    streams = parse_engine_streams(_engine_tv_response())
    assert len(streams) == 1
    assert streams[0]["kind"] == "dash"
    assert streams[0]["quality"] == 1080
    assert "CloudFront-Policy=" in streams[0]["cookie"]


def test_parse_engine_streams_movie_kind_from_extension():
    from middleware.primetv import parse_engine_streams

    streams = parse_engine_streams(_engine_movie_response())
    assert [s["quality"] for s in streams] == [1080, 360]
    assert all(s["kind"] == "mp4" for s in streams)
    assert all(s["cookie"] == "" for s in streams)


def test_parse_engine_streams_failed_response_is_empty():
    from middleware.primetv import parse_engine_streams

    assert parse_engine_streams({"success": False, "streams": []}) == []
    assert parse_engine_streams(None) == []


def test_parse_easyplex_videos():
    from middleware.primetv import parse_easyplex_videos

    videos = parse_easyplex_videos(_easyplex_response())
    assert len(videos) == 3
    direct = [v for v in videos if v["resolved"]]
    assert len(direct) == 2
    shahid = next(v for v in videos if "shahidtv" in v["url"])
    assert shahid["quality"] == 1080
    assert shahid["referer"] == "https://downloader.disk.yandex.ru/"
    assert shahid["user_agent"].startswith("Mozilla/5.0 (Android 16)")
    embed = next(v for v in videos if "fasel-hd" in v["url"])
    assert not embed["resolved"]


def test_extract_embed_links_common_patterns():
    from middleware.primetv import extract_embed_links

    html = """
    <script>
      var file = "https://cdn.x.com/p/master.m3u8";
      var src = 'https://cdn.y.com/film/file.mp4';
      sources : ["https://cdn.z.com/q/out.m3u8?t=1"];
      window.play('https://cdn.w.com/r/movie.mp4');
    </script>
    """
    links = extract_embed_links(html, "https://embed.test/player", limit=10)
    assert "https://cdn.x.com/p/master.m3u8" in links
    assert "https://cdn.y.com/film/file.mp4" in links
    assert "https://cdn.z.com/q/out.m3u8?t=1" in links
    assert "https://cdn.w.com/r/movie.mp4" in links


def test_extract_embed_links_unescapes_and_normalizes():
    from middleware.primetv import extract_embed_links

    html = 'url: "https:\\/\\/cdn.x.com\\/p\\/master.m3u8\\u003Ft=1"'
    links = extract_embed_links(html, "https://embed.test/", limit=1)
    assert links == ["https://cdn.x.com/p/master.m3u8?t=1"]


def test_extract_embed_links_skips_posters_and_ads():
    from middleware.primetv import extract_embed_links

    html = (
        'poster: "https://cdn.x.com/thumb/posters/m.jpg", '
        'url: "https://cdn.x.com/ads/sprite.png", '
        'file: "https://cdn.x.com/movie.mp4"'
    )
    links = extract_embed_links(html, "https://embed.test/")
    assert links == ["https://cdn.x.com/movie.mp4"]


def test_resolve_merges_engine_and_easyplex(monkeypatch):
    from middleware import primetv

    router = _Router()
    monkeypatch.setattr(primetv, "_get", router)
    monkeypatch.setattr(primetv, "_primetv_cache", {})
    monkeypatch.setattr(primetv, "_cfg", lambda: dict(primetv._DEFAULT_CONFIG["primetv"]))

    res = primetv.resolve(27205, "movie", title="Inception", year=2010)
    urls = [s["url"] for s in res.servers]
    # engine streams first (sorted by quality), then easyplex direct + embed link
    assert "https://bcdn.hakunaymatata.com/resource/a.mp4?sign=1&t=1786715434" in urls
    assert any("shahidtv" in u for u in urls)
    assert any("egybestvid" in u for u in urls)
    assert any("cdn.fasel.test" in u for u in urls)  # resolved embed page
    assert len(urls) == len(set(urls))  # deduped
    # sorted by quality descending
    quals = [next(s["quality"] for s in res.servers if s["url"] == u) for u in urls]
    assert quals == sorted(quals, reverse=True)


def test_resolve_tv_passes_season_episode(monkeypatch):
    from middleware import primetv

    router = _Router()
    monkeypatch.setattr(primetv, "_get", router)
    monkeypatch.setattr(primetv, "_primetv_cache", {})
    monkeypatch.setattr(primetv, "_cfg", lambda: dict(primetv._DEFAULT_CONFIG["primetv"]))

    res = primetv.resolve(1396, "tv", title="Breaking Bad", season=1, episode=1)
    assert res.servers and res.servers[0]["kind"] == "dash"
    assert any("&se=1" in u or "&ep=1" in u for u in router.calls if "engine.php" in u)


def test_providers_config_has_primetv_block():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json")
    with open(path, encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert "primetv" in cfg
    assert cfg["primetv"]["enabled"] is True
    assert cfg["primetv"]["engine_base_url"].startswith("https://")


def test_is_enabled_defaults():
    from middleware.primetv import is_enabled

    assert is_enabled() is True


def test_is_playable_media_accepts_media_only():
    from middleware.primetv import _is_playable_media

    assert _is_playable_media("https://cdn.x.com/hls2/01/00001/x_y/master.m3u8?t=1")
    assert _is_playable_media("https://bcdn.hakunaymatata.com/resource/a.mp4?sign=1")
    assert _is_playable_media("https://sacdn.hakunaymatata.com/dash/a/index_web.mpd")
    assert _is_playable_media("https://cdn.x.com/urlset/master.txt")
    # embed pages are HTML, not media — these show up as broken "mp4" in VLC
    assert not _is_playable_media("https://mp4plus.org/embed-57fzdih46d01.html")
    assert not _is_playable_media("https://embed.test/e/abc")
    assert not _is_playable_media("https://cdn.x.com/thumb/poster.jpg")


def test_verify_live_accepts_2xx(monkeypatch):
    from middleware import primetv

    class _Resp:
        status_code = 206

        def close(self):
            pass

    class _FakeHttp:
        def get(self, *a, **kw):
            return _Resp()

    monkeypatch.setattr(primetv, "_http", _FakeHttp())
    monkeypatch.setattr(primetv, "_fallback_http", _FakeHttp())
    assert primetv._verify_live("https://cdn.x.com/movie.mp4") is True


def test_verify_live_rejects_429_and_errors(monkeypatch):
    from middleware import primetv

    class _Resp429:
        status_code = 429

        def close(self):
            pass

    class _FakeHttp:
        def __init__(self, status):
            self._status = status

        def get(self, *a, **kw):
            if self._status == 0:
                raise RuntimeError("timeout")
            return _Resp429()

    monkeypatch.setattr(primetv, "_http", _FakeHttp(429))
    monkeypatch.setattr(primetv, "_fallback_http", _FakeHttp(429))
    assert primetv._verify_live("https://bcdn.x.com/a.mp4") is False

    monkeypatch.setattr(primetv, "_http", _FakeHttp(0))
    monkeypatch.setattr(primetv, "_fallback_http", _FakeHttp(0))
    assert primetv._verify_live("https://bcdn.x.com/a.mp4") is False


def test_resolve_drops_embed_pages(monkeypatch):
    """resolved:True entries pointing at HTML embed pages must not be listed."""
    from middleware import primetv

    class _FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {
                "success": True,
                "provider": "easyplex",
                "videos": [
                    {
                        "server": "VIP Fast",
                        "link": "https://mp4plus.org/embed-57fzdih46d01.html",
                        "header": "https://mp4plus.org/",
                        "useragent": "Mozilla/5.0",
                        "hd": 1,
                        "hls": 0,
                        "resolved": True,
                    }
                ],
            }

    class _Router:
        def __call__(self, url, referer=None, timeout=25, headers=None, **kw):
            return _FakeResp()

    monkeypatch.setattr(primetv, "_get", _Router())
    monkeypatch.setattr(primetv, "_primetv_cache", {})
    monkeypatch.setattr(primetv, "_cfg", lambda: dict(primetv._DEFAULT_CONFIG["primetv"]))

    res = primetv.resolve(27205, "movie", title="Inception", year=2010)
    assert res.servers == []
    assert not any("mp4plus" in s["url"] for s in res.servers)


def test_resolve_verify_live_filters_dead_servers(monkeypatch):
    from middleware import primetv

    router = _Router()
    monkeypatch.setattr(primetv, "_get", router)
    monkeypatch.setattr(primetv, "_primetv_cache", {})
    cfg = dict(primetv._DEFAULT_CONFIG["primetv"])
    cfg["verify_live"] = True
    monkeypatch.setattr(primetv, "_cfg", lambda: cfg)

    def fake_verify(url, referer="", cookie="", user_agent="", timeout=8.0):
        return "shahidtv" not in url and "mp4plus" not in url

    monkeypatch.setattr(primetv, "_verify_live", fake_verify)

    res = primetv.resolve(27205, "movie", title="Inception", year=2010)
    urls = [s["url"] for s in res.servers]
    assert urls  # engine + easyplex direct + resolved embed survive
    assert not any("shahidtv" in u for u in urls)
