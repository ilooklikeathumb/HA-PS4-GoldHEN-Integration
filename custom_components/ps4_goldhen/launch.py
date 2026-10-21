"""Start and close games from HA: the media player's source list + game browser, the "Close game"
button, and bringing back the suspended game after a wake.

Sends the integration's own `launch_payload/launch-app.elf` to GoldHEN's BinLoader (port 9090) with the
title ID written into it: the ELF holds a 16-byte marker that is replaced by the title ID (bytes 0-9, NUL-padded; all
NUL = no title) and a flag in byte 10: `K` = close the running game first, `R` (no title) = only bring back the
suspended game. The payload calls what Itemzflow calls: `sceSystemServiceKillApp(sceSystemServiceGetAppIdOfBigApp(),
-1, 0, 0)` to close, then `sceLncUtilLaunchApp` with SkipSystemUpdateCheck for the logged-in user; source and build in
`payload_src/launch-app/` (ps4-payload-dev/sdk, GPL-3+). Result on klog: `ps4_goldhen launch: <title ID> -> 0x<code>`;
the media player follows from AppFocusChanged.

Picking a game while another one runs closes that one without asking: picking means switching.
Without the close, the console itself asks "The following application will close" on the TV.
Same title already running → sent anyway (the payload leaves it running, see below). Tested live on two PS4 Pros
(FW 11.00 + 12.00).

The game running before rest (or before the PS button) stays suspended as the console's "big app"; klog
clears the title on rest, so HA can't tell, but the payload can. When the picked title is that app, the payload never
closes it: it launches the same title, which answers "already running" and makes the console resume the existing
process (what pressing its tile does), so no progress is lost. Tested on a PS4 Pro (FW 12.00) after an HA rest + wake +
login: two games resumed in their own processes, one of them at the same spot.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from . import _send_bin_bytes
from .const import DOMAIN
from .rest import async_payload_preflight

_LOGGER = logging.getLogger(__name__)

# Not in bundled_payloads/: those are copied to the panel's list, and a send from there skips the checks below
LAUNCH_PAYLOAD = Path(__file__).parent / "launch_payload" / "launch-app.elf"

# right after a wake GoldHEN's BinLoader isn't listening yet (9090 refused a connect 1 s after
# LOGIN). A refused connect means nobody is there, so an after-wake send is retried, never a bare probe.
PAYLOADER_WAIT = 25.0
PAYLOADER_RETRY = 0.5

_MARKER = b"PS4GH_TITLE_ID_\x00"
_TITLE_ID = re.compile(r"[A-Z]{4}\d{5}")


def build_payload(elf: bytes, title_id: str | None, close_first: bool, resume_only: bool = False) -> bytes:
    """The launch ELF with its marker replaced: title ID (or none = close / resume only) + the flag."""
    if title_id is not None and not _TITLE_ID.fullmatch(title_id):
        raise HomeAssistantError(f"Not a PS4 title ID: {title_id}")
    if resume_only and title_id is not None:
        raise HomeAssistantError("Resume takes no title ID: it brings back whichever game is suspended")
    if elf.count(_MARKER) != 1:
        raise HomeAssistantError("The bundled launch payload is damaged (marker not found once)")
    flag = b"R" if resume_only else b"K" if close_first else b"\0"
    slot = (title_id or "").encode().ljust(10, b"\0") + flag
    return elf.replace(_MARKER, slot.ljust(len(_MARKER), b"\0"))


async def _async_send(
    hass: HomeAssistant, entry: ConfigEntry, title_id: str | None, what: str, *,
    resume_only: bool = False, after_wake: bool = False,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    firmware = await async_payload_preflight(hass, entry, what, after_wake=after_wake)
    elf = await hass.async_add_executor_job(LAUNCH_PAYLOAD.read_bytes)
    payload = build_payload(elf, title_id, close_first=not resume_only, resume_only=resume_only)
    _LOGGER.info("%s: sending %s to %s (firmware %s)", what, title_id or ("resume" if resume_only else "close"),
                 data["host"], firmware)
    deadline = time.monotonic() + (PAYLOADER_WAIT if after_wake else 0)
    refused = 0
    while True:
        try:
            await _send_bin_bytes(data["host"], int(data["binloader_port"]), payload, LAUNCH_PAYLOAD.name)
            break
        except HomeAssistantError as err:
            if not isinstance(err.__cause__, ConnectionRefusedError) or time.monotonic() + PAYLOADER_RETRY > deadline:
                raise
            refused += 1
            await asyncio.sleep(PAYLOADER_RETRY)
    if refused:
        _LOGGER.debug("%s: BinLoader refused %d times after the wake, then took it", what, refused)


async def async_launch_title(hass: HomeAssistant, entry: ConfigEntry, title_id: str, *, after_wake: bool = False) -> None:
    """Start a game on this entry's PS4, closing the running one first, or bring it back if it's the suspended one.
    `after_wake`: HA just woke the console and logged in. Raises HomeAssistantError (shown to the user)."""
    tid = title_id.strip().upper()
    if not _TITLE_ID.fullmatch(tid):
        raise HomeAssistantError(f"Not a PS4 title ID: {title_id}")
    # no "already running" shortcut. A title from DDP (after an HA restart) can be a game in the background,
    # and the payload never closes the picked game: in front it's a no-op (0x8094000c), in the background it resumes.
    await _async_send(hass, entry, tid, "Launch", after_wake=after_wake)


async def async_resume_suspended(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """After HA woke the console and logged in, bring back the game suspended by rest (nothing suspended →
    the payload does nothing). Raises HomeAssistantError."""
    await _async_send(hass, entry, None, "Resume game", resume_only=True, after_wake=True)


async def async_close_game(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Close the running game (back to the home screen). Unsaved progress is lost, like Close Application."""
    await _async_send(hass, entry, None, "Close game")
