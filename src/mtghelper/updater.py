"""Bring one set's data up to date: 17Lands public data, Scryfall card text, grades.

Used by `mtghelper sync` and, automatically, by the overlay when a draft of a
set starts. Everything is cached: a set checked in the last 24 hours costs no
network requests at all, and unchanged 17Lands files are never downloaded twice.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config, grades, scryfall, seventeenlands


def update_set(
    conn: sqlite3.Connection,
    expansion: str,
    fmt: str = config.DEFAULT_FORMAT,
    download_dir: Path | str = "data/downloads",
    force: bool = False,
    keep_download: bool = False,
    card_text: bool = True,
    log=print,
) -> bool:
    """Returns True when the set has grades afterwards."""
    expansion = expansion.upper()
    try:
        published = seventeenlands.sync_ratings(
            conn, expansion, fmt, download_dir=download_dir,
            force=force, keep_download=keep_download, log=log,
        )
    except Exception as exc:
        conn.rollback()
        log(f"  17Lands public data {expansion}: FAILED ({exc})")
        return False
    if not published:
        leftovers = conn.execute(
            "SELECT COUNT(*) FROM ratings WHERE expansion = ? AND format = ?", (expansion, fmt)
        ).fetchone()[0]
        if leftovers:
            log(f"  Note: the cache still holds older {expansion} ratings that did not come from")
            log(f"    the public datasets. Remove them with: mtghelper forget {expansion}")
        return False
    try:
        seventeenlands.sync_pick_stats(conn, expansion, fmt, download_dir=download_dir, force=force, log=log)
    except Exception as exc:  # grades do not need pick statistics
        conn.rollback()
        log(f"  17Lands draft data {expansion}: FAILED ({exc}); pick statistics not updated")
    if card_text:
        try:
            scryfall.sync_cards(conn, expansion, force=force, log=log)
        except Exception as exc:  # ratings are still usable without card text
            log(f"  Scryfall {expansion}: FAILED ({exc}); card text not updated")
    counts = grades.compute_grades(conn, expansion, fmt)
    if not counts:
        log("  Grades: not enough games in the data to grade this set")
        return False
    log(f"  Grades: {counts.get('', 0)} cards graded, plus {len(counts) - 1} colour pairs")
    return True
