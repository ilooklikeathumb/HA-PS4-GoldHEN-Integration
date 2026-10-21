"""PS4 GoldHEN numbers: Fan target temperature."""
from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import fan
from .const import CONF_PS4_HOST, DOMAIN
from .entity import ps4_device_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities([PS4FanTargetNumber(coordinator, entry)])


class PS4FanTargetNumber(CoordinatorEntity, RestoreNumber):
    """The temperature HA holds the fan at while Fan control is on (fan.py). Kept across restarts. What the console
    holds right now is the `console_value` attribute; `off_value` = what turning Fan control off sends back (the
    console's own target from before; null = nothing to send)."""
    _attr_has_entity_name = True
    _attr_name = "Fan target temperature"
    _attr_icon = "mdi:fan-chevron-up"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_device_class = NumberDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = fan.MIN_THRESHOLD
    _attr_native_step = 1

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        # unique ID says fan_threshold (the community's name for the value)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_fan_threshold"
        self._attr_device_info = ps4_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_number_data()
        if last is not None and last.native_value is not None:
            fan.restore_wanted(self.hass, self._entry.entry_id, round(last.native_value))

    @property
    def native_max_value(self) -> float:
        # the model's factory target; the console info read updates the coordinator, so this follows it
        return fan.max_threshold(self.hass, self._entry.entry_id)

    @property
    def native_value(self) -> int | None:
        return fan.wanted(self.hass, self._entry.entry_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # no fixed `stock` attribute: 83 is only the PS4 Pro CUH-7215B's own target
        return {"console_value": fan.console_value(self.hass, self._entry.entry_id),
                "off_value": fan.off_value(self.hass, self._entry.entry_id)}

    async def async_set_native_value(self, value: float) -> None:
        try:
            await fan.async_set_wanted(self.hass, self._entry, round(value), send=True)
        finally:
            self.async_write_ha_state()
