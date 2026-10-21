"""Device-page entities for PS4 screenshots: Latest screenshot and Sync latest screenshot.

They exist only while screenshots are on for the console. screenshots.register_platform
adds and removes them when the panel's settings change, without reloading the entry. The platforms import this module
inside their setup, so the existing platform tests keep their small HA stand-ins.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from . import capture_files as cf
from . import screenshots
from .const import DOMAIN
from .entity import ps4_device_info


class _ScreenshotEntity:
    """Re-render on the console's screenshot signal (settings saved, latest shot changed, a copy started or ended)."""

    _attr_should_poll = False
    _entry_id: str

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(  # type: ignore[attr-defined]
            async_dispatcher_connect(self.hass, screenshots.signal(self._entry_id), self._on_signal)  # type: ignore[attr-defined]
        )

    @callback
    def _on_signal(self) -> None:
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _rt(self):
        return screenshots.runtime(self.hass, self._entry_id)  # type: ignore[attr-defined]


class PS4LatestScreenshotImage(_ScreenshotEntity, ImageEntity):
    """The newest shot on the PS4 as the console's own 440x248 thumbnail (~11 KB, one kept per console).

    Stays on in rest / off: it's a past shot, not a live view. Refreshed when the console comes on, a game is left, the
    panel lists shots, or a copy runs.
    """

    _attr_has_entity_name = True
    _attr_name = "Latest screenshot"
    _attr_icon = "mdi:image-outline"
    _attr_content_type = "image/jpeg"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        ImageEntity.__init__(self, hass)
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_latest_screenshot"
        self._attr_device_info = ps4_device_info(entry)
        self._latest: dict[str, Any] | None = None  # filled once added: HA sets self.hass only then

    async def async_added_to_hass(self) -> None:
        self._sync()
        await super().async_added_to_hass()

    def _sync(self) -> None:
        rt = self._rt()
        latest = rt.settings.get("latest") if rt else None
        if latest == self._latest:
            return
        self._latest = latest
        self._cached_image = None
        when = dt_util.parse_datetime(latest["modified_utc"]) if latest and latest.get("modified_utc") else None
        self._attr_image_last_updated = (when or dt_util.utcnow()) if latest else None

    @callback
    def _on_signal(self) -> None:
        self._sync()
        super()._on_signal()

    @property
    def entity_picture(self) -> str | None:
        return super().entity_picture if self._latest else None  # no shot yet → the row shows the icon

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._latest:
            return None
        rt = self._rt()
        game_map = self.hass.data[DOMAIN][self._entry_id].get("game_map", {})
        return {
            "game": cf.game_folder(self._latest["title_id"], game_map),
            "title_id": self._latest["title_id"],
            "taken": self._latest.get("taken"),  # console clock
            "trophy": self._latest.get("trophy", False),
            "in_ha": bool(rt and self._latest["name"] in rt.names),
            "ps4_path": cf.photo_remote(self._latest["rel"]),
        }

    async def async_image(self) -> bytes | None:
        return await screenshots.async_latest_thumb(self.hass, self._entry_id)


class PS4ImportLatestScreenshotButton(_ScreenshotEntity, ButtonEntity):
    """Copy the newest shot to HA's media folder. No confirmation: one file, add-only, skipped if already there."""

    _attr_has_entity_name = True
    _attr_name = "Sync latest screenshot"
    _attr_icon = "mdi:image-move"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_import_latest_screenshot"
        self._attr_device_info = ps4_device_info(entry)

    async def async_press(self) -> None:
        await screenshots.async_import_latest(self.hass, self._entry)
