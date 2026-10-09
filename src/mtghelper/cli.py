"""Command line entry point.

    mtghelper sync HOB                 download public data + card text, compute grades
    mtghelper grades HOB --top 20      list the best cards
    mtghelper grades HOB --colors WU   best cards in white-blue decks
    mtghelper card "Lightning Bolt"    show everything known about a card
    mtghelper recommend HOB --pack "Card A; Card B" --pool "Card C; Card D"
                                       rank a pack given the cards already picked
    mtghelper evaluate HOB             replay real drafts and compare with the recommender
    mtghelper draft                    follow Arena's log and rank every pack you are shown
    mtghelper draft --replay           the same for drafts already in the log
    mtghelper overlay                  the same in a window that floats over Arena
    mtghelper overlay --demo           preview the overlay on drafts already in the log
    mtghelper shortcut                 desktop shortcut that opens the overlay
    mtghelper deck                     suggest a deck from the last draft in Arena's log
    mtghelper forget FRA               delete a set's ratings and grades from the cache
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import (config, db, deckbuilder, draftview, evaluate, grades, logreader, recommender,
               seventeenlands, updater)

CREDIT = "Win rates computed from 17Lands public datasets (CC BY 4.0): https://www.17lands.com/public_datasets"


def cmd_sync(args) -> int:
    conn = db.connect(args.db)
    download_dir = Path(args.db).parent / "downloads"
    for expansion in args.expansion:
        print(f"Syncing {expansion.upper()}")
        updater.update_set(conn, expansion, args.format, download_dir=download_dir, force=args.force,
                           keep_download=args.keep_download, card_text=not args.no_scryfall)
    return 0


def cmd_grades(args) -> int:
    conn = db.connect(args.db)
    rows = conn.execute(
        "SELECT r.name, MAX(g.grade) AS grade, MAX(g.gih_wr) AS gih_wr, MAX(g.adj_wr) AS adj_wr, "
        "       MAX(g.games) AS games, MAX(c.rarity) AS rarity, MAX(c.colors) AS colors "
        "FROM grades g "
        "JOIN ratings r USING (expansion, format, colors, arena_id) "
        "LEFT JOIN cards c USING (arena_id) "
        "WHERE g.expansion = ? AND g.format = ? AND g.colors = ? "
        "GROUP BY r.name ORDER BY adj_wr DESC LIMIT ?",
        (args.expansion.upper(), args.format, args.colors.upper(), args.top),
    ).fetchall()
    if not rows:
        print("No grades found. Run `mtghelper sync` for this set first.")
        return 1
    shrunk = config.SHRINK_SMALL_SAMPLES
    adj_head = f"{'Adj WR':>7} " if shrunk else ""
    print(f"{'Grade':<6}{adj_head}{'GIH WR':>7}  {'Games':>8}  {'Rarity':<9}{'Col':<5}Name")
    for r in rows:
        grade = r["grade"] + ("?" if r["games"] < config.LOW_CONFIDENCE_GAMES else "")
        adj = f"{r['adj_wr'] * 100:>6.1f}% " if shrunk else ""
        print(
            f"{grade:<6}{adj}{r['gih_wr'] * 100:>6.1f}%  "
            f"{r['games']:>8}  {(r['rarity'] or ''):<9}{(r['colors'] or '-'):<5}{r['name']}"
        )
    print(f"\n? = fewer than {config.LOW_CONFIDENCE_GAMES} games; the grade itself is not adjusted.")
    print(CREDIT)
    return 0


def cmd_card(args) -> int:
    conn = db.connect(args.db)
    if args.query.isdigit():
        cards = conn.execute("SELECT * FROM cards WHERE arena_id = ?", (int(args.query),)).fetchall()
    else:
        cards = conn.execute(
            "SELECT * FROM cards WHERE name LIKE ? ORDER BY name LIMIT 5", (f"%{args.query}%",)
        ).fetchall()
    if not cards:
        print("No card found in the cache.")
        return 1
    for c in cards:
        print(f"{c['name']}  {c['mana_cost'] or ''}   [arena id {c['arena_id']}, {c['set_code']}]")
        print(f"  {c['type_line'] or ''}  ({c['rarity']})")
        if c["oracle_text"]:
            for line in c["oracle_text"].splitlines():
                print(f"  {line}")
        if c["power"]:
            print(f"  {c['power']}/{c['toughness']}")
        for g in conn.execute(
            "SELECT * FROM grades WHERE arena_id = ? ORDER BY expansion, colors", (c["arena_id"],)
        ):
            where = g["colors"] or "all decks"
            shaky = "?" if g["games"] < config.LOW_CONFIDENCE_GAMES else ""
            print(
                f"  {g['expansion']} {g['format']} {where}: {g['grade']}{shaky} "
                f"({g['gih_wr'] * 100:.1f}% GIH WR over {g['games']} games)"
            )
        print()
    return 0


def _names(data: recommender.SetData, text: str) -> list[str]:
    """Parse 'Card A; Card B' into card names, or exit with the ones not recognised."""
    names, unknown = [], []
    for part in filter(None, (p.strip() for p in (text or "").split(";"))):
        found = data.find(part)
        (names if found else unknown).append(found or part)
    if unknown:
        raise SystemExit("Not recognised (or ambiguous) in this set: " + "; ".join(unknown))
    return names


def cmd_recommend(args) -> int:
    conn = db.connect(args.db)
    try:
        data = recommender.load_set(conn, args.expansion, args.format)
    except LookupError as exc:
        print(exc)
        return 1
    pack, pool = _names(data, args.pack), _names(data, args.pool)
    result = recommender.recommend(data, pack, pool)
    if pool:
        lean = ", ".join(f"{pair} {weight:.0%}" for pair, weight in result.leaning[:3])
        print(f"Pool of {len(pool)}: leaning {lean}  (pool weighs {result.commitment:.0%} in the ranking)")
    else:
        print("Empty pool: ranking by overall strength")
    print(f"{'#':>2}  {'Score':>6}  {'Grade':<6}{'GIH WR':>7}  {'Col':<5}{'Name':<34}Why")
    for i, s in enumerate(result.suggestions, 1):
        wr = f"{s.gih_wr * 100:.1f}%" if s.gih_wr is not None else "-"
        print(f"{i:>2}  {s.score:>+6.2f}  {(s.grade or '-'):<6}{wr:>7}  {(s.colors or '-'):<5}"
              f"{s.name:<34}{'; '.join(s.reasons)}")
    print("\nScore: 0 = an average card of the set, +1 = three letter grades better.")
    print(CREDIT)
    return 0


def cmd_evaluate(args) -> int:
    conn = db.connect(args.db)
    expansion = args.expansion.upper()
    try:
        data = recommender.load_set(conn, expansion, args.format)
    except LookupError as exc:
        print(exc)
        return 1
    path = seventeenlands.draft_file_path(Path(args.db).parent / "downloads", expansion, args.format)
    if not path.exists():
        print(f"Draft data file not found ({path.name}). Run `mtghelper sync {expansion} --force` first.")
        return 1
    who = f" by players with a win rate of at least {args.min_win_rate:.0%}" if args.min_win_rate else ""
    print(f"Replaying up to {args.drafts:,} {expansion} drafts{who} (well under a minute per 2,000) ...")
    picks, drafts, skipped = evaluate.load_picks(path, args.drafts, args.min_win_rate)
    if args.tune:
        print(f"Loaded {drafts:,} drafts, {len(picks):,} picks. Trying settings one at a time;")
        print("this takes a few minutes. Figures are agreement with the players' picks.")
        evaluate.tune(picks, data, dict(config.RECOMMENDER))
        return 0
    report = evaluate.score(picks, data)
    report.drafts, report.skipped = drafts, skipped
    print(evaluate.format_report(report))
    return 0


def cmd_draft(args) -> int:
    conn = db.connect(args.db)
    log = Path(args.log) if args.log else logreader.DEFAULT_LOG
    if not log.exists():
        print(f"Log not found: {log}")
        print('Turn on "Detailed Logs (Plugin Support)" in Arena (Options > Account), restart it,')
        print("or point to a copy of the log with --log.")
        return 1
    if args.replay:
        draftview.replay(conn, log, top=args.top)
    else:
        draftview.follow(conn, log, top=args.top)
    return 0


def cmd_overlay(args) -> int:
    log = Path(args.log) if args.log else logreader.DEFAULT_LOG
    try:
        from . import overlay
    except ImportError as exc:
        print(f"The overlay needs PySide6 ({exc}). Install it with: pip install -e .")
        return 1
    db.connect(args.db).close()  # create the cache file if it does not exist yet
    return overlay.run_safely(args.db, log, demo_delay=args.demo, rows=args.rows, sync=not args.no_sync)


def cmd_shortcut(args) -> int:
    from . import shortcut
    try:
        path = shortcut.create_desktop_shortcut(name=args.name)
    except Exception as exc:
        print(f"Could not create the shortcut: {exc}")
        return 1
    print(f"Created {path}")
    print("Double-click it to open the overlay. It updates the set's 17Lands data by itself")
    print("when a draft starts. Run this command again if you move the project folder.")
    return 0


def cmd_deck(args) -> int:
    conn = db.connect(args.db)
    if args.pool:
        if not args.set:
            print("--pool needs --set, e.g. --set HOB")
            return 1
        try:
            data = recommender.load_set(conn, args.set, args.format)
        except LookupError as exc:
            print(exc)
            return 1
        suggestion = deckbuilder.suggest(data, _names(data, args.pool))
    else:
        log = Path(args.log) if args.log else logreader.DEFAULT_LOG
        if not log.exists():
            print(f"Log not found: {log}")
            return 1
        tracker = draftview.DraftTracker(conn)
        finished = None
        for event in logreader.read_events(log):
            tracker.handle(event, live=False)
            if isinstance(event, logreader.DraftCompleted):
                finished = tracker.deck_suggestion()
        if finished is None:
            print("No finished draft with graded cards found in the log.")
            if tracker.session.note:
                print(tracker.session.note)
            return 1
        suggestion = finished
    for line in draftview.deck_lines(suggestion):
        print(line)
    print(CREDIT)
    return 0


def cmd_forget(args) -> int:
    conn = db.connect(args.db)
    expansion = args.expansion.upper()
    removed = 0
    for table in ("ratings", "grades", "datasets", "pick_stats", "pick_datasets"):
        removed += conn.execute(f"DELETE FROM {table} WHERE expansion = ?", (expansion,)).rowcount
    conn.execute("DELETE FROM sync_log WHERE key LIKE ?", (f"17lands%:{expansion}:%",))
    conn.commit()
    print(f"Removed {removed} cached rows for {expansion}. Card text from Scryfall is kept.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mtghelper", description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(config.DEFAULT_DB_PATH), help="path to the SQLite cache")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("sync", help="download data for one or more sets")
    p.add_argument("expansion", nargs="+", help="set code(s), e.g. HOB")
    p.add_argument("--format", default=config.DEFAULT_FORMAT)
    p.add_argument("--no-scryfall", action="store_true", help="skip card text")
    p.add_argument("--force", action="store_true", help="download and recompute even if unchanged")
    p.add_argument("--keep-download", action="store_true", help="keep the downloaded game data file")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("grades", help="list graded cards for a set")
    p.add_argument("expansion")
    p.add_argument("--format", default=config.DEFAULT_FORMAT)
    p.add_argument("--colors", default="", help="colour pair such as WU")
    p.add_argument("--top", type=int, default=25)
    p.set_defaults(func=cmd_grades)

    p = sub.add_parser("card", help="look up a card by name or Arena id")
    p.add_argument("query")
    p.set_defaults(func=cmd_card)

    p = sub.add_parser("recommend", help="rank the cards in a pack")
    p.add_argument("expansion")
    p.add_argument("--format", default=config.DEFAULT_FORMAT)
    p.add_argument("--pack", required=True, help='cards in the pack, separated by ";"')
    p.add_argument("--pool", default="", help='cards already picked, separated by ";"')
    p.set_defaults(func=cmd_recommend)

    p = sub.add_parser("evaluate", help="replay real drafts and compare with the recommender")
    p.add_argument("expansion")
    p.add_argument("--format", default=config.DEFAULT_FORMAT)
    p.add_argument("--drafts", type=int, default=2000, help="number of drafts to replay")
    p.add_argument("--min-win-rate", type=float, default=None,
                   help="only drafts by players with at least this game win rate, e.g. 0.6")
    p.add_argument("--tune", action="store_true",
                   help="show how agreement changes when each recommender setting is varied")
    p.set_defaults(func=cmd_evaluate)

    p = sub.add_parser("draft", help="follow Arena's log and rank every pack you are shown")
    p.add_argument("--log", help="path to Player.log (default: Arena's own log)")
    p.add_argument("--replay", action="store_true", help="show drafts already in the log, then stop")
    p.add_argument("--top", type=int, default=8, help="cards shown per pack")
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("overlay", help="show the pack ranking in a window over Arena")
    p.add_argument("--log", help="path to Player.log (default: Arena's own log)")
    p.add_argument("--rows", type=int, default=None, help="cards shown per pack (default: last used, or 8)")
    p.add_argument("--demo", type=float, nargs="?", const=2.0, default=None, metavar="SECONDS",
                   help="replay the drafts already in the log, one pack every SECONDS (default 2)")
    p.add_argument("--no-sync", action="store_true",
                   help="do not update the set's 17Lands data when a draft starts")
    p.set_defaults(func=cmd_overlay)

    p = sub.add_parser("shortcut", help="put an 'MTG Draft Helper' shortcut on the desktop (Windows)")
    p.add_argument("--name", default="MTG Draft Helper", help="name of the shortcut")
    p.set_defaults(func=cmd_shortcut)

    p = sub.add_parser("deck", help="suggest a deck from your last draft (or from a list of cards)")
    p.add_argument("--log", help="path to Player.log (default: Arena's own log)")
    p.add_argument("--set", help="set code, with --pool")
    p.add_argument("--pool", help='your cards, separated by ";" (instead of reading the log)')
    p.add_argument("--format", default=config.DEFAULT_FORMAT)
    p.set_defaults(func=cmd_deck)

    p = sub.add_parser("forget", help="delete a set's ratings and grades from the cache")
    p.add_argument("expansion")
    p.set_defaults(func=cmd_forget)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
