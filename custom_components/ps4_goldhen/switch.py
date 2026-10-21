"""PS4 GoldHEN switches: Live temperatures, Fan control."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import fan, live_sensors
from .const import DOMAIN, CONF_PS4_HOST
from .entity import ps4_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([PS4LiveSensorsSwitch(entry), PS4FanControlSwitch(entry)])


class PS4LiveSensorsSwitch(RestoreEntity, SwitchEntity):
    """On (the default): the live sensors payload runs whenever the console is up. Off: HA stops it."""
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:thermometer-lines"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        host = entry.data[CONF_PS4_HOST]
        self._attr_unique_id = f"{DOMAIN}_{host}_live_sensors"
        self._attr_device_info = ps4_device_info(entry)
        self._attr_name = "Live temperatures"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        enabled = last is None or last.state != "off"  # on by default
        await live_sensors.async_set_enabled(self.hass, self._entry, enabled, send=False)

    @property
    def is_on(self) -> bool:
        return live_sensors.is_enabled(self.hass, self._entry.entry_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await live_sensors.async_set_enabled(self.hass, self._entry, True, send=True)
        finally:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await live_sensors.async_set_enabled(self.hass, self._entry, False, send=True)
        finally:
            self.async_write_ha_state()


class PS4FanControlSwitch(RestoreEntity, SwitchEntity):
    """Off (the default): the console's own fan target (e.g. 83 °C on a CUH-72xx Pro; turning it off sends that back).
    On: HA holds the Fan target temperature slider's value and re-sends it if the console drifts (fan.py)."""
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:fan-auto"

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        host = entry.data[CONF_PS4_HOST]
        self._attr_unique_id = f"{DOMAIN}_{host}_fan_control"
        self._attr_device_info = ps4_device_info(entry)
        self._attr_name = "Fan control"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        await fan.async_set_enabled(self.hass, self._entry, last is not None and last.state == "on", send=False)

    @property
    def is_on(self) -> bool:
        return fan.is_enabled(self.hass, self._entry.entry_id)

    async def async_turn_on(self, **kwargs: Any) -> None:
        try:
            await fan.async_set_enabled(self.hass, self._entry, True, send=True)
        finally:
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        try:
            await fan.async_set_enabled(self.hass, self._entry, False, send=True)
        finally:
            self.async_write_ha_state()
