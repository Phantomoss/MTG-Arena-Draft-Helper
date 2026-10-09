"""The recommender's view of a draft as it happens in Arena.

`DraftTracker` turns log events into display-ready updates (a ranked pack, a
pick verdict, a summary). Two front ends show them:

* the terminal: `mtghelper draft` / `mtghelper draft --replay` (this module)
* the overlay window: `mtghelper overlay` (overlay.py)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config, db, deckbuilder, logreader, recommender, scryfall

FORMAT_NAMES = {"QuickDraft": "Quick Draft", "PremierDraft": "Premier Draft",
                "TradDraft": "Traditional Draft", "PickTwoDraft": "Pick Two Draft"}


@dataclass
class Session:
    """One draft being followed: card data, names, and how the picks went."""
    conn: sqlite3.Connection
    set_code: str | None = None
    fmt: str | None = None
    data: recommender.SetData | None = None
    note: str = ""
    event_name: str = ""
    names: dict[int, str] = field(default_factory=dict)
    agreement: list[int] = field(default_factory=list)   # rank of each pick (0 = top choice)
    last_ranking: list[str] = field(default_factory=list)
    lookups_left: int = 300  # Scryfall lookups for unknown cards, per session
    # Called with the set code when a draft of a new set starts, before its
    # ratings are loaded (the overlay uses it to update the set's data).
    prepare: Callable[[str], None] | None = None

    @property
    def title(self) -> str:
        return f"{self.set_code or '?'} {FORMAT_NAMES.get(self.fmt or '', self.fmt or 'draft')}"

    def start(self, event_name: str) -> None:
        set_code, kind = logreader.set_and_format(event_name)
        self.event_name, self.set_code, self.fmt = event_name, set_code, kind
        self.data, self.note, self.agreement, self.last_ranking = None, "", [], []
        if not set_code:
            self.note = f"Unrecognised event name {event_name!r}: showing card names only."
            return
        if self.prepare is not None:
            self.prepare(set_code)
        # Ratings for the event's own format if we have them, else Premier Draft.
        for fmt in dict.fromkeys([kind, config.DEFAULT_FORMAT]):
            public = self.conn.execute(
                "SELECT 1 FROM datasets WHERE expansion = ? AND format = ?", (set_code, fmt)
            ).fetchone()
            if not public:
                continue
            try:
                self.data = recommender.load_set(self.conn, set_code, fmt)
            except LookupError:
                continue
            if fmt != kind:
                self.note = f"Using {set_code} {fmt} ratings (no {kind} data)."
            return
        self.note = (f"No 17Lands public data for {set_code} yet: showing card names only, "
                     f"no grades or recommendations.")

    def name(self, arena_id: int) -> str:
        if arena_id in self.names:
            return self.names[arena_id]
        name = self.data.name_by_id.get(arena_id) if self.data else None
        if name is None:
            row = self.conn.execute("SELECT name FROM cards WHERE arena_id = ?", (arena_id,)).fetchone()
            name = row["name"] if row else None
        if name is None and self.lookups_left > 0:
            self.lookups_left -= 1
            name = _scryfall_name(self.conn, arena_id)
        # Ids Scryfall does not know have so far been basic lands.
        self.names[arena_id] = name or f"#{arena_id} (basic land?)"
        return self.names[arena_id]


def _scryfall_name(conn: sqlite3.Connection, arena_id: int) -> str | None:
    """Look an unknown Arena id up on Scryfall once and cache the card."""
    try:
        session = scryfall.PoliteSession(config.SCRYFALL_DELAY_S)
        card = session.get_json(f"{scryfall.API}/cards/arena/{arena_id}", allow_404=True)
    except Exception:
        return None
    if not card:
        return None
    row = scryfall.parse_card(card, arena_id=arena_id)
    db.upsert(conn, "cards", row)
    conn.commit()
    return row["name"]


# --- What the front ends display ------------------------------------------------

@dataclass
class Row:
    rank: int
    name: str
    grade: str | None = None
    gih_wr: float | None = None
    colors: str = ""
    reasons: list[str] = field(default_factory=list)


@dataclass
class PackView:
    title: str                 # "FRA Quick Draft"
    pack: int
    pick: int
    pool_size: int
    leaning: str               # "WB 61%, BR 20%" or "-"
    rated: bool                # False: names only, no grades
    rows: list[Row]
    pairs: list[tuple[str, float]] = field(default_factory=list)  # colour pairs, best first (empty pool: none)


@dataclass
class Update:
    kind: str                  # "note", "waiting", "pack", "pick", "done", "deck"
    text: str = ""
    pack: PackView | None = None
    rank: int | None = None    # for "pick": where the taken card ranked (0 = top)
    deck: deckbuilder.Suggestion | None = None  # for "deck": the suggested build


class DraftTracker:
    """Feeds on log events, keeps the draft state, emits `Update`s."""

    def __init__(self, conn: sqlite3.Connection, prepare: Callable[[str], None] | None = None):
        self.session = Session(conn, prepare=prepare)
        self.state = logreader.DraftState()
        self._waiting: logreader.PackShown | None = None

    def handle(self, event: logreader.Event | None, live: bool = True) -> list[Update]:
        if event is None:  # backlog read: show the pack waiting for a pick, if any
            updates = [Update("waiting", "Watching for draft picks...")]
            if self._waiting is not None:
                updates.append(Update("pack", pack=self._pack_view(self._waiting)))
            self._waiting = None
            return updates
        updates: list[Update] = []
        session = self.session
        if isinstance(event, logreader.PackShown) and event.event != session.event_name:
            session.start(event.event)
            if session.note and live:
                updates.append(Update("note", session.note))
        self.state.apply(event)
        if not live:
            self._waiting = event if isinstance(event, logreader.PackShown) else None
            return updates
        if isinstance(event, logreader.PackShown):
            updates.append(Update("pack", pack=self._pack_view(event)))
        elif isinstance(event, logreader.CardPicked):
            updates.extend(self._pick_updates(event))
        elif isinstance(event, logreader.DraftCompleted):
            updates.append(Update("done", self._summary()))
            deck = self.deck_suggestion()
            if deck is not None:
                updates.append(Update("deck", deck=deck))
        return updates

    def deck_suggestion(self) -> deckbuilder.Suggestion | None:
        """A deck from the current pool, if the set has grades and the pool is complete enough."""
        session = self.session
        pool = [session.name(i) for i in self.state.pool]
        if session.data is None or len(pool) < config.DECKBUILDER["spells"]:
            return None
        return deckbuilder.suggest(session.data, pool)

    def _pack_view(self, pack: logreader.PackShown) -> PackView:
        session = self.session
        names = [session.name(i) for i in pack.cards]
        pool = [session.name(i) for i in self.state.pool]
        if session.data is None:
            session.last_ranking = []
            rows = [Row(i, n) for i, n in enumerate(sorted(names), 1)]
            return PackView(session.title, pack.pack, pack.pick, len(pool), "-", False, rows)
        seen = [(pick, [session.name(i) for i in cards]) for pick, cards in self.state.round_packs]
        result = recommender.recommend(session.data, names, pool, seen)
        session.last_ranking = [s.name for s in result.suggestions]
        lean = ", ".join(f"{p} {w:.0%}" for p, w in result.leaning[:2]) if pool else "-"
        rows = [Row(i, s.name, s.grade, s.gih_wr, s.colors, s.reasons)
                for i, s in enumerate(result.suggestions, 1)]
        pairs = list(result.leaning) if pool else []
        return PackView(session.title, pack.pack, pack.pick, len(pool), lean, True, rows, pairs)

    def _pick_updates(self, pick: logreader.CardPicked) -> list[Update]:
        session = self.session
        updates = []
        for arena_id in pick.cards:
            name = session.name(arena_id)
            if name in session.last_ranking:
                rank = session.last_ranking.index(name)
                session.agreement.append(rank)
                verdict = "the top choice" if rank == 0 else f"ranked #{rank + 1}"
                updates.append(Update("pick", f"you took {name} ({verdict})", rank=rank))
            else:
                updates.append(Update("pick", f"you took {name}"))
        return updates

    def _summary(self) -> str:
        ranks = self.session.agreement
        if not ranks:
            return "Draft complete."
        n = len(ranks)
        top1 = sum(r == 0 for r in ranks)
        top3 = sum(r < 3 for r in ranks)
        return f"Draft complete: you took the top choice {top1} of {n} times, a top-3 card {top3} of {n} times."


# --- Terminal front end --------------------------------------------------------------

def print_update(update: Update, top: int = 8, out=print) -> None:
    if update.kind == "note":
        out(f"\n{update.text}")
    elif update.kind == "waiting":
        out(f"{update.text} (Ctrl+C to stop)")
    elif update.kind == "pick":
        out(f"  -> {update.text}")
    elif update.kind == "done":
        out(f"\n{update.text}")
    elif update.kind == "deck" and update.deck:
        out("")
        for line in deck_lines(update.deck):
            out(line)
    elif update.kind == "pack" and update.pack:
        view = update.pack
        out("")
        if not view.rated:
            out(f"{view.title} - pack {view.pack}, pick {view.pick}")
            for row in view.rows:
                out(f"   {row.name}")
            return
        out(f"{view.title} - pack {view.pack}, pick {view.pick} - pool {view.pool_size}, leaning {view.leaning}")
        out(f"  {'#':>2}  {'Grade':<6}{'GIH WR':>7}  {'Col':<5}{'Card':<34}Why")
        for row in view.rows[:top]:
            wr = f"{row.gih_wr * 100:.1f}%" if row.gih_wr is not None else "-"
            out(f"  {row.rank:>2}  {(row.grade or '-'):<6}{wr:>7}  {(row.colors or '-'):<5}"
                f"{row.name:<34}{'; '.join(row.reasons)}")
        if len(view.rows) > top:
            out(f"      ... {len(view.rows) - top} more")


def deck_lines(suggestion: deckbuilder.Suggestion) -> list[str]:
    """The deck suggestion as plain text lines."""
    deck = suggestion.best
    lands = ", ".join(f"{n} {name}" for name, n in deck.lands.items())
    lines = [f"Suggested deck: {deck.pair}  -  {len(deck.spells)} spells, {sum(deck.lands.values())} lands "
             f"({lands}); {deck.creatures} creatures, average mana value {deck.average_cmc:.1f}"]
    by_cost: dict[int, list[deckbuilder.DeckCard]] = {}
    for card in deck.spells:
        by_cost.setdefault(min(int(card.cmc), 6), []).append(card)
    for cost in sorted(by_cost):
        label = f"{cost}+" if cost == 6 else str(cost)
        names = ", ".join(f"{c.name} ({c.grade or '-'})" + ("" if c.creature else "*") for c in by_cost[cost])
        lines.append(f"  {label}: {names}")
    lines.append("  (* = not a creature)")
    lines += [f"  Note: {n}" for n in deck.notes]
    lines += [f"  Splash idea: {s}" for s in suggestion.splashes]
    for alt in suggestion.alternatives:
        lines.append(f"  Next best: {alt.pair} ({alt.score - deck.score:+.1f}, "
                     f"{alt.creatures} creatures)")
    if deck.bench:
        lines.append("  Best cards left out: " + ", ".join(f"{c.name} ({c.grade or '-'})" for c in deck.bench[:5]))
    return lines


def run(conn: sqlite3.Connection, events, top: int = 8, out=print) -> Session:
    """Print a draft from a stream of (event, live) pairs."""
    tracker = DraftTracker(conn)
    for event, live in events:
        for update in tracker.handle(event, live):
            print_update(update, top, out)
    return tracker.session


def replay(conn: sqlite3.Connection, log: Path | str, top: int = 8, out=print) -> Session:
    """Walk through every draft already in a log file."""
    return run(conn, ((e, True) for e in logreader.read_events(log)), top, out)


def follow(conn: sqlite3.Connection, log: Path | str, top: int = 8, out=print,
           stop=lambda: False) -> None:
    """Follow the log live. A draft already in progress is picked up at its current pack."""
    out(f"Reading {log}")
    try:
        run(conn, logreader.follow_events(log, stop=stop), top, out)
    except KeyboardInterrupt:
        out("\nStopped.")
