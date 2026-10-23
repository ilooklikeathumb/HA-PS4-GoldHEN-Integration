"""Live temperatures and fan duty: CPU temperature, SoC temperature and Fan duty.

`sensors_payload/ps4-sensors.elf` (source `payload_src/sensors/`) stays running on the console and prints one klog
line every 30 s,
  ps4_goldhen sensors: cpu=68;soc=88;soc0=82;duty=482;thr=83;ver=2
and the klog listener hands every line to `async_handle_line` (after console_info's). That line stops there: it's
kept out of the Klog tab (and the recorder) and Klog Last Line. `soc` is the hottest of the 16 SoC sensors; duty is
out of 1024.
`thr` = the fan target the console holds (Fan control + its slider, fan.py, whose payload line also ends here), and
`ver` = the payload build. A line from an older build (no `ver`) gets a new copy sent, same rules as a start.
Each `thr` also goes to `fan.async_check` (Fan control on → HA re-sends its target if the console drifted).

The "Live temperatures" switch (on by default) decides whether the payload runs. It starts when the console is on
with no game running and no copy has been heard from: 1 min after klog connects (a running copy prints every 30 s, and
the console-info read goes out at 20 s), or 10 min after the last line. A check every minute plus one timed for the
end of that minute cover boot + loading GoldHEN, an HA restart and waking from rest alike, at most one send per 10 min.
A newer copy replaces a running one (run file on the console), so a spare send is harmless. Off sends
`ps4-sensors-stop.elf`. A line arriving while the switch is off (it was turned off in rest) gets the stop too.

The payload writes its line to /dev/console, which klog delivers within a second. Its fallback,
klog_puts, is held until something else logs: up to 34 s in a game, 5 min 03 s on the idle home screen. So a reading
counts as current for 10 min, after which the sensors show unavailable. A line that lacks a field (or carries it out of
range) makes that one sensor unavailable until a line has it again; the others stay.

How others do it: PS5 exporters and overlays read the same libkernel calls (Marice/ps5-exporter,
drakmor/fan_target, onionHEN); Itemzflow reads the CPU temperature on PS4. A GoldHEN plugin can't: it only runs inside
a game, where privileged calls crash it (upstream's PS4StateJSON).
"""
from __future__ import annotations

from datetime import timedelta
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval

from . import fan
from .const import DOMAIN, POWER_ON, SENSOR_CPU_TEMP, SENSOR_FAN_DUTY, SENSOR_FAN_THRESHOLD, SENSOR_SOC_TEMP

_LOGGER = logging.getLogger(__name__)

# Not in bundled_payloads/: those are copied to the panel's list, and a send from there skips the checks
PAYLOAD_DIR = Path(__file__).parent / "sensors_payload"
SENSORS_PAYLOAD = PAYLOAD_DIR / "ps4-sensors.elf"
STOP_PAYLOAD = PAYLOAD_DIR / "ps4-sensors-stop.elf"
MARKER = "ps4_goldhen sensors:"
PAYLOAD_VERSION = 2    # payload_src/sensors/main.c VERSION; a line with an older one gets the new copy sent

DUTY_SCALE = 1024
FRESH_FOR = 600        # s a reading stays current (klog holds payload lines: 34 s in a game, 5 min 03 s idle)
START_AFTER_CONNECT = 60   # s after klog connects without a line before HA starts the payload
RETRY = 600            # s between automatic sends (start or stop)
CHECK_EVERY = timedelta(seconds=60)

_TEMP_RANGE = (1, 124)
_FIELDS = ("cpu", "soc", "soc0", "duty", "thr", "ver")
_RANGES = {"duty": (0, DUTY_SCALE), "ver": (1, 999)}  # the rest are °C
_STATES = ("started", "stopped", "stop-requested")
SENSOR_KEYS = (SENSOR_CPU_TEMP, SENSOR_SOC_TEMP, SENSOR_FAN_DUTY, SENSOR_FAN_THRESHOLD)


def parse_line(line: str) -> dict[str, Any] | None:
    """The payload's line → its valid fields ({} if none); None if it isn't that line."""
    _, marker, rest = line.partition(MARKER)
    if not marker:
        return None
    values: dict[str, Any] = {}
    for field in rest.strip().split(";"):
        key, sep, value = field.partition("=")
        key, value = key.strip(), value.strip()
        if not sep:
            continue
        if key == "state":
            if value in _STATES:
                values["state"] = value
            continue
        if key not in _FIELDS or not value.isdigit():
            continue
        number = int(value)
        low, high = _RANGES.get(key, _TEMP_RANGE)
        if low <= number <= high:
            values[key] = number
    return values


def readings(values: dict[str, Any]) -> dict[str, int]:
    """Parsed fields → the coordinator keys the sensors read. Fan duty as a whole percent."""
    out = {}
    if "cpu" in values:
        out[SENSOR_CPU_TEMP] = values["cpu"]
    if "soc" in values:
        out[SENSOR_SOC_TEMP] = values["soc"]
    if "duty" in values:
        out[SENSOR_FAN_DUTY] = round(values["duty"] * 100 / DUTY_SCALE)
    if "thr" in values:
        out[SENSOR_FAN_THRESHOLD] = values["thr"]
    return out


def _now(hass: HomeAssistant) -> float:
    return hass.loop.time()


def _state(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data.get("live_sensors") if data else None


def _push(data: dict[str, Any]) -> None:
    coordinator = data["coordinator"]
    coordinator.async_set_updated_data({**(coordinator.data or {}), **data["klog_data"]})


def _clear(data: dict[str, Any]) -> None:
    """No current reading: the sensors go unavailable (with no value) instead of showing an old one."""
    data["live_sensors"]["last_reading"] = None
    if any(data["klog_data"].get(key) is not None for key in SENSOR_KEYS):
        data["klog_data"].update(dict.fromkeys(SENSOR_KEYS))
        _push(data)


@callback
def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Per-entry state + the once-a-minute check. The switch restores `enabled` when it's added."""
    state = hass.data[DOMAIN][entry.entry_id]["live_sensors"] = {
        "enabled": True,
        "last_reading": None,  # loop time of the last line with readings
        "last_line": None,     # loop time of the last line from a running copy (readings or started)
        "connected_at": None,  # loop time klog last connected
        "last_start": None,    # loop time of the last automatic start send
        "last_stop": None,     # loop time of the last automatic stop send
        "task": None,          # the send in flight, if any
        "start_timer": None,   # cancels the check timed for START_AFTER_CONNECT after the last connect
    }
    fan.init_state(hass.data[DOMAIN][entry.entry_id])  # Fan control switch + slider state

    @callback
    def _tick(_now_utc) -> None:
        async_check(hass, entry)

    entry.async_on_unload(async_track_time_interval(hass, _tick, CHECK_EVERY))
    entry.async_on_unload(lambda: _cancel_start_timer(state))


def _cancel_start_timer(state: dict[str, Any]) -> None:
    if state.get("start_timer") is not None:
        state["start_timer"]()
        state["start_timer"] = None


def is_enabled(hass: HomeAssistant, entry_id: str) -> bool:
    state = _state(hass, entry_id)
    return bool(state and state["enabled"])


def is_live(hass: HomeAssistant, entry_id: str, key: str | None = None) -> bool:
    """The switch is on, the console is on and the last reading is under FRESH_FOR old; with `key`, that last reading
    also had this sensor's field."""
    state = _state(hass, entry_id)
    if not state or not state["enabled"] or state["last_reading"] is None:
        return False
    data = hass.data[DOMAIN][entry_id]
    if (data["coordinator"].data or {}).get("power_state") != POWER_ON:
        return False
    if key is not None and data["klog_data"].get(key) is None:
        return False
    return _now(hass) - state["last_reading"] < FRESH_FOR


@callback
def async_on_klog_connected(hass: HomeAssistant, entry_id: str) -> None:
    """Note the time, and check again right when START_AFTER_CONNECT is up (the minute check alone added up to 1 min)."""
    state = _state(hass, entry_id)
    if state is None:
        return
    state["connected_at"] = _now(hass)
    _cancel_start_timer(state)

    @callback
    def _due_check(_now_utc) -> None:
        state["start_timer"] = None
        if (entry := hass.config_entries.async_get_entry(entry_id)) is not None:
            async_check(hass, entry)

    state["start_timer"] = async_call_later(hass, START_AFTER_CONNECT + 1, _due_check)


@callback
def async_handle_line(hass: HomeAssistant, entry_id: str, line: str) -> bool:
    """True if `line` is the payload's line (or the fan payload's). The caller then drops it."""
    if fan.async_handle_line(hass, entry_id, line):
        return True
    values = parse_line(line)
    if values is None:
        return False
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if data is None or "live_sensors" not in data:
        return True
    state = data["live_sensors"]
    if values.get("state") in ("stopped", "stop-requested"):
        _LOGGER.debug("Live temperatures: payload says %s", values["state"])
        return True

    now = _now(hass)
    state["last_line"] = now
    if not state["enabled"]:
        # a copy still runs, e.g. the switch went off while the console was in rest
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and _due(state["last_stop"], now) and _on_and_idle(data):
            state["last_stop"] = now
            _run(hass, entry, state, async_stop(hass, entry), "stop")
        return True

    new = readings(values)
    if new:
        state["last_reading"] = now
        data["klog_data"].update({**dict.fromkeys(SENSOR_KEYS), **new})  # a field this line lacks has no current value
        _push(data)
        if "thr" in values and (entry := hass.config_entries.async_get_entry(entry_id)) is not None:
            fan.async_check(hass, entry, values["thr"], now)
        if values.get("ver", 1) < PAYLOAD_VERSION:
            # a copy from an older build (it ran through an update): replace it, same rules as a start
            entry = hass.config_entries.async_get_entry(entry_id)
            if entry is not None and _due(state["last_start"], now) and _on_and_idle(data):
                state["last_start"] = now
                _LOGGER.info("Live temperatures: replacing a payload from an older build")
                _run(hass, entry, state, async_start(hass, entry), "start")
    return True


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Once a minute: drop stale readings, and start the payload when no copy has been heard from."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is None or "live_sensors" not in data:
        return
    state = data["live_sensors"]
    now = _now(hass)
    on = (data["coordinator"].data or {}).get("power_state") == POWER_ON
    if state["last_reading"] is not None and (not on or now - state["last_reading"] >= FRESH_FOR):
        _clear(data)  # also in rest / off, so a wake doesn't bring back the reading from before it
    if not state["enabled"] or state["connected_at"] is None or not _on_and_idle(data):
        return
    if state["last_line"] is not None and state["last_line"] >= state["connected_at"]:
        quiet = now - state["last_line"] >= FRESH_FOR  # it was running and went silent (reboot, crash)
    else:
        quiet = now - state["connected_at"] >= START_AFTER_CONNECT  # nothing heard since klog connected
    if quiet and _due(state["last_start"], now):
        state["last_start"] = now
        _run(hass, entry, state, async_start(hass, entry), "start")


def _due(last: float | None, now: float) -> bool:
    return last is None or now - last >= RETRY


def _on_and_idle(data: dict[str, Any]) -> bool:
    from .console_info import _on_and_idle as on_and_idle  # same rule as the automatic console-info read

    return on_and_idle(data)


def _run(hass: HomeAssistant, entry: ConfigEntry, state: dict[str, Any], coro, what: str) -> None:
    """One automatic send at a time; failures only reach the debug log (the next check tries again)."""
    task = state.get("task")
    if task is not None and not task.done():
        coro.close()
        return

    async def _guarded() -> None:
        try:
            await coro
        except HomeAssistantError as err:
            _LOGGER.debug("Live temperatures: automatic %s not sent: %s", what, err)

    state["task"] = entry.async_create_background_task(
        hass, _guarded(), name=f"{DOMAIN}_live_sensors_{what}_{entry.entry_id}"
    )


async def _async_send(hass: HomeAssistant, entry: ConfigEntry, payload: Path, what: str) -> None:
    from . import _send_bin_tcp  # __init__ imports this module
    from .rest import async_payload_preflight

    data = hass.data[DOMAIN][entry.entry_id]
    firmware = await async_payload_preflight(hass, entry, what)
    _LOGGER.info("Live temperatures: sending %s to %s (firmware %s)", payload.name, data["host"], firmware)
    await _send_bin_tcp(data["host"], int(data["binloader_port"]), str(payload))


async def async_start(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Send the resident payload (replaces a running copy). HomeAssistantError if it isn't sent."""
    if not hass.data[DOMAIN][entry.entry_id]["klog_state_machine"].klog_connected:
        # the readings come back over klog
        raise HomeAssistantError("Klog isn't connected, so the readings would get lost; nothing sent")
    await _async_send(hass, entry, SENSORS_PAYLOAD, "Live temperatures")


async def async_stop(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Send the stop payload. HomeAssistantError if it isn't sent."""
    await _async_send(hass, entry, STOP_PAYLOAD, "Stopping live temperatures")


async def async_set_enabled(hass: HomeAssistant, entry: ConfigEntry, enabled: bool, *, send: bool) -> None:
    """The switch. With `send`, act now when the console is on (even mid-game: it's the user's own action)."""
    data = hass.data[DOMAIN][entry.entry_id]
    state = data["live_sensors"]
    state["enabled"] = enabled
    if not enabled:
        _clear(data)
    if not send or (data["coordinator"].data or {}).get("power_state") != POWER_ON:
        return  # in rest / off: the minute check starts it later, or a stray line gets the stop
    if enabled:
        state["last_start"] = _now(hass)
        await async_start(hass, entry)
    else:
        state["last_stop"] = _now(hass)
        await async_stop(hass, entry)
