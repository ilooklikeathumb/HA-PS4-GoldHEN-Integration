"""PS4 GoldHEN device: groups an entry's entities on one HA device page."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import CONF_CONSOLE_NAME, CONF_PS4_HOST, DOMAIN

# The device page's visit link opens the GoldHEN panel (_PANEL_URL_PATH in __init__.py)
_PANEL_CONFIGURATION_URL = "homeassistant://ps4_goldhen"


def ps4_device_info(entry: ConfigEntry) -> DeviceInfo:
    """One device per config entry; keyed by entry_id so an IP change keeps the same device.

    No model / firmware / serial here: console_info.py writes those to the device registry, and a
    fixed value here would overwrite them each time the entities are set up.
    The name is the console's own from setup (older entries have none: "PS4"); a rename in HA wins.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.data.get(CONF_CONSOLE_NAME) or "PS4",
        manufacturer="Sony Interactive Entertainment",
        configuration_url=_PANEL_CONFIGURATION_URL,
    )


def media_player_unique_id(entry: ConfigEntry) -> str:
    """The console's media player (the panel's Wake / Rest button finds it by this)."""
    return f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_media_player"


def device_name(hass: HomeAssistant, entry: ConfigEntry) -> str | None:
    """The console's name as the user sees it in HA (their rename first): panel picker, folders."""
    # Not async_get_device(identifiers=…): deprecated in HA 2026.10, gone in 2027.8. One device per entry here.
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    device = next((d for d in devices if (DOMAIN, entry.entry_id) in d.identifiers), None)
    return (device.name_by_user or device.name) if device else None
