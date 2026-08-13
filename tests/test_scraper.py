"""Unit tests for the scraping engine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scraper.fetcher import FetchSettings, Fetcher, FetchedPage  # noqa: E402
from scraper.generic import GenericScraper, extract_text  # noqa: E402
from scraper.sites import load_sites, find_config  # noqa: E402
from scraper.storage import to_csv, to_json  # noqa: E402
from scraper.base import SiteConfig  # noqa: E402


HTML = """
<html><body>
<article class="movie-card">
  <h2 class="movie-title">Inception</h2>
  <span class="year">2010</span>
  <span class="rating">8.8</span>
  <a class="movie-link" href="/movies/inception">Details</a>
</article>
<article class="movie-card">
  <h2 class="movie-title">The Matrix</h2>
  <span class="year">1999</span>
  <span class="rating">8.7</span>
  <a class="movie-link" href="/movies/the-matrix">Details</a>
</article>
</body></html>
"""


class FakeFetcher:
    def get_soup(self, url: str, **kwargs) -> FetchedPage:
        from bs4 import BeautifulSoup

        if url.startswith("https://site.test/movies/"):
            title = url.rsplit("/", 1)[-1].replace("-", " ").title()
            html = f"<div class='synopsis'>Synopsis of {title}</div>"
            soup = BeautifulSoup(html, "lxml")
            return FetchedPage(url=url, soup=soup, status_code=200)
        return FetchedPage(url=url, soup=BeautifulSoup(HTML, "lxml"), status_code=200)

    def post_soup(self, url: str, data: dict | None = None, **kwargs) -> FetchedPage:
        from bs4 import BeautifulSoup

        if data == {"View": "1"}:
            html = """
            <ul class="serversList">
              <li data-link="https://ruby.test/embed-abc"><span><p>StreamRuby</p></span></li>
              <li data-link="https://mix.test/e/xyz"><span><p>Mixdrop</p></span></li>
            </ul>
            <ul class="donwload-servers-list">
              <li>
                <span class="ser-name">تحميل مباشر</span>
                <div class="server-info"><em>1080p</em></div>
                <a class="ser-link" href="https://dl.test/movie.mp4">حمل الان</a>
              </li>
            </ul>
            """
            soup = BeautifulSoup(html, "lxml")
            return FetchedPage(url=url, soup=soup, status_code=200)
        return FetchedPage(url=url, soup=BeautifulSoup(HTML, "lxml"), status_code=200)


def make_config() -> SiteConfig:
    return SiteConfig(
        name="test",
        base_url="https://site.test/",
        search_url="https://site.test/search/{query}",
        item_selector="article.movie-card",
        fields={
            "title": "h2.movie-title",
            "year": "span.year",
            "rating": "span.rating",
            "detail_url": "a.movie-link@href",
        },
        custom={"detail_fields": {"description": ".synopsis"}},
    )


def test_extract_text_attribute():
    from bs4 import BeautifulSoup

    node = BeautifulSoup("<a href='/x'>go</a>", "lxml")
    assert extract_text(node, "a@href") == "/x"
    assert extract_text(node, "a") == "go"


def test_extract_text_self_attribute():
    from bs4 import BeautifulSoup

    soup = BeautifulSoup("<ul><li data-link='https://x.test/e/1'><p>Server</p></li></ul>", "lxml")
    node = soup.select_one("li")
    assert extract_text(node, "@data-link") == "https://x.test/e/1"


def test_extract_text_self_text():
    from bs4 import BeautifulSoup

    node = BeautifulSoup("<button class='server-btn'>sirver 1</button>", "lxml").button
    assert extract_text(node, ".") == "sirver 1"


def test_generic_scraper_extra_detail_pages():
    from bs4 import BeautifulSoup

    class SubFetcher(FakeFetcher):
        def get_soup(self, url: str, **kwargs) -> FetchedPage:
            if url.endswith("/watch"):
                html = """
                <div class="watch-modern">
                  <button class="server-btn active" data-link="https://hg.test/e/1">sirver 1</button>
                  <button class="server-btn" data-link="https://mix.test/e/2">sirver 2</button>
                </div>
                """
                return FetchedPage(url=url, soup=BeautifulSoup(html, "lxml"), status_code=200)
            if url.endswith("/download"):
                html = """
                <div class="download-cards">
                  <div class="download-card">
                    <a class="download-btn" href="https://dl.test/m1.mp4"><h4>تحميل مباشر</h4></a>
                  </div>
                </div>
                """
                return FetchedPage(url=url, soup=BeautifulSoup(html, "lxml"), status_code=200)
            return super().get_soup(url, **kwargs)

    config = make_config()
    config.fields["detail_url"] = "a.movie-link@href"
    config.custom = {
        "extra_detail_pages": [
            {"suffix": "watch", "servers": ["watch_servers"]},
            {"suffix": "download", "servers": ["download_servers"]},
        ],
        "watch_servers": {"item_selector": "button.server-btn", "fields": {"name": ".", "url": "@data-link"}},
        "download_servers": {"item_selector": "div.download-card", "fields": {"name": "h4", "url": "a.download-btn@href"}},
    }
    scraper = GenericScraper(config, SubFetcher())
    items = scraper.scrape("inception", with_details=True)
    first = items[0]
    assert first["watch_servers"] == [
        {"name": "sirver 1", "url": "https://hg.test/e/1"},
        {"name": "sirver 2", "url": "https://mix.test/e/2"},
    ]
    assert first["download_servers"] == [{"name": "تحميل مباشر", "url": "https://dl.test/m1.mp4"}]


def test_resolve_embed_packer_fallback_non_earnvids():
    from bs4 import BeautifulSoup

    from scraper.resolver import resolve_embed

    class Rfetcher:
        def get_soup(self, url: str, **kwargs) -> FetchedPage:
            return FetchedPage(url=url, soup=BeautifulSoup(f"<script>{PACKED}</script>", "lxml"), status_code=200)

    res = resolve_embed("https://streamhg.test/e/abc", Rfetcher())
    assert res.get("direct_url") == "http://srv.cdn/path/hls.m3u8"


def test_extract_server_list():
    from bs4 import BeautifulSoup

    from scraper.generic import extract_server_list

    soup = BeautifulSoup(
        """
        <ul class="serversList">
          <li data-link="https://a.test/e/1"><span><p>Ruby</p></span></li>
          <li data-link="https://b.test/e/2"><span><p>Mix</p></span></li>
        </ul>
        """,
        "lxml",
    )
    spec = {
        "item_selector": "ul.serversList > li[data-link]",
        "fields": {"name": "p", "url": "@data-link"},
    }
    assert extract_server_list(soup, spec) == [
        {"name": "Ruby", "url": "https://a.test/e/1"},
        {"name": "Mix", "url": "https://b.test/e/2"},
    ]


def test_generic_scraper_post_detail_servers():
    config = make_config()
    config.detail_method = "post"
    config.detail_data = {"View": "1"}
    config.custom = {
        "watch_servers": {
            "item_selector": "ul.serversList > li[data-link]",
            "fields": {"name": "p", "url": "@data-link"},
        },
        "download_servers": {
            "item_selector": "ul.donwload-servers-list > li",
            "fields": {"name": "span.ser-name", "quality": "div.server-info em", "url": "a.ser-link@href"},
        },
    }
    scraper = GenericScraper(config, FakeFetcher())
    items = scraper.scrape("inception", with_details=True)
    first = items[0]
    assert first["watch_servers"] == [
        {"name": "StreamRuby", "url": "https://ruby.test/embed-abc"},
        {"name": "Mixdrop", "url": "https://mix.test/e/xyz"},
    ]
    assert first["download_servers"] == [
        {"name": "تحميل مباشر", "quality": "1080p", "url": "https://dl.test/movie.mp4"}
    ]


def test_generic_scraper_with_details():
    scraper = GenericScraper(make_config(), FakeFetcher())
    items = scraper.scrape("inception", with_details=True)
    assert len(items) == 2
    first = items[0]
    assert first["title"] == "Inception"
    assert first["year"] == "2010"
    assert first["rating"] == "8.8"
    assert first["detail_url"] == "https://site.test/movies/inception"
    assert first["description"] == "Synopsis of Inception"
    assert first["source"] == "test"


def test_generic_scraper_no_details():
    scraper = GenericScraper(make_config(), FakeFetcher())
    items = scraper.scrape("inception", with_details=False)
    assert "description" not in items[0]


def test_watch_only_skips_download_subpage():
    class F(FakeFetcher):
        calls: list[str] = []

        def get_soup(self, url: str, **kwargs) -> FetchedPage:
            F.calls.append(url)
            if url.endswith("/watch"):
                from bs4 import BeautifulSoup

                html = '<button class="server-btn" data-link="https://hg.test/e/1">سيرفر 1</button>'
                return FetchedPage(url=url, soup=BeautifulSoup(html, "lxml"), status_code=200)
            return super().get_soup(url, **kwargs)

    config = make_config()
    config.fields["detail_url"] = "a.movie-link@href"
    config.custom = {
        "extra_detail_pages": [
            {"suffix": "watch", "servers": ["watch_servers"]},
            {"suffix": "download", "servers": ["download_servers"]},
        ],
        "watch_servers": {"item_selector": "button.server-btn", "fields": {"name": ".", "url": "@data-link"}},
    }
    scraper = GenericScraper(config, F())
    items = scraper.scrape("inception", with_details=True, watch_only=True)
    assert "download_servers" not in items[0]
    assert items[0]["watch_servers"] == [{"name": "سيرفر 1", "url": "https://hg.test/e/1"}]
    assert not any("/download" in call for call in F.calls)


def test_sites_load_and_find():
    configs = load_sites("configs")
    names = [c.name for c in configs]
    assert "akwams" in names
    assert "egydead" in names
    assert find_config("akwams") is not None
    assert find_config("ak") is not None  # prefix match


def test_akwams_config_loads():
    config = find_config("akwams")
    assert config is not None
    assert config.search_url == "https://akwams.org/?s={query}"
    assert config.item_selector == "a.movie__block"
    custom = config.custom
    assert custom["extra_detail_pages"][0]["suffix"] == "watch"
    assert custom["watch_servers"]["item_selector"] == "button.server-btn"
    assert custom["download_servers"]["fields"]["url"] == "a.download-btn@href"


def test_storage_roundtrip():
    import tempfile

    items = [{"title": "فيلم", "rating": "8.8"}]
    with tempfile.TemporaryDirectory() as tmp:
        j = to_json(items, Path(tmp) / "out.json")
        assert json.loads(j.read_text(encoding="utf-8")) == items
        c = to_csv(items, Path(tmp) / "out.csv")
        assert "فيلم" in c.read_text(encoding="utf-8-sig")


PACKED = r"""eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\b'+c.toString(a)+'\\b','g'),k[c]);return p}('2="3://4.5/6/1.m3u8"',36,11,'w|hls|u|http|srv|cdn|path|z|h|https|x'.split('|')))"""


def test_unpack_packer():
    from scraper.resolver import unpack_packer

    assert "http://srv.cdn/path/hls.m3u8" in unpack_packer(PACKED)


PACKED_URLSET = (
    "eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace("
    "new RegExp('\\\\b'+c.toString(a)+'\\\\b','g'),k[c]);return p}("
    "'0=\"1://2/3/x,l,n,.4/5.txt\";',36,25,"
    "'var|https|host|path|urlset|master|||||||||||||||||||'.split('|')))"
)


def test_unpack_packer_keeps_literal_l_n_in_urlset():
    """Base-36 letters `l`/`n` inside ",l,n,.urlset" are literal text; the
    decoder must skip empty table entries (`if(k[c])`) instead of blanking
    them out (regression: ",l,n," used to come back as ",,,")."""
    from scraper.resolver import unpack_packer

    out = unpack_packer(PACKED_URLSET)
    assert "x,l,n,.urlset/master.txt" in out
    assert ",,," not in out


def test_resolve_embed_earnvids():
    from bs4 import BeautifulSoup

    from scraper.resolver import resolve_embed

    class Rfetcher:
        def get_soup(self, url: str, **kwargs) -> FetchedPage:
            return FetchedPage(url=url, soup=BeautifulSoup(f"<script>{PACKED}</script>", "lxml"), status_code=200)

    res = resolve_embed("https://vidhide.com/v/abc", Rfetcher())
    assert res.get("direct_url") == "http://srv.cdn/path/hls.m3u8"
    assert res.get("method") == "packer"


def test_rate_limited_error_on_429():
    import requests

    class BlockingAdapter(requests.adapters.HTTPAdapter):
        def send(self, request, **kwargs):
            resp = requests.Response()
            resp.status_code = 429
            resp.url = request.url
            resp.request = request
            return resp

    session = requests.Session()
    session.mount("http://", BlockingAdapter())
    session.mount("https://", BlockingAdapter())
    fetcher = Fetcher(settings=FetchSettings(retries=2), session=session)

    from scraper.fetcher import RateLimitedError

    with pytest.raises(RateLimitedError):
        fetcher.get_soup("http://site.test/x")


def _fake_check(status_by_url):
    def fake(url: str, timeout: float = 10.0):
        status = status_by_url.get(url, 404)
        if status == 0:  # network error
            return {"alive": False, "status": None, "content_type": "", "body": b"", "error": "boom"}
        return {
            "alive": 200 <= status < 400,
            "status": status,
            "content_type": "video/mp4" if url.endswith(".mp4") else "text/html",
            "body": b"",
        }

    return fake


def test_verify_server_keeps_alive_embed(monkeypatch):
    from scraper.verify import verify_server

    monkeypatch.setattr("scraper.verify.check_link", _fake_check({"https://ok.test/e/1": 200}))
    server = {"name": "سيرفر 1", "url": "https://ok.test/e/1"}
    assert verify_server(server, delay=0) is True
    assert server["playable"] == "embed"


def test_verify_server_drops_dead(monkeypatch):
    from scraper.verify import verify_server

    monkeypatch.setattr("scraper.verify.check_link", _fake_check({"https://dead.test/e/1": 500}))
    assert verify_server({"name": "سيرفر 1", "url": "https://dead.test/e/1"}, delay=0) is False


def test_verify_server_direct_fallback(monkeypatch):
    from scraper.verify import verify_server

    monkeypatch.setattr(
        "scraper.verify.check_link",
        _fake_check({"https://cdn.test/x.mp4": 403, "https://emb.test/e/1": 200}),
    )
    server = {"name": "سيرفر 1", "url": "https://emb.test/e/1", "direct_url": "https://cdn.test/x.mp4"}
    assert verify_server(server, delay=0) is True
    assert "direct_url" not in server
    assert server["playable"] == "embed"


def test_label_servers():
    from scraper.verify import label_servers

    item = {
        "watch_servers": [{"name": "▶ سيرفر 1", "url": "a"}, {"name": "▶ سيرفر 2", "url": "b"}],
        "download_servers": [{"name": "تحميل مباشر", "url": "c"}],
    }
    label_servers(item)
    assert item["watch_servers"][0]["name"] == "سيرفر 1"
    assert item["watch_servers"][0]["server_number"] == 1
    assert item["watch_servers"][0]["original_name"] == "▶ سيرفر 1"
    assert item["watch_servers"][1]["name"] == "سيرفر 2"
    assert item["download_servers"][0]["name"] == "تحميل 1"


def test_tmdb_title_and_search_query(monkeypatch):
    import requests as real_requests

    from scraper.tmdb import search_query, tmdb_title

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "title": "بداية",
                "original_title": "Inception",
                "overview": "نظرة",
            }

    monkeypatch.setattr(real_requests, "get", lambda *a, **k: FakeResp())
    info = tmdb_title(27205, key="k", media_type="movie")
    assert info["title"] == "بداية"
    assert search_query(info) == "بداية"
    assert search_query(info, prefer_arabic=False) == "Inception"
