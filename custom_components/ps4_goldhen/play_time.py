"""Play time per console, per game and per session.

Counts while a game has focus (game_session.py: console on, klog connected). Home screen, rest, off and klog drops
pause it. A session is one game in one sitting: the same game back within 30 min (PS button, a short rest) continues
it, a different game or 30 min away ends it. Time while HA was down isn't counted (nobody saw it).
Kept in .storage/ps4_goldhen.play_time.<entry> (no recorder or history queries):
  games    {title_id: {name, seconds, sessions, first, last}}  lifetime, per game
  days     {local date: {title_id: seconds}}                    the last 400 days
  sessions [{title_id, name, start, end, seconds}]               the last 1000, oldest first
  open     the session in progress (or paused < 30 min), else None
Device page: Play time today (feeds HA's long-term statistics) + Current session. Panel tab: ps4_goldhen/play_time.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import partial
import logging
from typing import Any, Callable

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import game_session
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MERGE_GAP = timedelta(minutes=30)
TICK = timedelta(seconds=60)
MAX_STEP = timedelta(minutes=10)  # most one accrual counts: a clock jump or a stalled HA isn't play
DAYS_KEPT = 400
SESSIONS_KEPT = 1000
PANEL_SESSIONS = 50
_SAVE_DELAY = 10      # s; shorter than TICK, so a save lands between ticks (each call restarts Store's timer)
_STORE_VERSION = 1


def signal_updated(entry_id: str) -> str:
    return f"{DOMAIN}_play_time_updated_{entry_id}"


def _iso(value: datetime) -> str:
    return value.isoformat()


def _dt(value: str) -> datetime:
    return dt_util.parse_datetime(value)  # type: ignore[return-value]


class PlayTimeBook:
    """The stored play time of one console. No HA objects: the tests drive it with their own clock and time zone."""

    def __init__(self, data: dict[str, Any] | None = None,
                 to_local: Callable[[datetime], datetime] | None = None) -> None:
        data = data or {}
        self.games: dict[str, dict[str, Any]] = data.get("games") or {}
        self.days: dict[str, dict[str, float]] = data.get("days") or {}
        self.sessions: list[dict[str, Any]] = data.get("sessions") or []
        self.open: dict[str, Any] | None = data.get("open")
        self.active: str | None = None  # the game counting now (not stored: after a restart it has to be seen again)
        self._to_local = to_local or dt_util.as_local

    def to_store(self) -> dict[str, Any]:
        return {"games": self.games, "days": self.days, "sessions": self.sessions, "open": self.open}

    def local_day(self, when: datetime) -> date:
        return self._to_local(when).date()

    # ── what game_session reports ──────────────────────────────────────────────

    def start(self, title_id: str, name: str | None, now: datetime) -> bool:
        """A game took focus. Returns True if a session was closed (a different game, or back after > 30 min)."""
        self.accrue(now)
        closed = False
        o = self.open
        if o is not None and (o["title_id"] != title_id or now - _dt(o["last"]) > MERGE_GAP):
            closed = self._close()
            o = None
        if o is None:
            self.open = {"title_id": title_id, "name": name, "start": _iso(now), "last": _iso(now), "seconds": 0.0}
        else:
            o["last"] = _iso(now)  # same sitting: the time away isn't counted
            o["name"] = name or o.get("name")
        game = self.games.setdefault(
            title_id, {"name": name, "seconds": 0.0, "sessions": 0, "first": _iso(now), "last": _iso(now)})
        game["name"] = name or game.get("name")
        self.active = title_id
        return closed

    def stop(self, now: datetime) -> None:
        """The game left focus: counted up to now, the session stays open for 30 min."""
        self.accrue(now)
        self.active = None

    def accrue(self, now: datetime) -> bool:
        o = self.open
        if self.active is None or o is None or o["title_id"] != self.active:
            return False
        last = _dt(o["last"])
        if now <= last:
            return False
        end = last + min(now - last, MAX_STEP)
        self._add(self.active, last, end)
        o["seconds"] = round(o["seconds"] + (end - last).total_seconds(), 1)
        o["last"] = _iso(now)
        self.games[self.active]["last"] = _iso(now)
        return True

    def expire(self, now: datetime) -> bool:
        """Close a paused session once 30 min have passed. True if one was closed."""
        o = self.open
        if o is None or self.active == o["title_id"] or now - _dt(o["last"]) <= MERGE_GAP:
            return False
        return self._close()

    def prune(self, now: datetime) -> None:
        oldest = (self.local_day(now) - timedelta(days=DAYS_KEPT - 1)).isoformat()
        for day in [d for d in self.days if d < oldest]:
            del self.days[day]

    # ── reading ────────────────────────────────────────────────────────────────

    def today_seconds(self, now: datetime) -> float:
        return sum(self.days.get(self.local_day(now).isoformat(), {}).values())

    def session_seconds(self) -> float:
        return self.open["seconds"] if self.open else 0.0

    def last_days_seconds(self, now: datetime, n_days: int) -> float:
        """The last n local days, today included (7 = today + the 6 days before)."""
        first = (self.local_day(now) - timedelta(days=n_days - 1)).isoformat()
        return round(sum(sum(per_game.values()) for day, per_game in self.days.items() if day >= first), 1)

    def summary(self, now: datetime) -> dict[str, Any]:
        today = self.local_day(now)

        def since(n_days: int) -> str:
            return (today - timedelta(days=n_days - 1)).isoformat()

        def total(first_day: str, title_id: str | None = None) -> float:
            return round(sum(
                secs for day, per_game in self.days.items() if day >= first_day
                for tid, secs in per_game.items() if title_id in (None, tid)), 1)

        o = self.open
        games = []
        for tid, g in self.games.items():
            sessions = g["sessions"] + (1 if o and o["title_id"] == tid and o["seconds"] > 0 else 0)
            if g["seconds"] <= 0 and not sessions:
                continue
            games.append({"title_id": tid, **g, "sessions": sessions, "week": total(since(7), tid)})
        games.sort(key=lambda g: g["last"], reverse=True)
        recent = [dict(s, open=False, playing=False) for s in reversed(self.sessions[-PANEL_SESSIONS:])]
        if o and o["seconds"] > 0:
            recent.insert(0, {"title_id": o["title_id"], "name": o.get("name"), "start": o["start"],
                              "end": o["last"], "seconds": o["seconds"], "open": True,
                              "playing": self.active == o["title_id"]})
        firsts = [g["first"] for g in games]
        return {
            "today": total(since(1)), "week": total(since(7)), "month": total(since(30)),
            "total": round(sum(g["seconds"] for g in self.games.values()), 1),
            "since": min(firsts) if firsts else None,
            "games": games,
            "sessions": recent[:PANEL_SESSIONS],
        }

    # ── internals ──────────────────────────────────────────────────────────────

    def _add(self, title_id: str, start: datetime, end: datetime) -> None:
        """Count [start, end) for the game, split at local midnight into the per-day totals."""
        self.games[title_id]["seconds"] = round(self.games[title_id]["seconds"] + (end - start).total_seconds(), 1)
        while start < end:
            local = self._to_local(start)
            midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=local.tzinfo)
            part_end = min(end, midnight)
            per_game = self.days.setdefault(local.date().isoformat(), {})
            per_game[title_id] = round(per_game.get(title_id, 0.0) + (part_end - start).total_seconds(), 1)
            start = part_end

    def _close(self) -> bool:
        o, self.open = self.open, None
        if o is None:
            return False
        if self.active == o["title_id"]:
            self.active = None
        if o["seconds"] <= 0:
            return False
        self.sessions.append({"title_id": o["title_id"], "name": o.get("name"), "start": o["start"],
                              "end": o["last"], "seconds": o["seconds"]})
        del self.sessions[:-SESSIONS_KEPT]
        self.games[o["title_id"]]["sessions"] += 1
        return True


# ── HA glue ────────────────────────────────────────────────────────────────────

@dataclass
class _Runtime:
    entry: ConfigEntry
    store: Store
    book: PlayTimeBook
    day: date


def runtime(hass: HomeAssistant, entry_id: str) -> _Runtime | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data.get("play_time") if isinstance(data, dict) else None


def _store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.play_time.{entry_id}")


async def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Before game_session.async_setup, so the first game_started finds the book."""
    store = _store(hass, entry.entry_id)
    book = PlayTimeBook(await store.async_load())
    now = dt_util.utcnow()
    rt = _Runtime(entry, store, book, book.local_day(now))
    hass.data[DOMAIN][entry.entry_id]["play_time"] = rt
    if book.expire(now):  # HA was down past the 30 min
        _changed(hass, rt, soon=True)
    book.prune(now)
    entry_id = entry.entry_id
    entry.async_on_unload(async_dispatcher_connect(
        hass, game_session.signal_game_started(entry_id), partial(_on_started, hass, rt)))
    entry.async_on_unload(async_dispatcher_connect(
        hass, game_session.signal_game_left(entry_id), partial(_on_left, hass, rt)))
    entry.async_on_unload(async_track_time_interval(hass, partial(_on_tick, hass, rt), TICK))
    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, partial(_on_stop, hass, rt)))
    _register_once(hass)


async def async_unload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Options save / reload: count up to now and write it before the next setup reads the store."""
    rt = runtime(hass, entry.entry_id)
    if rt is not None:
        rt.book.accrue(dt_util.utcnow())
        await rt.store.async_save(rt.book.to_store())


async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted: its play time goes with it."""
    await _store(hass, entry.entry_id).async_remove()


@callback
def _changed(hass: HomeAssistant, rt: _Runtime, soon: bool = False) -> None:
    rt.store.async_delay_save(rt.book.to_store, 1 if soon else _SAVE_DELAY)
    async_dispatcher_send(hass, signal_updated(rt.entry.entry_id))


@callback
def _on_started(hass: HomeAssistant, rt: _Runtime, payload: dict[str, Any]) -> None:
    closed = rt.book.start(payload["title_id"], payload.get("name"), payload["started"])
    _changed(hass, rt, soon=closed)


@callback
def _on_left(hass: HomeAssistant, rt: _Runtime, payload: dict[str, Any]) -> None:
    if payload.get("background"):  # a game that was already paused: no time to add
        return
    rt.book.stop(payload["ended"])
    _changed(hass, rt)


@callback
def _on_tick(hass: HomeAssistant, rt: _Runtime, now: datetime) -> None:
    counted = rt.book.accrue(now)
    closed = rt.book.expire(now)
    day = rt.book.local_day(now)
    new_day = day != rt.day
    if new_day:  # Play time today goes back to 0
        rt.day = day
        rt.book.prune(now)
    if counted or closed or new_day:
        _changed(hass, rt, soon=closed)


@callback
def _on_stop(hass: HomeAssistant, rt: _Runtime, _event) -> None:
    """HA stopping: count up to now; Store writes pending saves at the final write."""
    if rt.book.accrue(dt_util.utcnow()):
        rt.store.async_delay_save(rt.book.to_store, 0)


# ── Panel API (admin only, like the panel) ─────────────────────────────────────

@websocket_api.websocket_command({vol.Required("type"): "ps4_goldhen/play_time", vol.Required("entry_id"): str})
@websocket_api.require_admin
@websocket_api.async_response
async def _ws_play_time(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
    rt = runtime(hass, msg["entry_id"])
    if rt is None:
        connection.send_error(msg["id"], "play_time_error", "Unknown PS4")
        return
    now = dt_util.utcnow()
    rt.book.accrue(now)
    connection.send_result(msg["id"], rt.book.summary(now))


def _register_once(hass: HomeAssistant) -> None:
    flag = f"{DOMAIN}_play_time_registered"
    if not hass.data.get(flag):
        websocket_api.async_register_command(hass, _ws_play_time)
        hass.data[flag] = True
