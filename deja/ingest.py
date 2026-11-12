"""Copy Chrome history into deja's permanent archive (data/deja.db)."""
from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

CHROME_DIR = Path.home() / "Library/Application Support/Google/Chrome"
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "deja.db"

# Chrome stores microseconds since 1601-01-01. The archive stores microseconds
# since the Unix epoch, so timestamps stay exact and the unique key stays safe.
CHROME_EPOCH_OFFSET_US = 11_644_473_600 * 1_000_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    id         INTEGER PRIMARY KEY,
    url        TEXT    NOT NULL,
    title      TEXT    NOT NULL DEFAULT '',
    visited_at INTEGER NOT NULL,  -- microseconds since the Unix epoch, UTC
    source     TEXT    NOT NULL,  -- e.g. 'chrome:Default'
    UNIQUE (source, url, visited_at)
);
CREATE INDEX IF NOT EXISTS visits_visited_at ON visits (visited_at);

-- Where each source's last sync stopped. The cursor is Chrome's own visit id,
-- not visited_at: visits synced from another device arrive late with old
-- timestamps, and a time watermark would skip them.
CREATE TABLE IF NOT EXISTS sync_state (
    source        TEXT PRIMARY KEY,
    last_visit_id INTEGER NOT NULL
);

-- Written only by the explicit `deja enrich` command.
CREATE TABLE IF NOT EXISTS lookups (
    title_key    TEXT PRIMARY KEY,  -- the normalised series name that was sent
    looked_up_at INTEGER NOT NULL,
    anilist_id   INTEGER            -- NULL when nothing matched
);
CREATE TABLE IF NOT EXISTS media (
    anilist_id INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    synopsis   TEXT NOT NULL,
    genres     TEXT NOT NULL,  -- ', ' separated
    tags       TEXT NOT NULL,  -- ', ' separated
    status     TEXT NOT NULL,  -- e.g. 'releasing', 'finished'
    chapters   INTEGER,        -- total chapters, when the series is complete
    cover_file TEXT,           -- file name inside data/covers/
    site_url   TEXT NOT NULL
);
"""

# Full-text index over every visit, kept current by triggers.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE visits_fts USING fts5(
    title, url, content='visits', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER visits_fts_insert AFTER INSERT ON visits BEGIN
    INSERT INTO visits_fts (rowid, title, url) VALUES (new.id, new.title, new.url);
END;
CREATE TRIGGER visits_fts_delete AFTER DELETE ON visits BEGIN
    INSERT INTO visits_fts (visits_fts, rowid, title, url)
    VALUES ('delete', old.id, old.title, old.url);
END;
CREATE TRIGGER visits_fts_update AFTER UPDATE ON visits BEGIN
    INSERT INTO visits_fts (visits_fts, rowid, title, url)
    VALUES ('delete', old.id, old.title, old.url);
    INSERT INTO visits_fts (rowid, title, url) VALUES (new.id, new.title, new.url);
END;
INSERT INTO visits_fts (visits_fts) VALUES ('rebuild');
"""


@dataclass
class Check:
    chrome_rows: int      # rows in Chrome's visits table
    chrome_distinct: int  # distinct (url, visit_time) pairs among them
    missing: int          # distinct pairs that are not in the archive


@dataclass
class SyncResult:
    source: str
    added: int
    titles_filled: int
    archived: int
    check: Optional[Check] = None


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(str(path))
    os.chmod(path, 0o600)  # browsing data is private
    db.executescript(SCHEMA)
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'visits_fts'").fetchall():
        db.executescript(FTS_SCHEMA)
    return db


def find_profiles(chrome_dir: Path = CHROME_DIR) -> List[Tuple[str, Path]]:
    """Every Chrome profile that has a History file, as (source, path)."""
    return [
        ("chrome:" + history.parent.name, history)
        for history in sorted(chrome_dir.glob("*/History"))
        if history.is_file()
    ]


def _snapshot(history: Path, dest_dir: Path) -> Path:
    """Copy History and its journal, so the live file is never opened as a database.

    Chrome keeps a transaction open while it runs, so the journal is needed to
    roll the copy back to the last committed state. If Chrome writes during the
    copy, the two files may not match, so copy again.
    """
    names = [history.name + suffix for suffix in ("", "-journal", "-wal")]

    def stamp():
        stamps = []
        for name in names:
            try:
                st = (history.parent / name).stat()
                stamps.append((st.st_size, st.st_mtime_ns))
            except FileNotFoundError:
                stamps.append(None)
        return stamps

    for _ in range(5):
        before = stamp()
        for name in names:
            dest = dest_dir / name
            try:
                shutil.copyfile(history.parent / name, dest)
            except FileNotFoundError:
                if dest.exists():
                    dest.unlink()
        if stamp() == before:
            break
        time.sleep(0.2)
    return dest_dir / history.name


def _scalar(db: sqlite3.Connection, sql: str, params: tuple = ()):
    return db.execute(sql, params).fetchall()[0][0]


def sync_source(
    db: sqlite3.Connection, source: str, history: Path, check: bool = False
) -> SyncResult:
    """Archive every visit in one Chrome History file that is not archived yet."""
    with tempfile.TemporaryDirectory(prefix="deja-") as tmp:
        copy = _snapshot(history, Path(tmp))
        db.execute("ATTACH DATABASE ? AS chrome", (str(copy),))
        try:
            if _scalar(db, "PRAGMA chrome.quick_check") != "ok":
                raise RuntimeError("the copy of %s is inconsistent; run sync again" % history)

            cursor = _scalar(
                db,
                "SELECT coalesce(max(last_visit_id), 0) FROM sync_state WHERE source = ?",
                (source,),
            )
            newest = _scalar(db, "SELECT coalesce(max(id), 0) FROM chrome.visits")
            if newest < cursor:
                cursor = 0  # the History file was recreated and its ids restarted

            with db:
                added = db.execute(
                    """
                    INSERT OR IGNORE INTO visits (url, title, visited_at, source)
                    SELECT u.url, coalesce(u.title, ''), v.visit_time - ?, ?
                    FROM chrome.visits v JOIN chrome.urls u ON u.id = v.url
                    WHERE v.id > ?
                    ORDER BY v.id
                    """,
                    (CHROME_EPOCH_OFFSET_US, source, cursor),
                ).rowcount
                # A visit archived before its page finished loading has no title
                # yet. Chrome fills it in later, so pick it up on a later sync.
                titles_filled = db.execute(
                    """
                    UPDATE visits
                    SET title = (SELECT u.title FROM chrome.urls u
                                 WHERE u.url = visits.url AND u.title <> '')
                    WHERE source = ? AND title = ''
                      AND EXISTS (SELECT 1 FROM chrome.urls u
                                  WHERE u.url = visits.url AND u.title <> '')
                    """,
                    (source,),
                ).rowcount
                db.execute(
                    "INSERT OR REPLACE INTO sync_state (source, last_visit_id) VALUES (?, ?)",
                    (source, newest),
                )

            result = SyncResult(
                source=source,
                added=added,
                titles_filled=titles_filled,
                archived=_scalar(db, "SELECT count(*) FROM visits WHERE source = ?", (source,)),
            )
            if check:
                result.check = _check(db, source)
            return result
        finally:
            db.execute("DETACH DATABASE chrome")


def _check(db: sqlite3.Connection, source: str) -> Check:
    """Compare the whole Chrome copy with the archive, ignoring the cursor."""
    pairs = """
        SELECT DISTINCT u.url AS url, v.visit_time - ? AS visited_at
        FROM chrome.visits v JOIN chrome.urls u ON u.id = v.url
    """
    return Check(
        chrome_rows=_scalar(db, "SELECT count(*) FROM chrome.visits"),
        chrome_distinct=_scalar(
            db, "SELECT count(*) FROM (%s)" % pairs, (CHROME_EPOCH_OFFSET_US,)
        ),
        missing=_scalar(
            db,
            """
            SELECT count(*) FROM (%s) c
            WHERE NOT EXISTS (SELECT 1 FROM visits a
                              WHERE a.source = ? AND a.url = c.url
                                AND a.visited_at = c.visited_at)
            """ % pairs,
            (CHROME_EPOCH_OFFSET_US, source),
        ),
    )
