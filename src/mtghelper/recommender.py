"""Rank the cards in a draft pack.

Everything is measured on one scale: how many standard deviations a card's win
rate sits above the set average ("z"; one letter grade step is a third of it).

For each card in the pack the score blends two views:

* its overall strength (what you would take with an empty pool), and
* its strength in the colour pairs your pool is heading towards, where a card
  you could not cast is marked down.

The blend moves from the first view to the second as the pool fills up. Which
pairs you are heading towards is read from the good cards already picked, plus
a smaller signal from good cards that came round later than they usually do
(a sign the colour is open).

Nothing here touches the network or the Arena log: give it card names, get a
ranking. All tunable numbers are in config.RECOMMENDER.
"""

from __future__ import annotations

import math
import sqlite3
import statistics
from dataclasses import dataclass, field

from . import config, grades

COLORS = "WUBRG"


@dataclass
class Card:
    name: str
    colors: str = ""                 # 'WU', '' for colourless
    z: float | None = None           # overall strength, None if too few games
    gih_wr: float | None = None
    grade: str | None = None
    pair_z: dict[str, float] = field(default_factory=dict)  # strength per colour pair
    ata: float | None = None
    alsa: float | None = None
    cmc: float | None = None         # mana value
    type_line: str = ""              # e.g. "Creature — Human Warlock"
    mana_cost: str = ""              # e.g. "{2}{U}{B}" (Scryfall only)


@dataclass
class SetData:
    expansion: str
    fmt: str
    mean: float
    stdev: float
    cards: dict[str, Card]
    name_by_id: dict[int, str]

    def card(self, name: str) -> Card:
        """Look a card up by name; unknown names become unrated colourless cards."""
        return self.cards.get(name) or Card(name=name)

    def find(self, text: str) -> str | None:
        """Resolve user input (any case, or an unambiguous fragment) to a card name."""
        wanted = text.strip().lower()
        exact = [n for n in self.cards if n.lower() == wanted]
        if exact:
            return exact[0]
        partial = [n for n in self.cards if wanted in n.lower()]
        return partial[0] if len(partial) == 1 else None


@dataclass
class Suggestion:
    name: str
    score: float
    base: float          # overall strength (z)
    grade: str | None
    gih_wr: float | None
    colors: str
    reasons: list[str]


@dataclass
class Recommendation:
    suggestions: list[Suggestion]          # best first
    leaning: list[tuple[str, float]]       # colour pairs and their weight, best first
    commitment: float                      # 0 = pool ignored, 1 = pool decides


def load_set(conn: sqlite3.Connection, expansion: str, fmt: str = config.DEFAULT_FORMAT) -> SetData:
    """Read everything the recommender needs about a set from the cache."""
    expansion = expansion.upper()
    baseline = grades._baseline(conn, expansion, fmt, config.MIN_GIH_GAMES)
    if len(baseline) < 2:
        raise LookupError(f"No ratings for {expansion} {fmt}. Run `mtghelper sync {expansion}` first.")
    mean = statistics.mean(wr for wr, _ in baseline)
    stdev = statistics.pstdev(wr for wr, _ in baseline)

    cards: dict[str, Card] = {}
    name_by_id: dict[int, str] = {}
    pair_min = max(config.MIN_GIH_GAMES // 5, 1)
    for r in conn.execute(
        "SELECT r.arena_id, r.name, r.colors AS deck, r.gih_wr, r.gih_count, "
        "       c.colors, c.color_identity, c.source, c.cmc, c.type_line, c.mana_cost "
        "FROM ratings r LEFT JOIN cards c USING (arena_id) "
        "WHERE r.expansion = ? AND r.format = ? ORDER BY c.source = 'scryfall'",
        (expansion, fmt),
    ):
        card = cards.setdefault(r["name"], Card(name=r["name"]))
        name_by_id[r["arena_id"]] = r["name"]
        # Colours: the mana cost colours from Scryfall, else 17Lands' colour identity.
        colors = r["colors"] if r["source"] == "scryfall" else (r["color_identity"] or "")
        if colors or r["source"] == "scryfall":
            card.colors = "".join(c for c in COLORS if c in (colors or ""))
        # Card details: Scryfall rows come last (ORDER BY) and so take precedence.
        if r["cmc"] is not None:
            card.cmc = r["cmc"]
        if r["type_line"]:
            card.type_line = r["type_line"]
        if r["mana_cost"]:
            card.mana_cost = r["mana_cost"]
        if r["gih_wr"] is None:
            continue
        z = (r["gih_wr"] - mean) / stdev
        if r["deck"] == "" and r["gih_count"] >= config.MIN_GIH_GAMES:
            card.z, card.gih_wr, card.grade = z, r["gih_wr"], grades.letter_for(z)
        elif r["deck"] and r["gih_count"] >= pair_min:
            card.pair_z[r["deck"]] = z
    for r in conn.execute(
        "SELECT name, ata, alsa FROM pick_stats WHERE expansion = ? AND format = ?", (expansion, fmt)
    ):
        if r["name"] in cards:
            cards[r["name"]].ata, cards[r["name"]].alsa = r["ata"], r["alsa"]
    return SetData(expansion, fmt, mean, stdev, cards, name_by_id)


# --- Reading the draft so far ---------------------------------------------------

def colour_strength(data: SetData, pool: list[str], params: dict) -> dict[str, float]:
    """How much playable quality the pool holds in each colour."""
    strength = dict.fromkeys(COLORS, 0.0)
    for name in pool:
        card = data.card(name)
        z = card.z if card.z is not None else params["unknown_z"]
        if z > params["playable_z"]:
            for c in card.colors:
                strength[c] += z - params["playable_z"]
    return strength


def open_signal(data: SetData, seen: list[tuple[int, list[str]]]) -> dict[str, float]:
    """Evidence that a colour is under-drafted upstream.

    `seen` lists earlier packs as (pick number, card names), pick number starting
    at 1. A good card still in the pack well after its usual pick is a signal.
    """
    signal = dict.fromkeys(COLORS, 0.0)
    for pick_number, names in seen:
        for name in names:
            card = data.card(name)
            if card.z is None or card.z <= 0.5 or card.ata is None:
                continue
            lateness = min(max(pick_number - card.ata, 0.0), 5.0) / 5.0
            for c in card.colors:
                signal[c] += card.z * lateness
    return signal


def pair_weights(strength: dict[str, float], signal: dict[str, float], params: dict) -> dict[str, float]:
    """Share of belief in each of the ten colour pairs (sums to 1)."""
    affinity = {
        pair: sum(strength[c] + params["open_weight"] * signal[c] for c in pair)
        for pair in config.COLOR_PAIRS
    }
    top = max(affinity.values())
    raw = {pair: math.exp((a - top) / params["temperature"]) for pair, a in affinity.items()}
    total = sum(raw.values())
    return {pair: v / total for pair, v in raw.items()}


def value_in_pair(card: Card, pair: str, params: dict) -> float:
    """Strength of a card if the deck ends up in this colour pair."""
    base = card.z if card.z is not None else params["unknown_z"]
    missing = [c for c in card.colors if c not in pair]
    if not missing:
        return card.pair_z.get(pair, base)
    if len(missing) < len(card.colors):  # shares a colour: a possible splash
        return base - params["splash_penalty"] * len(missing)
    return base - params["off_colour_penalty"]


# --- Ranking ----------------------------------------------------------------------

def recommend(
    data: SetData,
    pack: list[str],
    pool: list[str],
    seen: list[tuple[int, list[str]]] | None = None,
    params: dict | None = None,
) -> Recommendation:
    """Rank `pack` (card names) given the cards already picked (`pool`).

    `seen` is optional: the earlier packs of the current round as
    (pick number starting at 1, card names), used to judge which colours are open.
    """
    params = {**config.RECOMMENDER, **(params or {})}
    commitment = min(1.0, len(pool) / params["commit_picks"])
    weights = pair_weights(colour_strength(data, pool, params), open_signal(data, seen or []), params)
    leaning = sorted(weights.items(), key=lambda kv: -kv[1])
    top_pair, top_weight = leaning[0]
    decided = commitment * top_weight  # how much the top pair really steers the pick

    suggestions = []
    for name in pack:
        card = data.card(name)
        base = card.z if card.z is not None else params["unknown_z"]
        in_colours = sum(w * value_in_pair(card, pair, params) for pair, w in weights.items())
        score = (1 - commitment) * base + commitment * in_colours

        reasons = [f"{card.grade} overall" if card.grade else "too few games to grade"]
        if pool and decided >= 0.2:
            missing = [c for c in card.colors if c not in top_pair]
            if not card.colors:
                reasons.append("colourless, fits any deck")
            elif not missing:
                reasons.append(f"fits {top_pair}")
                pair_z = card.pair_z.get(top_pair)
                if pair_z is not None and abs(pair_z - base) >= 0.34:
                    better = "better" if pair_z > base else "weaker"
                    reasons.append(f"{better} in {top_pair} ({grades.letter_for(pair_z)})")
            elif len(missing) < len(card.colors):
                reasons.append(f"needs {''.join(missing)} outside {top_pair}")
            else:
                reasons.append(f"off-colour for {top_pair}")
        suggestions.append(Suggestion(name, score, base, card.grade, card.gih_wr, card.colors, reasons))

    suggestions.sort(key=lambda s: -s.score)
    return Recommendation(suggestions, leaning, commitment)
