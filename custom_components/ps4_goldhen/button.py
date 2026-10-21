"""PS4 GoldHEN buttons: Rest mode, Read console info, Close game."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .console_info import async_read as async_read_console_info
from .const import DOMAIN, CONF_PS4_HOST
from .entity import ps4_device_info
from .launch import async_close_game
from .rest import async_enter_rest


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PS4 GoldHEN buttons."""
    async_add_entities([
        PS4StandbyButton(entry),
        PS4ConsoleInfoButton(entry),
        PS4CloseGameButton(entry),
    ])
    from . import screenshots, screenshot_entities  # preloaded by __init__
    screenshots.register_platform(
        hass, entry, "button", async_add_entities, lambda: [screenshot_entities.PS4ImportLatestScreenshotButton(entry)]
    )
    from . import save_backup, save_backup_entities  # preloaded by __init__
    save_backup.register_platform(
        hass, entry, "button", async_add_entities, lambda: [save_backup_entities.PS4BackUpSavesNowButton(entry)]
    )


class PS4StandbyButton(ButtonEntity):
    """Button to put the PS4 into rest mode — same as the media player's turn off."""
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = False  # works, but the media player is the main control
    _attr_icon = "mdi:power-sleep"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        host = entry.data[CONF_PS4_HOST]
        # unique_id keeps upstream's `standby`, so an upgrade keeps the entity ID
        self._attr_unique_id = f"{DOMAIN}_{host}_standby"
        self._attr_device_info = ps4_device_info(entry)
        self._attr_name = "Rest mode"

    async def async_press(self) -> None:
        """Send the bundled rest payload."""
        await async_enter_rest(self.hass, self._entry)


class PS4ConsoleInfoButton(ButtonEntity):
    """Read model and serial again. HA reads them once by itself; this is the refresh."""
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:information-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        host = entry.data[CONF_PS4_HOST]
        self._attr_unique_id = f"{DOMAIN}_{host}_read_console_info"
        self._attr_device_info = ps4_device_info(entry)
        self._attr_name = "Read console info"

    async def async_press(self) -> None:
        """Send the console-info payload; the device page updates when its klog line comes back."""
        await async_read_console_info(self.hass, self._entry)


class PS4CloseGameButton(ButtonEntity):
    """Close the running game, back to the home screen. Unsaved progress is lost."""
    _attr_has_entity_name = True
    _attr_icon = "mdi:close-box-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        host = entry.data[CONF_PS4_HOST]
        self._attr_unique_id = f"{DOMAIN}_{host}_close_game"
        self._attr_device_info = ps4_device_info(entry)
        self._attr_name = "Close game"

    async def async_press(self) -> None:
        """Send the launch payload in close-only mode; the media player goes idle when klog reports it."""
        await async_close_game(self.hass, self._entry)
