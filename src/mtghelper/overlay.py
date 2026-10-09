"""Draft overlay: a small always-on-top window that ranks the pack you are shown.

It shows the same information as `mtghelper draft`, in a panel that floats
over Arena: every card of the pack with its grade, colours and the reason for
its place, plus a bar showing which colour pairs the pool is leaning towards.

The buttons at the top right unlock it for moving, minimise it to the taskbar,
or close it. It always starts at the middle of the left screen edge, locked in
place. Right-click for options: how many cards to list, colour theme, opacity
(these three are remembered).

The panel does not try to sit on top of each card: Arena's card layout changes
with resolution and window size, so cards are listed by name instead.

Threading: a background thread follows Player.log and runs the recommender;
the window only receives finished `Update`s through a Qt signal.
"""

from __future__ import annotations

import html
import sys
import threading
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import (QAction, QActionGroup, QColor, QFont, QGuiApplication, QIcon, QLinearGradient,
                           QPainter, QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
                               QVBoxLayout, QWidget)

from . import db, draftview, logreader, updater

LEFT_MARGIN = 16      # pixels between the screen edge and the window
SCREEN_MARGIN = 40    # keep this much space free above and below the window
ALL = 0               # "rows" value meaning: list every card of the pack
COLOR_HEX = {"W": "#f3ecd2", "U": "#4fa3e0", "B": "#a58fbd", "R": "#e8674f", "G": "#4fbf73"}

# --- Themes -------------------------------------------------------------------------
# bg is any Qt style sheet background (a colour or a gradient). "tint" themes
# blend the colours of the pair the pool is leaning towards into the background.
THEMES: dict[str, dict] = {
    "Midnight": dict(bg="rgb(18, 20, 26)", border="rgba(255,255,255,40)", text="#e9e9ee",
                     muted="#a4a7b3", title="#ffffff", good="#6fd39a", mid="#e7c46a",
                     hover="rgba(255,255,255,35)", track="#2b2f38", font=13),
    "Parchment": dict(bg="qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #faf6ec, stop:1 #efe7d4)",
                      border="#cdbf9f", text="#2a2620", muted="#6e6556", title="#1c1813",
                      good="#1e8a4c", mid="#9a6b00", hover="rgba(0,0,0,25)", track="#ddd3bd", font=13,
                      # the usual cream white pip disappears on a light background
                      pips={"W": "#b39a4f"}),
    "Arena gold": dict(bg="qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #241a10, stop:1 #120d08)",
                       border="#b8924a", text="#efe6d6", muted="#b3a48a", title="#e6c27a",
                       good="#8fd37a", mid="#e6c27a", hover="rgba(230,194,122,40)", track="#3a2c1c", font=13),
    "Slate": dict(bg="qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1f2a3c, stop:1 #141b27)",
                  border="#3c5a80", text="#e6edf6", muted="#93a4bb", title="#ffffff",
                  good="#6fd3c0", mid="#f0c36a", hover="rgba(255,255,255,35)", track="#2a3850", font=13),
    "Pool colours": dict(bg="rgb(18, 20, 26)", border="rgba(255,255,255,50)", text="#eeeef2",
                         muted="#b0b3be", title="#ffffff", good="#6fd39a", mid="#e7c46a",
                         hover="rgba(255,255,255,35)", track="#2b2f38", font=13, tint=True),
    "High contrast": dict(bg="#000000", border="#ffffff", text="#ffffff", muted="#d0d0d0",
                          title="#ffe14d", good="#3dff8a", mid="#ffe14d", hover="rgba(255,255,255,60)",
                          track="#333333", font=15),
}
DEFAULT_THEME = "Midnight"


def stylesheet(theme: dict, bg: str | None = None) -> str:
    t = theme
    return f"""
#panel {{ background: {bg or t['bg']}; border: 1px solid {t['border']}; border-radius: 10px; }}
QLabel {{ color: {t['text']}; font-family: "Segoe UI", sans-serif; font-size: {t['font']}px; }}
#title {{ color: {t['title']}; font-size: {t['font'] + 1}px; font-weight: 600; }}
#sub, #note {{ color: {t['muted']}; font-size: {t['font'] - 1}px; }}
#pick {{ font-size: {t['font'] - 1}px; }}
QPushButton#lockbtn, QPushButton#minbtn, QPushButton#closebtn {{ color: {t['muted']};
    background: transparent; border: none; border-radius: 4px; font-size: 14px;
    min-width: 26px; max-width: 26px; min-height: 22px; }}
QPushButton#lockbtn:hover, QPushButton#minbtn:hover {{ color: {t['title']}; background: {t['hover']}; }}
QPushButton#lockbtn:checked {{ color: {t['mid']}; }}
QPushButton#closebtn:hover {{ color: white; background: #c42b1c; }}
"""


def mix(color: str, other: str, amount: float) -> QColor:
    """`amount` of `color` blended into `other`."""
    a, b = QColor(color), QColor(other)
    return QColor(*(round(getattr(a, ch)() * amount + getattr(b, ch)() * (1 - amount))
                    for ch in ("red", "green", "blue")))


def tinted_background(pair: str | None) -> str | None:
    """Background for the "Pool colours" theme: the leaning pair over a dark base."""
    if not pair or len(pair) != 2:
        return None
    first, second = (mix(COLOR_HEX[c], "#12141a", 0.38).name() for c in pair)
    return f"qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {first}, stop:1 {second})"


def icon_pixmap(size: int) -> QPixmap:
    """The app icon: a green "A+" grade chip with the five mana colours under it."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#12141a"))
    p.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 12 * s, 12 * s)
    p.setBrush(QColor("#2fa765"))
    p.drawRoundedRect(QRectF(10 * s, 9 * s, 44 * s, 30 * s), 6 * s, 6 * s)
    font = QFont("Segoe UI")
    font.setBold(True)
    font.setPixelSize(max(int(22 * s), 6))
    p.setFont(font)
    p.setPen(QColor("white"))
    p.drawText(QRectF(10 * s, 9 * s, 44 * s, 30 * s), Qt.AlignCenter, "A+")
    p.setPen(Qt.NoPen)
    for i, c in enumerate("WUBRG"):
        p.setBrush(QColor(COLOR_HEX[c]))
        p.drawEllipse(QRectF((9 + i * 10) * s, 45 * s, 7 * s, 7 * s))
    p.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(icon_pixmap(size))
    return icon


def save_icon(path: Path | str) -> bool:
    """Write the icon as a Windows .ico file (for the desktop shortcut)."""
    return icon_pixmap(256).scaled(QSize(256, 256)).save(str(path), "ICO")


def grade_color(grade: str | None) -> str:
    if not grade:
        return "#6b6f7b"
    return {"A": "#2fa765", "B": "#3c8fc6", "C": "#8a8f9c", "D": "#c97a3a"}.get(grade[0], "#c0453a")


def row_html(row: draftview.Row, best: bool, theme: dict, reasons: bool = True) -> str:
    grade = html.escape(row.grade or "-")
    chip = (f'<span style="background:{grade_color(row.grade)};color:white;'
            f'font-weight:600;">&nbsp;{grade:<2}&nbsp;</span>')
    colours = {**COLOR_HEX, **theme.get("pips", {})}
    pips = "".join(f'<span style="color:{colours[c]};">&#9679;</span>' for c in row.colors) or \
        f'<span style="color:{theme["muted"]};">&#9675;</span>'
    name = html.escape(row.name)
    name = f"<b>{name}</b>" if best else name
    why = html.escape("; ".join(row.reasons[1:])) if reasons else ""  # reasons[0] is the grade
    if why:
        why = (f'<br><span style="color:{theme["muted"]};font-size:{theme["font"] - 2}px;">'
               f'&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;{why}</span>')
    return f"{row.rank:>2}. {chip} {pips} {name}{why}"


class LeaningBar(QWidget):
    """A horizontal bar split between the colour pairs the pool leans towards."""

    SHOWN = 3  # pairs drawn individually; the rest are lumped together

    def __init__(self):
        super().__init__()
        self.pairs: list[tuple[str, float]] = []
        self.track = QColor("#2b2f38")
        self.setFixedHeight(18)

    def set_pairs(self, pairs: list[tuple[str, float]]) -> None:
        self.pairs = list(pairs)
        self.setVisible(bool(self.pairs))
        self.update()

    def segments(self) -> list[tuple[str | None, float]]:
        """(pair, share) for the bar; None is the "everything else" segment."""
        top = [(p, w) for p, w in self.pairs[: self.SHOWN] if w >= 0.03]
        rest = 1.0 - sum(w for _, w in top)
        return top + ([(None, rest)] if rest > 0.005 else [])

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        width, height = self.width(), self.height()
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.track)
        painter.drawRoundedRect(QRectF(0, 0, width, height), 4, 4)
        x = 0.0
        font = painter.font()
        font.setPixelSize(11)
        font.setBold(True)
        painter.setFont(font)
        for pair, share in self.segments():
            w = width * share
            rect = QRectF(x, 0, w, height)
            if pair is not None:
                gradient = QLinearGradient(rect.topLeft(), rect.topRight())
                first, second = (mix(COLOR_HEX[c], "#20232b", 0.75) for c in pair)
                gradient.setColorAt(0, first)
                gradient.setColorAt(1, second)
                painter.setBrush(gradient)
                painter.setPen(Qt.NoPen)
                painter.drawRoundedRect(rect.adjusted(0.5, 0, -0.5, 0), 4, 4)
                label = f"{pair} {share:.0%}" if w > 62 else pair if w > 26 else ""
                if label:
                    light = (first.lightness() + second.lightness()) / 2 > 150
                    painter.setPen(QColor("#15171c") if light else QColor("white"))
                    painter.drawText(rect.adjusted(6, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, label)
            x += w
        painter.end()


class Bridge(QObject):
    """Carries updates from the log thread to the window (Qt queues them)."""
    update = Signal(object)


class LogWorker(threading.Thread):
    """Follows (or, in demo mode, replays) the log and emits display updates."""

    def __init__(self, db_path: Path | str, log: Path | str, bridge: Bridge,
                 demo_delay: float | None = None, sync: bool = True):
        super().__init__(daemon=True)
        self.db_path, self.log, self.bridge, self.demo_delay = db_path, log, bridge, demo_delay
        self.sync = sync
        self.stopping = threading.Event()
        self._synced: set[str] = set()

    def _note(self, text: str) -> None:
        self.bridge.update.emit(draftview.Update("note", text))

    def _update_set(self, conn, set_code: str) -> None:
        """Make sure the set's 17Lands data is current (cached; cheap after the first time)."""
        if not self.sync or set_code in self._synced:
            return
        self._synced.add(set_code)
        self._note(f"Checking 17Lands data for {set_code}...")
        progress = lambda line: self._note(f"Updating {set_code}: {line.strip()}")  # noqa: E731
        try:
            updater.update_set(conn, set_code, download_dir=Path(self.db_path).parent / "downloads",
                               log=progress)
        except Exception as exc:  # never let an update failure stop the overlay
            self._note(f"Could not update {set_code} data ({exc}); using what is cached.")
            return
        self._note("")

    def run(self) -> None:
        try:
            conn = db.connect(self.db_path)  # sqlite connections belong to one thread
            tracker = draftview.DraftTracker(conn, prepare=lambda code: self._update_set(conn, code))
            for event, live in self._events():
                for update in tracker.handle(event, live):
                    if self.stopping.is_set():
                        return
                    self.bridge.update.emit(update)
                    if self.demo_delay and update.kind in ("pack", "pick"):
                        time.sleep(self.demo_delay if update.kind == "pack" else self.demo_delay / 3)
        except Exception as exc:  # show it rather than dying silently
            self.bridge.update.emit(draftview.Update("note", f"Error reading the log: {exc}"))

    def _events(self):
        if self.demo_delay is not None:
            yield from ((e, True) for e in logreader.read_events(self.log))
            return
        yield from logreader.follow_events(self.log, stop=self.stopping.is_set)


class Overlay(QWidget):
    def __init__(self, rows: int | None = None, settings: QSettings | None = None,
                 theme: str | None = None):
        # A normal (not Qt.Tool) window, so it gets a taskbar entry and can be
        # restored after minimising.
        super().__init__(None, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("MTG draft helper")
        self.settings = settings or QSettings("mtghelper", "overlay")
        # An explicit --rows wins; otherwise the last choice from the menu; otherwise every card.
        self.max_rows = rows if rows is not None else int(self.settings.value("rows", ALL))
        # Fully opaque unless chosen otherwise in the menu: see-through text is hard to read.
        self.setWindowOpacity(float(self.settings.value("opacity", 1.0)))
        name = theme or str(self.settings.value("theme", DEFAULT_THEME))
        self.theme_name = name if name in THEMES else DEFAULT_THEME
        self._quit = QApplication.instance().quit
        self._drag_from: QPoint | None = None
        self.locked = True          # position fixed until the lock button is pressed
        self._moved = False         # True once the user has dragged it somewhere
        self._last_pack: draftview.PackView | None = None

        self.panel = QFrame(self)
        self.panel.setObjectName("panel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.panel)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        self.title = QLabel("MTG draft helper", objectName="title")
        self.sub = QLabel("Waiting for a draft...", objectName="sub")
        self.bar = LeaningBar()
        self.bar.hide()
        self.note = QLabel("", objectName="note")
        self.note.setTextFormat(Qt.RichText)
        self.note.setWordWrap(True)
        self.note.hide()
        self.rows = QLabel("")
        self.rows.setTextFormat(Qt.RichText)
        self.pick = QLabel("", objectName="pick")
        self.pick.setWordWrap(True)
        self.minimise_button = QPushButton("–", objectName="minbtn", toolTip="Minimise")
        self.close_button = QPushButton("✕", objectName="closebtn", toolTip="Close")
        self.lock_button = QPushButton("✥", objectName="lockbtn", toolTip="Unlock to move",
                                       checkable=True)
        for button in (self.lock_button, self.minimise_button, self.close_button):
            button.setCursor(Qt.PointingHandCursor)
            button.setFocusPolicy(Qt.NoFocus)
        self.lock_button.toggled.connect(self._set_unlocked)
        self.minimise_button.clicked.connect(self.showMinimized)
        self.close_button.clicked.connect(lambda: self._quit())
        header = QHBoxLayout()
        header.setSpacing(2)
        header.addWidget(self.title, 1)
        header.addWidget(self.lock_button)
        header.addWidget(self.minimise_button)
        header.addWidget(self.close_button)
        layout.addLayout(header)
        for widget in (self.sub, self.bar, self.note, self.rows, self.pick):
            layout.addWidget(widget)
        self.setFixedWidth(380)
        self._apply_theme()
        self._place()

    @property
    def theme(self) -> dict:
        return THEMES[self.theme_name]

    # --- updates -------------------------------------------------------------------

    def apply(self, update: draftview.Update) -> None:
        if update.kind == "note":
            self.note.setText(update.text)
            self.note.setVisible(bool(update.text))
        elif update.kind == "waiting":
            if self._last_pack is None:
                self.sub.setText(update.text)
        elif update.kind == "pack" and update.pack:
            self.rows.setWordWrap(False)
            self._show_pack(update.pack)
        elif update.kind == "pick":
            t = self.theme
            color = t["good"] if update.rank == 0 else t["mid"] if update.rank in (1, 2) else t["muted"]
            self.pick.setText(f'<span style="color:{color};">&#10140; {html.escape(update.text)}</span>')
        elif update.kind == "done":
            self.sub.setText("Draft complete")
            self.rows.setText("")
            self.bar.set_pairs([])
            self.pick.setText(html.escape(update.text))
            self._last_pack = None
            self._apply_theme()
        elif update.kind == "deck" and update.deck:
            self._show_deck(update.deck)
        self._fit()
        self._place()

    def _show_pack(self, view: draftview.PackView) -> None:
        self._last_pack = view
        self.title.setText(f"{view.title}  -  pack {view.pack}, pick {view.pick}")
        if not view.rated:
            self.sub.setText("No grades for this set yet: card names only")
        elif view.pool_size == 0:
            self.sub.setText("First pick: ranked by card strength")
        else:
            self.sub.setText(f"Pool {view.pool_size} cards  -  leaning")
        self.bar.set_pairs(view.pairs if view.rated else [])
        self._apply_theme()
        self._render_rows(view, reasons=True)
        self.pick.setText("")
        # A whole pack with reasons can be taller than a small screen: drop the reasons then.
        self._fit()
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None and self.height() > screen.availableGeometry().height() - 2 * SCREEN_MARGIN:
            self._render_rows(view, reasons=False)

    def _show_deck(self, suggestion) -> None:
        """The suggested build after the last pick, grouped by mana value: one card per
        line, or several per line if that would not fit on the screen."""
        t = self.theme
        deck = suggestion.best
        lands = ", ".join(f"{n} {name}" for name, n in deck.lands.items())
        self.title.setText(f"Suggested deck  -  {deck.pair}")
        self.sub.setText(f"{lands}  -  {deck.creatures} creatures, avg. mana value {deck.average_cmc:.1f}")
        self.bar.set_pairs([])
        extra = [html.escape(n) for n in deck.notes]
        extra += [html.escape(f"Splash idea: {s}") for s in suggestion.splashes]
        extra += [html.escape(f"Next best: {a.pair} ({a.score - deck.score:+.1f})") for a in suggestion.alternatives]
        # Notes go in the word-wrapped note line: they can be longer than the panel is wide.
        self.note.setText("<br>".join(extra))
        self.note.setVisible(bool(extra))
        self.rows.setWordWrap(False)
        self.rows.setText(self._deck_html(deck, compact=False))
        self._fit()
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is not None and self.height() > screen.availableGeometry().height() - 2 * SCREEN_MARGIN:
            self.rows.setWordWrap(True)
            self.rows.setText(self._deck_html(deck, compact=True))
            self._fit()

    def _deck_html(self, deck, compact: bool) -> str:
        t = self.theme
        colours = {**COLOR_HEX, **t.get("pips", {})}
        groups: dict[int, list[str]] = {}
        for card in deck.spells:
            pips = "".join(f'<span style="color:{colours[c]};">&#9679;</span>' for c in card.colors) or \
                f'<span style="color:{t["muted"]};">&#9675;</span>'
            kind = "" if card.creature else f' <span style="color:{t["muted"]};">(spell)</span>'
            grade = html.escape(card.grade or "-")
            chip = (f'<span style="background:{grade_color(card.grade)};color:white;font-weight:600;">'
                    f'&nbsp;{grade:<2}&nbsp;</span>')
            groups.setdefault(min(int(card.cmc), 6), []).append(f"{chip} {pips} {html.escape(card.name)}{kind}")
        lines = []
        for cost in sorted(groups):
            label = "6+" if cost == 6 else str(cost)
            if compact:
                lines.append(f"<b>{label}:</b> " + ", ".join(groups[cost]))
            else:
                lines.append(f'<span style="color:{t["muted"]};font-size:{t["font"] - 2}px;">{label} mana</span>')
                lines += [f"&nbsp;&nbsp;{entry}" for entry in groups[cost]]
        return "<br>".join(lines)

    def _render_rows(self, view: draftview.PackView, reasons: bool) -> None:
        limit = len(view.rows) if self.max_rows == ALL else self.max_rows
        shown = view.rows[:limit]
        lines = [row_html(r, best=(i == 0 and view.rated), theme=self.theme, reasons=reasons)
                 for i, r in enumerate(shown)]
        if len(view.rows) > len(shown):
            lines.append(f'<span style="color:{self.theme["muted"]};">+{len(view.rows) - len(shown)} more</span>')
        self.rows.setText("<br>".join(lines))

    def _fit(self) -> None:
        """Resize to the content. (QWidget.adjustSize would cap a window at about two
        thirds of the screen height and cut the list off.)"""
        self.layout().activate()
        self.resize(self.width(), self.layout().totalHeightForWidth(self.width()))

    def _apply_theme(self) -> None:
        t = self.theme
        bg = None
        if t.get("tint") and self._last_pack and self._last_pack.pairs:
            bg = tinted_background(self._last_pack.pairs[0][0])
        self.setStyleSheet(stylesheet(t, bg))
        self.bar.track = QColor(t["track"])
        self.bar.update()

    def set_theme(self, name: str) -> None:
        if name not in THEMES:
            return
        self.theme_name = name
        self.settings.setValue("theme", name)
        if self._last_pack:
            self._show_pack(self._last_pack)
        else:
            self._apply_theme()
        self._fit()
        self._place()

    # --- moving and options ----------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.locked:
            self._drag_from = event.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, event):
        if self._drag_from is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_from)
            self._moved = True

    def mouseReleaseEvent(self, event):
        self._drag_from = None

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        for n, label in ((ALL, "Show every card"), (8, "Show 8 cards"), (5, "Show 5 cards")):
            action = QAction(label, menu, checkable=True, checked=self.max_rows == n)
            action.triggered.connect(lambda _=False, n=n: self._set_rows(n))
            menu.addAction(action)
        menu.addSeparator()
        themes = menu.addMenu("Theme")
        group = QActionGroup(themes)
        for name in THEMES:
            action = QAction(name, themes, checkable=True, checked=name == self.theme_name)
            action.triggered.connect(lambda _=False, name=name: self.set_theme(name))
            group.addAction(action)
            themes.addAction(action)
        opacity = menu.addMenu("Opacity")
        for pct in (70, 85, 100):
            action = QAction(f"{pct}%", opacity, checkable=True,
                             checked=abs(self.windowOpacity() - pct / 100) < 0.01)
            action.triggered.connect(lambda _=False, p=pct: self._set_opacity(p / 100))
            opacity.addAction(action)
        menu.addSeparator()
        menu.addAction("Quit", QApplication.instance().quit)
        menu.exec(event.globalPos())

    def _set_rows(self, n: int) -> None:
        self.max_rows = n
        self.settings.setValue("rows", n)
        if self._last_pack:
            self._show_pack(self._last_pack)
            self._fit()
            self._place()

    def _set_opacity(self, value: float) -> None:
        self.setWindowOpacity(value)
        self.settings.setValue("opacity", value)

    def _set_unlocked(self, unlocked: bool) -> None:
        self.locked = not unlocked
        self.lock_button.setToolTip("Lock position" if unlocked else "Unlock to move")
        self.setCursor(Qt.SizeAllCursor if unlocked else Qt.ArrowCursor)

    def _place(self) -> None:
        """Middle of the left screen edge, until the user moves the window."""
        if self._moved:
            return
        screen = self.screen() or QGuiApplication.primaryScreen()
        if screen is None:
            return
        area = screen.availableGeometry()
        self.move(area.left() + LEFT_MARGIN, area.center().y() - self.height() // 2)

    def showEvent(self, event):
        super().showEvent(event)
        self._fit()  # sizes computed while hidden are not reliable
        self._place()


def main(db_path: Path | str, log: Path | str, demo_delay: float | None = None,
         rows: int | None = None, sync: bool = True) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setWindowIcon(app_icon())
    window = Overlay(rows)
    bridge = Bridge()
    bridge.update.connect(window.apply)
    worker = LogWorker(db_path, log, bridge, demo_delay, sync=sync)
    if demo_delay is not None:
        window.note.setText("Demo: replaying the drafts in the log")
        window.note.show()
    elif not Path(log).exists():
        window.note.setText('Arena\'s log was not found. In Arena, turn on Options > Account > '
                            '"Detailed Logs (Plugin Support)", then restart Arena.')
        window.note.show()
    window.show()
    worker.start()
    code = app.exec()
    worker.stopping.set()
    return code


def run_safely(db_path: Path | str, log: Path | str, **kwargs) -> int:
    """`main`, but a crash is written to data/overlay-error.log: started from a
    shortcut there is no console to show it."""
    try:
        return main(db_path, log, **kwargs)
    except Exception:
        error_file = Path(db_path).parent / "overlay-error.log"
        error_file.write_text(traceback.format_exc(), encoding="utf-8")
        raise
