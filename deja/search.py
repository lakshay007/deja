"""Full-text search over the archive, and the time periods the commands accept."""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime, timedelta
from typing import List, NamedTuple, Optional, Tuple

MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

Period = Tuple[int, int]  # [start, end) in microseconds since the Unix epoch


class Hit(NamedTuple):
    url: str
    title: str
    last_visit: int
    visits: int


def _microseconds(day: date) -> int:
    return int(datetime(day.year, day.month, day.day).timestamp() * 1_000_000)


def _next_month(day: date) -> date:
    return date(day.year + day.month // 12, day.month % 12 + 1, 1)


def parse_period(text: str, today: Optional[date] = None) -> Period:
    """A period in local time: 2026, 2026-09, 2026-09-14, or a month name like 'sep'.

    A month name means the most recent such month that has already started.
    """
    today = today or date.today()
    text = text.strip().lower()
    if re.fullmatch(r"\d{4}", text):
        start, end = date(int(text), 1, 1), date(int(text) + 1, 1, 1)
    elif re.fullmatch(r"\d{4}-\d{1,2}", text):
        year, month = map(int, text.split("-"))
        start = date(year, month, 1)
        end = _next_month(start)
    elif re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
        start = date(*map(int, text.split("-")))
        end = start + timedelta(days=1)
    else:
        named = [m for m in MONTHS if len(text) >= 3 and m.startswith(text)]
        if len(named) != 1:
            raise ValueError(
                "cannot read %r as a period; use 2026, 2026-09, 2026-09-14 or a month name" % text
            )
        month = MONTHS.index(named[0]) + 1
        start = date(today.year if month <= today.month else today.year - 1, month, 1)
        end = _next_month(start)
    return _microseconds(start), _microseconds(end)


# Words that say nothing about a plot: "the one with the necromancer".
STOPWORDS = frozenset(
    "a about an and are as at be by for from had has he her his i in is it its of on one or "
    "she that the their they this to was were where which who with".split()
)
SUFFIXES = (
    "ations", "ation", "ings", "ing", "iest", "ies", "ied", "ers", "er", "ed", "es", "s", "y", "e",
)


def root(word: str) -> str:
    """A crude stem, so 'necromancer' finds 'Necromancy' and 'bullying' finds 'bullies'."""
    word = word.lower()
    for _ in range(2):
        for suffix in SUFFIXES:
            if word.endswith(suffix) and len(word) - len(suffix) >= 3:
                word = word[: -len(suffix)]
                break
    return word


def fts_query(words: str) -> str:
    """An FTS5 query matching rows that contain every word, by prefix."""
    return " ".join('"%s"*' % term for term in re.findall(r"\w+", words))


def loose_terms(words: str) -> List[str]:
    """One FTS5 query per meaningful word of a half-remembered description."""
    terms = [root(term) for term in re.findall(r"\w+", words) if term.lower() not in STOPWORDS]
    return ['"%s"*' % term for term in dict.fromkeys(terms)]


def search(
    db: sqlite3.Connection, words: str, period: Optional[Period] = None, limit: int = 20
) -> List[Hit]:
    """Pages whose title or address contains every word, best match first."""
    query = fts_query(words)
    if not query:
        return []
    start, end = period or (0, 2**62)
    rows = db.execute(
        """
        SELECT v.url, v.title, max(v.visited_at) AS last_visit, count(*) AS visits
        FROM visits_fts JOIN visits v ON v.id = visits_fts.rowid
        WHERE visits_fts MATCH ? AND rank MATCH 'bm25(10.0, 1.0)'  -- title counts 10x
          AND v.visited_at >= ? AND v.visited_at < ?
        GROUP BY v.url
        ORDER BY min(rank), last_visit DESC
        LIMIT ?
        """,
        (query, start, end, limit),
    )
    return [Hit(*row) for row in rows]
