"""Command line entry point: bin/deja <command>, or python3 -m deja <command>."""
from __future__ import annotations

import argparse
import errno
import sqlite3
import sys
import textwrap
from datetime import datetime
from typing import List, Optional

from deja import autosync, enrich, ingest, search, series, web


def _day(microseconds: int) -> str:
    return datetime.fromtimestamp(microseconds / 1_000_000).strftime("%d %b %Y").lstrip("0")


def _period(args: argparse.Namespace) -> Optional[search.Period]:
    return search.parse_period(args.period) if args.period else None


def cmd_sync(args: argparse.Namespace) -> int:
    profiles = ingest.find_profiles()
    if not profiles:
        print("No Chrome history found under %s" % ingest.CHROME_DIR, file=sys.stderr)
        return 1

    db = ingest.open_db()
    failed = False
    width = max(len(source) for source, _ in profiles)
    for source, history in profiles:
        try:
            result = ingest.sync_source(db, source, history, check=args.check)
        except (OSError, sqlite3.Error, RuntimeError) as error:
            print("%s  failed: %s" % (source.ljust(width), error), file=sys.stderr)
            failed = True
            continue

        line = "%s  +%s new, %s archived" % (
            source.ljust(width),
            format(result.added, ","),
            format(result.archived, ","),
        )
        if result.titles_filled:
            line += ", %s titles filled in" % format(result.titles_filled, ",")
        print(line)

        if result.check:
            check = result.check
            print(
                "%s  check: Chrome has %s visits (%s distinct), %s missing from the archive"
                % (
                    " " * width,
                    format(check.chrome_rows, ","),
                    format(check.chrome_distinct, ","),
                    format(check.missing, ","),
                )
            )
            failed = failed or check.missing > 0

    series_count, chapter_visits, unsorted = series.rebuild(db)
    print("%s series from %s chapter visits" % (series_count, format(chapter_visits, ",")))
    if unsorted:
        print(
            "%d chapter page%s could not be matched to a series: deja manhwa --unsorted"
            % (unsorted, "" if unsorted == 1 else "s")
        )
    db.close()
    return 1 if failed else 0


def cmd_manhwa(args: argparse.Namespace) -> int:
    db = ingest.open_db()
    series.rebuild(db)
    if args.unsorted:
        pages = series.list_unsorted(db)
        db.close()
        if not pages:
            print("Every chapter page was matched to a series.")
            return 0
        print("Chapter pages whose series could not be named, so they are in no series:")
        _print_hits(pages)
        return 0
    words = " ".join(args.words)
    found = series.list_series(db, words, _period(args))
    details = {
        row[0]: row[1:]
        for row in db.execute("SELECT anilist_id, genres, status, synopsis FROM media")
    }
    db.close()
    if not found:
        what = "No series match %r" % words if words else "No series"
        print(what + (" in %s." % args.period if args.period else "."), file=sys.stderr)
        return 1

    for s in found:
        name = s.name + (" (novel)" if s.kind == "novel" else "")
        chapters = "%d chapter%s read" % (s.chapters_read, "" if s.chapters_read == 1 else "s")
        print("%s  ·  chapter %g  ·  %s" % (name, s.last_chapter, _day(s.last_read)))
        print("    %s  ·  %s" % (chapters, s.sites))
        if s.aliases:
            print("    also called %s" % s.aliases)
        if s.anilist_id in details:
            genres, status, synopsis = details[s.anilist_id]
            print("    %s" % "  ·  ".join(part for part in (genres, status) if part))
            if words and synopsis:  # when searching, show enough to recognise it
                print("    %s" % textwrap.shorten(synopsis, 240, placeholder="…"))
        print("    %s" % s.continue_url)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    db = ingest.open_db()
    words = " ".join(args.words)
    hits = search.search(db, words, _period(args), args.limit)
    db.close()
    if not hits:
        print("Nothing in the archive matches %r." % words, file=sys.stderr)
        return 1
    _print_hits(hits)
    return 0


def _print_hits(hits: List[search.Hit]) -> None:
    for hit in hits:
        visits = "" if hit.visits == 1 else "  ·  %d visits" % hit.visits
        print("%s  ·  %s%s" % (hit.title or "(no title)", _day(hit.last_visit), visits))
        print("    %s" % hit.url)


def cmd_enrich(args: argparse.Namespace) -> int:
    db = ingest.open_db()
    series.rebuild(db)
    titles = enrich.pending(db, args.refresh)
    if not titles:
        print("Every series has already been looked up. Use --refresh to ask again.")
        db.close()
        return 0
    print("Sending up to %d series titles to AniList (%s), nothing else:" % (len(titles), enrich.API_URL))

    def report(result: enrich.Result) -> None:
        print("  %s  ->  %s" % (result.name, result.title or "no match"))

    results = enrich.enrich(db, refresh=args.refresh, report=report)
    matched = sum(1 for result in results if result.title)
    print("%d of %d titles matched. Covers are saved in %s" % (matched, len(results), enrich.COVERS_DIR))
    db.close()
    return 0


def _take_over_port(port: int):
    """The port is busy: say what holds it, and offer to stop that and start here."""
    holder = web.port_holder(port)
    if holder is None:
        raise RuntimeError("port %d is in use; try another with --port" % port)
    pid, command = holder
    another = web.is_deja(command)
    what = "another deja web" if another else command
    if not sys.stdin.isatty():
        raise RuntimeError(
            "port %d is in use by %s (pid %d); stop it or pick another with --port" % (port, what, pid)
        )
    try:
        answer = input("Port %d is in use by %s (pid %d). Stop it and start here? [y/N] " % (port, what, pid))
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes"):
        if another:
            print("Left it running. deja is at http://127.0.0.1:%d" % port)
        else:
            print("Left it running. Pick another port with: deja web --port %d" % (port + 1))
        return None
    try:
        released = web.stop(pid, port)
    except OSError as error:
        raise RuntimeError("could not stop pid %d: %s" % (pid, error.strerror or error))
    if not released:
        raise RuntimeError("pid %d did not let go of port %d" % (pid, port))
    return web.make_server(port)


def cmd_web(args: argparse.Namespace) -> int:
    try:
        server = web.make_server(args.port)
    except OSError as error:
        if error.errno != errno.EADDRINUSE:
            raise RuntimeError("cannot listen on port %d: %s" % (args.port, error.strerror or error))
        server = _take_over_port(args.port)
        if server is None:
            return 0
    print("deja is at http://127.0.0.1:%d  (Ctrl-C to stop)" % server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        server.server_close()
    return 0


def cmd_autosync(args: argparse.Namespace) -> int:
    if args.action == "on":
        autosync.turn_on()
        print("Daily sync is on: at login and every day at %d:00." % autosync.HOUR)
        print("Agent: %s" % autosync.PLIST_PATH)
        print("Log:   %s" % autosync.LOG_PATH)
    elif args.action == "off":
        autosync.turn_off()
        print("Daily sync is off. The archive is untouched.")
    else:
        loaded, tail = autosync.status()
        print("Daily sync is %s." % ("on" if loaded else "off"))
        if tail:
            print("Last lines of %s:" % autosync.LOG_PATH)
            print(tail)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="deja", description="A local search engine over my own browser history."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    period_help = "only this period: 2026, 2026-09, 2026-09-14 or a month name such as september"

    sync = commands.add_parser("sync", help="copy new Chrome history into the archive")
    sync.add_argument(
        "--check",
        action="store_true",
        help="also verify that every visit Chrome holds is in the archive",
    )
    sync.set_defaults(run=cmd_sync)

    manhwa = commands.add_parser(
        "manhwa", help="list the series I have been reading and where I stopped"
    )
    manhwa.add_argument("words", nargs="*", help="only series whose name roughly matches")
    manhwa.add_argument("--in", dest="period", metavar="PERIOD", help=period_help)
    manhwa.add_argument(
        "--unsorted",
        action="store_true",
        help="list chapter pages that could not be matched to a series",
    )
    manhwa.set_defaults(run=cmd_manhwa)

    find = commands.add_parser("search", help="full-text search across all history")
    find.add_argument("words", nargs="+", help="words that the title or address must contain")
    find.add_argument("--in", dest="period", metavar="PERIOD", help=period_help)
    find.add_argument("-n", dest="limit", type=int, default=20, help="how many results (default 20)")
    find.set_defaults(run=cmd_search)

    lookup = commands.add_parser(
        "enrich",
        help="look series up on AniList for synopsis, genres and cover (sends series titles)",
    )
    lookup.add_argument(
        "--refresh", action="store_true", help="look every series up again, not only new ones"
    )
    lookup.set_defaults(run=cmd_enrich)

    site = commands.add_parser("web", help="serve the cover grid and search page on this Mac only")
    site.add_argument("--port", type=int, default=web.DEFAULT_PORT, help="default %d" % web.DEFAULT_PORT)
    site.set_defaults(run=cmd_web)

    auto = commands.add_parser("autosync", help="run sync every day in the background")
    auto.add_argument("action", choices=["on", "off", "status"])
    auto.set_defaults(run=cmd_autosync)

    args = parser.parse_args(argv)
    try:
        return args.run(args)
    except ValueError as error:
        parser.error(str(error))
    except RuntimeError as error:
        print("deja: %s" % error, file=sys.stderr)
        return 1
