"""Command line interface: scrape a configured site and export results.

Examples:
    python cli.py --site example --query "inception"
    python cli.py --sites "egydead,akwams" --query "inception"
    python cli.py --tmdb 27205 --type movie --sites "egydead,akwams"
    python cli.py --site example --query "inception" --no-details
    python cli.py --site example --query "inception" --format csv --out results.csv
    python cli.py --list
    python cli.py --site example --query "godfather" --delay 2 --max-pages 3
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

from scraper.fetcher import FetchSettings
from scraper.sites import available_sites, build_scraper, find_config
from scraper.storage import to_csv, to_json


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scrape movie/series listings from configured sites.")
    parser.add_argument("--site", help="Site name (as configured in configs/)")
    parser.add_argument("--sites", help="Comma-separated site names to search (default: all configured)")
    parser.add_argument("--query", help="Search query; omit to scrape the home/listings page")
    parser.add_argument("--tmdb", type=int, help="Search by TMDB id (movie/tv); needs --tmdb-key or TMDB_API_KEY")
    parser.add_argument("--tmdb-key", help="TMDB API key (overrides the TMDB_API_KEY env var)")
    parser.add_argument("--type", choices=["movie", "tv"], default="movie", help="Media type when using --tmdb")
    parser.add_argument("--no-details", action="store_true", help="Skip fetching item detail pages")
    parser.add_argument("--watch-only", action="store_true", help="Keep watch servers only; drop download links")
    parser.add_argument("--no-resolve", action="store_true", help="Skip resolving watch servers to direct URLs")
    parser.add_argument("--no-verify", action="store_true", help="Skip live link checks (drops dead servers)")
    parser.add_argument("--no-label", action="store_true", help="Keep original server names instead of numbering")
    parser.add_argument("--format", choices=["json", "csv"], default="json")
    parser.add_argument("--out", help="Output file path (auto-generated if omitted)")
    parser.add_argument("--list", action="store_true", help="List configured sites and exit")
    parser.add_argument("--delay", type=float, default=1.5, help="Seconds between requests (default 1.5)")
    parser.add_argument("--jitter", type=float, default=0.5, help="Random extra delay up to this many seconds")
    parser.add_argument("--max-pages", type=int, default=-1, help="Cap on total pages fetched (-1 = unlimited)")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("cli")

    if args.list:
        sites = available_sites()
        print("\n".join(sites) if sites else "No sites configured. Add a JSON file to configs/.")
        return 0

    if args.tmdb:
        from scraper.tmdb import api_key, search_query, tmdb_title

        key = api_key(args.tmdb_key)
        if not key:
            parser.error("--tmdb requires a TMDB API key: pass --tmdb-key or set TMDB_API_KEY")
        try:
            info = tmdb_title(args.tmdb, key=key, media_type=args.type)
        except requests.exceptions.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                parser.error(
                    "TMDB rejected the API key (401). The key must be the ~32-char "
                    "v3 API key from https://www.themoviedb.org/settings/api"
                )
            parser.error(f"TMDB request failed: {exc}")
        query = search_query(info)
        log.info("TMDB %s #%s -> searching %r", info["media_type"], info["tmdb_id"], query)
    else:
        query = args.query

    names = [n.strip() for n in args.sites.split(",")] if args.sites else ([args.site] if args.site else available_sites())
    configs: list = []
    for name in names:
        config = find_config(name)
        if config is None:
            log.error("Unknown site %r. Configured sites: %s", name, ", ".join(available_sites()))
            return 1
        configs.append(config)

    settings = FetchSettings(
        delay=args.delay,
        jitter=args.jitter,
        max_pages=args.max_pages,
        timeout=args.timeout,
    )

    items: list = []
    for config in configs:
        if args.no_resolve:
            config.custom["resolve_servers"] = False
        if args.no_verify:
            config.custom["verify_servers"] = False
        if args.no_label:
            config.custom["label_servers"] = False
        scraper = build_scraper(config, settings)
        log.info("Scraping %s (%s)", config.name, config.base_url)
        try:
            items.extend(scraper.scrape(query, with_details=not args.no_details, watch_only=args.watch_only))
        except Exception as exc:
            log.error("Scrape of %s failed: %s", config.name, exc)

    log.info("Collected %d items from %s", len(items), ", ".join(c.name for c in configs))

    if not items:
        print("No items found.")
        return 0

    if args.out:
        out_path = Path(args.out)
    else:
        slug = (query or "home").replace(" ", "_").lower()[:40]
        out_path = Path("output") / f"{'-'.join(c.name for c in configs)}_{slug}.{args.format}"

    out_path.parent.mkdir(parents=True, exist_ok=True)
    (to_json if args.format == "json" else to_csv)(items, out_path)
    print(f"Saved {len(items)} items -> {out_path}")

    if args.verbose:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
