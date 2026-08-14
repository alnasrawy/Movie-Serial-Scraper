"""Unit tests for the foreign (vidsrc) provider parsing helpers.

Pure parsing only — no network, no WebAssembly execution.
"""

from __future__ import annotations

import json


def _embed_html() -> str:
    return (
        '<html><body>\n'
        '<iframe id="player_iframe" '
        'src="https://cloudorchestranova.com/embed/movie/27205?vs=AbC123token"></iframe>\n'
        "</body></html>"
    )


def _player_html() -> str:
    cfg = {
        "mediaType": "movie",
        "tmdb": "27205",
        "api": "https://data.vidsrcme.ru/api.php?type=movie&tmdb=27205&stream_urls",
        "streamBase": "",
        "cacheBase": "/embed/iframe_player/cache.php",
    }
    return "<script>window.CONFIG = %s;</script>" % json.dumps(cfg)


def test_extract_iframe_src():
    from middleware.vidsrc import extract_iframe_src

    url = extract_iframe_src(_embed_html())
    assert url and url.startswith("https://cloudorchestranova.com/embed/movie/27205")
    assert "vs=AbC123token" in url


def test_extract_config():
    from middleware.vidsrc import extract_config

    cfg = extract_config(_player_html())
    assert cfg["mediaType"] == "movie"
    assert "stream_urls" in cfg["api"]
    assert cfg["streamBase"] == ""


def test_api_url_for_movie():
    from middleware.vidsrc import api_url_for, extract_config

    cfg = extract_config(_player_html())
    assert api_url_for(cfg, "movie") == cfg["api"]


def test_api_url_for_tv_appends_season_episode():
    from middleware.vidsrc import api_url_for

    cfg = {"streamBase": "https://data.vidsrcme.ru/api.php?type=tv&tmdb=111"}
    url = api_url_for(cfg, "tv", season=2, episode=5)
    assert "&season=2" in url
    assert "&episode=5" in url
    assert "&stream_urls" in url


def test_parse_token_plain_string():
    from middleware.vidsrc import parse_token

    assert parse_token("  eyJhbGciOiJIUzI1NiJ9  ") == "eyJhbGciOiJIUzI1NiJ9"


def test_parse_token_json_forms():
    from middleware.vidsrc import parse_token

    assert parse_token('{"token": "abc"}') == "abc"
    assert parse_token('{"data": "def"}') == "def"
    assert parse_token('{"result": "xyz"}') == "xyz"
    assert parse_token("not json at all") == "not json at all"


def test_stamp_token_appends_or_uses_separator():
    from middleware.vidsrc import stamp_token

    assert stamp_token("https://h/pl/master.m3u8", "T") == "https://h/pl/master.m3u8?token=T"
    assert stamp_token("https://h/pl/master.m3u8?x=1", "T") == "https://h/pl/master.m3u8?x=1&token=T"
    assert stamp_token("https://h/pl/master.m3u8", "") == "https://h/pl/master.m3u8"


def test_parse_stream_urls_plain_array():
    from middleware.vidsrc import parse_stream_urls

    urls = parse_stream_urls({"stream_urls": ["https://a/pl/1/master.m3u8", "https://b/pl/2/master.m3u8"]})
    assert len(urls) == 2
    assert urls[0].endswith("master.m3u8")


def test_parse_stream_urls_encrypted_without_wasm_returns_empty():
    """An encrypted payload without a `vs` block has no decryptor -> no urls."""
    from middleware.vidsrc import parse_stream_urls

    assert parse_stream_urls({"stream_urls": "CMh51p7fczCUAQ==", "vs": {}}) == []


def test_parse_stream_urls_reads_vs_from_top_level(monkeypatch):
    """The real API puts `vs` at the top level, `stream_urls` inside `data`."""
    from middleware import vidsrc

    monkeypatch.setattr(vidsrc, "_decrypt_via_wasm", lambda enc, wurl, wb64: ["https://a/pl/master.m3u8"])
    out = vidsrc.parse_stream_urls(
        {"data": {"stream_urls": "ENC"}, "vs": {"wasm_url": "https://w/x.wasm"}}
    )
    assert out == ["https://a/pl/master.m3u8"]


def test_parse_stream_urls_missing_returns_empty():
    from middleware.vidsrc import parse_stream_urls

    assert parse_stream_urls({}) == []
    assert parse_stream_urls({"stream_urls": ""}) == []
    assert parse_stream_urls({"stream_urls": None}) == []


def test_providers_config_file_shape():
    import json as _json
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "configs", "providers.json")
    cfg = _json.load(open(path, encoding="utf-8"))
    assert "vidsrc" in cfg
    assert cfg["vidsrc"]["embed_base"].startswith("https://")
    assert cfg["primetv"]["enabled"] is True
    assert "subtitles" in cfg


def test_is_enabled_defaults():
    from middleware.vidsrc import is_enabled

    assert is_enabled() is False
