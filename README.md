# deja

A personal search engine over your own browser history, running entirely on your Mac.

Its first job is to answer one question: *which manhwa was I reading, and where did I stop?*

Chrome deletes history after about 90 days. deja copies it into its own permanent archive, works out which series you have been reading and the last chapter you reached in each, and lets you search all of it from the terminal or a local web page.

## What it does

- **Keeps your history.** `deja sync` copies new Chrome visits into a local SQLite archive, so nothing is lost when Chrome prunes.
- **Tracks what you read.** It recognises chapter pages from their title and address, on any site, and groups them into series. The same series read on several sites, or under several translated names, becomes one entry.
- **Shows where you stopped.** Each series has the highest chapter you opened and a link back to that page.
- **Searches everything.** Full-text search across every page you have visited, not only manhwa.
- **Finds a series by its plot.** After an optional lookup on AniList, "the one with the necromancer" finds the series even when you have forgotten its name.
- **Has a web page.** A cover grid with a bookmark on each cover showing your chapter, served on your machine only.

## Requirements

- macOS with Google Chrome
- The Python 3.9 or newer that ships with macOS developer tools (`/usr/bin/python3`)

There is nothing to install: deja uses only the Python standard library.

## Getting started

```sh
git clone https://github.com/lakshay007/deja.git
cd deja
bin/deja sync
bin/deja manhwa
```

To run `deja` from any folder, link it into a directory on your `PATH`:

```sh
ln -s "$PWD/bin/deja" ~/.local/bin/deja
```

## Commands

| Command | What it does |
|---|---|
| `deja sync` | Archive new Chrome history and rebuild the series list. Add `--check` to verify that every visit Chrome holds is in the archive. |
| `deja manhwa` | List every series, most recently read first, with the last chapter, the date and a link to continue. |
| `deja manhwa <words>` | Find a series by name (typos are forgiven) or, after `deja enrich`, by plot. |
| `deja manhwa --in september` | Only what you read in a period. Accepts `2026`, `2026-09`, `2026-09-14` or a month name. |
| `deja manhwa --unsorted` | Chapter pages deja could not match to a series. |
| `deja search <words>` | Full-text search across all archived history. Takes `--in` and `-n <count>` too. |
| `deja enrich` | Look up new series on AniList for synopsis, genres, status and cover. |
| `deja web` | Serve the cover grid and search page at `http://127.0.0.1:8765`. |
| `deja autosync on` | Run `deja sync` at login and every day at noon. Also `off` and `status`. |

A typical session:

```sh
deja sync                              # bring in today's browsing
deja manhwa                            # what am I reading, and where did I stop?
deja manhwa --in october               # what was I reading in October?
deja manhwa the one with the necromancer
deja search sqlite fts5                # any page I visited, by title or address
deja web                               # the same, as a page with covers
```

## How it works

**Sync.** Chrome locks its `History` file while it runs, so deja copies the file (and its journal) to a temporary folder and reads the copy. It remembers the last visit it archived for each Chrome profile and reads only newer ones. Visits synced from another device can arrive late with old timestamps, so the cursor is Chrome's own visit id, not the time of the visit.

**Series.** A page counts as a chapter when its title says "Chapter 12" (or "Ch. 12") and the number also appears in its address. That second check keeps out videos, games and searches that merely mention a chapter. When the title is no help, deja reads the series name from the address, ignoring ids and generic words. A page that is clearly a chapter but cannot be named with confidence is listed under `--unsorted`, never guessed at.

**Merging names.** Sites dedicated to one series often use a different translation of its name than their own domain does. Any name read on such a site is treated as the series the domain is named after. After `deja enrich`, names that resolve to the same AniList entry are merged as well.

**Where you stopped.** This is the highest chapter you opened, not the most recent page, so rereading an early chapter does not move your place. The link opens that chapter, not the next one.

**Plot search.** `deja enrich` stores each series' synopsis, genres and tags. Searching by plot drops filler words, reduces the rest to rough word roots, and returns the series that mention the most of them. It matches words, not meaning: "necromancer" finds a series tagged "Necromancy", but "raises the dead" does not.

Everything derived (series, chapters, the unsorted list) is rebuilt from the raw visits each time, so improving the rules never needs a migration.

## Privacy

Your history stays on your Mac. The archive lives in `data/`, which is git-ignored and readable only by you.

One command talks to the network, and only when you run it: `deja enrich` sends series titles to [AniList](https://anilist.co) and downloads the cover images it gets back. No addresses, dates, chapters or reading sites are sent. Each title is looked up once.

The web page is served on `127.0.0.1` only and loads nothing from the network; covers come from `data/covers/`.

Two things worth knowing:

- Deleting a page from Chrome's history does not remove it from deja's archive.
- AniList's API is free for non-commercial use and forbids bulk collection. deja waits between requests and never repeats a lookup unless you pass `--refresh`.

## Limits

- macOS and Chrome only. Other browsers are not read yet.
- Only reading done in Chrome on this Mac, or on a phone with Chrome sync on, reaches the archive.
- Novels are listed alongside comics with a "novel" tag and are never merged with the comic of the same name, because their chapter numbers differ.
- Anime episodes are ignored.
- `deja autosync` cannot work while the project sits in a folder macOS protects from background jobs, such as Desktop. Keep the clone somewhere like `~/deja` if you want daily sync. Syncing by hand every few weeks loses nothing either.

## Layout

```
bin/deja         the command
deja/ingest.py   copy Chrome history into the archive
deja/series.py   find series and chapters in the archive
deja/search.py   full-text search and time periods
deja/enrich.py   AniList lookup
deja/web.py      the local web page (web.css is its stylesheet)
deja/autosync.py the daily launchd agent
deja/cli.py      the commands
data/            the archive, covers and logs (never committed)
```
