"""Unit tests for the subtitle provider: parsing, decoding, srt->vtt.

No network — fixtures are inline.
"""

from __future__ import annotations

import gzip

_ARABIC_SRT = (
    "1\r\n"
    "00:00:01,000 --> 00:00:03,000\r\n"
    "مرحبا بالعالم\r\n"
    "www.example.com\r\n"
    "\r\n"
    "2\r\n"
    "00:01:02,500 --> 00:01:05,750\r\n"
    "السلام عليكم\r\n"
)


def _fake_subs() -> list[dict]:
    return [
        {"SubLanguageID": "eng", "SubDownloadsCnt": "10", "SubFileName": "e.srt"},
        {"SubLanguageID": "ara", "SubDownloadsCnt": "50", "SubFileName": "ar1.srt"},
        {"SubLanguageID": "ara", "SubDownloadsCnt": "3", "SubFileName": "ar2.srt"},
        {"SubLanguageID": "per", "SubDownloadsCnt": "7", "SubFileName": "fa.srt"},
    ]


def test_available_languages_counts_and_orders():
    from middleware.subtitles import available_languages

    langs = available_languages(_fake_subs())
    assert langs[0]["code"] == "ara"  # most matches first
    assert langs[0]["count"] == 2
    codes = [l["code"] for l in langs]
    assert "eng" in codes and "per" in codes


def test_best_for_language_picks_most_downloaded():
    from middleware.subtitles import best_for_language

    best = best_for_language(_fake_subs(), "ara")
    assert best is not None
    assert best["SubFileName"] == "ar1.srt"
    assert best_for_language(_fake_subs(), "zzz") is None


def test_decompress_gzip():
    from middleware.subtitles import _decompress

    raw = _ARABIC_SRT.encode("utf-8")
    data, kind = _decompress(gzip.compress(raw))
    assert kind == "gzip"
    assert data == raw


def test_decode_utf8_and_cp1256():
    from middleware.subtitles import _decode

    assert "مرحبا" in _decode(_ARABIC_SRT.encode("utf-8"))
    cp1256 = _ARABIC_SRT.encode("cp1256")
    assert "مرحبا" in _decode(cp1256)


def test_srt_to_vtt_rewrites_timestamps_and_keeps_arabic():
    from middleware.subtitles import srt_to_vtt

    vtt = srt_to_vtt(_ARABIC_SRT)
    assert vtt.startswith("WEBVTT")
    assert "00:00:01.000 --> 00:00:03.000" in vtt
    assert "00:01:02.500 --> 00:01:05.750" in vtt
    assert "مرحبا بالعالم" in vtt
    # cue numbers and leading gap are dropped
    assert "\n1\n" not in vtt
    assert not vtt.startswith("WEBVTT\n\n\n")


def test_search_uses_numeric_imdb_id(monkeypatch):
    from middleware import subtitles

    calls = []

    class FakeResp:
        status_code = 200
        text = "[]"

        def json(self):
            return []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return FakeResp()

    monkeypatch.setattr(subtitles, "_get", fake_get)
    subtitles._search_cache.clear()
    assert subtitles.search("tt1375666") == []
    assert len(calls) == 1
    assert "/imdbid-1375666" in calls[0]
