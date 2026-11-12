"""`deja web`: a local page with a search box and a cover grid of what I am reading.

Served on 127.0.0.1 only. The page loads nothing from the network: covers come
from data/covers/, where `deja enrich` saved them.
"""
from __future__ import annotations

import os
import re
import signal
import sqlite3
import subprocess
import time
from datetime import date, datetime
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from deja import enrich, ingest, search, series

DEFAULT_PORT = 8765
COVER_RE = re.compile(r"\d+\.(jpg|jpeg|png|webp)")
COVER_TYPES = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
STYLE_PATH = Path(__file__).with_name("web.css")
MAX_QUERY_LENGTH = 200


def _when(microseconds: int, today: date) -> str:
    day = datetime.fromtimestamp(microseconds / 1_000_000).date()
    days = (today - day).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if 1 < days < 14:
        return "%d days ago" % days
    return day.strftime("%d %b %Y").lstrip("0")


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _link(url: str) -> str:
    """An href for a URL out of the archive. Only web addresses are linked."""
    return escape(url, quote=True) if urlsplit(url).scheme in ("http", "https") else "#"


class _Details(NamedTuple):
    """What `deja enrich` stored about a series; empty until it has run."""
    synopsis: str = ""
    genres: str = ""
    status: str = ""
    total: Optional[int] = None  # chapters in the finished series
    cover_file: Optional[str] = None


def _total(s: series.Series, details: _Details) -> Optional[int]:
    """The series' length, when it is known and comparable with the chapter read."""
    if details.total and details.total >= s.last_chapter and s.kind != "novel":
        return details.total
    return None


def _cover(s: series.Series, details: _Details, named: bool = True) -> str:
    """The cover, as a link that continues the series, with a bookmark at the chapter reached.

    A series with no cover art gets a plain one carrying its name, unless the
    name is already printed beside it (`named=False`).
    """
    if details.cover_file:
        art = '<img src="/covers/%s" alt="">' % escape(details.cover_file, quote=True)
    else:
        art = '<span class="plain">%s</span>' % (escape(s.name) if named else "")
    total = _total(s, details)
    read = ""
    if total:
        read = '<progress value="%g" max="%d" aria-label="%g of %d chapters read"></progress>' % (
            s.last_chapter, total, s.last_chapter, total
        )
    return (
        '<a class="cover" href="%s" target="_blank" rel="noopener noreferrer" '
        'aria-label="Continue %s, chapter %g">%s<span class="mark" aria-hidden="true">%g</span>%s</a>'
    ) % (_link(s.continue_url), escape(s.name, quote=True), s.last_chapter, art, s.last_chapter, read)


def _standing(s: series.Series, details: _Details) -> str:
    """Genres and whether the series is still coming out, as one line."""
    total = _total(s, details)
    if details.status == "finished" and total:
        status = "Finished at %d chapters" % total
    else:
        status = _sentence(details.status)
    return ". ".join(part for part in (details.genres, status) if part)


def _latest(s: series.Series, details: _Details, today: date) -> str:
    """The series read most recently, large, with the button to carry on."""
    where = "Read %s on %s" % (_when(s.last_read, today), escape(s.sites.split(", ")[0]))
    lines = ['<p class="when">%s%s</p>' % (where, ", as a novel" if s.kind == "novel" else "")]
    standing = _standing(s, details)
    if standing:
        lines.append('<p class="standing">%s</p>' % escape(standing))
    if details.synopsis:
        lines.append('<p class="synopsis">%s</p>' % escape(details.synopsis))
    if s.aliases:
        lines.append('<p class="aliases">Also called %s</p>' % escape(s.aliases))
    return (
        '<section class="latest">%s<div class="about"><h1>%s</h1>%s'
        '<a class="button" href="%s" target="_blank" rel="noopener noreferrer">Continue chapter %g</a>'
        "</div></section>"
    ) % (_cover(s, details, named=False), escape(s.name), "".join(lines), _link(s.continue_url), s.last_chapter)


def _book(s: series.Series, details: _Details, today: date) -> str:
    """One series on the shelf: cover, name, when it was last read."""
    when = _sentence(_when(s.last_read, today)) + (", novel" if s.kind == "novel" else "")
    lines = ['<p class="when">%s</p>' % when]
    total = _total(s, details)
    if total:
        lines.append('<p class="count">%g of %d chapters</p>' % (s.last_chapter, total))
    about = ""
    if s.aliases:
        about += "<p>Also called %s</p>" % escape(s.aliases)
    standing = _standing(s, details)
    if standing:
        about += "<p>%s</p>" % escape(standing)
    if details.synopsis:
        about += "<p>%s</p>" % escape(details.synopsis)
    if about:
        lines.append("<details><summary>About</summary>%s</details>" % about)
    return '<li class="book">%s<h3>%s</h3>%s</li>' % (_cover(s, details), escape(s.name), "".join(lines))


def _page(hit: search.Hit, today: date) -> str:
    visits = '<span class="visits">%d visits</span>' % hit.visits if hit.visits > 1 else ""
    return (
        '<li><a href="%s" target="_blank" rel="noopener noreferrer">%s</a>'
        '<span class="host">%s</span><span class="date">%s</span>%s</li>'
    ) % (
        _link(hit.url), escape(hit.title or hit.url),
        escape(urlsplit(hit.url).hostname or ""), _sentence(_when(hit.last_visit, today)), visits,
    )


def _heading(level: int, text: str, note: str) -> str:
    return '<div class="heading"><h%d>%s</h%d><p>%s</p></div>' % (level, text, level, note)


def _plural(count: int, word: str) -> str:
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def render(db: sqlite3.Connection, query: str = "", today: Optional[date] = None) -> str:
    """The whole page, as HTML."""
    today = today or date.today()
    query = query.strip()[:MAX_QUERY_LENGTH]
    found = series.list_series(db, query)
    stored: Dict[int, _Details] = {
        row[0]: _Details(*row[1:])
        for row in db.execute(
            "SELECT anilist_id, synopsis, genres, status, chapters, cover_file FROM media"
        )
    }

    def details(s: series.Series) -> _Details:
        return stored.get(s.anilist_id, _Details())

    def shelf(books: List[series.Series]) -> str:
        return '<ul class="grid">%s</ul>' % "".join(_book(s, details(s), today) for s in books)

    parts = []
    if query:
        hits = search.search(db, query, limit=30)
        level = 1
        if found:
            parts.append("<section>%s%s</section>" % (
                _heading(level, "Series", "%d found" % len(found)), shelf(found)
            ))
            level = 2
        if hits:
            parts.append('<section>%s<ol class="pages">%s</ol></section>' % (
                _heading(level, "Pages you visited", _plural(len(hits), "page")),
                "".join(_page(hit, today) for hit in hits),
            ))
        if not found and not hits:
            parts.append(
                '<section class="empty"><h1>Nothing matches “%s”</h1>'
                '<p>Try fewer words, or <a href="/">go back to your shelf</a>.</p></section>'
                % escape(query)
            )
    elif found:
        parts.append(_latest(found[0], details(found[0]), today))
        if len(found) > 1:
            parts.append("<section>%s%s</section>" % (
                _heading(2, "Your shelf", "%d more series, latest first" % (len(found) - 1)),
                shelf(found[1:]),
            ))
        unsorted = series.list_unsorted(db)
        if unsorted:
            parts.append('<section>%s<ol class="pages">%s</ol></section>' % (
                _heading(2, "Chapters without a series", "deja could not tell which series these belong to"),
                "".join(_page(page, today) for page in unsorted),
            ))
    else:
        parts.append(
            '<section class="empty"><h1>Nothing here yet</h1>'
            "<p>Run <code>deja sync</code> in a terminal to bring in your Chrome history, "
            "then reload this page.</p></section>"
        )

    visits, first = db.execute("SELECT count(*), min(visited_at) FROM visits").fetchall()[0]
    footer = ""
    if visits:
        footer = "%s visits archived since %s. Nothing on this page leaves this Mac." % (
            format(visits, ","),
            datetime.fromtimestamp(first / 1_000_000).strftime("%d %b %Y").lstrip("0"),
        )

    return PAGE.format(
        title="deja: %s" % escape(query) if query else "deja",
        query=escape(query, quote=True),
        clear='<a class="clear" href="/">Clear</a>' if query else "",
        main="".join(parts),
        footer=footer,
    )


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="stylesheet" href="/style.css">
</head>
<body>
<header class="top">
  <a class="brand" href="/">deja</a>
  <form class="search" action="/" method="get" role="search">
    <input type="search" name="q" value="{query}" maxlength="200" autocomplete="off"
           placeholder="Search a series, a plot, or any page you visited" aria-label="Search">
    <button type="submit">Search</button>
    {clear}
  </form>
</header>
<main>{main}</main>
<footer>{footer}</footer>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "deja"

    def do_GET(self) -> None:
        self._respond(send_body=True)

    def do_HEAD(self) -> None:
        self._respond(send_body=False)

    def _respond(self, send_body: bool) -> None:
        # Another site must not be able to read this page by pointing its own
        # domain at 127.0.0.1, so only answer to our own address.
        if self.headers.get("Host", "") not in self.server.allowed_hosts:
            return self._send(403, "text/plain; charset=utf-8", b"Forbidden\n", send_body)

        parts = urlsplit(self.path)
        if parts.path == "/":
            query = parse_qs(parts.query).get("q", [""])[0]
            db = ingest.open_db(self.server.db_path)
            try:
                body = render(db, query).encode()
            finally:
                db.close()
            return self._send(200, "text/html; charset=utf-8", body, send_body)
        if parts.path == "/style.css":
            return self._send(200, "text/css; charset=utf-8", STYLE_PATH.read_bytes(), send_body)
        if parts.path.startswith("/covers/"):
            name = parts.path[len("/covers/"):]
            cover = self.server.covers_dir / name
            if COVER_RE.fullmatch(name) and cover.is_file():
                kind = COVER_TYPES[name.rsplit(".", 1)[1]]
                return self._send(200, kind, cover.read_bytes(), send_body, cache="max-age=86400")
        self._send(404, "text/plain; charset=utf-8", b"Not found\n", send_body)

    def _send(self, status: int, kind: str, body: bytes, send_body: bool, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self'; style-src 'self'; form-action 'self'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # searches are private; keep them out of the terminal


def make_server(
    port: int = DEFAULT_PORT,
    db_path: Path = ingest.DB_PATH,
    covers_dir: Path = enrich.COVERS_DIR,
) -> ThreadingHTTPServer:
    db = ingest.open_db(db_path)
    series.rebuild(db)
    db.close()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    port = server.server_address[1]
    server.db_path = db_path
    server.covers_dir = covers_dir
    server.allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
    return server


def port_holder(port: int) -> Optional[Tuple[int, str]]:
    """(pid, command line) of the process listening on a local port, if it can be found."""
    try:
        pids = subprocess.run(
            ["lsof", "-nP", "-iTCP:%d" % port, "-sTCP:LISTEN", "-t"],
            capture_output=True, text=True,
        ).stdout.split()
        if not pids:
            return None
        command = subprocess.run(
            ["ps", "-p", pids[0], "-o", "command="], capture_output=True, text=True
        ).stdout.strip()
    except OSError:
        return None
    return int(pids[0]), command


def is_deja(command: str) -> bool:
    """Whether a command line is another `deja web`."""
    return re.search(r"\bdeja\b.*\sweb\b", command) is not None


def stop(pid: int, port: int, patience: float = 5.0) -> bool:
    """Ask a process to quit, then wait for it to let go of the port."""
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        if port_holder(port) is None:
            return True
        time.sleep(0.1)
    return False
