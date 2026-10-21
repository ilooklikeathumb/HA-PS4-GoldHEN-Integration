"""Device-page entities that come and go with a feature's settings, without reloading the entry.

Screenshots and save backup: a platform's setup registers a factory, and reconcile() adds the
entities when the feature wants them. It removes them, registry entry included, when the feature stops wanting them.
"""
from __future__ import annotations

from typing import Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er


class DynamicEntities:
    def __init__(self, wanted: Callable[[str], bool]) -> None:
        self._wanted = wanted  # platform → should its entities exist with the current settings
        self.platforms: dict[str, tuple[Callable, Callable[[], list]]] = {}
        self.entities: dict[str, list] = {}

    @callback
    def register(self, hass: HomeAssistant, platform: str, add_entities: Callable, factory: Callable[[], list]) -> None:
        """Called from a platform's setup."""
        self.platforms[platform] = (add_entities, factory)
        self.reconcile(hass, platform)

    @callback
    def reconcile(self, hass: HomeAssistant, platform: str | None = None) -> None:
        """One platform, or every registered one after a settings change."""
        for name in [platform] if platform else list(self.platforms):
            add_entities, factory = self.platforms[name]
            want, have = self._wanted(name), self.entities.get(name)
            if want and not have:
                self.entities[name] = factory()
                add_entities(self.entities[name])
            elif have and not want:
                registry = er.async_get(hass)
                for entity in self.entities.pop(name):
                    if entity.entity_id and registry.async_get(entity.entity_id):
                        registry.async_remove(entity.entity_id)  # the entity removes itself from the state machine too
                    elif entity.hass is not None:
                        hass.async_create_task(entity.async_remove())
