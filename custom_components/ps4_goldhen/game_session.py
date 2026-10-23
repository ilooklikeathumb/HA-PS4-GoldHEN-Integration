"""Which game is being played, as signals. Play time and save backup listen.

A game counts as played while the console is on and responding, the game has focus and klog is connected (the source
Status and the media player read). Each change fires a dispatcher signal for that console:
- `signal_game_started(entry_id)` → {entry_id, title_id, name, started}
- `signal_game_left(entry_id)` → {entry_id, title_id, name, started, ended, reason, background}
  reason: switched (another game took focus) / closed / home_screen (PS button, game still running) / rest / off /
  not_responding (on, but klog silent and no DDP reply: frozen) / klog_lost (HA can't see the console, the game may
  still run). background=False: the game had focus until `ended`.
  background=True: a game already left running behind the home screen (`suspended_title_id`) was closed, replaced,
  or the console went to rest / off; started / ended are its last time in focus (None if HA didn't see it).
Unloading the entry (HA restart, options save) fires nothing: nobody left the game.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .const import DOMAIN, POWER_OFF, POWER_REST, SENSOR_GAME_NAME, SENSOR_TITLE_ID

REASON_SWITCHED = "switched"
REASON_CLOSED = "closed"
REASON_HOME_SCREEN = "home_screen"
REASON_REST = "rest"
REASON_OFF = "off"
REASON_NOT_RESPONDING = "not_responding"
REASON_KLOG_LOST = "klog_lost"

_CLOSE_STATE_REASONS = frozenset({"game_close_detected", "bgft_game_stopped", "exit_scene_to_home"})


def signal_game_started(entry_id: str) -> str:
    return f"{DOMAIN}_game_started_{entry_id}"


def signal_game_left(entry_id: str) -> str:
    return f"{DOMAIN}_game_left_{entry_id}"


@dataclass
class Segment:
    title_id: str
    name: str | None
    started: datetime
    ended: datetime | None = None


class GameSessionTracker:
    """Coordinator data in, (signal kind, payload) out. No HA objects, so the tests drive it directly."""

    def __init__(self) -> None:
        self.segment: Segment | None = None      # the game with focus now
        self.suspended: str | None = None        # the game left running behind the home screen / in rest
        self._power: str | None = None
        self._last: dict[str, Segment] = {}      # each game's last time in focus, for background events

    def update(self, data: dict[str, Any], now: datetime, name_for: Callable[[str], str | None]) -> list[tuple[str, dict]]:
        power = data.get("power_state")
        tid = data.get(SENSOR_TITLE_ID)
        connected = bool(data.get("klog_connected"))
        responding = not data.get("not_responding")
        focus = tid if tid and connected and responding and power not in (POWER_REST, POWER_OFF) else None
        suspended = data.get("suspended_title_id")
        prev_power, prev_suspended = self._power, self.suspended
        self._power, self.suspended = power, suspended
        out: list[tuple[str, dict]] = []

        seg = self.segment
        if seg is not None and seg.title_id != focus:
            if focus:
                reason = REASON_SWITCHED
            elif power == POWER_REST:
                reason = REASON_REST
            elif power == POWER_OFF:
                reason = REASON_OFF
            elif not responding:
                reason = REASON_NOT_RESPONDING
            elif not connected:
                reason = REASON_KLOG_LOST
            elif data.get("state_reason") in _CLOSE_STATE_REASONS:
                reason = REASON_CLOSED
            else:
                reason = REASON_HOME_SCREEN
            seg.ended = now
            self._last[seg.title_id] = seg
            self.segment = None
            out.append(("left", self._payload(seg, reason, background=False)))

        # the game running in the background: closed / replaced, or it goes to rest / off with the console
        if prev_suspended and prev_suspended != focus and (seg is None or seg.title_id != prev_suspended):
            reason = None
            if suspended != prev_suspended:
                reason = REASON_SWITCHED if focus else (REASON_OFF if power == POWER_OFF else REASON_CLOSED)
            elif power == POWER_REST and prev_power != POWER_REST:
                reason = REASON_REST
            if reason:
                last = self._last.get(prev_suspended) or Segment(prev_suspended, name_for(prev_suspended), None)  # type: ignore[arg-type]
                out.append(("left", self._payload(last, reason, background=True)))

        if focus and self.segment is None:
            self.segment = Segment(focus, name_for(focus), now)
            out.append(("started", {"title_id": focus, "name": self.segment.name, "started": now}))
        return out

    @staticmethod
    def _payload(seg: Segment, reason: str, background: bool) -> dict[str, Any]:
        return {"title_id": seg.title_id, "name": seg.name, "started": seg.started, "ended": seg.ended,
                "reason": reason, "background": background}


# ── HA glue ────────────────────────────────────────────────────────────────────

def _tracker(hass: HomeAssistant, entry_id: str) -> GameSessionTracker | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data.get("game_session") if isinstance(data, dict) else None


def current(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """The game being played on that console now: {title_id, name, started}, else None."""
    tracker = _tracker(hass, entry_id)
    seg = tracker.segment if tracker else None
    return {"title_id": seg.title_id, "name": seg.name, "started": seg.started} if seg else None


def suspended(hass: HomeAssistant, entry_id: str) -> str | None:
    """Title ID of the game left running behind the home screen / in rest, else None."""
    tracker = _tracker(hass, entry_id)
    return tracker.suspended if tracker else None


@callback
def async_setup(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    hass.data[DOMAIN][entry.entry_id]["game_session"] = GameSessionTracker()
    listener = partial(_on_coordinator, hass, entry.entry_id, coordinator)
    entry.async_on_unload(coordinator.async_add_listener(listener))
    listener()


def _name_for(hass: HomeAssistant, entry_id: str, data: dict[str, Any], tid: str) -> str | None:
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    name = (entry_data.get("game_map") or {}).get(tid, {}).get("name")
    if not name and data.get(SENSOR_TITLE_ID) == tid:
        name = data.get(SENSOR_GAME_NAME)
    return name


@callback
def _on_coordinator(hass: HomeAssistant, entry_id: str, coordinator) -> None:
    tracker = _tracker(hass, entry_id)
    if tracker is None:
        return
    data = coordinator.data or {}
    for kind, payload in tracker.update(data, dt_util.utcnow(), partial(_name_for, hass, entry_id, data)):
        signal = signal_game_started(entry_id) if kind == "started" else signal_game_left(entry_id)
        async_dispatcher_send(hass, signal, {"entry_id": entry_id, **payload})
