"""Resolve a TMDB id (movie or TV show) to a searchable title.

The user's app already knows the TMDB id and the title; this module is a
convenience that lets the CLI accept just ``--tmdb <id>`` and look the title
up itself. It prefers the Arabic title (TMDB ``ar-SA``), falling back to the
original title, so the site search returns the best matches.
"""

from __future__ import annotations

import os

import requests

_API = "https://api.themoviedb.org/3/{kind}/{id}"
_KINDS = {"movie": "movie", "tv": "tv", "series": "tv", "show": "tv"}


def api_key(provided: str | None = None) -> str | None:
    return provided or os.environ.get("TMDB_API_KEY") or os.environ.get("TMDB_API")


def tmdb_title(
    tmdb_id: int | str,
    key: str | None = None,
    media_type: str = "movie",
    language: str = "ar-SA",
    timeout: float = 20.0,
) -> dict:
    """Return {"title": str, "original_title": str, "overview": str} for a TMDB id.

    Raises requests.HTTPError on failure (404 = unknown id).
    """
    kind = _KINDS.get((media_type or "movie").lower(), "movie")
    resp = requests.get(
        _API.format(kind=kind, id=tmdb_id),
        params={"api_key": key, "language": language},
        timeout=timeout,
    )
    resp.raise_for_status()
    # TMDB always sends UTF-8; force it so short Arabic titles don't get
    # misdetected (chardet guesses "ascii"/"cp1252" and mangles them with '?').
    resp.encoding = "utf-8"
    data = resp.json()
    title = data.get("title") or data.get("name") or data.get("original_title") or ""
    original = data.get("original_title") or data.get("original_name") or title
    return {
        "tmdb_id": str(tmdb_id),
        "media_type": kind,
        "title": title,
        "original_title": original,
        "overview": data.get("overview") or "",
    }


def search_query(info: dict, prefer_arabic: bool = True) -> str:
    """Pick the best search string from a resolved TMDB record."""
    title = (info.get("title") or "").strip()
    original = (info.get("original_title") or "").strip()
    if not title:
        return original
    if prefer_arabic and title != original:
        return title
    return original or title
