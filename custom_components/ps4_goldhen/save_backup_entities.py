"""Device-page entities for PS4 save backup: Back up saves now, Save backup status, Last save
backup.

They exist only while save backup isn't Off for the console (panel → Saves). save_backup.register_platform adds and
removes them when the setting changes, without reloading the entry. The platforms import this module inside their
setup, so the existing platform tests keep their small HA stand-ins.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from . import save_backup
from .const import DOMAIN
from .entity import ps4_device_info


class _SaveBackupEntity:
    """Re-render on the console's save backup signal (settings saved, a pass started, progressed or ended)."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _entry_id: str

    def _setup(self, entry: ConfigEntry, key: str) -> None:
        self._entry = entry
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.entry_id}_{key}"
        self._attr_device_info = ps4_device_info(entry)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(  # type: ignore[attr-defined]
            async_dispatcher_connect(self.hass, save_backup.signal(self._entry_id), self._on_signal)  # type: ignore[attr-defined]
        )

    @callback
    def _on_signal(self) -> None:
        self.async_write_ha_state()  # type: ignore[attr-defined]

    def _rt(self):
        return save_backup.runtime(self.hass, self._entry_id)  # type: ignore[attr-defined]


class PS4BackUpSavesNowButton(_SaveBackupEntity, ButtonEntity):
    """Check every game on the PS4 and copy the changed saves (in the background; the status sensor follows it)."""

    _attr_name = "Back up saves now"
    _attr_icon = "mdi:content-save-all"

    def __init__(self, entry: ConfigEntry) -> None:
        self._setup(entry, "back_up_saves_now")

    async def async_press(self) -> None:
        await save_backup.async_start_backup(self.hass, self._entry_id)


class PS4SaveBackupStatusSensor(_SaveBackupEntity, SensorEntity):
    _attr_translation_key = "save_backup_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = save_backup.STATUS_OPTIONS
    _attr_icon = "mdi:content-save-check"
    # "3/40" moves on every copied chunk while the state stays "Backing up"; the recorder would save each one
    _unrecorded_attributes = frozenset({"progress"})

    def __init__(self, entry: ConfigEntry) -> None:
        self._setup(entry, "save_backup_status")

    @property
    def native_value(self) -> str | None:
        rt = self._rt()
        return save_backup.status(rt) if rt else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        rt = self._rt()
        if rt is None:
            return None
        last = rt.settings["last"] or {}
        return {
            "mode": rt.settings["mode"],
            "last_finished": last.get("finished"),
            "checked": last.get("checked"), "copied": last.get("copied"),
            "skipped": last.get("skipped"), "retried": last.get("retried"), "open_games": last.get("open"),
            "error": last.get("error"),
            "progress": f"{rt.progress['done']}/{rt.progress['total']}" if rt.running else None,
            "used_bytes": rt.usage["bytes"], "versions": rt.usage["versions"],
            "limit_bytes": int(float(rt.settings["limit_gb"]) * (1 << 30)),
        }


class PS4LastSaveBackupSensor(_SaveBackupEntity, SensorEntity):
    """When a pass last finished with everything backed up ("no backup in 7 days" automations)."""

    _attr_translation_key = "last_save_backup"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:content-save-outline"

    def __init__(self, entry: ConfigEntry) -> None:
        self._setup(entry, "last_save_backup")

    @property
    def native_value(self) -> datetime | None:
        rt = self._rt()
        value = rt.settings["last_success"] if rt else None
        return dt_util.parse_datetime(value) if value else None
