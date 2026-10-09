"""Suggest a deck from a draft pool: two colours, 23 spells, 17 basic lands.

For each of the ten colour pairs:

1. Every castable card in the pool gets a value: its win-rate strength in that
   colour pair if 17Lands has enough games of it there, else its overall
   strength (same scale as the grades: 1.0 = three letter steps).
2. The 23 best are taken, then improved by swapping cards in and out while the
   deck score goes up. The score is the sum of the values minus penalties for a
   deck that does not work as a deck: too few creatures, too many expensive
   spells, too few cheap ones (limits in config.DECKBUILDER).
3. Basic lands are split by the coloured mana symbols of the chosen spells.

The best-scoring pair is suggested; the next ones are listed with how much
weaker they are. Strong off-colour cards are mentioned as possible splashes,
but never added automatically. Non-basic lands are not considered yet.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import config, recommender

BASICS = {"W": "Plains", "U": "Island", "B": "Swamp", "R": "Mountain", "G": "Forest"}
BASIC_NAMES = set(BASICS.values())
SYMBOL = re.compile(r"\{([^}]+)\}")


@dataclass
class DeckCard:
    name: str
    value: float
    grade: str | None
    colors: str
    cmc: float
    creature: bool


@dataclass
class Deck:
    pair: str
    spells: list[DeckCard]
    lands: dict[str, int]                 # basic land name -> count
    score: float
    notes: list[str] = field(default_factory=list)
    bench: list[DeckCard] = field(default_factory=list)   # castable cards left out

    @property
    def creatures(self) -> int:
        return sum(c.creature for c in self.spells)

    @property
    def average_cmc(self) -> float:
        return sum(c.cmc for c in self.spells) / len(self.spells) if self.spells else 0.0

    def curve(self) -> dict[int, int]:
        """Spells per mana value, 6 meaning 6 or more."""
        counts = dict.fromkeys(range(0, 7), 0)
        for c in self.spells:
            counts[min(int(c.cmc), 6)] += 1
        return counts


@dataclass
class Suggestion:
    best: Deck
    alternatives: list[Deck]
    splashes: list[str]                   # off-colour cards worth considering


def is_land(card: recommender.Card) -> bool:
    return "Land" in card.type_line.split("//")[0]


def is_creature(card: recommender.Card) -> bool:
    return "Creature" in card.type_line.split("//")[0]


def colour_symbols(card: recommender.Card) -> dict[str, float]:
    """Coloured mana symbols in the (front face) cost; hybrid symbols count half each."""
    counts = dict.fromkeys("WUBRG", 0.0)
    cost = card.mana_cost.split("//")[0]
    if not cost:  # no Scryfall text: assume one symbol per colour
        for c in card.colors:
            counts[c] += 1
        return counts
    for symbol in SYMBOL.findall(cost):
        colours = [c for c in symbol.split("/") if c in counts]
        for c in colours:
            counts[c] += 1 / len(colours)
    return counts


def _deck_card(card: recommender.Card, pair: str, params: dict) -> DeckCard:
    value = recommender.value_in_pair(card, pair, params)
    return DeckCard(card.name, value, card.grade, card.colors, card.cmc or 0.0, is_creature(card))


def _score(cards: list[DeckCard], rules: dict) -> float:
    score = sum(c.value for c in cards)
    creatures = sum(c.creature for c in cards)
    expensive = sum(c.cmc >= 5 for c in cards)
    cheap = sum(c.cmc <= 2 for c in cards)
    score -= rules["creature_penalty"] * max(0, rules["min_creatures"] - creatures)
    score -= rules["expensive_penalty"] * max(0, expensive - rules["max_expensive"])
    score -= rules["cheap_penalty"] * max(0, rules["min_cheap"] - cheap)
    return score


def _best_spells(candidates: list[DeckCard], rules: dict) -> tuple[list[DeckCard], list[DeckCard]]:
    """23 cards maximising the deck score: best values first, then improving swaps."""
    ranked = sorted(candidates, key=lambda c: -c.value)
    deck, bench = ranked[: rules["spells"]], ranked[rules["spells"]:]
    improved = True
    while improved and bench:
        improved = False
        current = _score(deck, rules)
        best_gain, best_swap = 1e-9, None
        for i, out in enumerate(deck):
            for j, inn in enumerate(bench):
                trial = deck[:i] + [inn] + deck[i + 1:]
                gain = _score(trial, rules) - current
                if gain > best_gain:
                    best_gain, best_swap = gain, (i, j)
        if best_swap:
            i, j = best_swap
            deck[i], bench[j] = bench[j], deck[i]
            improved = True
    deck.sort(key=lambda c: (c.cmc, -c.value))
    bench.sort(key=lambda c: -c.value)
    return deck, bench


def split_lands(spells: list[DeckCard], cards: dict[str, recommender.Card], pair: str, total: int) -> dict[str, int]:
    """Basic lands per colour of the pair, in proportion to the mana symbols (largest remainder)."""
    weight = dict.fromkeys(pair, 0.0)
    for spell in spells:
        for c, n in colour_symbols(cards[spell.name]).items():
            if c in weight:
                weight[c] += n
    if sum(weight.values()) == 0:
        weight = dict.fromkeys(pair, 1.0)
    shares = {c: total * w / sum(weight.values()) for c, w in weight.items()}
    counts = {c: int(s) for c, s in shares.items()}
    for c in sorted(shares, key=lambda c: -(shares[c] - counts[c]))[: total - sum(counts.values())]:
        counts[c] += 1
    return {BASICS[c]: n for c, n in counts.items() if n}


def build_for_pair(data: recommender.SetData, pool: list[str], pair: str,
                   rules: dict | None = None, params: dict | None = None) -> Deck:
    rules = {**config.DECKBUILDER, **(rules or {})}
    params = {**config.RECOMMENDER, **(params or {})}
    cards = {name: data.card(name) for name in pool}
    candidates = [
        _deck_card(cards[name], pair, params)
        for name in pool
        if name not in BASIC_NAMES and not is_land(cards[name])
        and all(c in pair for c in cards[name].colors)
    ]
    spells, bench = _best_spells(candidates, rules)
    notes = []
    if len(spells) < rules["spells"]:
        notes.append(f"Only {len(spells)} playable cards in {pair}: fill with more lands or a third colour.")
    lands_total = rules["lands_low_curve"] if spells and \
        sum(c.cmc for c in spells) / len(spells) <= rules["low_curve"] else rules["lands"]
    lands_total += max(0, rules["spells"] - len(spells))  # a short deck still needs 40 cards
    lands = split_lands(spells, cards, pair, lands_total)
    deck = Deck(pair, spells, lands, _score(spells, rules), notes, bench)
    if deck.creatures < rules["min_creatures"]:
        notes.append(f"Only {deck.creatures} creatures (aim for {rules['min_creatures']}+).")
    expensive = sum(c.cmc >= 5 for c in spells)
    if expensive > rules["max_expensive"]:
        notes.append(f"{expensive} spells cost 5 or more: the deck may be slow.")
    return deck


def suggest(data: recommender.SetData, pool: list[str], rules: dict | None = None,
            params: dict | None = None) -> Suggestion:
    """Best deck over the ten colour pairs, plus runners-up and splash ideas."""
    rules = {**config.DECKBUILDER, **(rules or {})}
    decks = sorted((build_for_pair(data, pool, pair, rules, params) for pair in config.COLOR_PAIRS),
                   key=lambda d: -d.score)
    best = decks[0]
    splashes = []
    for name in dict.fromkeys(pool):
        card = data.card(name)
        if card.z is None or card.z < rules["splash_min_z"] or is_land(card) or not card.colors:
            continue
        missing = [c for c in card.colors if c not in best.pair]
        if len(missing) == 1:
            splashes.append(f"{name} ({card.grade}) needs {missing[0]}: "
                            f"2-3 {BASICS[missing[0]]} if you have fixing")
    return Suggestion(best, decks[1: 1 + rules["alternatives"]], splashes)
