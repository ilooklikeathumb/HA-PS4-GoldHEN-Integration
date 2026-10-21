"""Rest mode from HA: media_player.turn_off and the "Rest mode" button.

Sends the integration's own `rest_payload/rest-mode.elf` to GoldHEN's BinLoader (port 9090). The payload calls Sony's
`sceSystemStateMgrEnterStandby`, the call behind Power → Enter Rest Mode, and exits; source and build in
`payload_src/rest-mode/` (ps4-payload-dev/sdk, GPL-3+). No pairing, so it works for any GoldHEN user whose BinLoader
runs ELF payloads (GoldHEN 2.4b18.10+). The power state then follows from klog: `Enter Standby Mode Request` →
`State Change: MAIN_ON_STANDBY`.

A payload built for another firmware can freeze the console, and the SDK's startup code patches the kernel from a
per-firmware table. So before sending: the console must be on (klog and DDP `SRCH` both say so) and its DDP
`system-version` must be one the SDK lists as confirmed.
"""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import _send_bin_tcp
from .const import DOMAIN, POWER_OFF, POWER_REST
from .ddp import async_search

_LOGGER = logging.getLogger(__name__)

# Not in bundled_payloads/: those are copied to the panel's list, and a send from there skips the checks below
REST_PAYLOAD = Path(__file__).parent / "rest_payload" / "rest-mode.elf"

# ps4-payload-dev/sdk v0.9 README, "Working firmwares": the checked (confirmed) ones only
CONFIRMED_FIRMWARE = frozenset({
    "4.71", "4.72", "4.73", "4.74", "5.03", "5.04", "5.05", "5.06", "5.07",
    "7.00", "7.01", "7.02", "7.50", "7.51", "7.55", "8.00", "8.01", "8.02", "8.03", "8.50", "8.52",
    "9.00", "9.03", "9.04", "9.50", "9.51", "9.60", "10.00", "10.01", "10.50", "10.70", "10.71",
    "11.00", "11.02", "11.50", "11.52", "12.00", "12.02",
})

async def async_payload_preflight(hass: HomeAssistant, entry: ConfigEntry, what: str, *, after_wake: bool = False) -> str:
    """The checks before any of the integration's own payloads is sent (rest mode, console info).

    `after_wake`: HA has just woken the console and logged in. klog can still say rest for a minute or two
    after a deep-rest wake, so only DDP decides then.
    Returns the console's firmware; raises HomeAssistantError naming `what` (shown to the user) if nothing may be sent.
    """
    data = hass.data[DOMAIN][entry.entry_id]
    power = (data["coordinator"].data or {}).get("power_state")
    if power == POWER_REST and not after_wake:
        raise HomeAssistantError("The PS4 is in rest mode")
    if power == POWER_OFF and not after_wake:
        raise HomeAssistantError("The PS4 is off")

    try:
        reply = await async_search(data["host"])
    except OSError as err:
        raise HomeAssistantError(f"PS4 firmware check (DDP) failed: {err}") from err
    if reply is None:
        raise HomeAssistantError("The PS4 didn't answer the firmware check (DDP, UDP 987)")
    if reply.code != 200:
        raise HomeAssistantError(f"The PS4 isn't on (DDP status {reply.code})")
    if reply.firmware not in CONFIRMED_FIRMWARE:
        raise HomeAssistantError(
            f"{what} isn't confirmed for PS4 firmware {reply.firmware or reply.system_version or 'unknown'}; nothing sent"
        )
    return reply.firmware


async def async_enter_rest(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Put this entry's PS4 into rest mode. Raises HomeAssistantError (shown to the user) if it isn't sent."""
    data = hass.data[DOMAIN][entry.entry_id]
    if (data["coordinator"].data or {}).get("power_state") == POWER_REST:
        raise HomeAssistantError("The PS4 is already in rest mode")
    firmware = await async_payload_preflight(hass, entry, "Rest mode")

    _LOGGER.info("Rest mode: sending %s to %s (firmware %s)", REST_PAYLOAD.name, data["host"], firmware)
    await _send_bin_tcp(data["host"], int(data["binloader_port"]), str(REST_PAYLOAD))
