"""Central settings. Everything tunable lives here."""

from __future__ import annotations

import os
from pathlib import Path

# The project folder (this file is src/mtghelper/config.py inside it).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Where the SQLite cache lives: data/ in the project folder, wherever the command
# is started from (a desktop shortcut starts elsewhere). Override with MTGHELPER_DB.
DEFAULT_DB_PATH = Path(os.environ.get("MTGHELPER_DB", PROJECT_ROOT / "data" / "mtghelper.db"))

# Identify ourselves to Scryfall and to the 17Lands public file server.
USER_AGENT = "mtghelper/0.1 (personal MTG Arena Limited helper)"
REQUEST_TIMEOUT_S = 30
SCRYFALL_DELAY_S = 0.12  # Scryfall asks for 50-100 ms between requests

# `sync` re-checks remote sources at most this often unless --force is given.
CACHE_TTL_HOURS = 24

DEFAULT_FORMAT = "PremierDraft"

# The ten two-colour pairs, in WUBRG order as 17Lands writes them.
COLOR_PAIRS = ["WU", "WB", "WR", "WG", "UB", "UR", "UG", "BR", "BG", "RG"]

# --- Grading -----------------------------------------------------------------
# A card needs at least this many "game in hand" games to receive a grade.
MIN_GIH_GAMES = 500
# Below this many games a grade is shown with a "?": the letter is unchanged,
# the mark only says the sample is still small.
LOW_CONFIDENCE_GAMES = 1500

# Optional, OFF by default: grades use the raw win rate of every card with
# enough games, so bombs with few games but very high win rates stay at the top.
# Turning this on pulls win rates toward the set average for small samples.
SHRINK_SMALL_SAMPLES = False
# Used only when shrinking: floor for the estimated real spread between cards.
MIN_TRUE_SD = 0.01

# Grade = how many standard deviations a card's GIH win rate sits from the set
# average. Each row is (minimum z-score, letter). Checked top to bottom.
GRADE_BANDS: list[tuple[float, str]] = [
    (2.00, "A+"),
    (1.67, "A"),
    (1.33, "A-"),
    (1.00, "B+"),
    (0.67, "B"),
    (0.33, "B-"),
    (0.00, "C+"),
    (-0.33, "C"),
    (-0.67, "C-"),
    (-1.00, "D+"),
    (-1.33, "D"),
    (-1.67, "D-"),
]
LOWEST_GRADE = "F"

# --- Pick recommender -----------------------------------------------------------
# Scores are in "grade steps": 1.0 = one standard deviation of card win rates in
# the set, i.e. three letter steps (B- to A-). See recommender.py for the logic.
#
# temperature and splash_penalty were set on 2026-10-08 from
# `mtghelper evaluate HOB --tune --drafts 1000 --min-win-rate 0.6` (agreement
# with picks of players winning 60%+ of their games). Result with these values:
# the player's pick is the first choice 44% of the time and in the top three 80%,
# against 34% and 69% for "highest grade in the pack".
# Going further (temperature 0.2, splash_penalty 0, commit_picks 8) gained
# under one point each and was not adopted: it only copies players more closely.
# open_weight made no measurable difference at any value.
RECOMMENDER = {
    # After this many picks the pool fully decides which colours count.
    "commit_picks": 18,
    # How sharply the two best colour pairs dominate the others (lower = sharper).
    "temperature": 0.5,
    # Cost of a card you could not cast in a colour pair at all.
    "off_colour_penalty": 2.0,
    # Cost per missing colour of a card that shares one colour with the pair.
    "splash_penalty": 0.6,
    # Weight of "this colour seems to be flowing" relative to cards already picked.
    "open_weight": 0.5,
    # Pool cards below this score are assumed not to make the deck.
    "playable_z": -0.5,
    # Score assumed for cards with too few games to be rated.
    "unknown_z": -1.0,
}

# --- Deck builder ---------------------------------------------------------------
# Scores use the same scale as the recommender (1.0 = one standard deviation of
# card win rates). The penalties keep the 23 spells playable as a deck.
DECKBUILDER = {
    "spells": 23,
    "lands": 17,
    "lands_low_curve": 16,      # used when the average mana value is at most low_curve
    "low_curve": 2.6,
    "min_creatures": 14,
    "creature_penalty": 0.35,   # per creature short of min_creatures
    "max_expensive": 5,         # spells costing 5 or more
    "expensive_penalty": 0.4,   # per spell over max_expensive
    "min_cheap": 5,             # spells costing 2 or less
    "cheap_penalty": 0.25,      # per spell short of min_cheap
    "splash_min_z": 1.33,       # an off-colour card at least this good (A-) is worth a splash note
    "alternatives": 2,          # other colour pairs shown after the best one
}
