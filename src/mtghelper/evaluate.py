"""Replay real drafts from 17Lands draft data and compare picks with the recommender.

For every pick of every replayed draft the recommender sees exactly what the
player saw (the pack, their pool, the packs that came before) and its ranking
is compared with the card the player took.

Agreement with humans is a sanity check, not a score to maximise: players make
mistakes, and the recommender is supposed to disagree with some of their picks.
The useful comparisons are against the "grades only" baseline (always take the
highest overall grade) and between settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from . import recommender


@dataclass
class Tally:
    picks: int = 0
    top1: int = 0
    top3: int = 0
    base_top1: int = 0
    base_top3: int = 0

    def add(self, rank: int, base_rank: int) -> None:
        self.picks += 1
        self.top1 += rank == 0
        self.top3 += rank < 3
        self.base_top1 += base_rank == 0
        self.base_top3 += base_rank < 3


@dataclass
class Report:
    drafts: int = 0
    skipped: int = 0   # picks whose card could not be found in the pack
    overall: Tally = field(default_factory=Tally)
    by_pack: dict[int, Tally] = field(default_factory=dict)      # 1, 2, 3
    by_stage: dict[str, Tally] = field(default_factory=dict)     # picks 1-3, 4-8, 9+


def _stage(pick_number: int) -> str:
    return "picks 1-3" if pick_number <= 3 else "picks 4-8" if pick_number <= 8 else "picks 9+"


def _expand(row, columns: list[tuple[str, str]]) -> list[str]:
    """Column counts -> list of card names (a name repeated once per copy)."""
    names: list[str] = []
    for column, name in columns:
        count = row[column]
        if count:
            names.extend([name] * int(count))
    return names


@dataclass
class Pick:
    pack_number: int                      # 1, 2, 3
    pick_number: int                      # 1 = first pick of the pack
    pack: list[str]
    pool: list[str]
    seen: list[tuple[int, list[str]]]     # earlier packs of this round
    picked: str


def load_picks(
    path: Path | str,
    max_drafts: int = 2000,
    min_win_rate: float | None = None,
    chunksize: int = 20_000,
) -> tuple[list[Pick], int, int]:
    """Read up to `max_drafts` drafts. Returns (picks, drafts read, picks skipped)."""
    header = list(pd.read_csv(path, nrows=0).columns)
    pack_cols = [(c, c[len("pack_card_"):]) for c in header if c.startswith("pack_card_")]
    pool_cols = [(c, c[len("pool_"):]) for c in header if c.startswith("pool_")]
    if not pack_cols or not pool_cols:
        raise ValueError("Unexpected layout: this is not a 17Lands draft data file.")
    meta = ["draft_id", "pack_number", "pick_number", "pick"]
    if "user_game_win_rate_bucket" in header:
        meta.append("user_game_win_rate_bucket")
    card_cols = [c for c, _ in pack_cols + pool_cols]

    picks: list[Pick] = []
    drafts = skipped = 0
    current = None
    use_draft = True
    seen: list[tuple[int, list[str]]] = []
    current_pack = None
    reader = pd.read_csv(
        path, usecols=meta + card_cols, dtype={c: "int8" for c in card_cols}, chunksize=chunksize
    )
    for chunk in reader:
        for row in chunk.to_dict("records"):
            if row["draft_id"] != current:
                if drafts >= max_drafts:
                    return picks, drafts, skipped
                current, seen, current_pack = row["draft_id"], [], None
                win_rate = row.get("user_game_win_rate_bucket")
                use_draft = not (
                    min_win_rate is not None and (pd.isna(win_rate) or win_rate < min_win_rate)
                )
                drafts += use_draft
            if not use_draft:
                continue
            if row["pack_number"] != current_pack:  # signals are read within one pack round
                current_pack, seen = row["pack_number"], []
            pick_number = int(row["pick_number"]) + 1
            pack = _expand(row, pack_cols)
            if row["pick"] not in pack:
                skipped += 1
                continue
            picks.append(Pick(int(row["pack_number"]) + 1, pick_number, pack,
                              _expand(row, pool_cols), list(seen), row["pick"]))
            seen.append((pick_number, pack))
    return picks, drafts, skipped


def score(picks: list[Pick], data: recommender.SetData, params: dict | None = None) -> Report:
    """Compare the recommender's ranking with the card taken, for every pick."""
    report = Report()
    for p in picks:
        ranking = [s.name for s in recommender.recommend(data, p.pack, p.pool, p.seen, params).suggestions]
        baseline = sorted(p.pack, key=lambda n: -(data.card(n).z if data.card(n).z is not None else -9))
        rank, base_rank = ranking.index(p.picked), baseline.index(p.picked)
        for tally in (
            report.overall,
            report.by_pack.setdefault(p.pack_number, Tally()),
            report.by_stage.setdefault(_stage(p.pick_number), Tally()),
        ):
            tally.add(rank, base_rank)
    return report


def replay(
    path: Path | str,
    data: recommender.SetData,
    max_drafts: int = 2000,
    min_win_rate: float | None = None,
    params: dict | None = None,
) -> Report:
    """Replay up to `max_drafts` drafts from a draft data file."""
    picks, drafts, skipped = load_picks(path, max_drafts, min_win_rate)
    report = score(picks, data, params)
    report.drafts, report.skipped = drafts, skipped
    return report


# Values tried by `tune`, one setting at a time, the others left as configured.
TUNE_GRID = {
    "commit_picks": [8, 12, 18, 26, 40],
    "temperature": [0.2, 0.35, 0.5, 0.75, 1.25, 2.0],
    "off_colour_penalty": [1.0, 2.0, 3.0, 4.5],
    "splash_penalty": [0.0, 0.3, 0.6, 1.2, 2.0],
    "open_weight": [0.0, 0.25, 0.5, 1.0, 2.0],
}


def tune(picks: list[Pick], data: recommender.SetData, base: dict, log=print) -> None:
    """Show how agreement with players changes when one setting is varied."""
    pct = lambda n, d: f"{100 * n / d:5.1f}%" if d else "    -"
    for name, values in TUNE_GRID.items():
        log(f"\n{name}  (currently {base[name]})")
        log(f"  {'value':>7}   {'top 1':>6}  {'top 3':>6}   {'pack 1':>6}  {'pack 2':>6}  {'pack 3':>6}   (pack columns: top 1)")
        for value in sorted(set(values) | {base[name]}):
            r = score(picks, data, {**base, name: value})
            packs = "  ".join(pct(r.by_pack[k].top1, r.by_pack[k].picks) if k in r.by_pack else "    -"
                              for k in (1, 2, 3))
            mark = "  <- current" if value == base[name] else ""
            log(f"  {value:>7}   {pct(r.overall.top1, r.overall.picks)}  "
                f"{pct(r.overall.top3, r.overall.picks)}   {packs}{mark}")


def format_report(report: Report) -> str:
    def line(label: str, t: Tally) -> str:
        if not t.picks:
            return f"{label:<12}{0:>8}"
        pct = lambda n: f"{100 * n / t.picks:5.1f}%"
        return (f"{label:<12}{t.picks:>8}   {pct(t.top1)}  {pct(t.top3)}   "
                f"{pct(t.base_top1)}  {pct(t.base_top3)}")

    lines = [
        f"Replayed {report.drafts:,} drafts, {report.overall.picks:,} picks"
        + (f" ({report.skipped} picks skipped)" if report.skipped else ""),
        "",
        "Share of picks where the player's card was the recommender's first choice (top 1)",
        "or among its first three (top 3), next to the same for 'highest grade in the pack'.",
        "",
        f"{'':<12}{'picks':>8}   {'recommender':^14}   {'grades only':^14}",
        f"{'':<12}{'':>8}   {'top 1':>6}  {'top 3':>6}   {'top 1':>6}  {'top 3':>6}",
        line("all picks", report.overall),
    ]
    lines += [line(f"pack {k}", report.by_pack[k]) for k in sorted(report.by_pack)]
    lines += [line(k, report.by_stage[k]) for k in ("picks 1-3", "picks 4-8", "picks 9+") if k in report.by_stage]
    return "\n".join(lines)
