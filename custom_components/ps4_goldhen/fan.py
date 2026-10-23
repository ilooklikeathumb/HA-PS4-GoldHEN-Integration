"""Fan control from HA: the "Fan control" switch and the "Fan target temperature" slider.

The console's fan controller holds the chip at a target temperature: the fan idles until the chip reaches it, then
spins up as hard as it takes to stay there. Lower = cooler and louder. That one number is all there is: no fan
curve and no RPM on a PS4 (the kernel reports duty only). The community calls it the "fan threshold".

- **Fan control off** (the default) = the console's own target, its factory value unless another tool changed it. Turning
  it off sends back what the console held before Fan control took over (`before`, kept in .storage), once; in rest /
  off, or after a send that failed, that waits until the console reports in (`restore_to`, also kept in .storage, so an
  HA restart in between doesn't lose it). `before` is what the console reported when Fan control went on, or the
  `was` of HA's first send after that. Unknown = HA never changed the target, so Off sends nothing.
  Off never sends a fixed 83: only the CUH-72xx Pros ship with 83, the others with 78-80, so a fixed 83 would leave
  them hotter than stock until unplugged. Factory values per model: xalfie's table,
  gbatemp.net/threads/default-fan-threshold-payload.623315.
- **On** = HA holds the slider's value (kept across restarts). Moving the slider sends it at once. The slider goes from
  50 up to the model's factory target (`FACTORY_TARGETS`, model from console_info), so HA never holds a console
  hotter than it left the factory; until the model is known, up to the lowest factory target in the table. When the
  live sensors line shows the console holding something else (unplugged, another tool, a send that failed), HA sends
  it again: console on, no game running, at most once per 10 min.

A send is `fan_payload/ps4-fan.elf` (source `payload_src/fan/`) with the °C written over a 16-byte marker, like the
launch payload. It reads channel 0's 28-byte object from /dev/icc_fan, changes byte 5 only, writes it back, reads it
again, and prints
  ps4_goldhen fan: state=set;thr=72;was=83
which `async_handle_line` consumes (kept out of the Klog tab and the recorder). What the console holds comes from
that line and from the live sensors payload's `thr` every 30 s (needs Live temperatures on); the slider shows it as
`console_value`.

Fan probe (PS4 Pro CUH-7215B, FW 12.00): stock 83 °C on channel 0; a written value holds through game launch and
exit and through rest, and survives reboots until the console is unplugged (Zer0xFF).
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store

from .const import DOMAIN, POWER_ON, SENSOR_FAN_THRESHOLD

_LOGGER = logging.getLogger(__name__)

# Not in bundled_payloads/: those are copied to the panel's list, and a send from there skips the checks
FAN_PAYLOAD = Path(__file__).parent / "fan_payload" / "ps4-fan.elf"
_MARKER = b"PS4GH_FAN_THR__\x00"
LINE_MARKER = "ps4_goldhen fan:"

MIN_THRESHOLD = 50  # other tools allow 40-60; idle runs 45-70 °C; Zer0xFF's 0 sent the fan to violent speeds
MAX_THRESHOLD = 83  # the highest factory target (CUH-72xx Pros); the payload has the same limits
# Factory target per model series (xalfie's table, above). CUH-12xx: one reading on a CUH-1215A after a power cut;
# the table has none for that series.
FACTORY_TARGETS = {"CUH-10": 80, "CUH-11": 80, "CUH-12": 78, "CUH-20": 80, "CUH-22": 80,
                   "CUH-70": 79, "CUH-71": 79, "CUH-72": 83}
UNKNOWN_MODEL_TARGET = min(FACTORY_TARGETS.values())  # model not read yet or not in the table
CONFIRM_TIMEOUT = 15   # s for the payload's line (it comes through /dev/console within a second)
AUTO_RETRY = 600       # s between automatic sends
_STORE_VERSION = 1

_STEPS = ("target", "open", "get", "set", "verify")


def build_payload(elf: bytes, threshold: int) -> bytes:
    """The fan ELF with its marker replaced by the target (ASCII digits, NUL-padded)."""
    if not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise HomeAssistantError(f"Fan target must be {MIN_THRESHOLD}-{MAX_THRESHOLD} °C, not {threshold}")
    if elf.count(_MARKER) != 1:
        raise HomeAssistantError("The bundled fan payload is damaged (marker not found once)")
    return elf.replace(_MARKER, str(threshold).encode().ljust(len(_MARKER), b"\0"))


def parse_line(line: str) -> dict[str, Any] | None:
    """The fan payload's line → its fields; None if it isn't that line."""
    _, marker, rest = line.partition(LINE_MARKER)
    if not marker:
        return None
    values: dict[str, Any] = {}
    for field in rest.strip().split(";"):
        key, sep, value = (part.strip() for part in field.partition("="))
        if not sep:
            continue
        if key == "state" and value in ("set", "failed"):
            values["state"] = value
        elif key == "step" and value in _STEPS:
            values["step"] = value
        elif key in ("thr", "was") and value.isdigit() and 1 <= int(value) <= 124:
            values[key] = int(value)
        elif key == "errno" and value.isdigit():
            values["errno"] = int(value)
    return values


def init_state(data: dict[str, Any]) -> None:
    """Per-entry state (live_sensors.async_setup calls this). The switch and slider restore theirs when added."""
    data["fan"] = {
        "enabled": False,          # the Fan control switch
        "wanted": None,            # the slider; None = the model's factory target
        "before": None,            # the console's own target from before Fan control took over (.storage)
        "restore_to": None,        # what a pending Off sends (.storage while pending)
        "restore_pending": False,  # switched off while `restore_to` couldn't be sent yet
        "store": None,
        "last_auto": None,         # loop time of the last automatic send
        "task": None,              # the automatic send in flight, if any
        "waiter": None,            # future for the payload's line while a send runs
    }


def _store(hass: HomeAssistant, entry: ConfigEntry) -> Store:
    return Store(hass, _STORE_VERSION, f"{DOMAIN}.fan.{entry.entry_id}")


async def async_load(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """`before` and a pending Off's `restore_to` from .storage, so an HA restart while Fan control is on, or before an
    Off reached the console, still knows what the console goes back to."""
    fan = hass.data[DOMAIN][entry.entry_id]["fan"]
    fan["store"] = _store(hass, entry)
    saved = await fan["store"].async_load()
    saved = saved if isinstance(saved, dict) else {}
    fan["before"] = _target(saved.get("before"))
    fan["restore_to"] = _target(saved.get("restore_to"))
    fan["restore_pending"] = fan["restore_to"] is not None


def _target(value: Any) -> int | None:
    return value if isinstance(value, int) and 1 <= value <= 124 else None


async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await _store(hass, entry).async_remove()


def _save(fan: dict[str, Any]) -> None:
    """`before`, and what a pending Off still owes the console, to .storage."""
    if fan["store"] is not None:
        fan["store"].async_delay_save(
            lambda: {"before": fan["before"], "restore_to": fan["restore_to"] if fan["restore_pending"] else None}, 1)


def off_value(hass: HomeAssistant, entry_id: str) -> int | None:
    """What turning Fan control off sends back (None: nothing, HA hasn't changed the console's own target)."""
    data = _data(hass, entry_id)
    if data is None:
        return None
    fan = data["fan"]
    return fan["before"] if fan["enabled"] else fan["restore_to"] if fan["restore_pending"] else None


def _data(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data if data and "fan" in data else None


def is_enabled(hass: HomeAssistant, entry_id: str) -> bool:
    data = _data(hass, entry_id)
    return bool(data and data["fan"]["enabled"])


def factory_target(model: str | None) -> int:
    """`CUH-7215B B01X` → 83: the model's factory target; UNKNOWN_MODEL_TARGET if the model isn't in the table."""
    return FACTORY_TARGETS.get((model or "").strip().upper()[:6], UNKNOWN_MODEL_TARGET)


def max_threshold(hass: HomeAssistant, entry_id: str) -> int:
    """The slider's top for this console: its model's factory target."""
    data = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    return factory_target((data.get("console_info") or {}).get("info", {}).get("model"))


def wanted(hass: HomeAssistant, entry_id: str) -> int | None:
    """What Fan control holds: the slider's value, never above the console's factory target."""
    data = _data(hass, entry_id)
    if data is None:
        return None
    top = max_threshold(hass, entry_id)
    value = data["fan"]["wanted"]
    return top if value is None else min(value, top)


def restore_wanted(hass: HomeAssistant, entry_id: str, value: int) -> None:
    """The slider's value from before an HA restart. Kept as it was: `wanted` caps it once the model is known."""
    if MIN_THRESHOLD <= value <= MAX_THRESHOLD and (data := _data(hass, entry_id)) is not None:
        data["fan"]["wanted"] = value


def console_value(hass: HomeAssistant, entry_id: str) -> int | None:
    """The target the console last reported holding (None in rest / off or before the first report)."""
    data = _data(hass, entry_id)
    return (data["coordinator"].data or {}).get(SENSOR_FAN_THRESHOLD) if data else None


def _on(data: dict[str, Any]) -> bool:
    return (data["coordinator"].data or {}).get("power_state") == POWER_ON


@callback
def async_handle_line(hass: HomeAssistant, entry_id: str, line: str) -> bool:
    """True if `line` is the fan payload's line. The caller then drops it: nothing else may see it."""
    values = parse_line(line)
    if values is None:
        return False
    data = _data(hass, entry_id)
    if data is None:
        return True
    if "thr" in values:  # what the console holds now, set or not
        data["klog_data"][SENSOR_FAN_THRESHOLD] = values["thr"]
        coordinator = data["coordinator"]
        coordinator.async_set_updated_data({**(coordinator.data or {}), **data["klog_data"]})
    fan = data["fan"]
    if values.get("state") == "set":
        if not fan["enabled"] and fan["restore_pending"] and values.get("thr") == fan["restore_to"]:
            fan["restore_pending"] = False
            _save(fan)
        if fan["enabled"] and fan["before"] is None and "was" in values:
            fan["before"] = values["was"]  # Fan control went on in rest / off: the first send tells
            _save(fan)
    waiter = fan["waiter"]
    if waiter is not None and not waiter.done():
        waiter.set_result(values)
    if values.get("state") == "failed":
        _LOGGER.warning("Fan control: the payload failed at %s (errno %s)", values.get("step"), values.get("errno"))
    return True


async def async_set_threshold(hass: HomeAssistant, entry: ConfigEntry, threshold: int) -> None:
    """Send the fan payload and wait for its line. HomeAssistantError (shown to the user) if it isn't set."""
    from . import _send_bin_bytes  # __init__ imports live_sensors, which imports this module
    from .rest import async_payload_preflight

    data = hass.data[DOMAIN][entry.entry_id]
    payload = build_payload(await hass.async_add_executor_job(FAN_PAYLOAD.read_bytes), threshold)
    if not data["klog_state_machine"].klog_connected:
        # the confirmation comes back over klog
        raise HomeAssistantError("Klog isn't connected, so the result would get lost; nothing sent")
    firmware = await async_payload_preflight(hass, entry, "Fan control")

    fan = data["fan"]
    if fan["waiter"] is not None and not fan["waiter"].done():
        raise HomeAssistantError("A fan target change is still in progress")
    waiter = fan["waiter"] = hass.loop.create_future()
    try:
        _LOGGER.info("Fan control: sending %s (%d °C) to %s (firmware %s)", FAN_PAYLOAD.name, threshold,
                     data["host"], firmware)
        await _send_bin_bytes(data["host"], int(data["binloader_port"]), payload, FAN_PAYLOAD.name)
        try:
            values = await asyncio.wait_for(waiter, CONFIRM_TIMEOUT)
        except asyncio.TimeoutError as err:
            raise HomeAssistantError("The PS4 didn't confirm the fan target (no klog line)") from err
    finally:
        fan["waiter"] = None
    if values.get("state") != "set" or values.get("thr") != threshold:
        raise HomeAssistantError(
            f"The PS4 didn't take the fan target: failed at {values.get('step', 'unknown')}"
            f" (errno {values.get('errno', '?')}), it holds {values.get('thr', 'unknown')} °C"
        )


async def async_set_wanted(hass: HomeAssistant, entry: ConfigEntry, value: int, *, send: bool) -> None:
    """The slider. With `send`: applied now if Fan control is on and the console is on (else kept for later)."""
    top = max_threshold(hass, entry.entry_id)
    if not MIN_THRESHOLD <= value <= top:
        raise HomeAssistantError(
            f"Fan target must be {MIN_THRESHOLD}-{top} °C on this console (its factory target is the top), not {value}"
        )
    data = hass.data[DOMAIN][entry.entry_id]
    data["fan"]["wanted"] = value
    if send and data["fan"]["enabled"] and _on(data):
        await async_set_threshold(hass, entry, value)


async def async_set_enabled(hass: HomeAssistant, entry: ConfigEntry, enabled: bool, *, send: bool) -> None:
    """The switch. With `send` and the console on: on sends the slider's value, off sends back what the console held
    before (even mid-game: the user's action). In rest / off it waits for the console's next report (`async_check`)."""
    data = hass.data[DOMAIN][entry.entry_id]
    fan = data["fan"]
    fan["enabled"] = enabled
    if not send:
        return
    if enabled:
        if fan["restore_pending"]:  # the last Off never reached the console: it still holds HA's value
            fan["before"] = fan["restore_to"]
        fan["restore_pending"] = False
        if fan["before"] is None and (held := console_value(hass, entry.entry_id)) is not None:
            fan["before"] = held
        _save(fan)
        target = wanted(hass, entry.entry_id)
    else:
        target = fan["restore_to"] = fan["before"]
        fan["before"] = None
        fan["restore_pending"] = target is not None
        _save(fan)  # owed until it lands: a failed send or an HA restart keeps it
        if target is None:
            return  # HA never changed the console's own target
    if _on(data):
        if console_value(hass, entry.entry_id) != target:
            await async_set_threshold(hass, entry, target)
        if fan["restore_pending"]:
            fan["restore_pending"] = False
            _save(fan)


@callback
def async_check(hass: HomeAssistant, entry: ConfigEntry, held: int, now: float) -> None:
    """The console reported holding `held` °C (live sensors line): send the right target if it differs."""
    data = _data(hass, entry.entry_id)
    if data is None:
        return
    fan = data["fan"]
    if fan["enabled"]:
        target = wanted(hass, entry.entry_id)
    elif fan["restore_pending"]:
        if held == fan["restore_to"]:
            fan["restore_pending"] = False
            _save(fan)
            return
        target = fan["restore_to"]
    else:
        return  # not HA's to manage: the console's own value or another tool's
    if held == target or not (fan["last_auto"] is None or now - fan["last_auto"] >= AUTO_RETRY):
        return
    from .console_info import _on_and_idle  # same rule as the other automatic sends

    if not _on_and_idle(data):
        return
    task = fan["task"]
    if task is not None and not task.done():
        return
    fan["last_auto"] = now
    _LOGGER.info("Fan control: the console holds %d °C, sending %d °C", held, target)

    async def _send() -> None:
        try:
            await async_set_threshold(hass, entry, target)
        except HomeAssistantError as err:
            _LOGGER.warning("Fan control: automatic send of %d °C failed: %s", target, err)

    fan["task"] = entry.async_create_background_task(hass, _send(), name=f"{DOMAIN}_fan_{entry.entry_id}")
