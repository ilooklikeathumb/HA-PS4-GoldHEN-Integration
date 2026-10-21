"""Console info on the device page: firmware, hardware model and serial number.

Firmware comes from DDP `system-version` (answered on and in rest, no GoldHEN needed). The same reply's `host-id` is
the console's MAC: it goes on the device as a network connection, which HA's DHCP watcher (IP follow,
config_flow `async_step_dhcp`) and router trackers such as UniFi's match on. Model and serial need code
on the console: `info_payload/console-info.elf` (source `payload_src/console-info/`) prints one klog line,
  ps4_goldhen console-info: model=CUH-7215B B01X;serial=<serial>
and the klog listener hands every line to `async_handle_line` first. That line stops there, so the serial is kept
out of the Klog tab (and the recorder) and Klog Last Line. Values are cached in .storage (private), not in the entry
options, because saving options reloads the entry. Read once per console, automatically; the "Read console info"
button reads again.

How others do it: HA core's ps4 integration takes firmware from DDP and hard-codes the model; PS5
payloads read model and serial with sceKernelGetHwModelName / HwSerialNumber. A GoldHEN plugin can't: it runs inside
the game's sandbox, where those calls crash the game. No Console ID (IDPS): a payload process gets a masked one —
sceKernelGetIdPs and sysctl machdep.idps both gave the same placeholder (7 of the 8 unique bytes zero).
Orbis Toolbox gets the real one because it runs inside the system UI, so this integration has no Console ID.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store

from .const import DOMAIN, POWER_ON, SENSOR_FW_VERSION, SENSOR_HW_MODEL, SENSOR_TITLE_ID
from .ddp import DDPReply, async_search
from .entity import ps4_device_info

_LOGGER = logging.getLogger(__name__)

# Not in bundled_payloads/: those are copied to the panel's list, and a send from there skips the checks
INFO_PAYLOAD = Path(__file__).parent / "info_payload" / "console-info.elf"
MARKER = "ps4_goldhen console-info:"

_STORE_VERSION = 1
_AUTO_DELAY = 20       # s after klog connects before the console-info payload may go out
_AUTO_WAIT = 300       # s to wait for the console to be on and out of games; after that, next connect
_AUTO_RECHECK = 10     # s between those in-memory checks (no network traffic)

_FIELDS = {
    "model":  re.compile(r"^[A-Za-z0-9-]{1,32}( [A-Za-z0-9_-]{1,16})?$"),
    "serial": re.compile(r"^[A-Za-z0-9-]{1,32}$"),
}
_CACHED = (*_FIELDS, "firmware", "mac")
_HOST_ID_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_CUH_RE = re.compile(r"^CUH-(\d)")
_FAMILY = {"1": "PlayStation 4", "2": "PlayStation 4 Slim", "7": "PlayStation 4 Pro"}
DEFAULT_MODEL = "PlayStation 4"


def parse_info_line(line: str) -> dict[str, str] | None:
    """The payload's line → its valid fields ({} if none); None if it isn't that line."""
    _, marker, rest = line.partition(MARKER)
    if not marker:
        return None
    info = {}
    for field in rest.strip().split(";"):
        key, sep, value = field.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key in _FIELDS and _FIELDS[key].match(value):
            info[key] = value
    return info


def mac_of(reply: DDPReply | None) -> str | None:
    """DDP `host-id` → MAC: `00005E005301` → `00:00:5e:00:53:01`; None if it isn't one."""
    host_id = (reply.headers.get("host-id") or "").strip() if reply is not None else ""
    return dr.format_mac(host_id) if _HOST_ID_RE.match(host_id) else None


def model_family(model: str | None) -> str:
    """`CUH-7215B B01X` → `PlayStation 4 Pro`: CUH-1xxx original, 2xxx Slim, 7xxx Pro."""
    m = _CUH_RE.match(model or "")
    return _FAMILY.get(m.group(1), DEFAULT_MODEL) if m else DEFAULT_MODEL


def device_fields(info: dict[str, Any]) -> dict[str, Any]:
    """Cached info → device registry fields. Only known ones, so nothing already on the device page gets blanked.

    `CUH-7215B B01X` splits into model_id `CUH-7215B` and hw_version `B01X` (what the suffix encodes isn't documented).
    """
    model_id, _, hw_version = (info.get("model") or "").partition(" ")
    fields = {"model": model_family(info.get("model"))}
    if model_id:
        fields["model_id"] = model_id
    if hw_version:
        fields["hw_version"] = hw_version
    if info.get("serial"):
        fields["serial_number"] = info["serial"]
    if info.get("firmware"):
        fields["sw_version"] = info["firmware"]
    if info.get("mac"):
        # the registry only ever adds connections, so the entities' own DeviceInfo (none) can't drop it
        fields["connections"] = {(dr.CONNECTION_NETWORK_MAC, info["mac"])}
    return fields


def _complete(info: dict[str, Any]) -> bool:
    return all(info.get(key) for key in _FIELDS)


@callback
def async_apply(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Put the cached info on the device page and in the diagnostic sensors' data."""
    data = hass.data[DOMAIN][entry.entry_id]
    info = data["console_info"]["info"]
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=entry.entry_id, **ps4_device_info(entry), **device_fields(info)
    )
    if info.get("mac"):
        # get_or_create only adds connections. After a console swap (new host, new MAC) the old MAC has to go, or a
        # DHCP sighting of the old console would move the entry back to it.
        wanted = {c for c in device.connections if c[0] != dr.CONNECTION_NETWORK_MAC}
        wanted.add((dr.CONNECTION_NETWORK_MAC, info["mac"]))
        if device.connections != wanted:
            registry.async_update_device(device.id, new_connections=wanted)
    klog_data = data["klog_data"]
    klog_data[SENSOR_FW_VERSION] = info.get("firmware")
    klog_data[SENSOR_HW_MODEL] = info.get("model")
    coordinator = data["coordinator"]
    coordinator.async_set_updated_data({**(coordinator.data or {}), **klog_data})


@callback
def _async_update(hass: HomeAssistant, entry: ConfigEntry, new: dict[str, str]) -> None:
    state = hass.data[DOMAIN][entry.entry_id]["console_info"]
    merged = {**state["info"], **new}
    if merged == state["info"]:
        return
    state["info"] = merged
    state["store"].async_delay_save(lambda: state["info"], 1)
    async_apply(hass, entry)


async def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Load the cache and show it; the klog connect handler does the rest."""
    store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.console_info.{entry.entry_id}", private=True)
    cached = await store.async_load()
    cached = cached if isinstance(cached, dict) else {}
    info = {key: value for key, value in cached.items() if key in _CACHED}
    hass.data[DOMAIN][entry.entry_id]["console_info"] = {
        "store": store,
        "info": info,
        "auto_sent": False,  # one automatic send per HA run at most; the button can always send
    }
    if info != cached:  # drops fields this version doesn't keep
        store.async_delay_save(lambda: info, 1)
    async_apply(hass, entry)
    # one DDP search per setup (DDP answers in rest too), so the MAC is there without waiting for klog
    # and follows a host change made in Configure
    entry.async_create_background_task(hass, _async_refresh_ddp(hass, entry), name=f"{DOMAIN}_ddp_{entry.entry_id}")


async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted: drop its cached serial too."""
    await Store(hass, _STORE_VERSION, f"{DOMAIN}.console_info.{entry.entry_id}", private=True).async_remove()


@callback
def async_handle_line(hass: HomeAssistant, entry_id: str, line: str) -> bool:
    """True if `line` is the payload's console-info line. The caller then drops it: nothing else may see it."""
    info = parse_info_line(line)
    if info is None:
        return False
    entry = hass.config_entries.async_get_entry(entry_id)
    if entry is None or entry_id not in hass.data.get(DOMAIN, {}):
        return True
    if not info:
        _LOGGER.warning("Console info: the payload's line had no readable fields")
        return True
    _LOGGER.info("Console info read: %s (%s)", info.get("model", "model unknown"), ", ".join(sorted(info)))
    _async_update(hass, entry, info)
    return True


@callback
def async_on_klog_connected(hass: HomeAssistant, entry_id: str) -> None:
    """Each klog connect: refresh the firmware from DDP, then read the console info if none is cached yet."""
    entry = hass.config_entries.async_get_entry(entry_id)
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if entry is None or data is None:
        return
    task = data["console_info"].get("task")
    if task is not None and not task.done():
        return
    data["console_info"]["task"] = entry.async_create_background_task(
        hass, _async_after_connect(hass, entry), name=f"{DOMAIN}_console_info_{entry_id}"
    )


def _on_and_idle(data: dict[str, Any]) -> bool:
    """Console on per klog and no game running: an automatic payload never lands mid-game."""
    from . import _is_real_game_title_id  # __init__ imports this module

    klog = data["coordinator"].data or {}
    return (
        data["klog_state_machine"].klog_connected
        and klog.get("power_state") == POWER_ON
        and not _is_real_game_title_id(klog.get(SENSOR_TITLE_ID))
    )


async def _async_refresh_ddp(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """MAC and power + running game from one DDP search (each setup and klog connect); no reply (fully off,
    unreachable) changes nothing. The firmware only when none is cached or the MAC says it's another console: the
    device info is read when the console is added, then only by the button (`async_read` takes it from its own
    check)."""
    if (data := hass.data.get(DOMAIN, {}).get(entry.entry_id)) is None:
        return
    reply = None
    with contextlib.suppress(OSError):
        reply = await async_search(data["host"])
    if reply is None or entry.entry_id not in hass.data.get(DOMAIN, {}):
        return
    info = hass.data[DOMAIN][entry.entry_id]["console_info"]["info"]
    mac = mac_of(reply)
    new = {"mac": mac} if mac else {}
    if reply.firmware and (not info.get("firmware") or (mac and info.get("mac") and mac != info["mac"])):
        new["firmware"] = reply.firmware
    if new:
        _async_update(hass, entry, new)
    from . import async_apply_ddp_status  # __init__ imports this module

    # the running game + on, so the automatic read below never goes out while a game runs
    async_apply_ddp_status(hass, entry.entry_id, reply)


async def _async_after_connect(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    state = data["console_info"]

    await _async_refresh_ddp(hass, entry)

    if _complete(state["info"]) or state["auto_sent"]:
        return
    await asyncio.sleep(_AUTO_DELAY)
    waited = _AUTO_DELAY
    while not _on_and_idle(data):
        if waited >= _AUTO_WAIT or not data["klog_state_machine"].klog_connected:
            _LOGGER.debug("Console info: console not on and idle; trying again at the next klog connect")
            return
        await asyncio.sleep(_AUTO_RECHECK)
        waited += _AUTO_RECHECK
    if _complete(state["info"]):
        return  # the button read it while we waited for the game to end

    state["auto_sent"] = True
    try:
        await async_read(hass, entry)
    except HomeAssistantError as err:
        _LOGGER.debug("Console info: automatic read not sent: %s", err)


async def async_read(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Send the console-info payload; its klog line fills in the rest. HomeAssistantError if it isn't sent."""
    from . import _send_bin_tcp  # __init__ imports this module
    from .rest import async_payload_preflight

    data = hass.data[DOMAIN][entry.entry_id]
    if not data["klog_state_machine"].klog_connected:
        # the answer comes back over klog
        raise HomeAssistantError("Klog isn't connected, so the console info would get lost; nothing sent")
    firmware = await async_payload_preflight(hass, entry, "Reading console info")
    _async_update(hass, entry, {"firmware": firmware})

    _LOGGER.info("Console info: sending %s to %s (firmware %s)", INFO_PAYLOAD.name, data["host"], firmware)
    await _send_bin_tcp(data["host"], int(data["binloader_port"]), str(INFO_PAYLOAD))
