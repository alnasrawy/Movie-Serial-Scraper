"""
run.py — نقطة الدخول الوحيدة لمشروع السكراب.

    python run.py --list                    # عرض المواقع المتاحة
    python run.py --serve                   # تشغيل الخادم على :8000
    python run.py --serve --port 9000       # تشغيل على بورت مخصص
    python run.py --query "inception"       # سكراب بحث
    python run.py --tmdb 27205              # بحث عبر TMDB
    python run.py --final "inception"       # روابط نهائية مع بروكسي
    python run.py --direct "inception"      # روابط مباشرة بدون بروكسي
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
try:
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
except Exception:
    pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from scraper.sites import available_sites, build_scraper, find_config
from scraper.fetcher import FetchSettings
from scraper.storage import to_json, to_csv
from middleware.http_resolver import resolve_http
from middleware.server import (
    _scrape_all,
    _resolve_embed,
    _proxy_url,
    app,
)

log = logging.getLogger("run")


def cmd_list():
    sites = available_sites()
    print("المواقع المتاحة:")
    for s in sites:
        print(f"  - {s}")
    if not sites:
        print("  (لا يوجد مواقع. أضف ملف JSON في configs/)")


def cmd_serve(args):
    import uvicorn
    port = int(os.environ.get("PORT") or os.environ.get("MIDDLEWARE_PORT") or args.port)
    print(f"تشغيل الخادم على http://0.0.0.0:{port} ...")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def cmd_scrape(args):
    if args.tmdb:
        from scraper.tmdb import api_key, search_query, tmdb_title
        key = api_key()
        if not key:
            print("TMDB_API_KEY غير موجود"); return 1
        info = tmdb_title(args.tmdb, key=key, media_type=args.type)
        query = search_query(info)
        print(f"TMDB #{info['tmdb_id']} -> {query}")
    else:
        query = args.query

    names = [n.strip() for n in args.sites.split(",")] if args.sites else available_sites()
    items = []
    for name in names:
        cfg = find_config(name)
        if cfg is None:
            print(f"موقع غير معروف: {name}"); return 1
        scraper = build_scraper(cfg, FetchSettings(delay=0.1, timeout=15, retries=1))
        items.extend(scraper.scrape(query, with_details=True, watch_only=args.watch_only))

    print(f"وجدنا {len(items)} عنصر")
    if not items:
        return 0
    out = Path(args.out) if args.out else Path("output") / f"{(query or 'home').replace(' ', '_')[:40]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    (to_json if args.format == "json" else to_csv)(items, out)
    print(f"حفّظنا -> {out}")
    return 0


def cmd_final(args):
    query = args.final
    sites = [s.strip() for s in (args.sites or "akwams,egydead").split(",") if s.strip()]
    items = []
    for name in sites:
        cfg = find_config(name)
        if cfg is None:
            continue
        scraper = build_scraper(cfg, FetchSettings(delay=0.5, timeout=20))
        print(f"[1/3] سكراب {cfg.name} ...", flush=True)
        items.extend(scraper.scrape(query, with_details=True, watch_only=True))
    if not items:
        print("لا يوجد نتائج"); return 1

    port = args.port

    async def resolve_all():
        print(f"[2/3] جاري حل الروابط ...", flush=True)
        for item in items:
            item["final_servers"] = []
            for sv in (item.get("watch_servers") or [])[:6]:
                url = sv.get("url")
                if not url:
                    continue
                res = await _resolve_embed(url, referer=item.get("detail_url"))
                if res.get("kind") == "none":
                    continue
                item["final_servers"].append({
                    "name": sv.get("name"),
                    "kind": res["kind"],
                    "url": f"http://127.0.0.1:{port}" + _proxy_url(res["sid"], res["url"]),
                })

    asyncio.run(resolve_all())

    print("\n[3/3] ===== الروابط النهائية =====", flush=True)
    total = 0
    for item in items:
        servers = item.get("final_servers", [])
        total += len(servers)
        print(f"\n* {item.get('title')} [{item.get('source')}]")
        for fs in servers:
            print(f"  - [{fs['name']}] {fs['url']}")
    print(f"\n{total} رابط. البروكسي شغال على http://127.0.0.1:{port}")

    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning")
    return 0


def cmd_direct(args):
    query = args.direct
    sites = [s.strip() for s in (args.sites or "akwams,egydead").split(",") if s.strip()]
    items = []
    seen = set()
    for item in _scrape_all(query, sites):
        key = (item.get("source"), item.get("detail_url") or item.get("title"))
        if key not in seen:
            seen.add(key)
            items.append(item)
    if not items:
        print(f"لا يوجد نتائج لـ {query}"); return 1

    import requests
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    results = []
    for item in items:
        for sv in item.get("watch_servers") or []:
            embed = sv.get("url")
            if not embed:
                continue
            got = resolve_http(embed, referer=item.get("detail_url"))
            if not got:
                continue
            if args.check:
                try:
                    r = requests.get(got["url"], headers={"User-Agent": UA}, timeout=15)
                    if r.status_code >= 400:
                        continue
                except Exception:
                    continue
            results.append({"site": item.get("source"), "name": sv.get("name"), "kind": got["kind"], "url": got["url"]})

    print(f"\n===== الروابط المباشرة ({len(results)}) =====")
    for r in results:
        print(f"  [{r['site']}] {r['name']}: {r['url']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in results:
                fh.write(f"# {r['site']} | {r['name']}\n{r['url']}\n")
        print(f"حفّظنا -> {args.out}")
    return 0 if results else 1


def main():
    parser = argparse.ArgumentParser(
        description="movies_serial_scraping — سكراب + خادم + روابط مباشرة"
    )
    parser.add_argument("--list", action="store_true", help="عرض المواقع المتاحة")
    parser.add_argument("--serve", action="store_true", help="تشغيل الخادم")
    parser.add_argument("--port", type=int, default=8000, help="البورت (8000)")
    parser.add_argument("--query", help="بحث بالاسم")
    parser.add_argument("--tmdb", type=int, help="بحث عبر TMDB id")
    parser.add_argument("--type", choices=["movie", "tv"], default="movie", help="نوع المحتوى")
    parser.add_argument("--sites", help="المواقع مفصولة بفاصلة")
    parser.add_argument("--watch-only", action="store_true", help="روابط المشاهدة فقط")
    parser.add_argument("--final", help="روابط نهائية مع بروكسي")
    parser.add_argument("--direct", help="روابط مباشرة بدون بروكسي")
    parser.add_argument("--check", action="store_true", help="فحص الروابط قبل الطباعة")
    parser.add_argument("--out", help="مسار ملف الإخراج")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if args.list:
        cmd_list()
    elif args.serve:
        cmd_serve(args)
    elif args.final:
        sys.exit(cmd_final(args))
    elif args.direct:
        sys.exit(cmd_direct(args))
    elif args.query or args.tmdb:
        sys.exit(cmd_scrape(args))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
