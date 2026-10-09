"""Read draft events from MTG Arena's Player.log.

Arena writes this log when "Detailed Logs (Plugin Support)" is enabled in
Options -> Account. This module only reads it; it never touches the game.

Two draft flavours write different messages:

* Quick Draft (against bots), checked against a real log (FRA, 2026-10-08):
    ==> BotDraftDraftPick {"id":..., "request":"{...\"PickInfo\":{\"CardIds\":[...],
        \"PackNumber\":0,\"PickNumber\":0}}"}
    <== BotDraftDraftStatus(<id>)   or   <== BotDraftDraftPick(<id>)
    {"CurrentModule":..., "Payload":"{\"DraftStatus\":\"PickNext\",\"PackNumber\":0,
        \"PickNumber\":1,\"DraftPack\":[\"106355\",...],\"PickedCards\":[...]}"}
  Pack and pick numbers start at 0.

* Premier / Traditional draft (against people): NOT yet checked against a real
  log. Parsed from the commonly documented messages:
    Draft.Notify {"draftId":..., "SelfPack":1, "SelfPick":1, "PackCards":"1,2,3"}
    ==> EventPlayerDraftMakePick {"request":"{\"GrpIds\":[...],\"Pack\":1,\"Pick\":1}"}
  Pack and pick numbers start at 1.

Everything is turned into a few plain events (`PackShown`, `CardPicked`,
`DraftCompleted`) with pack and pick numbers starting at 1, so the rest of the
program does not care which flavour produced them.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

DEFAULT_LOG = Path(os.environ.get("USERPROFILE", Path.home())) / (
    "AppData/LocalLow/Wizards Of The Coast/MTGA/Player.log"
)

EVENT_NAME = re.compile(r"^(?P<kind>[A-Za-z]*Draft)_(?P<set>[A-Z0-9]+)_")
REQUEST = re.compile(r"==> (?P<name>[A-Za-z0-9_]+) (?P<json>\{.*\})\s*$")
RESPONSE = re.compile(r"<== (?P<name>[A-Za-z0-9_]+)\((?P<id>[^)]*)\)\s*$")
NOTIFY = re.compile(r"Draft\.Notify (?P<json>\{.*\})\s*$")


@dataclass
class PackShown:
    event: str
    pack: int                 # 1-3
    pick: int                 # 1-14
    cards: list[int]          # Arena ids in the pack
    pool: list[int] | None    # cards picked so far, when the log says (Quick Draft)


@dataclass
class CardPicked:
    event: str
    pack: int
    pick: int
    cards: list[int]          # usually one card; two in Pick Two drafts


@dataclass
class DraftCompleted:
    event: str
    pool: list[int]


Event = PackShown | CardPicked | DraftCompleted


def set_and_format(event_name: str) -> tuple[str | None, str | None]:
    """'QuickDraft_FRA_20261008' -> ('FRA', 'QuickDraft')."""
    m = EVENT_NAME.match(event_name or "")
    return (m["set"], m["kind"]) if m else (None, None)


def _ids(values: Iterable) -> list[int]:
    return [int(v) for v in values]


def _loads(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return None


class Parser:
    """Turns log lines into draft events. Feed it lines in order."""

    def __init__(self):
        self.pending_response: str | None = None   # name of a <== whose body comes next
        self.event: str = ""                       # last draft event name seen
        self.premier_pool: list[int] = []

    def feed(self, line: str) -> list[Event]:
        line = line.rstrip("\r\n")
        if not line.strip():
            # Blank lines carry nothing; skipping them also keeps a pending
            # response alive if line endings were doubled (\r\r\n).
            return []
        if self.pending_response is not None:
            name, self.pending_response = self.pending_response, None
            if line.startswith("{"):
                return self._response(name, _loads(line))
        if m := RESPONSE.search(line):
            self.pending_response = m["name"]
            return []
        if m := REQUEST.search(line):
            return self._request(m["name"], _loads(m["json"]))
        if m := NOTIFY.search(line):
            return self._premier_notify(_loads(m["json"]))
        return []

    # --- Quick Draft ----------------------------------------------------------

    def _response(self, name: str, body) -> list[Event]:
        if name not in ("BotDraftDraftStatus", "BotDraftDraftPick") or not isinstance(body, dict):
            return []
        payload = _loads(body.get("Payload", ""))
        if not isinstance(payload, dict):
            return []
        self.event = payload.get("EventName") or self.event
        pool = _ids(payload.get("PickedCards") or [])
        if payload.get("DraftStatus") == "Completed":
            return [DraftCompleted(self.event, pool)]
        cards = _ids(payload.get("DraftPack") or [])
        if not cards:
            return []
        return [PackShown(self.event, int(payload["PackNumber"]) + 1,
                          int(payload["PickNumber"]) + 1, cards, pool)]

    def _request(self, name: str, body) -> list[Event]:
        if not isinstance(body, dict):
            return []
        request = _loads(body.get("request", ""))
        if not isinstance(request, dict):
            return []
        if name == "EventJoin" and "Draft" in (request.get("EventName") or ""):
            self.event = request["EventName"]
            self.premier_pool = []
            return []
        if name == "BotDraftDraftPick":
            info = request.get("PickInfo") or {}
            self.event = info.get("EventName") or request.get("EventName") or self.event
            return [CardPicked(self.event, int(info["PackNumber"]) + 1,
                               int(info["PickNumber"]) + 1, _ids(info.get("CardIds") or []))]
        if name == "EventPlayerDraftMakePick":
            raw = request.get("GrpIds") or ([request["GrpId"]] if request.get("GrpId") else [])
            cards = _ids(raw)
            self.premier_pool.extend(cards)
            events: list[Event] = [CardPicked(self.event, int(request.get("Pack", 0)),
                                              int(request.get("Pick", 0)), cards)]
            if len(self.premier_pool) >= 42:
                events.append(DraftCompleted(self.event, list(self.premier_pool)))
            return events
        return []

    # --- Premier / Traditional draft (unverified) -------------------------------

    def _premier_notify(self, body) -> list[Event]:
        if not isinstance(body, dict) or not body.get("PackCards"):
            return []
        cards = _ids(c for c in str(body["PackCards"]).split(",") if c.strip())
        return [PackShown(self.event, int(body.get("SelfPack", 1)), int(body.get("SelfPick", 1)),
                          cards, list(self.premier_pool))]


def read_events(path: Path | str) -> Iterator[Event]:
    """All draft events in a log file, from the start."""
    parser = Parser()
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield from parser.feed(line)


def follow_events(path: Path | str, poll_s: float = 0.5,
                  stop=lambda: False) -> Iterator[tuple[Event | None, bool]]:
    """Yield (event, live) as Arena writes the log, like `tail -f`.

    Everything already in the file is read first and yielded with live=False,
    so a draft in progress can be picked up; later events come with live=True.
    Once that backlog is read, (None, True) is yielded a single time.
    Arena rewrites the log from scratch at every start: when the file shrinks
    the reader starts over from the top.
    """
    path = Path(path)
    parser = Parser()
    position = 0
    backlog_end = path.stat().st_size if path.exists() else 0
    buffer = ""
    caught_up = False
    while not stop():
        if not caught_up and position >= backlog_end:
            caught_up = True
            yield None, True
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            time.sleep(poll_s)
            continue
        if size < position:  # new Arena session
            position, buffer, parser, backlog_end = 0, "", Parser(), 0
        if size > position:
            with open(path, "rb") as fh:
                fh.seek(position)
                chunk = fh.read(size - position)
            live = position >= backlog_end
            position = size
            buffer += chunk.decode("utf-8", errors="replace")
            *lines, buffer = buffer.split("\n")
            for line in lines:
                for event in parser.feed(line):
                    yield event, live
        else:
            time.sleep(poll_s)


@dataclass
class DraftState:
    """What the player has seen and taken so far in the current draft."""
    event: str = ""
    pool: list[int] = field(default_factory=list)
    round_packs: list[tuple[int, list[int]]] = field(default_factory=list)  # this pack round
    current: PackShown | None = None

    def apply(self, ev: Event) -> None:
        if isinstance(ev, PackShown):
            if ev.event != self.event or (ev.pack == 1 and ev.pick == 1):
                self.event, self.pool, self.round_packs = ev.event, [], []
            if self.current and self.current.pack != ev.pack:
                self.round_packs = []
            if ev.pool is not None:
                self.pool = list(ev.pool)
            self.current = ev
        elif isinstance(ev, CardPicked):
            # The next pack message replaces the pool when it carries one (Quick Draft).
            self.pool.extend(ev.cards)
            if self.current:
                self.round_packs.append((self.current.pick, list(self.current.cards)))
        elif isinstance(ev, DraftCompleted):
            self.pool = list(ev.pool) or self.pool
            self.current = None
