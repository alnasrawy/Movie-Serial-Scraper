"""One command: scrape the sites and produce final playable links for a native player.

Usage:
    python final_links.py "Spider-Man: Brand New Day"
    python final_links.py "inception" --sites akwams,egydead --out final.json

It scrapes the watch servers, resolves each embed in a headless browser, keeps
a live HLS proxy on http://127.0.0.1:<port> (so the token-renewal/PNG-unwrap
magic keeps working), and prints the final URLs — paste any into VLC/ExoPlayer
while the script runs. Press Ctrl+C to stop.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    import ctypes
    ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    ctypes.windll.kernel32.SetConsoleCP(65001)
except Exception:
    pass

import uvicorn

from scraper.fetcher import FetchSettings
from scraper.sites import available_sites, build_scraper, find_config
from middleware import server


def scrape_sites(query: str, sites: list[str]) -> list[dict]:
    configs: list = []
    for name in sites:
        cfg = find_config(name)
        if cfg is None:
            print(f"Unknown site {name!r}. Available: {', '.join(available_sites())}")
            sys.exit(1)
        configs.append(cfg)
    settings = FetchSettings(delay=0.5, timeout=20)
    items: list[dict] = []
    for cfg in configs:
        scraper = build_scraper(cfg, settings)
        print(f"[1/3] Scraping {cfg.name} ...", flush=True)
        items.extend(scraper.scrape(query, with_details=True, watch_only=True))
    return items


async def resolve_items(items: list[dict], port: int, timeout: float, max_servers: int) -> None:
    print(f"[2/3] Resolving {sum(1 for i in items for _ in i.get('watch_servers', []))} embed links "
          f"(pure HTTP first, browser fallback) ...", flush=True)
    for item in items:
        item["final_servers"] = []
        for sv in (item.get("watch_servers") or [])[:max_servers]:
            url = sv.get("url")
            if not url:
                continue
            print(f"      {sv.get('name')}: {url}", flush=True)
            res = await server._resolve_embed(url, referer=item.get("detail_url"))
            if res.get("kind") == "none":
                continue
            item["final_servers"].append({
                "name": sv.get("name"),
                "original_name": sv.get("original_name"),
                "kind": res["kind"],
                "url": f"http://127.0.0.1:{port}" + server._proxy_url(res["sid"], res["url"]),
            })


def print_report(items: list[dict]) -> None:
    print("\n[3/3] ===== FINAL LINKS (open in VLC / ExoPlayer) =====", flush=True)
    total = 0
    for item in items:
        servers = item.get("final_servers", [])
        total += len(servers)
        print(f"\n* {item.get('title')}  [{item.get('source')} | {item.get('quality')}]")
        if not servers:
            print("    (no watch server could be resolved right now — try another title)")
        for fs in servers:
            print(f"    - [{fs['name']}] {fs['kind']}")
            print(f"      {fs['url']}")
    print(f"\n{total} working link(s).\n")


async def main(args) -> int:
    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    items = scrape_sites(args.query, sites)
    if not items:
        print("No items found.")
        return 1

    await resolve_items(items, args.port, args.timeout, args.max_servers)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False, indent=2)
        print(f"Saved -> {args.out}")

    print_report(items)

    config = uvicorn.Config(server.app, host="0.0.0.0", port=args.port, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.1)
    print(f"HLS proxy is live on http://127.0.0.1:{args.port} — links stay valid while "
          f"this runs. Press Ctrl+C to stop.", flush=True)
    try:
        await serve_task
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Scrape akwams/egydead and produce final playable links in one go."
    )
    parser.add_argument("query", help="Search query (quote it if it has spaces)")
    parser.add_argument("--sites", default="akwams,egydead", help="Comma-separated sites")
    parser.add_argument("--port", type=int, default=8000, help="Port for the HLS proxy")
    parser.add_argument("--timeout", type=float, default=25.0, help="Seconds per embed")
    parser.add_argument("--max-servers", type=int, default=6, help="Servers to try per title")
    parser.add_argument("--out", help="Optional JSON output path")
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args)))
