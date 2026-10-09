"""SQLite cache: schema and small helpers.

Tables
------
cards     one row per Arena card (keyed by arena_id), text from Scryfall
ratings   statistics computed from 17Lands public game data,
          per (expansion, format, colors, arena_id)
datasets  which public data file each set's ratings were computed from
pick_stats  ATA / ALSA per card name, from 17Lands public draft data
grades    letter grade derived from ratings, same key
sync_log  when each remote resource was last fetched
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
    arena_id         INTEGER PRIMARY KEY,
    name             TEXT NOT NULL,
    set_code         TEXT,
    collector_number TEXT,
    rarity           TEXT,
    colors           TEXT,      -- e.g. 'WU', '' for colourless
    color_identity   TEXT,
    mana_cost        TEXT,
    cmc              REAL,
    type_line        TEXT,
    oracle_text      TEXT,
    power            TEXT,
    toughness        TEXT,
    keywords         TEXT,      -- comma separated
    layout           TEXT,
    image_url        TEXT,
    scryfall_id      TEXT,
    source           TEXT NOT NULL,   -- 'scryfall' or '17lands' (name/rarity only)
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name COLLATE NOCASE);

CREATE TABLE IF NOT EXISTS ratings (
    expansion   TEXT NOT NULL,
    format      TEXT NOT NULL,
    colors      TEXT NOT NULL DEFAULT '',   -- '' = all decks, 'WU' = only WU decks
    arena_id    INTEGER NOT NULL,
    name        TEXT NOT NULL,
    seen_count  INTEGER,   -- pick statistics: unused for now (need draft data)
    alsa        REAL,
    pick_count  INTEGER,
    ata         REAL,
    game_count  INTEGER,   -- games played with the card in the deck
    play_rate   REAL,
    gp_wr       REAL,      -- win rate when in the deck
    oh_wr       REAL,      -- win rate when in the opening hand
    gd_wr       REAL,      -- win rate when drawn later
    gih_count   INTEGER,   -- games where the card was in hand at some point
    gih_wr      REAL,      -- win rate in those games: the main quality signal
    gnd_wr      REAL,      -- win rate when never drawn
    iwd         REAL,      -- improvement when drawn
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (expansion, format, colors, arena_id)
);

CREATE TABLE IF NOT EXISTS grades (
    expansion TEXT NOT NULL,
    format    TEXT NOT NULL,
    colors    TEXT NOT NULL DEFAULT '',
    arena_id  INTEGER NOT NULL,
    gih_wr    REAL NOT NULL,      -- raw win rate from 17lands
    adj_wr    REAL NOT NULL,      -- pulled toward the set average by sample size
    games     INTEGER NOT NULL,   -- sample size behind gih_wr
    z_score   REAL NOT NULL,      -- computed from adj_wr
    grade     TEXT NOT NULL,
    PRIMARY KEY (expansion, format, colors, arena_id)
);

CREATE TABLE IF NOT EXISTS datasets (
    expansion    TEXT NOT NULL,
    format       TEXT NOT NULL,
    etag         TEXT,              -- version of the downloaded file
    size         INTEGER,
    games        INTEGER,           -- number of games it contained
    processed_at TEXT NOT NULL,
    PRIMARY KEY (expansion, format)
);

CREATE TABLE IF NOT EXISTS pick_stats (
    expansion  TEXT NOT NULL,
    format     TEXT NOT NULL,
    name       TEXT NOT NULL,
    seen_count INTEGER,   -- picks at which the card was in the pack
    pick_count INTEGER,   -- times it was taken
    ata        REAL,      -- average taken at (1 = first pick)
    alsa       REAL,      -- average last seen at
    PRIMARY KEY (expansion, format, name)
);

CREATE TABLE IF NOT EXISTS pick_datasets (
    expansion    TEXT NOT NULL,
    format       TEXT NOT NULL,
    etag         TEXT,
    size         INTEGER,
    picks        INTEGER,
    processed_at TEXT NOT NULL,
    PRIMARY KEY (expansion, format)
);

CREATE TABLE IF NOT EXISTS sync_log (
    key        TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    """Open (and create if needed) the cache database."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # Grades are derived data: if the table has an older layout, rebuild it.
    # The next `sync` (or compute_grades call) fills it again.
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(grades)")}
    if columns and "adj_wr" not in columns:
        conn.execute("DROP TABLE grades")
    conn.executescript(SCHEMA)
    return conn


def is_fresh(conn: sqlite3.Connection, key: str, ttl_hours: float) -> bool:
    row = conn.execute("SELECT fetched_at FROM sync_log WHERE key = ?", (key,)).fetchone()
    if row is None:
        return False
    age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
    return age < timedelta(hours=ttl_hours)


def mark_synced(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(
        "INSERT INTO sync_log(key, fetched_at) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET fetched_at = excluded.fetched_at",
        (key, now_iso()),
    )


def upsert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    """INSERT OR REPLACE a dict into a table (keys must be column names)."""
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
