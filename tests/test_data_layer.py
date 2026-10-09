import json
import csv
import gzip
import sqlite3

import pytest

from mtghelper import cli, config, db, grades, scryfall, seventeenlands

# --- A tiny file in the layout of 17Lands public game data ----------------------

CARDS = ["Bomb", "Azog, Moria's Ruin", "Filler"]
PREFIXES = ["opening_hand_", "drawn_", "tutored_", "deck_", "sideboard_"]

# (main_colors, won, {card: (deck, opening_hand, drawn)})
GAMES = [
    ("WU", True,  {"Bomb": (1, 1, 0), "Filler": (2, 0, 2)}),
    ("WU", True,  {"Bomb": (1, 0, 1), "Azog, Moria's Ruin": (1, 0, 0)}),
    ("WU", False, {"Bomb": (1, 0, 0), "Filler": (1, 1, 0)}),
    ("UW", True,  {"Filler": (1, 0, 1)}),           # 'UW' must be read as 'WU'
    ("BR", False, {"Azog, Moria's Ruin": (1, 1, 1), "Filler": (1, 0, 0)}),
    ("BRG", True, {"Bomb": (1, 1, 0)}),             # three colours: overall only
]


def write_game_file(path, games=GAMES):
    header = ["expansion", "event_type", "main_colors", "won"]
    header += [p + c for c in CARDS for p in PREFIXES]
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        for colors, won, cards in games:
            row = ["TST", "PremierDraft", colors, won]
            for card in CARDS:
                deck, opening, drawn = cards.get(card, (0, 0, 0))
                row += [opening, drawn, 0, deck, 0]
            writer.writerow(row)
    return path


CARD_LIST = [
    {"id": "1", "expansion": "TST", "name": "Bomb", "rarity": "mythic", "color_identity": "W", "types": "Creature"},
    {"id": "2", "expansion": "TST", "name": "Azog, Moria's Ruin", "rarity": "rare", "color_identity": "B", "types": "Creature"},
    {"id": "3", "expansion": "OLD", "name": "Filler", "rarity": "common", "color_identity": "U", "types": "Instant"},
    {"id": "4", "expansion": "OLD2", "name": "Filler", "rarity": "common", "color_identity": "U", "types": "Instant"},
    {"id": "9", "expansion": "OLD", "name": "Bomb", "rarity": "mythic", "color_identity": "W", "types": "Creature"},
]


def test_aggregate_counts_games_once_per_card(tmp_path):
    names, counts, total = seventeenlands.aggregate_games(write_game_file(tmp_path / "g.csv.gz"), chunksize=4)
    assert names == CARDS and total == 6
    i = {n: k for k, n in enumerate(names)}
    all_decks = counts[""]
    # Bomb: in 4 decks; in hand in 3 games (all won); in deck but never drawn once (lost).
    assert (all_decks["gp"][i["Bomb"]], all_decks["gih"][i["Bomb"]], all_decks["gih_w"][i["Bomb"]]) == (4, 3, 3)
    assert (all_decks["gnd"][i["Bomb"]], all_decks["gnd_w"][i["Bomb"]]) == (1, 0)
    # Azog (comma in the name): in hand once, in a lost game where it was both opened and drawn.
    assert (all_decks["gih"][i["Azog, Moria's Ruin"]], all_decks["gih_w"][i["Azog, Moria's Ruin"]]) == (1, 0)
    # Filler: two copies drawn in one game still count as one game.
    assert (all_decks["gih"][i["Filler"]], all_decks["gih_w"][i["Filler"]]) == (3, 2)
    assert (all_decks["oh"][i["Filler"]], all_decks["gd"][i["Filler"]]) == (1, 2)
    # Colour pairs: 'UW' is folded into 'WU'; the three-colour deck is in no pair.
    assert (counts["WU"]["gih"][i["Bomb"]], counts["WU"]["gih"][i["Filler"]]) == (2, 3)
    assert counts["BR"]["gih"][i["Azog, Moria's Ruin"]] == 1
    assert sum(int(counts[p]["gp"][i["Bomb"]]) for p in config.COLOR_PAIRS) == 3


def test_rejects_files_that_are_not_game_data(tmp_path):
    path = tmp_path / "other.csv.gz"
    with gzip.open(path, "wt") as fh:
        fh.write("a,b\n1,2\n")
    with pytest.raises(ValueError):
        seventeenlands.aggregate_games(path)


def test_ids_prefer_this_expansion_and_fall_back_to_all_printings():
    mapping = seventeenlands.ids_by_name(CARD_LIST, "TST")
    assert [r["id"] for r in mapping["Bomb"]] == ["1"]
    assert [r["id"] for r in mapping["Filler"]] == ["3", "4"]


def synced(tmp_path, monkeypatch, published=True, etag="v1"):
    """Run sync_ratings against the tiny local file instead of the network."""
    source = write_game_file(tmp_path / "source.csv.gz")
    calls = {"downloads": 0}

    def fake_download(url, dest, log=print):
        calls["downloads"] += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(source.read_bytes())

    info = {"etag": etag, "size": source.stat().st_size} if published else None
    monkeypatch.setattr(seventeenlands, "remote_info", lambda url: info)
    monkeypatch.setattr(seventeenlands, "download", fake_download)
    monkeypatch.setattr(seventeenlands, "fetch_card_list", lambda: CARD_LIST)
    return calls


def test_sync_stores_ratings_and_cleans_up(tmp_path, monkeypatch):
    calls = synced(tmp_path, monkeypatch)
    conn = db.connect(":memory:")
    lines = []
    assert seventeenlands.sync_ratings(conn, "tst", download_dir=tmp_path / "dl", log=lines.append)
    assert calls["downloads"] == 1 and not list((tmp_path / "dl").glob("*"))
    bomb = conn.execute("SELECT * FROM ratings WHERE arena_id = 1 AND colors = ''").fetchone()
    assert bomb["gih_count"] == 3 and bomb["gih_wr"] == 1.0 and bomb["game_count"] == 4
    assert bomb["gnd_wr"] == 0.0 and bomb["iwd"] == 1.0
    # Filler has no printing in TST, so both of its printings carry the rating.
    assert conn.execute("SELECT COUNT(*) FROM ratings WHERE name = 'Filler' AND colors = ''").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM ratings WHERE arena_id = 9").fetchone()[0] == 0
    assert conn.execute("SELECT games FROM datasets WHERE expansion = 'TST'").fetchone()[0] == 6
    assert len(scryfall.missing_cards(conn, "TST")) == 4

    # Second run: nothing is downloaded again, even when forced past the 24h check.
    monkeypatch.setattr(config, "CACHE_TTL_HOURS", 0)
    assert seventeenlands.sync_ratings(conn, "TST", download_dir=tmp_path / "dl", log=lines.append)
    assert calls["downloads"] == 1 and "unchanged" in lines[-1]


def test_sync_reports_unpublished_sets(tmp_path, monkeypatch):
    synced(tmp_path, monkeypatch, published=False)
    conn = db.connect(":memory:")
    lines = []
    assert seventeenlands.sync_ratings(conn, "NEW", download_dir=tmp_path, log=lines.append) is False
    assert "not published" in lines[0]
    assert conn.execute("SELECT COUNT(*) FROM ratings").fetchone()[0] == 0


# --- Grading ------------------------------------------------------------------------

def add_rating(conn, arena_id, name, gih_wr, gih_count=1000, colors=""):
    db.upsert(conn, "ratings", {
        "expansion": "TST", "format": "PremierDraft", "colors": colors, "arena_id": arena_id,
        "name": name, "gih_wr": gih_wr, "gih_count": gih_count, "fetched_at": db.now_iso(),
    })


def sample_set(conn):
    for arena_id, name, wr in [(1, "Bomb", 0.66), (2, "Good", 0.59), (3, "Average", 0.56),
                               (4, "Filler", 0.53), (5, "Bad", 0.46)]:
        add_rating(conn, arena_id, name, wr)
    add_rating(conn, 6, "Tiny sample", 0.90, gih_count=20)
    add_rating(conn, 7, "No data", None, gih_count=0)


def test_letter_bands():
    assert grades.letter_for(2.5) == "A+"
    assert grades.letter_for(0.0) == "C+"
    assert grades.letter_for(-0.01) == "C"
    assert grades.letter_for(-3) == "F"


def test_grades_use_raw_win_rates_and_skip_tiny_samples():
    conn = db.connect(":memory:")
    sample_set(conn)
    add_rating(conn, 8, "Early bomb", 0.74, gih_count=543)
    assert grades.compute_grades(conn, "TST") == {"": 6}
    rows = conn.execute("SELECT * FROM grades ORDER BY z_score DESC").fetchall()
    assert rows[0]["arena_id"] == 8
    assert all(r["adj_wr"] == r["gih_wr"] for r in rows)
    order = ["A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-", "D+", "D", "D-", "F"]
    ranks = [order.index(r["grade"]) for r in rows]
    assert ranks == sorted(ranks) and ranks[0] < ranks[-1]


def test_several_printings_do_not_shift_the_set_average():
    one, many = db.connect(":memory:"), db.connect(":memory:")
    sample_set(one)
    sample_set(many)
    for extra_id in (101, 102, 103):
        add_rating(many, extra_id, "Bomb", 0.66)
    grades.compute_grades(one, "TST")
    assert grades.compute_grades(many, "TST") == {"": 5}
    z = lambda c, i: c.execute("SELECT z_score FROM grades WHERE arena_id = ?", (i,)).fetchone()[0]
    assert z(one, 3) == pytest.approx(z(many, 3))
    assert z(many, 101) == pytest.approx(z(many, 1))


def test_color_pair_uses_overall_baseline():
    conn = db.connect(":memory:")
    sample_set(conn)
    add_rating(conn, 3, "Average", 0.66, colors="WU")  # in WU decks it plays like the Bomb
    assert grades.compute_grades(conn, "TST") == {"": 5, "WU": 1}
    grade = lambda i, c: conn.execute(
        "SELECT grade FROM grades WHERE arena_id = ? AND colors = ?", (i, c)).fetchone()[0]
    assert grade(3, "WU") == grade(1, "")


def test_optional_shrinkage_pulls_small_samples_toward_the_mean(monkeypatch):
    monkeypatch.setattr(config, "SHRINK_SMALL_SAMPLES", True)
    assert grades.shrink(0.70, 100, 0.55, 300) < grades.shrink(0.70, 5000, 0.55, 300) < 0.70
    conn = db.connect(":memory:")
    sample_set(conn)
    add_rating(conn, 8, "Lucky rare", 0.66, gih_count=500)
    add_rating(conn, 9, "Proven", 0.66, gih_count=20000)
    grades.compute_grades(conn, "TST")
    adj = {r["arena_id"]: r for r in conn.execute("SELECT * FROM grades WHERE colors = ''")}
    assert adj[8]["adj_wr"] < adj[9]["adj_wr"] < 0.66
    assert adj[8]["z_score"] < adj[9]["z_score"]


# --- Cache, Scryfall parsing, CLI ---------------------------------------------------

def test_old_grades_table_is_rebuilt(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE grades (expansion TEXT, format TEXT, colors TEXT, arena_id INTEGER, "
                "gih_wr REAL, z_score REAL, grade TEXT)")
    old.execute("INSERT INTO grades VALUES ('TST', 'PremierDraft', '', 1, 0.6, 1.0, 'B+')")
    old.commit()
    old.close()
    conn = db.connect(path)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(grades)")}
    assert {"adj_wr", "games"} <= columns
    assert conn.execute("SELECT COUNT(*) FROM grades").fetchone()[0] == 0


def test_freshness():
    conn = db.connect(":memory:")
    assert not db.is_fresh(conn, "k", 24)
    db.mark_synced(conn, "k")
    assert db.is_fresh(conn, "k", 24) and not db.is_fresh(conn, "k", 0)


def test_parse_double_faced_card():
    row = scryfall.parse_card({
        "arena_id": 9, "name": "Front // Back", "set": "tst", "cmc": 3.0,
        "type_line": "Creature // Land", "color_identity": ["G", "U"],
        "card_faces": [
            {"mana_cost": "{2}{G}", "oracle_text": "Front text", "colors": ["G"],
             "power": "2", "toughness": "2", "image_uris": {"normal": "http://front"}},
            {"mana_cost": "", "oracle_text": "Back text", "colors": []},
        ],
    })
    assert row["colors"] == "G" and row["color_identity"] == "UG"
    assert row["oracle_text"] == "Front text // Back text"
    assert row["mana_cost"] == "{2}{G}" and row["image_url"] == "http://front"


def test_cli_sync_grades_and_forget(tmp_path, monkeypatch, capsys):
    synced(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MIN_GIH_GAMES", 1)
    database = str(tmp_path / "cache" / "t.db")
    assert cli.main(["--db", database, "sync", "TST", "--no-scryfall"]) == 0
    assert "3 cards graded" in capsys.readouterr().out
    assert cli.main(["--db", database, "grades", "TST"]) == 0
    out = capsys.readouterr().out
    assert out.count("Filler") == 1 and "17Lands public datasets" in out
    assert out.index("Bomb") < out.index("Filler") < out.index("Azog")
    assert cli.main(["--db", database, "forget", "TST"]) == 0
    assert cli.main(["--db", database, "grades", "TST"]) == 1


# --- Pick statistics ----------------------------------------------------------------

DRAFT_CARDS = ["Bomb", "Azog, Moria's Ruin", "Filler", "White Two", "Blue One", "Red One"]

# One draft = list of (pack_number, pick_number, pick, pack contents, pool so far)
def draft_rows(draft_id, picks, win_rate=0.6):
    rows = []
    for pack_number, pick_number, pick, pack, pool in picks:
        row = {"expansion": "TST", "event_type": "PremierDraft", "draft_id": draft_id,
               "pack_number": pack_number, "pick_number": pick_number, "pick": pick,
               "user_game_win_rate_bucket": win_rate}
        for card in DRAFT_CARDS:
            row["pack_card_" + card] = pack.count(card)
            row["pool_" + card] = pool.count(card)
        rows.append(row)
    return rows


DRAFT_A = [
    (0, 0, "Bomb", ["Bomb", "Filler", "Red One", "Blue One"], []),
    (0, 1, "Blue One", ["Filler", "Blue One", "Red One"], ["Bomb"]),
    (0, 2, "Filler", ["Filler", "Red One"], ["Bomb", "Blue One"]),
    (1, 0, "White Two", ["White Two", "Red One", "Azog, Moria's Ruin"], ["Bomb", "Blue One", "Filler"]),
]
DRAFT_B = [
    (0, 0, "Red One", ["Red One", "Filler", "Filler"], []),
    (0, 1, "Filler", ["Filler", "Filler"], ["Red One"]),
]


def write_draft_file(path, drafts):
    rows = [r for i, picks in enumerate(drafts) for r in draft_rows(f"d{i}", picks)]
    with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


@pytest.mark.parametrize("chunksize", [1, 2, 3, 1000])
def test_pick_statistics_are_independent_of_chunking(tmp_path, chunksize):
    path = write_draft_file(tmp_path / "d.csv.gz", [DRAFT_A, DRAFT_B])
    stats, picks = seventeenlands.aggregate_picks(path, chunksize=chunksize)
    assert picks == 6
    assert stats["Bomb"] == {"seen_count": 1, "pick_count": 1, "ata": 1.0, "alsa": 1.0}
    # Filler: taken at picks 3 (draft A) and 2 (draft B); last seen at 3 and 2.
    assert stats["Filler"]["pick_count"] == 2 and stats["Filler"]["ata"] == 2.5
    assert stats["Filler"]["alsa"] == 2.5 and stats["Filler"]["seen_count"] == 5
    # Red One: last seen at pick 3 (A, pack 1), 1 (A, pack 2), 1 (B).
    assert stats["Red One"]["alsa"] == pytest.approx(5 / 3) and stats["Red One"]["ata"] == 1.0
    assert stats["Azog, Moria's Ruin"]["pick_count"] == 0 and stats["Azog, Moria's Ruin"]["ata"] is None


# --- Recommender --------------------------------------------------------------------

from mtghelper import evaluate, recommender  # noqa: E402


def make_set():
    """A hand-built set: mean 0, one z = one unit."""
    card = lambda name, colors, z, **kw: recommender.Card(
        name=name, colors=colors, z=z, gih_wr=0.56 + 0.03 * z, grade=grades.letter_for(z), **kw)
    cards = [
        card("Bomb", "W", 2.2, ata=1.3),
        card("White Two", "W", 0.8, ata=3.0),
        card("White Filler", "W", -0.2),
        card("Blue One", "U", 1.0, ata=2.5, pair_z={"WU": 1.6, "UB": 0.4}),
        card("Blue Filler", "U", 0.0),
        card("Red One", "R", 1.2, ata=2.0),
        card("Red Bomb", "R", 2.5, ata=1.2),
        card("Gold WU", "WU", 1.4),
        card("Gold UR", "UR", 1.4),
        card("Artifact", "", 0.3),
        card("Black Bad", "B", -1.5),
    ]
    return recommender.SetData("TST", "PremierDraft", 0.56, 0.03, {c.name: c for c in cards}, {})


def order(data, pack, pool, seen=None):
    return [s.name for s in recommender.recommend(data, pack, pool, seen).suggestions]


def test_empty_pool_ranks_by_overall_strength():
    data = make_set()
    result = recommender.recommend(data, ["Blue Filler", "Red One", "Bomb", "Artifact"], [])
    assert [s.name for s in result.suggestions] == ["Bomb", "Red One", "Artifact", "Blue Filler"]
    assert result.commitment == 0 and result.suggestions[0].score == pytest.approx(2.2)


def test_pool_colours_pull_the_ranking():
    data = make_set()
    pack = ["Red One", "Blue One", "White Two", "Artifact"]
    assert order(data, pack, [])[0] == "Red One"
    white_blue = ["Bomb", "White Two", "Blue One", "Gold WU", "White Filler", "Blue Filler"] * 2
    result = recommender.recommend(data, pack, white_blue)
    assert [s.name for s in result.suggestions][:2] == ["Blue One", "White Two"]
    assert result.leaning[0][0] == "WU" and result.leaning[0][1] > 0.5
    reasons = {s.name: s.reasons for s in result.suggestions}
    assert "fits WU" in reasons["Blue One"] and any("better in WU" in r for r in reasons["Blue One"])
    assert "off-colour for WU" in reasons["Red One"]
    assert "colourless, fits any deck" in reasons["Artifact"]


def test_a_bomb_still_wins_early_but_not_when_committed():
    data = make_set()
    pack = ["Red Bomb", "White Two"]
    early = ["Bomb", "White Two", "White Filler"]
    assert order(data, pack, early)[0] == "Red Bomb"
    late = ["Bomb", "White Two", "Blue One", "Gold WU", "White Filler", "Blue Filler"] * 4
    assert order(data, pack, late)[0] == "White Two"


def test_gold_cards_need_both_colours():
    data = make_set()
    pool = ["Bomb", "White Two", "Blue One", "Gold WU", "Blue Filler", "White Filler"] * 3
    result = recommender.recommend(data, ["Gold WU", "Gold UR"], pool)
    assert [s.name for s in result.suggestions] == ["Gold WU", "Gold UR"]
    assert "needs R outside WU" in result.suggestions[1].reasons


def test_late_good_cards_signal_an_open_colour():
    data = make_set()
    pool = ["Bomb", "Artifact"]            # one white card: red and blue are tied second colours
    pack = ["Red One", "Blue One"]
    without = recommender.recommend(data, pack, pool)
    late_red = [(6, ["Red Bomb", "Red One"]), (7, ["Red One"])]
    with_signal = recommender.recommend(data, pack, pool, late_red)
    gap = lambda r: {s.name: s.score for s in r.suggestions}["Red One"] - \
        {s.name: s.score for s in r.suggestions}["Blue One"]
    assert gap(with_signal) > gap(without)
    assert dict(with_signal.leaning)["WR"] > dict(without.leaning)["WR"]


def test_unknown_and_unrated_cards_do_not_crash():
    data = make_set()
    data.cards["Unrated"] = recommender.Card(name="Unrated", colors="G")
    result = recommender.recommend(data, ["Unrated", "Never Heard Of It", "Artifact"], ["Bomb"])
    assert result.suggestions[0].name == "Artifact"
    assert "too few games to grade" in result.suggestions[-1].reasons
    assert data.find("bomb") == "Bomb" and data.find("gold") is None and data.find("gold wu") == "Gold WU"


def test_load_set_and_replay_from_cache(tmp_path, monkeypatch, capsys):
    synced(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MIN_GIH_GAMES", 1)
    draft_source = write_draft_file(tmp_path / "draft_source.csv.gz", [DRAFT_A, DRAFT_B])
    real_download = seventeenlands.download

    def fake_download(url, dest, log=print):
        if "draft_data" in url:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(draft_source.read_bytes())
        else:
            real_download(url, dest, log)

    monkeypatch.setattr(seventeenlands, "download", fake_download)
    database = str(tmp_path / "cache" / "t.db")
    assert cli.main(["--db", database, "sync", "TST", "--no-scryfall"]) == 0
    out = capsys.readouterr().out
    assert "6 picks, 6 cards" in out

    conn = db.connect(database)
    data = recommender.load_set(conn, "TST")
    assert data.cards["Bomb"].ata == 1.0 and data.cards["Bomb"].colors == "W"
    assert data.cards["Bomb"].z > data.cards["Filler"].z > data.cards["Azog, Moria's Ruin"].z
    assert "WU" in data.cards["Bomb"].pair_z and data.name_by_id[1] == "Bomb"

    report = evaluate.replay(tmp_path / "cache" / "downloads" / "draft_data_public.TST.PremierDraft.csv.gz", data)
    assert report.drafts == 2 and report.overall.picks == 6 and report.skipped == 0
    assert report.by_pack[1].picks == 5 and report.by_pack[2].picks == 1
    assert evaluate.replay(draft_source, data, max_drafts=1).overall.picks == 4
    assert evaluate.replay(draft_source, data, min_win_rate=0.7).overall.picks == 0

    assert cli.main(["--db", database, "evaluate", "TST", "--drafts", "5"]) == 0
    assert "Replayed 2 drafts, 6 picks" in capsys.readouterr().out
    assert cli.main(["--db", database, "evaluate", "TST", "--tune"]) == 0
    out = capsys.readouterr().out
    assert out.count("<- current") == len(evaluate.TUNE_GRID) and "commit_picks" in out
    assert cli.main(["--db", database, "recommend", "TST", "--pack", "bomb; filler; azog", "--pool", "Bomb"]) == 0
    out = capsys.readouterr().out
    assert out.index("Bomb  ") < out.index("Filler  ") and "leaning" in out
    with pytest.raises(SystemExit):
        cli.main(["--db", database, "recommend", "TST", "--pack", "Nonexistent Card"])


# --- Arena log reader -----------------------------------------------------------------

from mtghelper import draftview, logreader  # noqa: E402

TS = "[UnityCrossThreadLogger]08/10/2026 17:28:10"


def bot_status(kind, request_id, pack, pick, cards, picked, status="PickNext", event="QuickDraft_TST_20261008"):
    """A Quick Draft response in the shape of a real Arena log (two lines)."""
    payload = json.dumps({"Result": "Success", "EventName": event, "DraftStatus": status,
                          "PackNumber": pack, "PickNumber": pick, "NumCardsToPick": 1,
                          "DraftPack": [str(c) for c in cards], "PackStyles": [],
                          "PickedCards": [str(c) for c in picked], "PickedStyles": []})
    body = json.dumps({"CurrentModule": "BotDraft", "Payload": payload, "DTO_InventoryInfo": {"Gold": 1}})
    return [TS, f"<== {kind}({request_id})", body]


def bot_pick(request_id, pack, pick, card, event="QuickDraft_TST_20261008"):
    request = json.dumps({"EventName": event, "PickInfo": {"EventName": event, "CardIds": [str(card)],
                                                           "PackNumber": pack, "PickNumber": pick}})
    return [f'[UnityCrossThreadLogger]==> BotDraftDraftPick {json.dumps({"id": request_id, "request": request})}']


def quick_draft_log():
    """Pack 1 with three picks, then pack 2 with one, then completion. Ids follow CARD_LIST."""
    join = json.dumps({"id": "j", "request": json.dumps({"EventName": "QuickDraft_TST_20261008"})})
    lines = ["noise line", f"[UnityCrossThreadLogger]==> EventJoin {join}",
             "[UnityCrossThreadLogger]==> BotDraftDraftStatus {\"id\":\"s\",\"request\":\"{}\"}"]
    lines += bot_status("BotDraftDraftStatus", "s", 0, 0, [3, 1, 2], [])
    lines += bot_pick("p1", 0, 0, 1)
    lines += bot_status("BotDraftDraftPick", "p1", 0, 1, [3, 2], [1])
    lines += bot_pick("p2", 0, 1, 3)
    lines += bot_status("BotDraftDraftPick", "p2", 0, 2, [2], [1, 3])
    lines += bot_pick("p3", 0, 2, 2)
    lines += bot_status("BotDraftDraftPick", "p3", 1, 0, [4, 9], [1, 3, 2])
    lines += bot_pick("p4", 1, 0, 9)
    lines += bot_status("BotDraftDraftPick", "p4", 1, 1, [], [1, 3, 2, 9], status="Completed")
    return "\r\n".join(lines) + "\r\n"


def test_quick_draft_events_from_log_lines(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(quick_draft_log().encode("utf-8"))
    events = list(logreader.read_events(path))
    kinds = [type(e).__name__ for e in events]
    assert kinds == ["PackShown", "CardPicked"] * 4 + ["DraftCompleted"]
    first = events[0]
    assert (first.event, first.pack, first.pick, first.cards, first.pool) == ("QuickDraft_TST_20261008", 1, 1, [3, 1, 2], [])
    assert (events[6].pack, events[6].pick, events[6].pool) == (2, 1, [1, 3, 2])
    assert (events[7].pack, events[7].pick, events[7].cards) == (2, 1, [9])
    assert events[-1].pool == [1, 3, 2, 9]
    assert logreader.set_and_format(first.event) == ("TST", "QuickDraft")
    assert logreader.set_and_format("PremierDraft_FRA_20261001") == ("FRA", "PremierDraft")
    assert logreader.set_and_format("Constructed_BestOf3") == (None, None)


def test_draft_state_tracks_pool_and_the_current_round(tmp_path):
    path = tmp_path / "Player.log"
    path.write_bytes(quick_draft_log().encode("utf-8"))
    state = logreader.DraftState()
    seen = []
    for event in logreader.read_events(path):
        state.apply(event)
        if isinstance(event, logreader.PackShown):
            seen.append((event.pack, event.pick, list(state.pool), [p for p, _ in state.round_packs]))
    assert seen == [(1, 1, [], []), (1, 2, [1], [1]), (1, 3, [1, 3], [1, 2]), (2, 1, [1, 3, 2], [])]
    assert state.pool == [1, 3, 2, 9] and state.current is None


def test_premier_draft_messages():
    parser = logreader.Parser()
    join = json.dumps({"id": "j", "request": json.dumps({"EventName": "PremierDraft_TST_20261001"})})
    assert parser.feed(f"[UnityCrossThreadLogger]==> EventJoin {join}") == []
    shown = parser.feed('[UnityCrossThreadLogger]Draft.Notify {"draftId":"d","SelfPick":1,"SelfPack":1,"PackCards":"3,1,2"}')
    assert len(shown) == 1 and shown[0].cards == [3, 1, 2] and shown[0].event == "PremierDraft_TST_20261001"
    request = json.dumps({"id": "x", "request": json.dumps({"DraftId": "d", "GrpIds": [1], "Pack": 1, "Pick": 1})})
    picked = parser.feed(f"[UnityCrossThreadLogger]==> EventPlayerDraftMakePick {request}")
    assert (picked[0].pack, picked[0].pick, picked[0].cards) == (1, 1, [1])
    nxt = parser.feed('Draft.Notify {"draftId":"d","SelfPick":2,"SelfPack":1,"PackCards":"5,6"}')
    assert nxt[0].pool == [1]


def test_blank_lines_between_header_and_body_are_tolerated(tmp_path):
    # Text-mode writes on Windows turn "\r\n" into "\r\r\n", which reads back
    # with an empty line between "<== ...(id)" and its JSON body.
    path = tmp_path / "Player.log"
    path.write_bytes(quick_draft_log().replace("\r\n", "\r\r\n").encode("utf-8"))
    kinds = [type(e).__name__ for e in logreader.read_events(path)]
    assert kinds == ["PackShown", "CardPicked"] * 4 + ["DraftCompleted"]


def test_parser_ignores_broken_and_unrelated_lines():
    parser = logreader.Parser()
    assert parser.feed("<== BotDraftDraftPick(abc)") == []
    assert parser.feed("{not json") == []
    assert parser.feed("<== GraphGetGraphState(x)") == []
    assert parser.feed('{"CurrentModule":"x","Payload":"{}"}') == []
    assert parser.feed('[UnityCrossThreadLogger]==> BotDraftDraftPick {"id":"1","request":"oops"}') == []


def draft_cache(tmp_path, monkeypatch):
    """A cache with TST ratings from the tiny public data file used above."""
    synced(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "MIN_GIH_GAMES", 1)
    monkeypatch.setattr(draftview, "_scryfall_name", lambda conn, arena_id: None)
    database = tmp_path / "cache" / "t.db"
    assert cli.main(["--db", str(database), "sync", "TST", "--no-scryfall"]) == 0
    return db.connect(database)


def test_replay_ranks_each_pack_and_scores_the_picks(tmp_path, monkeypatch, capsys):
    conn = draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))
    lines = []
    session = draftview.replay(conn, log, out=lines.append)
    text = "\n".join(lines)
    assert "TST Quick Draft - pack 1, pick 1" in text
    assert "Using TST PremierDraft ratings (no QuickDraft data)." in text
    first_pack = text.split("pack 1, pick 2")[0]
    assert first_pack.index("Bomb") < first_pack.index("Filler") < first_pack.index("Azog")
    assert "-> you took Bomb (the top choice)" in text
    assert "-> you took #9 (basic land?)" in text  # id 9 is unknown: shown by id
    assert session.agreement[:3] == [0, 0, 0]
    assert "Draft complete: you took the top choice 3 of 4 times" in text


def test_sets_without_public_data_show_names_only(tmp_path, monkeypatch):
    conn = draft_cache(tmp_path, monkeypatch)
    conn.execute("DELETE FROM datasets")  # e.g. ratings left over from the old website endpoint
    conn.commit()
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))
    lines = []
    draftview.replay(conn, log, out=lines.append)
    text = "\n".join(lines)
    assert "No 17Lands public data for TST yet" in text and "Grade" not in text
    assert "   Bomb" in text and "Draft complete." in text


def test_follow_picks_up_a_draft_in_progress_and_new_lines(tmp_path, monkeypatch):
    conn = draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    full = quick_draft_log().split("\r\n")
    cut = full.index(next(l for l in full if '"p2"' in l and "==>" in l))
    log.write_bytes(("\r\n".join(full[:cut]) + "\r\n").encode("utf-8"))  # waiting on pick 2
    lines = []
    ticks = {"n": 0}

    def stop():
        ticks["n"] += 1
        if ticks["n"] == 3:  # Arena writes the rest of the draft
            with open(log, "ab") as fh:
                fh.write("\r\n".join(full[cut:]).encode("utf-8"))
        return ticks["n"] > 8

    monkeypatch.setattr(logreader.time, "sleep", lambda s: None)
    draftview.follow(conn, log, out=lines.append, stop=stop)
    text = "\n".join(lines)
    assert text.count("pack 1, pick 1") == 0           # already decided before we started
    assert text.index("Watching") < text.index("pack 1, pick 2") < text.index("you took Filler")
    assert "pack 2, pick 1" in text and "Draft complete" in text


def test_cli_draft_command(tmp_path, monkeypatch, capsys):
    draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))
    database = str(tmp_path / "cache" / "t.db")
    assert cli.main(["--db", database, "draft", "--replay", "--log", str(log), "--top", "2"]) == 0
    out = capsys.readouterr().out
    assert "... 1 more" in out and "you took Bomb" in out
    assert cli.main(["--db", database, "draft", "--log", str(tmp_path / "missing.log")]) == 1
