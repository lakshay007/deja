"""Turn archived visits into series and the chapters read of each."""
from __future__ import annotations

import difflib
import re
import sqlite3
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from deja.search import Hit, loose_terms


class Reading(NamedTuple):
    name: str       # series name as the page spelled it
    chapter: float
    site: str
    kind: str       # 'comic' or 'novel'


class Series(NamedTuple):
    key: str
    name: str
    kind: str
    aliases: str         # other names the same series was read under, ' / ' separated
    anilist_id: Optional[int]  # set once `deja enrich` has matched the series
    last_chapter: float
    first_read: int      # microseconds since the Unix epoch, like visits.visited_at
    last_read: int
    chapters_read: int
    sites: str           # most recently used first, ', ' separated
    continue_url: str    # the page of the highest chapter read


# "World Extinction War Chapter 16 | Vortex Scans", "The Boxer - Chapter 1 | WebtoonLib",
# "The Legend of the Northern Blade · Ch.125"
TITLE_RE = re.compile(
    r"^(.+?)[\s\-–—:|·•]*\b(?:Chapter|Chap\.?|Ch\.?)\s*(\d+(?:\.\d+)?)\b", re.I
)
# The chapter marker in an address: /the-boxer-chapter-7/, /chapter-16, /chapter/185, /ch-12
CHAPTER_IN_PATH_RE = re.compile(
    r"(?:^|[/_-])(?:chapter|chap|ch)[-_/]?(\d+(?:[.-]\d+)?)(?=[/_-]|$)", re.I
)
# Words that are never a series name on their own, in a title or in an address.
GENERIC_WORDS = {
    "read", "chapter", "chapters", "chap", "ch", "episode", "page", "home", "loading", "list",
    "manga", "manhwa", "manhua", "webtoon", "webtoons", "comic", "comics", "novel", "novels",
    "series", "title", "titles", "book", "books", "viewer", "reader", "watch", "online",
    "raw", "scan", "scans", "en",
}
ERROR_PAGE_RE = re.compile(
    r"not found|\b404\b|just a moment|access denied|attention required", re.I
)
MIRROR_PREFIX_RE = re.compile(r"^(www|w\d+|m)\.")
TRAILING_WORDS = {
    "manga", "manhwa", "manhua", "webtoon", "comic", "comics", "novel",
    "scans", "scan", "online", "raw", "english",
}
# Derived tables: dropped and rebuilt from `visits` (and `lookups`) by rebuild().
DERIVED_SCHEMA = """
DROP TABLE IF EXISTS chapters;
DROP TABLE IF EXISTS names;
DROP TABLE IF EXISTS series;
DROP TABLE IF EXISTS series_fts;
DROP TABLE IF EXISTS unsorted;
CREATE TABLE unsorted (
    visit_id   INTEGER PRIMARY KEY,  -- a chapter page whose series could not be named
    url        TEXT    NOT NULL,
    title      TEXT    NOT NULL,
    visited_at INTEGER NOT NULL
);
CREATE TABLE chapters (
    visit_id   INTEGER PRIMARY KEY,
    series_key TEXT    NOT NULL,
    chapter    REAL    NOT NULL,
    site       TEXT    NOT NULL,
    url        TEXT    NOT NULL,
    visited_at INTEGER NOT NULL
);
CREATE INDEX chapters_visited_at ON chapters (visited_at);
CREATE TABLE names (
    title_key  TEXT PRIMARY KEY,  -- a normalised name some page used
    series_key TEXT    NOT NULL,  -- the series it belongs to
    name       TEXT    NOT NULL,  -- as the pages spelled it
    position   INTEGER NOT NULL   -- 0 for the series' main name, then aliases
);
CREATE TABLE series (
    key           TEXT PRIMARY KEY,
    name          TEXT    NOT NULL,
    kind          TEXT    NOT NULL,
    aliases       TEXT    NOT NULL,
    anilist_id    INTEGER,
    last_chapter  REAL    NOT NULL,
    first_read    INTEGER NOT NULL,
    last_read     INTEGER NOT NULL,
    chapters_read INTEGER NOT NULL,
    sites         TEXT    NOT NULL,
    continue_url  TEXT    NOT NULL
);
CREATE VIRTUAL TABLE series_fts USING fts5(
    key UNINDEXED, name, aliases, genres, tags, synopsis,
    tokenize='unicode61 remove_diacritics 2'
)
"""

# A host counts as dedicated to a series when it contains the series name and
# the name is at least this long, e.g. returnofthemounthuasectmanga.org.
MIN_HOST_NAME_LENGTH = 8


def normalise(name: str) -> str:
    """The key that makes one series on two sites compare equal."""
    text = name.lower().replace("'", "").replace("’", "")
    words = re.sub(r"[\W_]+", " ", text).split()
    if len(words) > 1 and words[0] == "read":
        words = words[1:]
    while len(words) > 1 and words[-1] in TRAILING_WORDS:
        words.pop()
    return " ".join(words)


def _clean_name(name: str) -> str:
    name = name.strip(" \t-–—:|·•")
    return re.sub(r"^read\s+", "", name, flags=re.I)


def _is_id(token: str) -> bool:
    """Whether a piece of an address is an id, not a word: 1286816, 190ml, b6cb3a."""
    digits = sum(character.isdigit() for character in token)
    if token.isdigit():
        return digits >= 4
    return digits >= 2 and not re.fullmatch(r"\d+(st|nd|rd|th)", token)


def _slug_name(slug: str) -> str:
    """A name from an address segment, with ids at either end removed."""
    tokens = [token for token in re.split(r"[-_]+", slug.lower()) if token]
    while tokens and _is_id(tokens[-1]):
        tokens.pop()
    while tokens and _is_id(tokens[0]):
        tokens.pop(0)
    return " ".join(tokens).title()


def _is_name(name: str) -> bool:
    """Whether text could be a series name: it has letters and is not just a generic word."""
    words = normalise(name).split()
    return any(character.isalpha() for character in name) and not set(words) <= GENERIC_WORDS


def _agree(one: str, other: str) -> bool:
    """Whether two names are plausibly the same series."""
    a, b = normalise(one).replace(" ", ""), normalise(other).replace(" ", "")
    if a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.8


def _number_in(number: float, text: str) -> bool:
    return re.search(r"(?<!\d)0*%d(?!\d)" % int(number), text) is not None


def _from_address(path: str) -> Tuple[List[str], Optional[float]]:
    """(names the address suggests, its chapter number).

    Names come from the path before the chapter marker, nearest first:
    /title/190ml-the-legend-of-the-northern-blade/1286816-chapter-125 gives
    ["The Legend Of The Northern Blade"] and 125; the id is not a name.
    """
    marker = CHAPTER_IN_PATH_RE.search(path)
    chapter = float(marker.group(1).replace("-", ".")) if marker else None
    head = path[: marker.start()] if marker else path
    names = [_slug_name(segment) for segment in reversed(head.split("/"))]
    return [name for name in names if _is_name(name)], chapter


def examine(title: str, url: str) -> Tuple[Optional[Reading], bool]:
    """(reading, unsorted) for one visit.

    `reading` is the series and chapter the page was, when that is clear.
    `unsorted` is True for a page that is a chapter of something whose series
    cannot be named with confidence; such pages are listed, never guessed at.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if not host or ERROR_PAGE_RE.search(title):
        return None, False
    site = MIRROR_PREFIX_RE.sub("", host)
    kind = "novel" if "novel" in host or "/novel" in parts.path.lower() else "comic"

    # Webtoons calls chapters episodes: "Ep. 7 - The Strong | The Boxer".
    if site == "webtoons.com":
        episode = parse_qs(parts.query).get("episode_no", [""])[0]
        segments = parts.path.strip("/").split("/")
        if not (episode.isdigit() and len(segments) >= 4 and segments[-1] == "viewer"):
            return None, False
        name = title.rsplit(" | ", 1)[-1] if " | " in title else _slug_name(segments[-3])
        return Reading(_clean_name(name), float(episode), site, kind), False

    address_names, address_chapter = _from_address(parts.path)
    # With no title to go on, prefer the fullest name: /solo-leveling-ragnarok/slrk-chapter-69
    address_name = max(address_names, key=lambda name: len(name.split()), default=None)

    match = TITLE_RE.match(title)
    if match:
        name, chapter = _clean_name(match.group(1)), float(match.group(2))
        # "Chapter 5" also appears in video and game titles. A real chapter
        # page carries the number in its address too.
        if not _number_in(chapter, parts.path):
            return None, False
        if _is_name(name):
            # A one-word name that the address contradicts is too weak to trust.
            trusted = (
                not address_names
                or len(normalise(name).split()) >= 2
                or any(_agree(name, other) for other in address_names)
            )
            return (Reading(name, chapter, site, kind), False) if trusted else (None, True)
        if address_name:
            return Reading(address_name, chapter, site, kind), False
        return None, True

    if address_chapter is not None:
        if address_name:
            return Reading(address_name, address_chapter, site, kind), False
        return None, True
    return None, False


def parse(title: str, url: str) -> Optional[Reading]:
    """The series and chapter a visit was reading, or None if that is not clear."""
    return examine(title, url)[0]


def _group_keys(
    pairs: Iterable[Tuple[str, str]], same_series: Iterable[Iterable[str]] = ()
) -> Dict[str, str]:
    """Map every title key to the key of the series it belongs to.

    `pairs` is (title key, site) for each chapter visit. Sites dedicated to one
    series often use a different translation of its name than their own domain,
    so every name read on such a site is the series the domain is named after.
    `same_series` lists keys already known to be one series.
    """
    parent: Dict[str, str] = {}

    def find(key: str) -> str:
        while parent.get(key, key) != key:
            key = parent[key]
        return key

    def union(owner: str, key: str) -> None:
        owner, key = find(owner), find(key)
        if owner != key:
            parent[key] = owner

    keys_on_site: Dict[str, set] = defaultdict(set)
    for key, site in pairs:
        keys_on_site[site].add(key)
    all_keys = set().union(*keys_on_site.values()) if keys_on_site else set()

    for site, keys in sorted(keys_on_site.items()):
        squashed_site = re.sub(r"[\W_]+", "", site)
        owners = [
            key for key in all_keys
            if len(key.replace(" ", "")) >= MIN_HOST_NAME_LENGTH
            and key.replace(" ", "") in squashed_site
        ]
        if owners:
            owner = max(owners, key=lambda key: (len(key), key))
            for key in sorted(keys):
                union(owner, key)
    for group in same_series:
        group = sorted(key for key in group if key in all_keys)
        for key in group[1:]:
            union(group[0], key)
    return {key: find(key) for key in all_keys}


def rebuild(db: sqlite3.Connection) -> Tuple[int, int, int]:
    """Recreate the derived tables (`chapters`, `series`, `unsorted`, ...) from `visits`.

    Returns (series, chapter visits, unsorted chapter pages). The tables are
    thrown away and rebuilt each time, so a parser change takes effect on the
    next run.
    """
    readings = []
    unsorted = []
    candidates = db.execute(
        """
        SELECT id, url, title, visited_at FROM visits
        WHERE title LIKE '%chapter%' OR title LIKE '%chap%' OR title LIKE '%ch.%'
           OR url LIKE '%chapter%' OR url LIKE '%chap%' OR url LIKE '%/ch-%' OR url LIKE '%-ch-%'
           OR url LIKE '%webtoons.com/%'
        ORDER BY visited_at
        """
    )
    for visit_id, url, title, visited_at in candidates:
        reading, is_unsorted = examine(title, url)
        if reading and normalise(reading.name):
            readings.append((visit_id, url, visited_at, reading))
        elif is_unsorted:
            unsorted.append((visit_id, url, title, visited_at))

    # Novels keep their own chapter numbering, so they never merge with comics.
    def title_key(reading: Reading) -> str:
        key = normalise(reading.name)
        return key + " [novel]" if reading.kind == "novel" else key

    groups = _group_keys(
        ((title_key(reading), reading.site) for _, _, _, reading in readings),
        _known_same_series(db),
    )

    spellings: Dict[str, Counter] = defaultdict(Counter)
    for _, _, _, reading in readings:
        spellings[title_key(reading)][reading.name] += 1

    # Each series' names: its own key first, then aliases by how often they were read.
    names: Dict[str, List[str]] = defaultdict(list)
    for key, series_key in groups.items():
        names[series_key].append(key)
    for series_key, keys in names.items():
        keys.sort(key=lambda key: (key != series_key, -sum(spellings[key].values()), key))
    kinds = {groups[title_key(reading)]: reading.kind for _, _, _, reading in readings}
    matched = dict(db.execute("SELECT title_key, anilist_id FROM lookups WHERE anilist_id IS NOT NULL"))

    # One transaction, so a reader never sees the tables missing or half filled.
    db.execute("BEGIN IMMEDIATE")
    with db:
        for statement in DERIVED_SCHEMA.split(";"):
            db.execute(statement)
        db.executemany(
            "INSERT INTO chapters VALUES (?, ?, ?, ?, ?, ?)",
            [
                (visit_id, groups[title_key(reading)], reading.chapter, reading.site, url, visited_at)
                for visit_id, url, visited_at, reading in readings
            ],
        )
        db.executemany(
            "INSERT INTO names VALUES (?, ?, ?, ?)",
            [
                (key, series_key, spellings[key].most_common(1)[0][0], position)
                for series_key, keys in names.items()
                for position, key in enumerate(keys)
            ],
        )
        rows = db.execute(
            "SELECT series_key, chapter, site, url, visited_at FROM chapters ORDER BY visited_at"
        ).fetchall()
        for series_key, stats in _aggregate(rows).items():
            shown = [spellings[key].most_common(1)[0][0] for key in names[series_key]]
            anilist_id = next((matched[key] for key in names[series_key] if key in matched), None)
            db.execute(
                "INSERT INTO series VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (series_key, shown[0], kinds[series_key], " / ".join(shown[1:]), anilist_id) + stats,
            )
        db.execute(
            """
            INSERT INTO series_fts (key, name, aliases, genres, tags, synopsis)
            SELECT s.key, s.name, s.aliases,
                   coalesce(m.title || ' ' || m.genres, ''), coalesce(m.tags, ''), coalesce(m.synopsis, '')
            FROM series s LEFT JOIN media m ON m.anilist_id = s.anilist_id
            """
        )
        db.executemany("INSERT INTO unsorted VALUES (?, ?, ?, ?)", unsorted)
    return len(names), len(readings), len({url for _, url, _, _ in unsorted})


def list_unsorted(db: sqlite3.Connection) -> List[Hit]:
    """Chapter pages whose series could not be named, most recent first."""
    rows = db.execute(
        """
        SELECT url, title, max(visited_at), count(*) FROM unsorted
        GROUP BY url ORDER BY max(visited_at) DESC
        """
    )
    return [Hit(*row) for row in rows]


def list_series(
    db: sqlite3.Connection, words: str = "", period: Optional[Tuple[int, int]] = None
) -> List[Series]:
    """Series, most recently read first.

    With `period` ([start, end) in microseconds), only series read in that time,
    with chapters, dates and sites counted inside it. With `words`, only series
    whose name roughly matches, best match first, followed by series whose
    description mentions the words.
    """
    found = [Series(*row) for row in db.execute("SELECT * FROM series")]
    if period:
        rows = db.execute(
            """
            SELECT series_key, chapter, site, url, visited_at FROM chapters
            WHERE visited_at >= ? AND visited_at < ? ORDER BY visited_at
            """,
            period,
        ).fetchall()
        in_period = _aggregate(rows)
        found = [
            Series(s.key, s.name, s.kind, s.aliases, s.anilist_id, *in_period[s.key])
            for s in found if s.key in in_period
        ]
    found.sort(key=lambda s: s.last_read, reverse=True)
    if words:
        scored = [(name_match(words, s), s) for s in found]
        named = [s for score, s in sorted(scored, key=lambda pair: -pair[0]) if score >= 0.6]
        # Then series whose synopsis, genres or tags mention the words, for when
        # the name is forgotten. Empty until `deja enrich` has run.
        # The best matches are the series that mention the most of the words.
        mentions: Counter = Counter()
        for term in loose_terms(words):
            for (key,) in db.execute("SELECT key FROM series_fts WHERE series_fts MATCH ?", (term,)):
                mentions[key] += 1
        best = max(mentions.values(), default=0)
        found = named + [s for s in found if s not in named and best and mentions[s.key] == best]
    return found


def name_match(words: str, series: Series) -> float:
    """How well `words` match a series name or alias, from 0 to 1. Typos are forgiven."""
    query = normalise(words)
    names = [normalise(name) for name in [series.name] + series.aliases.split(" / ") if name]
    if not query:
        return 0.0
    if any(all(word in name for word in query.split()) for name in names):
        return 1.0
    best = max(difflib.SequenceMatcher(None, query, name).ratio() for name in names)
    tokens = sorted({token for name in names for token in name.split()})
    if all(difflib.get_close_matches(word, tokens, 1, 0.75) for word in query.split()):
        best = max(best, 0.8)
    return best


def _known_same_series(db: sqlite3.Connection) -> List[List[str]]:
    """Title keys that `deja enrich` resolved to the same AniList entry."""
    same: Dict[int, List[str]] = defaultdict(list)
    for key, anilist_id in db.execute(
        "SELECT title_key, anilist_id FROM lookups WHERE anilist_id IS NOT NULL"
    ):
        same[anilist_id].append(key)
    return [keys for keys in same.values() if len(keys) > 1]


def _aggregate(rows: Iterable[tuple]) -> Dict[str, tuple]:
    """Per series: (last_chapter, first_read, last_read, chapters_read, sites, continue_url).

    `rows` is (series_key, chapter, site, url, visited_at), oldest first.
    """
    grouped: Dict[str, list] = defaultdict(list)
    for row in rows:
        grouped[row[0]].append(row[1:])
    stats = {}
    for key, visits in grouped.items():
        last_chapter = max(chapter for chapter, _, _, _ in visits)
        sites: List[str] = []
        for _, site, _, _ in reversed(visits):
            if site not in sites:
                sites.append(site)
        continue_url = [url for chapter, _, url, _ in visits if chapter == last_chapter][-1]
        stats[key] = (
            last_chapter,
            visits[0][3],
            visits[-1][3],
            len({chapter for chapter, _, _, _ in visits}),
            ", ".join(sites),
            continue_url,
        )
    return stats
