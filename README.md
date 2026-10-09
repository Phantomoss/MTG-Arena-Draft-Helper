# MTG Arena Limited Helper

Phase 0: the data layer. It builds a local SQLite cache with card statistics
computed from the 17Lands public datasets and card text from Scryfall, and
turns win rates into letter grades. The draft overlay and the gameplay
assistant will both read from this cache.

## Where the numbers come from

Win rates are computed locally from the **17Lands public datasets**
(https://www.17lands.com/public_datasets, licensed CC BY 4.0). 17Lands asks
tools not to scrape the API behind their website, so this project does not
use it.

Consequence: 17Lands publishes game data about **three weeks after a set is
released**. Until then `mtghelper sync` reports that the set is not published
and there are no grades for it.

## Setup (Windows, PowerShell)

```powershell
cd mtg-limited-helper
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest
```

Run `pip install -e .` again after an update that adds dependencies.
Each new PowerShell window needs `.venv\Scripts\Activate.ps1` first.

## Usage

```powershell
mtghelper sync HOB                  # download the set's game data, card text, compute grades
mtghelper grades HOB --top 20       # best cards of the set
mtghelper grades HOB --colors WU    # best cards in white-blue decks
mtghelper card "Bilbo"              # card text and grades
mtghelper recommend HOB --pack "Card A; Card B; Card C" --pool "Card D; Card E"
                                    # rank a pack given the cards already picked
mtghelper evaluate HOB              # replay real drafts and compare with the recommender
mtghelper draft                     # follow Arena while you draft: ranks every pack
mtghelper draft --replay --log "C:\path\to\Player.log"   # the same for a past draft
mtghelper overlay                   # the same in a small window floating over Arena
mtghelper overlay --demo --log "C:\path\to\Player.log"  # preview it on a past draft
mtghelper shortcut                  # put an "MTG Draft Helper" shortcut on the desktop
mtghelper deck                      # suggest a deck from the last draft in Arena's log
mtghelper deck --set HOB --pool "Card A; Card B; ..."   # ... or from a list of cards
mtghelper forget FRA                # delete a set's ratings and grades from the cache
```

Card names in `--pack` and `--pool` are separated by `;` (names can contain
commas). Any capitalisation works, and so does a fragment that matches one
card only, e.g. `smaug`.

The first `sync` of a set downloads two files (tens of MB each): game data for
the win rates and draft data for the pick statistics. Counting takes a minute
or two. The game data file is deleted afterwards (`--keep-download` keeps it);
the draft data file stays in `data/downloads` because `evaluate` replays it. Later syncs only download
again if 17Lands has updated the file; remote sources are checked at most once
every 24 hours unless you pass `--force`.

The cache is `data/mtghelper.db`. Set the `MTGHELPER_DB` environment variable
(or pass `--db`) to move it.

## Following a draft

`mtghelper draft` reads Arena's log (`%USERPROFILE%\AppData\LocalLow\Wizards Of
The Coast\MTGA\Player.log`) while you draft. For every pack you are shown it
prints the cards ranked by the recommender, and after each pick where your card
ranked. Started in the middle of a draft, it picks up at the current pack.

It needs "Detailed Logs (Plugin Support)" turned on in Arena (Options >
Account). It only reads the log; it never touches the game.

Quick Draft has been checked against a real log. Premier and Traditional
draft use different log messages that have not been checked yet. Grades and
recommendations appear only for sets with 17Lands public data; for newer sets
the packs are listed by name.

`mtghelper overlay` shows the same ranking in a small always-on-top panel.
It always opens at the middle of the left screen edge, locked in place. The
buttons at its top right are: `✥` to unlock it so it can be dragged (click
again to lock it where it is), `–` to minimise it to the taskbar, `✕` to close
it. It lists every card of the pack with its reason (on a screen too small for
that, the reasons are dropped), and a bar under the title shows which colour
pairs your pool is leaning towards. Right-click it to list fewer cards, pick a
colour theme (Midnight, Parchment, Arena gold, Slate, Pool colours, High
contrast) or make it see-through; those choices are remembered. It lists the
cards by name rather than marking them on Arena's screen, because Arena's card
layout changes with resolution and window size. For it to stay on top, run
Arena in windowed or borderless mode (Options > Graphics), not exclusive
full screen. `--demo` replays the drafts already in a log, one pack every two
seconds, to preview it without drafting.

### Desktop shortcut

`mtghelper shortcut` (run once, from the activated venv) creates an "MTG Draft
Helper" icon on the desktop. Double-clicking it opens the overlay with no
console window. When a draft starts, the overlay updates that set's 17Lands
data and card text by itself (the first time for a set this downloads about
100 MB and takes a minute or two; afterwards it is checked at most once a day).
`mtghelper overlay --no-sync` skips the update. If Arena's log does not exist
yet, the overlay says so and waits for it. If the overlay ever fails to start
from the shortcut, the error is written to `data/overlay-error.log`. Run
`mtghelper shortcut` again after moving the project folder.

The cache (`data/mtghelper.db`) always lives in the project folder, whichever
folder a command is started from.

Card ids Scryfall does not know are shown as `#id (basic land?)`: so far these
have always been basic lands.

Arena replaces `Player.log` every time it starts (the previous one becomes
`Player-prev.log`), so copy it before restarting if you want to replay a draft.

## Deck suggestions

When a draft ends, the overlay (and `mtghelper draft`) switches to a suggested
deck: the best two colours, 23 spells grouped by mana value, and the basic
lands split by the coloured mana symbols of those spells (17 lands, 16 if the
average mana value is 2.6 or less). `mtghelper deck` shows the same for the
last finished draft in Arena's log.

How it chooses: for each of the ten colour pairs, every castable card gets its
win-rate strength in that pair (or overall, if 17Lands has too few games of it
there); the 23 best are taken and then swapped while that improves the deck
score, which subtracts penalties for fewer than 14 creatures, more than 5 cards
costing 5+, or fewer than 5 cards costing 2 or less. The best pair wins; the
next ones are shown with how much weaker they are. Strong off-colour cards are
mentioned as splash ideas but never added automatically, and non-basic lands
are not considered yet. Limits are in `DECKBUILDER` in `config.py`.

It needs graded cards, so it only works for sets with 17Lands public data.

## How grades work

The grade is based on GIH WR: the share of games won among games where the
card was in hand at some point (opening hand or drawn). A game counts once per
card, however many copies were drawn, so numbers can differ slightly from the
17Lands website.

Each card is compared to the average of its set, and the letter shows how many
standard deviations above or below it sits (C+ is just above average, each
step is a third of a standard deviation). Colour-pair grades use only games
played by decks of exactly those two main colours, and are placed on the same
scale as overall grades, so they can be compared directly.

Grades use the raw win rate, so bombs with few games but very high win rates
stay at the top. Cards with fewer than 500 games in hand get no grade, and
grades resting on fewer than 1,500 games are shown with a `?` (the letter
itself is not changed). `SHRINK_SMALL_SAMPLES` in `config.py` (off by default)
pulls small samples toward the set average instead.

All thresholds are in `src/mtghelper/config.py`.

## How the recommender works

Every card is scored in standard deviations above the set average (the same
scale as the grades: +1 is three letter steps). The score blends:

- the card's **overall strength**, and
- its **strength in the colour pairs your pool points to**: the pair-specific
  win rate if you can cast it, a penalty if you would have to splash it, a
  bigger penalty if it is off-colour.

With an empty pool only the first counts; after about 18 picks only the second.
Which pairs the pool points to comes from the good cards already picked, plus a
smaller signal from good cards seen later than their usual pick (a hint that
the colour is open; the command line version does not use this signal, the
overlay will).

`mtghelper evaluate` replays real drafts and reports how often the player's
pick was the recommender's first choice or in its top three, next to the same
figures for "always take the highest grade". Agreement with players is a sanity
check, not a target: the point of the tool is to disagree with some picks.

The weights are in `RECOMMENDER` in `config.py`.

## Layout

| File | Role |
|---|---|
| `config.py` | settings: cache lifetime, request delays, grade bands |
| `db.py` | SQLite schema and helpers |
| `http.py` | polite HTTP client for Scryfall (delay between requests, retries) |
| `seventeenlands.py` | downloads a set's public game and draft data, counts wins and picks per card |
| `scryfall.py` | downloads card text for the rated cards |
| `grades.py` | converts win rates to letter grades |
| `recommender.py` | ranks a pack given the pool |
| `deckbuilder.py` | suggests 23 spells and basic lands from a pool |
| `evaluate.py` | replays real drafts to check the recommender |
| `logreader.py` | reads draft events from Arena's Player.log |
| `draftview.py` | turns log events into rankings and pick verdicts; terminal output (`mtghelper draft`) |
| `overlay.py` | the always-on-top window (`mtghelper overlay`, PySide6) and its icon |
| `updater.py` | brings one set's data up to date (used by `sync` and by the overlay) |
| `shortcut.py` | creates the desktop shortcut (`mtghelper shortcut`) |
| `cli.py` | the `mtghelper` command |

## Using it from code

```python
from mtghelper import config, db

conn = db.connect(config.DEFAULT_DB_PATH)
row = conn.execute(
    "SELECT c.name, c.oracle_text, g.grade FROM cards c "
    "LEFT JOIN grades g ON g.arena_id = c.arena_id AND g.colors = '' "
    "WHERE c.arena_id = ?", (103368,)
).fetchone()
```

`arena_id` is the same id MTG Arena writes in `Player.log`.

## Credits

Game statistics: 17Lands public datasets, https://www.17lands.com/public_datasets (CC BY 4.0).
Card text and images: Scryfall, https://scryfall.com.
