"""Card statistics from the 17Lands public datasets.

17Lands asks third-party tools not to scrape the API behind their website and
to use the public data dumps instead (https://www.17lands.com/usage_guidelines).
This module therefore only touches the files listed on
https://www.17lands.com/public_datasets (licensed CC BY 4.0):

* game_data_public.<SET>.<FORMAT>.csv.gz  one row per game, with per-card
  columns saying how many copies were in the deck, opening hand, drawn, ...
* draft_data_public.<SET>.<FORMAT>.csv.gz one row per pick, with the pack
  contents, the pool so far and the card taken
* cards.csv                               card name <-> Arena id

The game file is downloaded once per set (and again only when 17Lands updates
it), aggregated into per-card win rates, and stored in the `ratings` table.
Game data is published about three weeks after a set's release; before that
there is simply no data for the set.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import requests

from . import config, db

BASE = "https://17lands-public.s3.amazonaws.com/analysis_data"
GAME_DATA_URL = BASE + "/game_data/game_data_public.{expansion}.{fmt}.csv.gz"
DRAFT_DATA_URL = BASE + "/draft_data/draft_data_public.{expansion}.{fmt}.csv.gz"
CARDS_URL = BASE + "/cards/cards.csv"

HEADERS = {"User-Agent": config.USER_AGENT}
# How a card is counted in a game, and the column prefix it is read from.
METRICS = ("gp", "oh", "gd", "gih", "gnd")


# --- Downloading --------------------------------------------------------------

def remote_info(url: str) -> dict | None:
    """ETag and size of a public file, or None if it has not been published."""
    resp = requests.head(url, headers=HEADERS, timeout=config.REQUEST_TIMEOUT_S)
    if resp.status_code in (403, 404):  # S3 answers 403 for files that do not exist
        return None
    resp.raise_for_status()
    return {
        "etag": resp.headers.get("ETag", "").strip('"'),
        "size": int(resp.headers.get("Content-Length", 0)),
    }


def download(url: str, dest: Path, log=print) -> None:
    """Stream a file to disk, via a .part file so a broken download is never used."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    with requests.get(url, headers=HEADERS, stream=True, timeout=config.REQUEST_TIMEOUT_S) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length", 0))
        done = 0
        next_report = 0.25
        with open(part, "wb") as fh:
            for block in resp.iter_content(chunk_size=1 << 20):
                fh.write(block)
                done += len(block)
                if total and done / total >= next_report:
                    log(f"    downloaded {done / 1e6:.0f} of {total / 1e6:.0f} MB")
                    next_report += 0.25
    part.replace(dest)


def fetch_card_list() -> list[dict]:
    """17Lands' card list: one dict per Arena printing (id, expansion, name, ...)."""
    resp = requests.get(CARDS_URL, headers=HEADERS, timeout=config.REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    resp.encoding = "utf-8"
    return list(csv.DictReader(io.StringIO(resp.text)))


# --- Aggregating games --------------------------------------------------------

def _as_bool(series: pd.Series) -> np.ndarray:
    if series.dtype == bool:
        return series.to_numpy()
    return series.astype(str).str.strip().str.lower().isin(["true", "1"]).to_numpy()


def _normalise_colors(value) -> str:
    """'UW' -> 'WU'; missing -> ''."""
    if not isinstance(value, str):
        return ""
    return "".join(c for c in "WUBRG" if c in value)


def aggregate_games(path: Path | str, chunksize: int = 50_000) -> tuple[list[str], dict, int]:
    """Count games and wins per card, overall and per two-colour deck.

    Returns (card names, counts, total games). `counts[colors][metric]` is an
    array aligned with the card names, where colors is '' (all decks) or a pair
    such as 'WU', and metric is one of:

        gp   games with the card in the deck          gp_w   ... that were won
        oh   games with it in the opening hand        oh_w
        gd   games where it was drawn later           gd_w
        gih  games with it in hand at any point       gih_w
        gnd  games with it in the deck, never drawn   gnd_w

    A game counts once per card, however many copies were involved.
    """
    header = list(pd.read_csv(path, nrows=0).columns)
    names = [c[len("deck_"):] for c in header if c.startswith("deck_")]
    if not names:
        raise ValueError("No deck_<card> columns found: this is not a 17Lands game data file.")
    prefixes = {"deck": "deck_", "oh": "opening_hand_", "drawn": "drawn_"}
    columns = {key: [prefix + n for n in names] for key, prefix in prefixes.items()}
    missing = [c for cols in columns.values() for c in cols if c not in header]
    if missing or "won" not in header or "main_colors" not in header:
        raise ValueError(f"Unexpected game data layout (missing e.g. {missing[:3] or 'won/main_colors'}).")

    card_columns = [c for cols in columns.values() for c in cols]
    keys = [""] + config.COLOR_PAIRS
    counts = {
        key: {m + s: np.zeros(len(names), dtype=np.int64) for m in METRICS for s in ("", "_w")}
        for key in keys
    }
    total = 0
    reader = pd.read_csv(
        path,
        usecols=["won", "main_colors"] + card_columns,
        dtype={c: "int8" for c in card_columns},
        chunksize=chunksize,
    )
    for chunk in reader:
        won = _as_bool(chunk["won"])
        deck = chunk[columns["deck"]].to_numpy() > 0
        opening = chunk[columns["oh"]].to_numpy() > 0
        drawn = chunk[columns["drawn"]].to_numpy() > 0
        in_hand = opening | drawn
        seen = {"gp": deck, "oh": opening, "gd": drawn, "gih": in_hand, "gnd": deck & ~in_hand}
        colors = chunk["main_colors"].map(_normalise_colors).to_numpy()
        for key in keys:
            rows = slice(None) if key == "" else colors == key
            if key and not rows.any():
                continue
            won_rows = won[rows]
            for metric, matrix in seen.items():
                sub = matrix[rows]
                counts[key][metric] += sub.sum(axis=0)
                counts[key][metric + "_w"] += sub[won_rows].sum(axis=0)
        total += len(chunk)
    return names, counts, total


# --- Storing ------------------------------------------------------------------

def ids_by_name(card_list: list[dict], expansion: str) -> dict[str, list[dict]]:
    """Map card name -> Arena printings, preferring printings of this expansion.

    Cards that only exist in other expansions (bonus sheets, reprints) keep all
    their printings, so whichever one Arena shows in a pack can be looked up.
    """
    in_set: dict[str, list[dict]] = {}
    anywhere: dict[str, list[dict]] = {}
    for row in card_list:
        if not row.get("id") or not row.get("name"):
            continue
        anywhere.setdefault(row["name"], []).append(row)
        if (row.get("expansion") or "").upper() == expansion:
            in_set.setdefault(row["name"], []).append(row)
    return {name: in_set.get(name, rows) for name, rows in anywhere.items()}


def _lookup(mapping: dict[str, list[dict]], name: str) -> list[dict]:
    if name in mapping:
        return mapping[name]
    front = name.split(" // ")[0]  # double-faced cards may be listed by front face
    if front in mapping:
        return mapping[front]
    return next((rows for n, rows in mapping.items() if n.split(" // ")[0] == name), [])


def _number(text) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _rate(wins: int, games: int) -> float | None:
    return float(wins) / float(games) if games else None


def store_ratings(
    conn: sqlite3.Connection,
    expansion: str,
    fmt: str,
    names: list[str],
    counts: dict,
    mapping: dict[str, list[dict]],
) -> tuple[int, list[str]]:
    """Write aggregated counts to `ratings`. Returns (cards stored, unmatched names)."""
    fetched_at = db.now_iso()
    conn.execute("DELETE FROM ratings WHERE expansion = ? AND format = ?", (expansion, fmt))
    unmatched: list[str] = []
    stored = 0
    for i, name in enumerate(names):
        printings = _lookup(mapping, name)
        if not printings:
            unmatched.append(name)
            continue
        stored += 1
        for printing in printings:
            arena_id = int(printing["id"])
            for colors, c in counts.items():
                if colors and not c["gp"][i]:
                    continue  # never played in this colour pair: no row needed
                gih_wr = _rate(c["gih_w"][i], c["gih"][i])
                gnd_wr = _rate(c["gnd_w"][i], c["gnd"][i])
                db.upsert(conn, "ratings", {
                    "expansion": expansion, "format": fmt, "colors": colors,
                    "arena_id": arena_id, "name": name,
                    "game_count": int(c["gp"][i]),
                    "gp_wr": _rate(c["gp_w"][i], c["gp"][i]),
                    "oh_wr": _rate(c["oh_w"][i], c["oh"][i]),
                    "gd_wr": _rate(c["gd_w"][i], c["gd"][i]),
                    "gih_count": int(c["gih"][i]),
                    "gih_wr": gih_wr,
                    "gnd_wr": gnd_wr,
                    "iwd": gih_wr - gnd_wr if gih_wr is not None and gnd_wr is not None else None,
                    "fetched_at": fetched_at,
                })
            # Register the card, but never overwrite richer Scryfall data.
            if not conn.execute("SELECT 1 FROM cards WHERE arena_id = ?", (arena_id,)).fetchone():
                db.upsert(conn, "cards", {
                    "arena_id": arena_id, "name": name,
                    "set_code": (printing.get("expansion") or expansion).lower(),
                    "rarity": printing.get("rarity"),
                    "color_identity": printing.get("color_identity") or "",
                    "type_line": printing.get("types"),
                    "cmc": _number(printing.get("mana_value")),
                    "source": "17lands", "updated_at": fetched_at,
                })
    return stored, unmatched


# --- Orchestration ------------------------------------------------------------

def sync_ratings(
    conn: sqlite3.Connection,
    expansion: str,
    fmt: str = config.DEFAULT_FORMAT,
    download_dir: Path | str = "data/downloads",
    force: bool = False,
    keep_download: bool = False,
    log=print,
) -> bool:
    """Bring ratings for a set up to date. Returns False if no public data exists yet."""
    expansion = expansion.upper()
    label = f"17Lands public data {expansion} {fmt}"
    key = f"17lands-public:{expansion}:{fmt}"
    known = conn.execute(
        "SELECT etag FROM datasets WHERE expansion = ? AND format = ?", (expansion, fmt)
    ).fetchone()
    if known and not force and db.is_fresh(conn, key, config.CACHE_TTL_HOURS):
        log(f"  {label}: checked recently, skipping")
        return True

    url = GAME_DATA_URL.format(expansion=expansion, fmt=fmt)
    info = remote_info(url)
    if info is None:
        log(f"  {label}: not published (yet). 17Lands releases game data about three")
        log("    weeks after a set comes out; also check the set code and format.")
        return False
    if known and known["etag"] == info["etag"] and not force:
        db.mark_synced(conn, key)
        conn.commit()
        log(f"  {label}: unchanged since last download")
        return True

    path = Path(download_dir) / url.rsplit("/", 1)[1]
    log(f"  {label}: downloading {info['size'] / 1e6:.0f} MB")
    download(url, path, log=log)
    log("    counting games per card (this can take a minute)")
    names, counts, games = aggregate_games(path)
    mapping = ids_by_name(fetch_card_list(), expansion)
    stored, unmatched = store_ratings(conn, expansion, fmt, names, counts, mapping)
    conn.execute(
        "INSERT OR REPLACE INTO datasets(expansion, format, etag, size, games, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (expansion, fmt, info["etag"], info["size"], games, db.now_iso()),
    )
    db.mark_synced(conn, key)
    conn.commit()
    if not keep_download:
        path.unlink(missing_ok=True)
    log(f"    {games:,} games, {stored} cards")
    if unmatched:
        log(f"    no Arena id found for {len(unmatched)} cards: {', '.join(unmatched[:8])}")
    return True


# --- Pick statistics from draft data ------------------------------------------

def draft_file_path(download_dir: Path | str, expansion: str, fmt: str) -> Path:
    url = DRAFT_DATA_URL.format(expansion=expansion.upper(), fmt=fmt)
    return Path(download_dir) / url.rsplit("/", 1)[1]


def aggregate_picks(path: Path | str, chunksize: int = 50_000) -> tuple[dict[str, dict], int]:
    """Pick statistics per card name from a 17Lands draft data file.

    Returns ({name: {seen_count, pick_count, ata, alsa}}, number of picks).

        ata   average taken at: mean pick number (1 = first pick) when picked
        alsa  average last seen at: for every pack a card was opened in, the
              last pick number at which it was still in the pack, averaged
    """
    header = list(pd.read_csv(path, nrows=0).columns)
    names = [c[len("pack_card_"):] for c in header if c.startswith("pack_card_")]
    needed = ["draft_id", "pack_number", "pick_number", "pick"]
    if not names or any(c not in header for c in needed):
        raise ValueError("Unexpected layout: this is not a 17Lands draft data file.")
    pack_columns = ["pack_card_" + n for n in names]
    index = {n: i for i, n in enumerate(names)}

    seen = np.zeros(len(names), dtype=np.int64)
    picked = np.zeros(len(names), dtype=np.int64)
    pick_sum = np.zeros(len(names), dtype=np.int64)
    last_seen_sum = np.zeros(len(names), dtype=np.int64)
    last_seen_n = np.zeros(len(names), dtype=np.int64)
    total = 0

    def consume(frame: pd.DataFrame) -> None:
        nonlocal total
        if frame.empty:
            return
        number = frame["pick_number"].to_numpy().astype(np.int64) + 1  # 1-based
        in_pack = frame[pack_columns].to_numpy() > 0
        seen[:] += in_pack.sum(axis=0)
        taken = frame["pick"].map(index).to_numpy()
        valid = ~pd.isna(taken)
        np.add.at(picked, taken[valid].astype(int), 1)
        np.add.at(pick_sum, taken[valid].astype(int), number[valid])
        # Last pick number at which each card was still in each opened pack.
        at = pd.DataFrame(np.where(in_pack, number[:, None], 0).astype(np.int8))
        last = at.groupby([frame["draft_id"].to_numpy(), frame["pack_number"].to_numpy()]).max().to_numpy()
        last_seen_sum[:] += last.sum(axis=0)
        last_seen_n[:] += (last > 0).sum(axis=0)
        total += len(frame)

    reader = pd.read_csv(
        path, usecols=needed + pack_columns,
        dtype={c: "int8" for c in pack_columns}, chunksize=chunksize,
    )
    held = None  # rows of the draft cut in two by a chunk boundary
    for chunk in reader:
        if held is not None:
            chunk = pd.concat([held, chunk], ignore_index=True)
        tail = chunk["draft_id"] == chunk["draft_id"].iloc[-1]
        held = chunk[tail]
        consume(chunk[~tail])
    if held is not None:
        consume(held)

    stats = {}
    for name, i in index.items():
        stats[name] = {
            "seen_count": int(seen[i]),
            "pick_count": int(picked[i]),
            "ata": float(pick_sum[i]) / float(picked[i]) if picked[i] else None,
            "alsa": float(last_seen_sum[i]) / float(last_seen_n[i]) if last_seen_n[i] else None,
        }
    return stats, total


def sync_pick_stats(
    conn: sqlite3.Connection,
    expansion: str,
    fmt: str = config.DEFAULT_FORMAT,
    download_dir: Path | str = "data/downloads",
    force: bool = False,
    log=print,
) -> bool:
    """Bring pick statistics (ATA, ALSA) for a set up to date.

    The draft data file is kept on disk afterwards: `mtghelper evaluate` replays
    real drafts from it. Returns False if the file has not been published.
    """
    expansion = expansion.upper()
    label = f"17Lands draft data {expansion} {fmt}"
    key = f"17lands-public-draft:{expansion}:{fmt}"
    path = draft_file_path(download_dir, expansion, fmt)
    known = conn.execute(
        "SELECT etag FROM pick_datasets WHERE expansion = ? AND format = ?", (expansion, fmt)
    ).fetchone()
    if known and not force and db.is_fresh(conn, key, config.CACHE_TTL_HOURS):
        log(f"  {label}: checked recently, skipping")
        return True

    url = DRAFT_DATA_URL.format(expansion=expansion, fmt=fmt)
    info = remote_info(url)
    if info is None:
        log(f"  {label}: not published (yet)")
        return False
    if known and known["etag"] == info["etag"] and not force:
        db.mark_synced(conn, key)
        conn.commit()
        log(f"  {label}: unchanged since last download")
        return True

    log(f"  {label}: downloading {info['size'] / 1e6:.0f} MB")
    download(url, path, log=log)
    log("    counting picks per card (this can take a couple of minutes)")
    stats, picks = aggregate_picks(path)
    conn.execute("DELETE FROM pick_stats WHERE expansion = ? AND format = ?", (expansion, fmt))
    for name, row in stats.items():
        db.upsert(conn, "pick_stats", {"expansion": expansion, "format": fmt, "name": name, **row})
    conn.execute(
        "INSERT OR REPLACE INTO pick_datasets(expansion, format, etag, size, picks, processed_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (expansion, fmt, info["etag"], info["size"], picks, db.now_iso()),
    )
    db.mark_synced(conn, key)
    conn.commit()
    log(f"    {picks:,} picks, {len(stats)} cards (file kept in {path.parent} for `mtghelper evaluate`)")
    return True
