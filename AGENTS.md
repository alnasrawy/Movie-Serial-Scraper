# AGENTS.md — Guidance for AI coding assistants

This file gives an AI (like opencode, ChatGPT, Claude, Cursor) everything it
needs to work on this project safely and effectively.

## Project in one paragraph

A Python backend that scrapes movie/series watch servers from Arabic streaming
sites (**akwams** and **egydead**, both config-driven via JSON) and resolves
the hosts' JavaScript-protected embed pages into a playable **HLS stream** for a
**native player** (ExoPlayer / VLC). The scraping engine is generic (CSS
selectors from config files — no per-site code). The `middleware/` package runs
a real headless Chromium via Playwright, keeps short-lived CDN tokens alive by
reloading the embed session, and proxies the HLS playlists/segments.

## How to run

```powershell
pip install -r requirements.txt
python -m playwright install chromium        # only needed for the middleware
pip install -r requirements-dev.txt          # only for running tests

python cli.py --list
python cli.py --query "inception" --sites "akwams,egydead" --watch-only
python final_links.py "inception"            # one-shot: scrape + resolve + live HLS links
python -m middleware                          # standalone FastAPI server (uvicorn :8000)
python -m pytest tests -q                     # 38 tests, no network needed
```

## Architecture

```
configs/*.json      -> SiteConfig (CSS selectors, custom hooks). Sites are data.
scraper/
  base.py           -> BaseScraper.scrape(with_details, watch_only) + SiteConfig dataclass
  fetcher.py        -> requests wrapper: politeness delay, retries, page budget
  generic.py        -> CSS-driven parse_listing / parse_detail + server extraction
  sites.py          -> registry: load_sites / find_config / build_scraper
  resolver.py       -> best-effort direct-URL extraction (regex + Dean Edwards packer)
  verify.py         -> live link checks + numbering labels (سيرفر 1, ...)
  storage.py        -> JSON / CSV export
  tmdb.py           -> TMDB id -> Arabic search title (needs 32-char v3 API key)
middleware/
  http_resolver.py  -> PURE-HTTP resolver (no browser): GET embed -> unpack_packer
                       -> .urlset/master.txt -> HLS with Referer. Cheap/fast path.
  player.py         -> BrowserManager: long-lived Chromium, per-embed sessions,
                       ctx.request fetch (shares cookies, avoids headless TLS headers),
                       fetch() replays the embed Referer, refresh_session() mints new tokens
  server.py         -> FastAPI app: POST /watch (TMDB id -> ready server list, the
                       main-app contract), POST /resolve, GET /stream (rewrites m3u8,
                       strips PNG-wrapped TS, auto-refresh on 401/403), /health.
                       Hybrid resolution: HTTP-first, browser only when BROWSER_ENABLED=1.
Dockerfile, Dockerfile.lite, docker-compose.yml, render.yaml, .env.example, DEPLOYMENT.md
  -> run the backend on a VPS 24/7; the app calls http://IP:8000/watch.
     Dockerfile.lite = pure HTTP (free Render tier, ~100MB). Dockerfile = browser (VPS).
cli.py              -> argparse CLI wrapping the scraper
final_links.py      -> one command: scrape(watch_only) -> open each embed -> keep a
                       uvicorn proxy alive -> print final http://127.0.0.1:<port>/stream? URLs
tests/              -> 42 tests. conftest.py sets PROJECT_ROOT on sys.path and starts
                       a local mock site (tests/mock_site.py) — no internet required.
```

## Hard-won technical facts (do not "simplify" these away)

- The video hosts mint **short-lived tokens** (vibuxer/hgcloud URLs die in
  minutes). Never cache a resolved `direct_url` for long; always re-`/resolve`.
- Headless Chrome sends `sec-ch-ua: ...HeadlessChrome...` which the CDNs
  (vibuxer/hgcloud) use to **403 the page request**. That is why
  `BrowserManager.fetch` uses `ctx.request` (the APIRequestContext that shares
  the browser context's cookie jar but sends neutral TLS headers). Keep this.
- `Browser.new_context()` has **no** `referer` kwarg — set it on `page.goto`.
- **EarnVids-family hosts (smoothpre) resolve with pure HTTP — no browser:**
  the embed page's Dean Edwards packer holds the media URL *as static text*
  (e.g. `..._ ,l,n,.urlset/master.txt`), and the CDN serves HLS to a plain
  `requests` GET **with Referer = the embed page** (200, `application/vnd.apple.mpegurl`).
  `middleware/http_resolver.resolve_http()` implements this; it is the default
  path on the free tier. minochinos/morencius are a different page structure
  (no packer) and currently resolve in neither mode.
- **`unpack_packer` must skip empty table entries** (`if(k[c])` like the original
  JS): base-36 letters `l`/`n` inside the literal `,l,n,.urlset` path are NOT
  tokens. Replacing them with empty strings corrupted `,l,n,` into `,,,` (the
  CDN then 404s). Regression-tested in tests/test_scraper.py.
- vibuxer/hgcloud: token is minted by heavily obfuscated JS (obfuscator.io,
  `main.js?v=1.1.9`) that even hangs a plain Node run; browser-only host. Do not
  attempt static deobfuscation.
- tiktokcdn wraps MPEG-TS segments in a **PNG container**. `_strip_png_wrapper`
  must find a TS sync run (0x47 at i, i+188, i+2*188) — the PNG signature itself
  contains a 0x47 ("G"), so a naive `find(b"\x47")` is WRONG (regression tested).
- `mixdrop` (reCAPTCHA), `playmogo/dsvplay` (no media start) and `koramaup`
  (obfuscated JS) do **not** resolve yet — document, don't force.

## Conventions

- **No code comments unless the user asks.** Docstrings on modules/functions are
  fine (they are part of the existing style).
- Sites are **data, not code**: to add a site, add `configs/<name>.json` with
  CSS selectors + `custom` hooks. Do not hardcode selectors in Python.
- UI/UX text is **Arabic**. Server names use `سيرفر {n}` / `تحميل {n}`.
- Keep `watch_only` a first-class option everywhere (the product wants watch
  links only, no download links).
- `final_links.py` and `cli.py` must both keep working; they share `scraper/`.

## Roadmap (the mission for the next coding session)

Build the end-user **app** on top of this backend. The AI should design and
implement, using the files above as reference:

1. **Settings UI inside the app** for the movie/series sites:
   - toggle each site on/off, edit its `base_url`, per-site request delay,
   - these write back to the `configs/*.json` files (validate before saving).
2. **TMDB-driven interface**: browse/import movies & series from TMDB
   (popular/trending lists, search by id or title) and wire them into the
   scraper pipeline (`scraper.tmdb.tmdb_title` + `search_query` already exist).
3. **Scaling with user count** ("كلما نزل المستخدم أكثر جلبت أفلامًا/مسلسلات أكثر"):
   - a fetch queue + per-TMDB-id cache so the same title is scraped once,
   - a worker that prefetches trending/popular titles on a schedule,
   - rate-limit politely per site (the hosts block aggressive crawling).
4. Keep the ExoPlayer consumption contract: the app should call
   `POST /resolve` at playback time and feed `proxy_url` to the player
   (tokens expire, so resolve-per-play).

## Verification

- `python -m pytest tests -q` must stay green before finishing any change.
- `python -m compileall -q scraper middleware cli.py final_links.py tools`
- After middleware changes, run `python final_links.py "inception" --max-servers 1`
  once to smoke-test a real resolve (needs the installed Chromium).
