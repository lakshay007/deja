"""Look series up on AniList. The only part of deja that talks to the network.

It runs only from the explicit `deja enrich` command and sends series titles,
nothing else: no URLs, dates, chapters or reading sites. Each title is looked
up once and the answer is kept, so repeat runs send only new series.
"""
from __future__ import annotations

import difflib
import html
import json
import re
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional
from urllib.parse import urlsplit

from deja import series
from deja.ingest import DB_PATH

API_URL = "https://graphql.anilist.co"
COVERS_DIR = DB_PATH.parent / "covers"
USER_AGENT = "series-lookup/1.0"
# AniList allows 30 requests a minute while its API is degraded (90 normally).
SECONDS_BETWEEN_REQUESTS = 2.5
MIN_SIMILARITY = 0.85
MAX_COVER_BYTES = 5_000_000

QUERY = """
query ($search: String, $formats: [MediaFormat]) {
  Page(perPage: 5) {
    media(search: $search, type: MANGA, format_in: $formats) {
      id
      title { romaji english native }
      synonyms
      description(asHtml: false)
      genres
      tags { name rank isMediaSpoiler }
      status
      chapters
      coverImage { extraLarge large }
      siteUrl
    }
  }
}
"""
FORMATS = {"comic": ["MANGA", "ONE_SHOT"], "novel": ["NOVEL"]}


class Result(NamedTuple):
    name: str             # the title that was sent
    title: Optional[str]  # what AniList calls it, or None when nothing matched


def _post(payload: dict) -> dict:
    """One AniList request. Waits and retries when told to slow down."""
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt == 2:
                raise RuntimeError("AniList answered HTTP %d" % error.code) from error
            time.sleep(min(int(error.headers.get("Retry-After") or 60), 120) + 1)
        except urllib.error.URLError as error:
            raise RuntimeError("could not reach AniList: %s" % error.reason) from error
    raise AssertionError("unreachable")


def _download(url: str) -> bytes:
    if urlsplit(url).scheme != "https":
        raise RuntimeError("refusing a cover that is not served over https")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        data = response.read(MAX_COVER_BYTES + 1)
    if len(data) > MAX_COVER_BYTES:
        raise RuntimeError("cover is larger than %d bytes" % MAX_COVER_BYTES)
    return data


def _plain(text: Optional[str]) -> str:
    """AniList descriptions carry some HTML; keep the words."""
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\s*\n\s*", "\n", re.sub(r"[ \t]+", " ", text)).strip()


def _names(media: dict) -> List[str]:
    title = media.get("title") or {}
    names = [title.get("english"), title.get("romaji"), title.get("native")]
    return [name for name in names + list(media.get("synonyms") or []) if name]


def best_match(name: str, candidates: List[dict]) -> Optional[dict]:
    """The candidate that goes by `name`, or None if none is close enough.

    AniList's search always returns its nearest guesses, so a wrong series is
    more likely than no series. Only accept one of its own names for it.
    """
    key = series.normalise(name)

    def similarity(media: dict) -> float:
        return max(
            (difflib.SequenceMatcher(None, key, series.normalise(n)).ratio() for n in _names(media)),
            default=0.0,
        )

    scored = [(similarity(media), media) for media in candidates]
    score, media = max(scored, key=lambda pair: pair[0], default=(0.0, None))
    return media if score >= MIN_SIMILARITY else None


def pending(db: sqlite3.Connection, refresh: bool = False) -> List[tuple]:
    """(title_key, name, kind, series_key) still to send, a series' main name first."""
    return db.execute(
        """
        SELECT n.title_key, n.name, s.kind, s.key
        FROM names n JOIN series s ON s.key = n.series_key
        WHERE ? OR (s.anilist_id IS NULL
                    AND n.title_key NOT IN (SELECT title_key FROM lookups))
        ORDER BY s.last_read DESC, n.position
        """,
        (refresh,),
    ).fetchall()


def _store(db: sqlite3.Connection, media: dict, covers_dir: Path, download: Callable[[str], bytes]) -> None:
    cover_file = None
    cover = media.get("coverImage") or {}
    cover_url = cover.get("extraLarge") or cover.get("large")
    if cover_url:
        suffix = Path(urlsplit(cover_url).path).suffix.lower()
        cover_file = "%d%s" % (media["id"], suffix if suffix in (".jpg", ".jpeg", ".png", ".webp") else ".jpg")
        try:
            data = download(cover_url)
            covers_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            (covers_dir / cover_file).write_bytes(data)
        except (OSError, RuntimeError):
            cover_file = None  # the text is still worth keeping
    tags = [
        tag["name"] for tag in media.get("tags") or []
        if not tag.get("isMediaSpoiler") and (tag.get("rank") or 0) >= 50
    ]
    title = media.get("title") or {}
    db.execute(
        "INSERT OR REPLACE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            media["id"],
            title.get("english") or title.get("romaji") or title.get("native") or "",
            _plain(media.get("description")),
            ", ".join(media.get("genres") or []),
            ", ".join(tags),
            (media.get("status") or "").lower().replace("_", " "),
            media.get("chapters"),
            cover_file,
            media.get("siteUrl") or "",
        ),
    )


def enrich(
    db: sqlite3.Connection,
    refresh: bool = False,
    covers_dir: Path = COVERS_DIR,
    post: Callable[[dict], dict] = _post,
    download: Callable[[str], bytes] = _download,
    pause: Callable[[float], None] = time.sleep,
    report: Callable[[Result], None] = lambda result: None,
) -> List[Result]:
    """Look up every series that has no AniList match yet. Returns what was sent."""
    series.rebuild(db)
    results: List[Result] = []
    resolved = set()  # series matched during this run; their aliases need not be sent
    for title_key, name, kind, series_key in pending(db, refresh):
        if series_key in resolved:
            continue
        if results:
            pause(SECONDS_BETWEEN_REQUESTS)
        answer = post({"query": QUERY, "variables": {"search": name, "formats": FORMATS[kind]}})
        candidates = ((answer.get("data") or {}).get("Page") or {}).get("media") or []
        media = best_match(name, candidates)
        with db:
            if media:
                _store(db, media, covers_dir, download)
                resolved.add(series_key)
            db.execute(
                "INSERT OR REPLACE INTO lookups VALUES (?, ?, ?)",
                (title_key, int(time.time() * 1_000_000), media["id"] if media else None),
            )
        results.append(Result(name, db.execute(
            "SELECT title FROM media WHERE anilist_id = ?", (media["id"],)
        ).fetchall()[0][0] if media else None))
        report(results[-1])
    series.rebuild(db)  # attach the new details and merge names AniList says are one series
    return results
