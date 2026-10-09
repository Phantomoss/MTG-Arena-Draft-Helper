"""Fill in full card text from Scryfall for the cards 17lands told us about.

Strategy (light on the API):
1. One paginated search per set fetches every card of that set.
2. Cards still missing (bonus sheets, set-code mismatches) are fetched one by
   one by Arena id, then by exact name as a last resort.
"""

from __future__ import annotations

import sqlite3

from . import config, db
from .http import PoliteSession

API = "https://api.scryfall.com"


def _faces(card: dict, field: str) -> str | None:
    """Read a field, joining both faces with ' // ' for double-faced cards."""
    if card.get(field) is not None:
        return card[field]
    values = [f.get(field) for f in card.get("card_faces", []) if f.get(field)]
    return " // ".join(values) if values else None


def _image(card: dict) -> str | None:
    if "image_uris" in card:
        return card["image_uris"].get("normal")
    faces = card.get("card_faces") or []
    if faces and "image_uris" in faces[0]:
        return faces[0]["image_uris"].get("normal")
    return None


def parse_card(card: dict, arena_id: int | None = None) -> dict | None:
    """Turn a Scryfall card object into a `cards` row."""
    arena_id = arena_id or card.get("arena_id")
    if not arena_id:
        return None
    colors = card.get("colors")
    if colors is None:  # double-faced: colours live on the faces
        seen = [c for f in card.get("card_faces", []) for c in f.get("colors", [])]
        colors = [c for c in "WUBRG" if c in seen]
    return {
        "arena_id": int(arena_id),
        "name": card["name"],
        "set_code": card.get("set"),
        "collector_number": card.get("collector_number"),
        "rarity": card.get("rarity"),
        "colors": "".join(c for c in "WUBRG" if c in colors),
        "color_identity": "".join(c for c in "WUBRG" if c in card.get("color_identity", [])),
        "mana_cost": _faces(card, "mana_cost"),
        "cmc": card.get("cmc"),
        "type_line": card.get("type_line"),
        "oracle_text": _faces(card, "oracle_text"),
        "power": _faces(card, "power"),
        "toughness": _faces(card, "toughness"),
        "keywords": ",".join(card.get("keywords", [])),
        "layout": card.get("layout"),
        "image_url": _image(card),
        "scryfall_id": card.get("id"),
        "source": "scryfall",
        "updated_at": db.now_iso(),
    }


def fetch_set(session: PoliteSession, set_code: str) -> list[dict]:
    """All printings of a set, following pagination.

    The search deliberately has no `game:arena` filter: Scryfall can lag in
    flagging new sets. Cards without an Arena id are dropped by `parse_card`.
    """
    cards: list[dict] = []
    url = f"{API}/cards/search"
    params: dict | None = {"q": f"set:{set_code.lower()}", "unique": "prints"}
    while url:
        page = session.get_json(url, params=params, allow_404=True)
        if page is None:  # Scryfall answers 404 when a search has no results
            break
        cards.extend(page.get("data", []))
        url = page.get("next_page") if page.get("has_more") else None
        params = None  # next_page already contains the query
    return cards


def missing_cards(conn: sqlite3.Connection, expansion: str) -> list[sqlite3.Row]:
    """Cards rated for this set that still lack Scryfall data."""
    return conn.execute(
        "SELECT DISTINCT c.arena_id, c.name FROM cards c "
        "JOIN ratings r ON r.arena_id = c.arena_id "
        "WHERE r.expansion = ? AND c.source != 'scryfall'",
        (expansion,),
    ).fetchall()


def sync_cards(
    conn: sqlite3.Connection,
    expansion: str,
    force: bool = False,
    session: PoliteSession | None = None,
    log=print,
) -> None:
    expansion = expansion.upper()
    key = f"scryfall:{expansion}"
    if not force and db.is_fresh(conn, key, config.CACHE_TTL_HOURS) and not missing_cards(conn, expansion):
        log(f"  Scryfall {expansion}: cached, skipping")
        return
    session = session or PoliteSession(config.SCRYFALL_DELAY_S)

    stored = 0
    for card in fetch_set(session, expansion):
        row = parse_card(card)
        if row:
            db.upsert(conn, "cards", row)
            stored += 1
    conn.commit()

    fallback = 0
    unresolved: list[str] = []
    for miss in missing_cards(conn, expansion):
        card = session.get_json(f"{API}/cards/arena/{miss['arena_id']}", allow_404=True)
        if card is None:
            card = session.get_json(
                f"{API}/cards/named", params={"exact": miss["name"]}, allow_404=True
            )
        if card is None:
            unresolved.append(miss["name"])
            continue
        # Keep 17lands' Arena id: the Scryfall printing found by name may differ.
        db.upsert(conn, "cards", parse_card(card, arena_id=miss["arena_id"]))
        fallback += 1

    db.mark_synced(conn, key)
    conn.commit()
    log(f"  Scryfall {expansion}: {stored} cards from set search, {fallback} looked up individually")
    if unresolved:
        log(f"  Scryfall {expansion}: no match for {len(unresolved)}: {', '.join(unresolved[:10])}")
