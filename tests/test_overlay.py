"""Overlay tests, run without a screen (Qt's offscreen platform)."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from mtghelper import draftview, overlay  # noqa: E402


@pytest.fixture(scope="module")
def app():
    application = QApplication.instance() or QApplication([])
    yield application
    # Destroy every window before Python tears Qt down, or the process can crash on exit.
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    application.processEvents()


@pytest.fixture(autouse=True)
def _delete_windows(app):
    yield
    for widget in QApplication.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    app.processEvents()


@pytest.fixture
def window(app, tmp_path):
    # A private settings file, so tests never touch the real overlay settings.
    w = overlay.Overlay(rows=2, settings=QSettings(str(tmp_path / "overlay.ini"), QSettings.IniFormat))
    yield w


def pack(rated=True, n=3):
    rows = [draftview.Row(i, f"Card {i}", "A-" if rated else None, 0.6 if rated else None,
                          "WU" if i == 1 else "", ["A- overall", "fits WU"] if rated else [])
            for i in range(1, n + 1)]
    pairs = [("WU", 0.7), ("WB", 0.2), ("UB", 0.06), ("RG", 0.04)] if rated else []
    return draftview.PackView("TST Quick Draft", 1, 3, 2, "WU 70%, WB 20%", rated, rows, pairs)


def test_shows_a_ranked_pack(window):
    window.apply(draftview.Update("pack", pack=pack()))
    assert "pack 1, pick 3" in window.title.text()
    assert "Pool 2 cards" in window.sub.text() and not window.bar.isHidden()
    assert window.bar.pairs[0] == ("WU", 0.7)
    text = window.rows.text()
    assert "<b>Card 1</b>" in text and "Card 2" in text and "Card 3" not in text
    assert "+1 more" in text and "fits WU" in text and "A-" in text


def test_unrated_sets_and_pick_feedback(window):
    window.apply(draftview.Update("note", "No 17Lands public data for TST yet"))
    window.apply(draftview.Update("pack", pack=pack(rated=False)))
    assert "card names only" in window.sub.text() and not window.note.isHidden()
    assert "<b>" not in window.rows.text()
    window.apply(draftview.Update("pick", "you took Card 1 (the top choice)", rank=0))
    assert "you took Card 1" in window.pick.text() and "#6fd39a" in window.pick.text()
    window.apply(draftview.Update("done", "Draft complete: 3 of 4"))
    assert window.sub.text() == "Draft complete" and window.rows.text() == ""


def test_saved_row_count_is_used_unless_rows_is_given(app, tmp_path):
    settings = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    settings.setValue("rows", 14)
    assert overlay.Overlay(settings=settings).max_rows == 14
    assert overlay.Overlay(rows=3, settings=settings).max_rows == 3
    fresh = QSettings(str(tmp_path / "fresh.ini"), QSettings.IniFormat)
    assert overlay.Overlay(settings=fresh).max_rows == overlay.ALL


def test_row_count_setting_is_applied_and_saved(window):
    window.apply(draftview.Update("pack", pack=pack(n=10)))
    window._set_rows(5)
    assert window.rows.text().count("Card ") == 5 and "+5 more" in window.rows.text()
    assert int(window.settings.value("rows")) == 5


def test_worker_replays_a_log_into_the_window(app, window, tmp_path, monkeypatch):
    from test_data_layer import draft_cache, quick_draft_log  # same fixtures as the CLI tests

    draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))
    received = []
    bridge = overlay.Bridge()
    bridge.update.connect(received.append)
    bridge.update.connect(window.apply)
    worker = overlay.LogWorker(tmp_path / "cache" / "t.db", log, bridge, demo_delay=0)
    worker.start()
    deadline = time.time() + 10
    while (worker.is_alive() or len(received) < 10) and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    kinds = [u.kind for u in received]
    assert kinds.count("pack") == 4 and kinds.count("pick") == 4 and kinds[-1] == "done"
    assert "Draft complete" in window.sub.text()


def test_window_is_opaque_by_default(window):
    assert window.windowOpacity() == 1.0
    for theme in overlay.THEMES.values():  # no theme has a see-through background
        assert "rgba" not in theme["bg"]


def test_minimise_and_close_buttons(window):
    closed = []
    window._quit = lambda: closed.append(True)
    window.close_button.click()
    assert closed == [True]
    window.show()
    window.minimise_button.click()
    assert window.windowState() & overlay.Qt.WindowMinimized
    assert window.minimise_button.toolTip() == "Minimise" and window.close_button.toolTip() == "Close"
    assert window.windowType() == overlay.Qt.Window  # not Qt.Tool: has a taskbar entry to restore from


def _mouse(kind, window, global_pos, buttons):
    from PySide6.QtCore import QEvent, QPointF
    from PySide6.QtGui import QMouseEvent
    event_type = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                  "release": QEvent.MouseButtonRelease}[kind]
    local = QPointF(global_pos) - QPointF(window.pos())
    button = overlay.Qt.LeftButton if kind != "move" else overlay.Qt.NoButton
    return QMouseEvent(event_type, local, QPointF(global_pos), button, buttons, overlay.Qt.NoModifier)


def _drag(window, by):
    from PySide6.QtCore import QPoint
    start = window.pos() + QPoint(20, 10)
    window.mousePressEvent(_mouse("press", window, start, overlay.Qt.LeftButton))
    window.mouseMoveEvent(_mouse("move", window, start + by, overlay.Qt.LeftButton))
    window.mouseReleaseEvent(_mouse("release", window, start + by, overlay.Qt.NoButton))


def test_starts_middle_left_and_stays_centred_as_content_grows(window):
    window.show()
    area = window.screen().availableGeometry()
    assert window.x() == area.left() + overlay.LEFT_MARGIN
    assert abs(window.geometry().center().y() - area.center().y()) <= 1
    window.apply(draftview.Update("pack", pack=pack(n=10)))
    assert abs(window.geometry().center().y() - area.center().y()) <= 1


def test_locked_by_default_and_movable_once_unlocked(window):
    from PySide6.QtCore import QPoint
    window.show()
    home = window.pos()
    _drag(window, QPoint(150, 60))
    assert window.pos() == home and window.locked
    window.lock_button.click()  # unlock
    assert not window.locked and window.lock_button.toolTip() == "Lock position"
    _drag(window, QPoint(150, 60))
    assert window.pos() == home + QPoint(150, 60)
    moved_to = window.pos()
    window.lock_button.click()  # lock again where it is
    assert window.locked
    window.apply(draftview.Update("pack", pack=pack(n=10)))  # no snapping back
    assert window.pos() == moved_to


def test_position_is_not_remembered_between_launches(app, tmp_path):
    from PySide6.QtCore import QPoint
    settings = QSettings(str(tmp_path / "p.ini"), QSettings.IniFormat)
    first = overlay.Overlay(settings=settings)
    first.show()
    first.lock_button.click()
    _drag(first, QPoint(200, 0))
    first.close()
    second = overlay.Overlay(settings=settings)
    second.show()
    assert second.x() == second.screen().availableGeometry().left() + overlay.LEFT_MARGIN
    second.close()


def test_every_card_is_listed_by_default(app, tmp_path):
    w = overlay.Overlay(settings=QSettings(str(tmp_path / "all.ini"), QSettings.IniFormat))
    w.apply(draftview.Update("pack", pack=pack(n=14)))
    assert w.rows.text().count("Card ") == 14 and "more" not in w.rows.text()


def test_reasons_are_dropped_when_the_pack_would_not_fit_the_screen(app, tmp_path, monkeypatch):
    w = overlay.Overlay(settings=QSettings(str(tmp_path / "fit.ini"), QSettings.IniFormat))
    w.apply(draftview.Update("pack", pack=pack(n=14)))
    assert "fits WU" in w.rows.text()  # fits on the test screen
    monkeypatch.setattr(overlay, "SCREEN_MARGIN", 10_000)  # pretend the screen is tiny
    w.apply(draftview.Update("pack", pack=pack(n=14)))
    assert "fits WU" not in w.rows.text() and w.rows.text().count("Card ") == 14


def test_leaning_bar_segments_and_visibility(window):
    window.apply(draftview.Update("pack", pack=pack()))
    segments = window.bar.segments()
    assert [p for p, _ in segments] == ["WU", "WB", "UB", None]
    assert abs(sum(w for _, w in segments) - 1) < 1e-9
    window.grab()  # paints the bar
    window.apply(draftview.Update("pack", pack=pack(rated=False)))
    assert window.bar.isHidden()
    first_pick = pack()
    first_pick.pairs, first_pick.pool_size = [], 0
    window.apply(draftview.Update("pack", pack=first_pick))
    assert window.bar.isHidden() and "First pick" in window.sub.text()


@pytest.mark.parametrize("name", list(overlay.THEMES))
def test_every_theme_renders(app, tmp_path, name):
    w = overlay.Overlay(settings=QSettings(str(tmp_path / f"{name}.ini"), QSettings.IniFormat), theme=name)
    w.apply(draftview.Update("pack", pack=pack(n=6)))
    w.apply(draftview.Update("pick", "you took Card 1 (the top choice)", rank=0))
    assert not w.grab().isNull()
    assert overlay.THEMES[name]["good"] in w.pick.text()


def test_theme_choice_is_saved_and_pool_colours_tint_the_background(window):
    window.set_theme("Pool colours")
    assert window.settings.value("theme") == "Pool colours"
    window.apply(draftview.Update("pack", pack=pack()))
    assert "qlineargradient" in window.styleSheet()  # tinted by the WU leaning
    window.set_theme("Midnight")
    assert "qlineargradient" not in window.styleSheet()
    window.set_theme("No such theme")
    assert window.theme_name == "Midnight"


# --- Desktop shortcut, icon, automatic updates ---------------------------------------

from mtghelper import config, shortcut  # noqa: E402


def test_project_root_and_default_cache_location():
    assert (config.PROJECT_ROOT / "pyproject.toml").exists()
    assert config.DEFAULT_DB_PATH.parent.name == "data" or "MTGHELPER_DB" in os.environ


def test_shortcut_script_quotes_paths_safely():
    from pathlib import Path
    script = shortcut.shortcut_script("MTG Draft Helper", Path(r"C:\Users\o'neil\venv\pythonw.exe"),
                                      "-m mtghelper.cli overlay", Path(r"C:\Users\o'neil\proj"),
                                      Path(r"C:\x\icon.ico"))
    assert r"'C:\Users\o''neil\venv\pythonw.exe'" in script
    assert "$link.Arguments = '-m mtghelper.cli overlay'" in script
    assert r"$link.IconLocation = 'C:\x\icon.ico,0'" in script and "$link.Save()" in script


def test_create_desktop_shortcut_runs_powershell(app, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_DB_PATH", tmp_path / "data" / "mtghelper.db")
    calls = []

    class Done:
        returncode, stdout, stderr = 0, r"C:\Users\me\Desktop\MTG Draft Helper.lnk" + "\n", ""

    def fake_run(command, **kwargs):
        calls.append(command)
        return Done()

    path = shortcut.create_desktop_shortcut(run=fake_run)
    assert path.endswith("MTG Draft Helper.lnk")
    assert calls[0][0] == "powershell" and "mtghelper.cli overlay" in calls[0][-1]
    assert (tmp_path / "data" / "mtghelper.ico").exists()


def test_icon_draws_at_every_size(app):
    for size in (16, 64, 256):
        assert overlay.icon_pixmap(size).width() == size
    assert not overlay.app_icon().isNull()


def test_set_data_is_updated_once_when_a_draft_starts(app, tmp_path, monkeypatch):
    from test_data_layer import draft_cache, quick_draft_log

    draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))
    updated = []
    monkeypatch.setattr(overlay.updater, "update_set",
                        lambda conn, code, **kw: (updated.append(code), kw["log"]("Grades: 3 cards graded")))
    notes = []
    bridge = overlay.Bridge()
    bridge.update.connect(lambda u: notes.append(u.text) if u.kind == "note" else None)
    worker = overlay.LogWorker(tmp_path / "cache" / "t.db", log, bridge, demo_delay=0)
    worker.run()  # in this thread: deterministic
    app.processEvents()
    assert updated == ["TST"]
    assert notes[:3] == ["Checking 17Lands data for TST...", "Updating TST: Grades: 3 cards graded", ""]

    updated.clear()
    worker = overlay.LogWorker(tmp_path / "cache" / "t.db", log, bridge, demo_delay=0, sync=False)
    worker.run()
    assert updated == []


def test_a_failed_update_does_not_stop_the_overlay(app, tmp_path, monkeypatch):
    from test_data_layer import draft_cache, quick_draft_log

    draft_cache(tmp_path, monkeypatch)
    log = tmp_path / "Player.log"
    log.write_bytes(quick_draft_log().encode("utf-8"))

    def broken(*args, **kwargs):
        raise OSError("no internet")

    monkeypatch.setattr(overlay.updater, "update_set", broken)
    kinds, notes = [], []
    bridge = overlay.Bridge()
    bridge.update.connect(lambda u: (kinds.append(u.kind), notes.append(u.text)))
    overlay.LogWorker(tmp_path / "cache" / "t.db", log, bridge, demo_delay=0).run()
    app.processEvents()
    assert any("no internet" in n for n in notes)
    assert kinds.count("pack") == 4 and kinds[-1] == "done"


def test_crashes_are_written_to_a_file(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise ValueError("kaboom")

    monkeypatch.setattr(overlay, "main", boom)
    with pytest.raises(ValueError):
        overlay.run_safely(tmp_path / "mtghelper.db", tmp_path / "Player.log")
    assert "kaboom" in (tmp_path / "overlay-error.log").read_text(encoding="utf-8")


# --- Deck builder ------------------------------------------------------------------

from mtghelper import deckbuilder, recommender  # noqa: E402


def deck_set():
    """A set with enough cards for a white-black deck and a weaker blue-red one."""
    cards = {}

    def add(name, colors, z, cmc, kind="Creature — Test", cost=None, pair_z=None):
        cost = cost or "".join("{" + c + "}" for c in colors) or "{2}"
        cards[name] = recommender.Card(name=name, colors=colors, z=z, grade=recommender.grades.letter_for(z),
                                       cmc=cmc, type_line=kind, mana_cost=cost, pair_z=pair_z or {})
    for i in range(10):
        add(f"White {i}", "W", 0.6 - i * 0.05, 1 + i % 5)
        add(f"Black {i}", "B", 0.5 - i * 0.05, 1 + i % 4)
    for i in range(4):
        add(f"Black spell {i}", "B", 0.4, 2, kind="Instant", cost="{1}{B}")
    for i in range(6):
        add(f"Blue {i}", "U", -0.2, 3)
        add(f"Red {i}", "R", -0.3, 3)
    add("Red bomb", "R", 2.2, 4)
    add("Gold WB", "WB", 1.5, 3, cost="{W}{B}{1}", pair_z={"WB": 2.0})
    add("Hybrid", "WB", 0.3, 2, cost="{W/B}{W/B}")
    add("Artifact", "", 0.2, 2, kind="Artifact Creature — Golem")
    add("Dual Land", "", 0.1, 0, kind="Land")
    return recommender.SetData("TST", "PremierDraft", 0.56, 0.03, cards, {}), list(cards) + ["Plains"]


def test_deck_picks_the_strongest_pair_with_23_spells_and_17_lands():
    data, pool = deck_set()
    suggestion = deckbuilder.suggest(data, pool)
    deck = suggestion.best
    assert deck.pair == "WB"
    assert len(deck.spells) == 23 and sum(deck.lands.values()) in (16, 17)
    names = {c.name for c in deck.spells}
    assert "Gold WB" in names and "Artifact" in names
    assert not names & {"Red bomb", "Blue 0", "Dual Land", "Plains"}
    assert set(deck.lands) == {"Plains", "Swamp"}
    assert [c.cmc for c in deck.spells] == sorted(c.cmc for c in deck.spells)
    assert any("Red bomb" in s for s in suggestion.splashes)
    assert suggestion.alternatives and suggestion.alternatives[0].score <= deck.score


def test_creature_minimum_beats_a_slightly_better_spell():
    data, pool = deck_set()
    for i in range(10):  # instants a little better than the weakest creatures
        data.cards[f"Slightly better instant {i}"] = recommender.Card(
            name=f"Slightly better instant {i}", colors="W", z=0.3, grade="C+", cmc=2, type_line="Instant", mana_cost="{1}{W}")
    pool += [f"Slightly better instant {i}" for i in range(10)]
    deck = deckbuilder.build_for_pair(data, pool, "WB")
    assert deck.creatures >= 14


def test_lands_follow_the_mana_symbols():
    data, _ = deck_set()
    spells = [deckbuilder.DeckCard("White 0", 1, "B", "W", 2, True)] * 3 + \
             [deckbuilder.DeckCard("Black 0", 1, "B", "B", 2, True)]
    lands = deckbuilder.split_lands(spells, data.cards, "WB", 16)
    assert lands == {"Plains": 12, "Swamp": 4}
    assert deckbuilder.colour_symbols(data.cards["Hybrid"]) == {"W": 1.0, "U": 0, "B": 1.0, "R": 0, "G": 0}


def test_a_thin_pair_is_reported():
    data, pool = deck_set()
    deck = deckbuilder.build_for_pair(data, pool, "UR")
    assert len(deck.spells) < 23 and any("Only" in n for n in deck.notes)
    assert len(deck.spells) + sum(deck.lands.values()) == 40


def test_overlay_shows_the_deck_after_the_draft(window):
    data, pool = deck_set()
    window.apply(draftview.Update("deck", deck=deckbuilder.suggest(data, pool)))
    assert "Suggested deck  -  WB" in window.title.text()
    assert "Plains" in window.sub.text() and "creatures" in window.sub.text()
    assert "Gold WB" in window.rows.text() and "1 mana" in window.rows.text()
    assert "Splash idea" in window.note.text() and "Next best" in window.note.text()
    assert not window.note.isHidden()


def test_cli_deck_from_a_pool(tmp_path, monkeypatch, capsys):
    from test_data_layer import draft_cache
    from mtghelper import cli

    draft_cache(tmp_path, monkeypatch)
    database = str(tmp_path / "cache" / "t.db")
    assert cli.main(["--db", database, "deck", "--set", "TST", "--pool", "Bomb; Filler; Azog"]) == 0
    out = capsys.readouterr().out
    assert "Suggested deck:" in out and "Only" in out
    assert cli.main(["--db", database, "deck", "--pool", "Bomb"]) == 1


def test_deck_view_goes_compact_on_a_short_screen(window, monkeypatch):
    data, pool = deck_set()
    monkeypatch.setattr(overlay, "SCREEN_MARGIN", 10_000)
    window.apply(draftview.Update("deck", deck=deckbuilder.suggest(data, pool)))
    text = window.rows.text()
    assert "<b>1:</b>" in text and " mana</span>" not in text and window.rows.wordWrap()
    window.apply(draftview.Update("pack", pack=pack()))
    assert not window.rows.wordWrap()


def test_window_grows_to_show_the_whole_deck(window):
    data, pool = deck_set()
    window.apply(draftview.Update("deck", deck=deckbuilder.suggest(data, pool)))
    needed = window.layout().totalHeightForWidth(window.width())
    assert window.height() >= needed
    assert window.rows.height() >= window.rows.heightForWidth(window.rows.width())
