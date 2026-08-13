"""Local mock movie site for end-to-end CLI testing."""

import http.server
import json
import threading
from pathlib import Path

from bs4 import BeautifulSoup

MOVIES = [
    {"id": "inception", "title": "Inception", "year": "2010", "rating": "8.8", "genre": "Sci-Fi",
     "synopsis": "A thief who steals corporate secrets through dream-sharing technology."},
    {"id": "the-matrix", "title": "The Matrix", "year": "1999", "rating": "8.7", "genre": "Action",
     "synopsis": "A computer hacker learns the shocking truth about his reality."},
]

LISTING = """
<html><body>
{cards}
</body></html>
"""

CARD = """
<article class="movie-card">
  <h2 class="movie-title">{title}</h2>
  <span class="year">{year}</span>
  <span class="rating">{rating}</span>
  <span class="genre">{genre}</span>
  <a class="movie-link" href="/movies/{id}">Details</a>
</article>
"""


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/movies/"):
            movie_id = self.path.rsplit("/", 1)[-1]
            movie = next((m for m in MOVIES if m["id"] == movie_id), None)
            if movie:
                html = f"<html><body><div class='synopsis'>{movie['synopsis']}</div></body></html>"
                return self._send(html)
        cards = "".join(CARD.format(**m) for m in MOVIES)
        self._send(LISTING.format(cards=cards))

    def _send(self, html: str):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence
        pass


def start() -> tuple[str, threading.Thread]:
    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return f"http://127.0.0.1:{port}", thread


def write_config(base_url: str, out: Path):
    out.write_text(json.dumps({
        "name": "mock",
        "base_url": base_url + "/",
        "search_url": base_url + "/search/{query}",
        "item_selector": "article.movie-card",
        "fields": {
            "title": "h2.movie-title",
            "year": "span.year",
            "rating": "span.rating",
            "genre": "span.genre",
            "detail_url": "a.movie-link@href",
        },
        "custom": {"detail_fields": {"description": ".synopsis"}},
    }, indent=2), encoding="utf-8")
