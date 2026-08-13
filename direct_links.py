"""Extract DIRECT m3u8 playable links from the watch servers, no proxy needed.

Unlike final_links.py (which keeps a local HLS proxy), this prints the actual
media URLs the CDNs serve (EarnVids family: signed hls2 .../master.m3u8), which
play straight in ExoPlayer/VLC — no Referer header required for these hosts.

Usage:
    python direct_links.py "Inception"
    python direct_links.py --tmdb 27205 --type movie
    python direct_links.py "استهلال" --sites akwams,egydead --check --out links.txt
"""

from __future__ import annotations

import argparse
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

import requests

from middleware.http_resolver import resolve_http
from middleware.server import _scrape_all

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}


def _candidates(query: str | None, tmdb_id: int | None, media_type: str, tmdb_key: str | None) -> tuple[str, list[str]]:
    if tmdb_id is not None:
        from scraper.tmdb import api_key, search_query, tmdb_title

        key = api_key(tmdb_key)
        if not key:
            print("TMDB_API_KEY is not set: use --tmdb-key or set TMDB_API_KEY env.")
            sys.exit(1)
        info = tmdb_title(tmdb_id, key=key, media_type=media_type)
        primary = search_query(info)
        cands = [primary, info.get("original_title") or ""]
        return primary, [c for c in dict.fromkeys(x.strip() for x in cands if x.strip())] or [str(tmdb_id)]
    return (query or "").strip(), [(query or "").strip()]


def _probe(url: str, timeout: float = 15.0) -> int | None:
    try:
        return requests.get(url, headers=_UA, timeout=timeout).status_code
    except requests.RequestException:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Extract direct m3u8 links from akwams/egydead watch servers."
    )
    parser.add_argument("query", nargs="?", help="Search query (quote it if it has spaces)")
    parser.add_argument("--tmdb", type=int, help="TMDB id instead of a search query")
    parser.add_argument("--type", default="movie", choices=["movie", "tv"], help="Media type for TMDB lookups")
    parser.add_argument("--sites", default="akwams,egydead", help="Comma-separated sites")
    parser.add_argument("--tmdb-key", help="TMDB v3 API key (or set TMDB_API_KEY)")
    parser.add_argument("--check", action="store_true", help="Verify each direct link is alive before printing")
    parser.add_argument("--out", help="Optional output file (one URL per line)")
    parser.add_argument("--json", action="store_true", help="Also print machine-readable JSON")
    args = parser.parse_args()

    if not args.query and args.tmdb is None:
        parser.error("provide a search query or --tmdb <id>")

    sites = [s.strip() for s in args.sites.split(",") if s.strip()]
    primary, candidates = _candidates(args.query, args.tmdb, args.type, args.tmdb_key)

    items: list[dict] = []
    seen: set[tuple] = set()
    for cand in candidates:
        for item in _scrape_all(cand, sites):
            key = (item.get("source"), item.get("detail_url") or item.get("id") or item.get("title"))
            if key not in seen:
                seen.add(key)
                items.append(item)

    if not items:
        print("No items found for", primary)
        return 1

    results: list[dict] = []
    total_tried = 0
    for item in items:
        for sv in item.get("watch_servers") or []:
            embed = sv.get("url")
            if not embed:
                continue
            total_tried += 1
            got = resolve_http(embed, referer=item.get("detail_url"))
            if not got:
                continue
            status = _probe(got["url"]) if args.check else 200
            if args.check and (status is None or status >= 400):
                print(f"  [dead] {item.get('source')} {sv.get('name')}: HTTP {status}")
                continue
            results.append({
                "site": item.get("source"),
                "name": sv.get("name"),
                "kind": got["kind"],
                "url": got["url"],
            })

    print(f"\n===== DIRECT m3u8 LINKS ({len(results)} of {total_tried} servers) =====", flush=True)
    for r in results:
        print(f"  [{r['site']} | {r['name']}] {r['kind']}")
        print(f"    {r['url']}")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            for r in results:
                fh.write(f"# {r['site']} | {r['name']}\n{r['url']}\n")
        print(f"\nSaved -> {args.out}")

    if args.json:
        print("\nJSON:", json.dumps(results, ensure_ascii=False, indent=2))

    if not results:
        print("\nNo direct link worked for this title right now (hosts may be down or JS-gated).")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
