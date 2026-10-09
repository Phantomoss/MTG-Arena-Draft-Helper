"""Turn 17lands win rates into letter grades.

A card's grade is how far its "game in hand" win rate (GIH WR) sits from the
average of its set, measured in standard deviations (z-score).

By default the raw win rate of every card with enough games is used. One
optional safeguard exists in config.py, off by default:

* SHRINK_SMALL_SAMPLES: pull each win rate toward the set average, strongly
  when the card has few games and barely at all when it has many. The strength
  of the pull is estimated from the data itself (see `prior_strength`).
"""

from __future__ import annotations

import sqlite3
import statistics

from . import config

Samples = dict[int, tuple[float, int]]  # arena_id -> (gih_wr, games)


def letter_for(z: float) -> str:
    for minimum, letter in config.GRADE_BANDS:
        if z >= minimum:
            return letter
    return config.LOWEST_GRADE


def shrink(win_rate: float, games: int, mean: float, k: float) -> float:
    """Pull a win rate toward the mean, as if the card had played k extra average games."""
    return (win_rate * games + mean * k) / (games + k)


def prior_strength(samples: Samples, mean: float) -> float:
    """How many 'average' games to mix into every card (the k in `shrink`).

    The spread of win rates we observe is the real spread between cards plus
    sampling noise. Subtracting the expected noise leaves the real spread; the
    smaller it is compared to the noise of a single game, the larger k gets.
    """
    observed_var = statistics.pvariance(wr for wr, _ in samples.values())
    game_var = mean * (1 - mean)  # variance of one win/loss outcome
    noise_var = statistics.mean(game_var / n for _, n in samples.values())
    true_var = max(observed_var - noise_var, config.MIN_TRUE_SD**2)
    return game_var / true_var


def _eligible(conn, expansion, fmt, colors, min_games) -> Samples:
    """Cards with enough games in hand, per Arena id."""
    rows = conn.execute(
        "SELECT arena_id, gih_wr, gih_count FROM ratings "
        "WHERE expansion = ? AND format = ? AND colors = ? "
        "AND gih_wr IS NOT NULL AND gih_count >= ?",
        (expansion, fmt, colors, min_games),
    ).fetchall()
    return {r["arena_id"]: (r["gih_wr"], r["gih_count"]) for r in rows}


def _baseline(conn, expansion, fmt, min_games) -> list[tuple[float, int]]:
    """One (win rate, games) per card NAME: a card with several Arena printings
    must not weigh more than once in the set average."""
    rows = conn.execute(
        "SELECT name, MAX(gih_wr) AS wr, MAX(gih_count) AS n FROM ratings "
        "WHERE expansion = ? AND format = ? AND colors = '' "
        "AND gih_wr IS NOT NULL AND gih_count >= ? GROUP BY name",
        (expansion, fmt, min_games),
    ).fetchall()
    return [(r["wr"], r["n"]) for r in rows]


def compute_grades(
    conn: sqlite3.Connection,
    expansion: str,
    fmt: str = config.DEFAULT_FORMAT,
    min_games: int | None = None,
) -> dict[str, int]:
    """Recompute grades for a set from cached ratings. Returns {colors: cards graded}.

    Every colour pair is graded against the overall set baseline, so a "B" in
    WU decks means the same strength as a "B" overall and grades stay comparable.
    """
    expansion = expansion.upper()
    if min_games is None:
        min_games = config.MIN_GIH_GAMES
    conn.execute("DELETE FROM grades WHERE expansion = ? AND format = ?", (expansion, fmt))

    overall = _eligible(conn, expansion, fmt, "", min_games)
    baseline = _baseline(conn, expansion, fmt, min_games)
    if len(baseline) < 2:
        conn.commit()
        return {}
    mean = statistics.mean(wr for wr, _ in baseline)
    # k = 0 leaves every win rate exactly as computed from the 17Lands data.
    k = prior_strength(dict(enumerate(baseline)), mean) if config.SHRINK_SMALL_SAMPLES else 0.0
    stdev = statistics.pstdev(shrink(wr, n, mean, k) for wr, n in baseline)
    if stdev == 0:
        conn.commit()
        return {}

    color_keys = [
        r["colors"]
        for r in conn.execute(
            "SELECT DISTINCT colors FROM ratings WHERE expansion = ? AND format = ?",
            (expansion, fmt),
        )
    ]
    result: dict[str, int] = {}
    for colors in color_keys:
        # Colour-pair samples are much smaller, so scale the threshold down.
        threshold = min_games if colors == "" else max(min_games // 5, 1)
        samples = overall if colors == "" else _eligible(conn, expansion, fmt, colors, threshold)
        for arena_id, (wr, games) in samples.items():
            adjusted = shrink(wr, games, mean, k)
            z = (adjusted - mean) / stdev
            conn.execute(
                "INSERT INTO grades(expansion, format, colors, arena_id, "
                "gih_wr, adj_wr, games, z_score, grade) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (expansion, fmt, colors, arena_id, wr, adjusted, games, z, letter_for(z)),
            )
        result[colors] = len(samples)
    result[""] = len(baseline)  # report distinct cards, not Arena printings
    conn.commit()
    return result
