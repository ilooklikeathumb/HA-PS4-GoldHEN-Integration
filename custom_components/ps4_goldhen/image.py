"""PS4 GoldHEN image: cover art of the running game.

Same pattern as HA core's playstation_network "Now playing" image. The cover comes from the same sources as the
media player: Sony's CDN URL from app.db, else the console's icon0.png (disk cache, else FTP).
"""
from __future__ import annotations

import logging

from homeassistant.components.image import Image, ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from . import _read_or_fetch_cover
from . import ftp_session
from .const import DOMAIN, CONF_PS4_HOST, SENSOR_TITLE_ID, POWER_OFF, POWER_REST
from .entity import ps4_device_info

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([PS4GameCoverImage(hass, coordinator, entry)])
    from . import screenshots, screenshot_entities  # preloaded by __init__
    screenshots.register_platform(
        hass, entry, "image", async_add_entities, lambda: [screenshot_entities.PS4LatestScreenshotImage(hass, entry)]
    )


class PS4GameCoverImage(CoordinatorEntity, ImageEntity):
    _attr_has_entity_name = True
    _attr_name = "Game cover"
    _attr_icon = "mdi:sony-playstation"

    def __init__(self, hass: HomeAssistant, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        ImageEntity.__init__(self, hass)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_game_cover"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_data = hass.data[DOMAIN][entry.entry_id]  # updated in place (game_map refreshes)
        self._tid: str | None = None
        self._attr_image_url = None
        self._sync_cover()

    def _game_tid(self) -> str | None:
        """Title ID while a game shows on Current game; None on Home Screen, in rest or off."""
        data = self.coordinator.data or {}
        if data.get("power_state") in (POWER_REST, POWER_OFF):
            return None
        tid = data.get(SENSOR_TITLE_ID)
        return tid.strip().upper() if tid else None

    def _sync_cover(self) -> None:
        """New title or newly known CDN URL → drop the cached image and stamp the change."""
        tid = self._game_tid()
        cdn = self._entry_data.get("game_map", {}).get(tid, {}).get("cdn_cover") if tid else None
        url = cdn if cdn and cdn.startswith("http") else None
        if tid == self._tid and url == self._attr_image_url:
            return
        self._tid = tid
        self._attr_image_url = url
        self._cached_image = None
        self._attr_image_last_updated = dt_util.utcnow() if tid else None

    @callback
    def _handle_coordinator_update(self) -> None:
        self._sync_cover()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        # rest or fully off → unavailable, like the live temperatures; home screen → no picture
        power = (self.coordinator.data or {}).get("power_state")
        return super().available and power not in (POWER_REST, POWER_OFF)

    @property
    def entity_picture(self) -> str | None:
        # No game → no picture, so the row shows the icon (same rule as Current game)
        if self._tid is None:
            return None
        return super().entity_picture

    async def async_image(self) -> bytes | None:
        tid = self._tid
        if tid is None:
            return None
        if self._attr_image_url:  # Sony CDN cover from app.db, fetched + cached by ImageEntity
            return await super().async_image()
        if self._cached_image and tid == self._tid:
            return self._cached_image.content
        data = self._entry_data
        game_info = data.get("game_map", {}).get(tid, {})
        cover_path = game_info.get("cover") or f"/user/appmeta/{tid}/icon0.png"
        try:  # under the console's FTP lock; a second request for the same cover finds it in the disk cache
            content = await ftp_session.run(
                self.hass, data, _read_or_fetch_cover, data["host"], int(data["ftp_port"]), tid, cover_path
            )
        except Exception as err:  # noqa: BLE001 — no cover is a normal outcome (e.g. FTP off)
            _LOGGER.debug("Game cover for %s unavailable: %s", tid, err)
            return None
        if tid != self._tid:  # game changed while fetching
            return None
        self._attr_content_type = "image/png"
        self._cached_image = Image(content_type="image/png", content=content)
        return content
