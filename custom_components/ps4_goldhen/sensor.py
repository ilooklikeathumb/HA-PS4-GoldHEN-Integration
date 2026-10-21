"""PS4 GoldHEN sensors."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature, UnitOfPower, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    CONF_PS4_HOST,
    POWER_ON,
    SENSOR_CURRENT_GAME,
    SENSOR_CPU_TEMP,
    SENSOR_SOC_TEMP,
    SENSOR_TITLE_ID,
    SENSOR_GAME_NAME,
    SENSOR_GAME_COVER,
    SENSOR_SOC_POWER,
    SENSOR_CPU_POWER,
    SENSOR_GPU_POWER,
    SENSOR_TOTAL_POWER,
    SENSOR_FAN_DUTY,
    SENSOR_FW_VERSION,
    SENSOR_HW_MODEL,
    SENSOR_CONSOLE_ID,
    HOME_SCREEN,
    POWER_OFF,
    POWER_REST,
)
from .entity import ps4_device_info
from . import live_sensors
from . import play_time

_HOME_SCREEN_STATE = HOME_SCREEN
_IDLE_STATE        = "Idle"
_REST_MODE_STATE   = "Rest Mode"
_OFF_STATE         = "Off"

# optional: if an entity with this ID exists, its `rest` / `offline` state overrides
_PI_STATE_SENSOR = "sensor.ps4_state_pi"

# Status sensor keys; the words people see are in translations (entity.sensor.status.state)
STATUS_PLAYING     = "playing"
STATUS_HOME_SCREEN = "home_screen"
STATUS_REST        = "rest"
STATUS_OFF         = "off"

# the PS4State plugin (PS4StateJSON.prx) isn't working yet, so its sensors are created
# disabled. Flip to True once a working plugin publishes ps4_state.json.
# CPU / SoC temperature and Fan duty come from the live sensors payload (live_sensors.py).
_PLUGIN_SENSORS_ENABLED_DEFAULT = False


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        [
            PS4FTPStatusSensor(coordinator, entry),
            PS4CurrentGameSensor(coordinator, entry),
            PS4StatusSensor(coordinator, entry),
            PS4CPUTempSensor(coordinator, entry),
            PS4SoCTempSensor(coordinator, entry),
            PS4SoCPowerSensor(coordinator, entry),
            PS4CPUPowerSensor(coordinator, entry),
            PS4GPUPowerSensor(coordinator, entry),
            PS4TotalPowerSensor(coordinator, entry),
            PS4FanDutySensor(coordinator, entry),
            PS4FWVersionSensor(coordinator, entry),
            PS4HWModelSensor(coordinator, entry),
            PS4ConsoleIDSensor(coordinator, entry),
            PS4PlayTimeTodaySensor(entry),
            PS4PlayTimeLast7DaysSensor(entry),
            PS4PlayTimeLast30DaysSensor(entry),
            PS4CurrentSessionSensor(entry),
        ],
        update_before_add=False,
    )
    from . import save_backup, save_backup_entities  # preloaded by __init__
    save_backup.register_platform(hass, entry, "sensor", async_add_entities, lambda: [
        save_backup_entities.PS4SaveBackupStatusSensor(entry), save_backup_entities.PS4LastSaveBackupSensor(entry),
    ])


class PS4FTPStatusSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # off by default: it reads not_polled unless the FTP poll is on
    _attr_entity_registry_enabled_default = False
    _attr_name = "FTP status"
    _attr_icon = "mdi:sony-playstation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_ftp_status"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> str:
        data = self.coordinator.data or {}
        if data.get("telemetry_status") == "disabled":  # poll off, FTP state unknown
            return "not_polled"
        return "online" if data.get("ftp_reachable") else "offline"

    @property
    def extra_state_attributes(self) -> dict:
        """Expose optional telemetry health independently of FTP status."""
        return {
            "telemetry_status": (self.coordinator.data or {}).get(
                "telemetry_status", "not_polled"
            )
        }


class PS4CurrentGameSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Current game"
    # always the icon — the cover is the media player's picture (or image.ps4_game_cover)
    _attr_icon = "mdi:sony-playstation"
    # these change with klog lines, not with the game; the recorder would save a copy each time
    _unrecorded_attributes = frozenset({"state_signal_line", "state_reason", "pending_title_id", "power_trigger"})

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_current_game"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_id = entry.entry_id
        self._pi_state: str | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # an HA restart during deep rest finds klog refused — keep showing Rest instead of Home Screen
        last = await self.async_get_last_state()
        if last is not None:
            _restore_power(self, last.attributes.get("power_state") or {
                _REST_MODE_STATE: POWER_REST, _OFF_STATE: POWER_OFF
            }.get(last.state))
        pi = self.hass.states.get(_PI_STATE_SENSOR)
        if pi:
            self._pi_state = pi.state
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                [_PI_STATE_SENSOR],
                self._on_pi_state_change,
            )
        )

    @callback
    def _on_pi_state_change(self, event) -> None:
        new = event.data.get("new_state")
        if new:
            self._pi_state = new.state
            self.async_write_ha_state()

    def _ps4_state(self) -> str:
        data = self.coordinator.data or {}
        tid  = data.get(SENSOR_TITLE_ID)
        if not tid:
            return _HOME_SCREEN_STATE
        name = data.get(SENSOR_GAME_NAME)
        if name and name.strip():
            return name.strip()
        return tid

    def _power_class(self) -> str | None:
        """"rest" / "off" when the console isn't running, else None."""
        pi = (self._pi_state or "").strip().lower()
        power = (self.coordinator.data or {}).get("power_state")  # from klog
        if pi == "rest" or power == POWER_REST:
            return "rest"
        if pi == "offline" or power == POWER_OFF:
            return "off"
        return None

    @property
    def native_value(self) -> str:
        power = self._power_class()
        if power == "rest":
            return _REST_MODE_STATE
        if power == "off":
            return _OFF_STATE
        return self._ps4_state()

    @property
    def extra_state_attributes(self) -> dict:
        data      = self.coordinator.data or {}
        val       = self.native_value
        tid       = data.get(SENSOR_TITLE_ID)
        game_map  = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry_id, {})
            .get("game_map", {})
        )
        game_info = game_map.get(tid, {}) if tid else {}
        return {
            SENSOR_TITLE_ID:    tid,
            SENSOR_GAME_NAME:   data.get(SENSOR_GAME_NAME),
            "cdn_cover":        game_info.get("cdn_cover"),
            # dashboards + automations read this, not the state text
            "state_classification": (
                self._power_class()
                or ("home_screen" if val == _HOME_SCREEN_STATE else "game")
            ),
            "pi_state":          self._pi_state,
            "klog_connected":    data.get("klog_connected", False),
            "power_state":       data.get("power_state"),
            "power_trigger":     data.get("power_trigger"),
            "state_reason":      data.get("state_reason"),
            "pending_title_id":  data.get("pending_title_id"),
            "state_signal_line": data.get("state_signal_line"),
        }


def _restore_power(entity, power: str | None) -> None:
    """Seed the klog state machine with the power state from before an HA restart.

    Current game and Status both do it, so either one alone (the other disabled) still restores. The machine only
    takes the first value, and only while its own state is unknown.
    """
    entry_data = entity.hass.data.get(DOMAIN, {}).get(entity._entry_id, {})
    machine = entry_data.get("klog_state_machine")
    if machine is None:
        return
    machine.restore_power_state(power)
    entry_data["klog_data"].update(machine.snapshot())
    entity.coordinator.async_set_updated_data({**(entity.coordinator.data or {}), **entry_data["klog_data"]})


class PS4StatusSensor(CoordinatorEntity, RestoreEntity, SensorEntity):
    """What the console is doing: Playing / Home screen / Rest mode / Off.

    Same source and rules as media_player.ps4 (klog power state + running title), but keeps rest and off apart,
    which the media player can't (HA deprecated STANDBY). Automations use the keys; the shown words come from
    translations and can change without breaking them. Unknown until klog reports, like the media player.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = [STATUS_PLAYING, STATUS_HOME_SCREEN, STATUS_REST, STATUS_OFF]

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_status"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_id = entry.entry_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            _restore_power(self, {STATUS_REST: POWER_REST, STATUS_OFF: POWER_OFF}.get(last.state))

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        power = data.get("power_state")
        if power == POWER_REST:
            return STATUS_REST
        if power == POWER_OFF:
            return STATUS_OFF
        if data.get(SENSOR_TITLE_ID):
            return STATUS_PLAYING
        if power == POWER_ON:
            return STATUS_HOME_SCREEN
        return None


class PS4CPUTempSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True  # enabled by default (live sensors payload)
    _attr_name = "CPU temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_cpu_temp"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        # no current reading (switch off, console not on, nothing for 10 min) → unavailable
        return super().available and live_sensors.is_live(self.hass, self._entry_id)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        temp = data.get(SENSOR_CPU_TEMP)
        return float(temp) if temp is not None else None


class PS4SoCTempSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True  # enabled by default (live sensors payload)
    _attr_name = "SoC temperature"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_soc_temp"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        return super().available and live_sensors.is_live(self.hass, self._entry_id)

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data or {}
        temp = data.get(SENSOR_SOC_TEMP)
        return float(temp) if temp is not None else None


class PS4SoCPowerSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = _PLUGIN_SENSORS_ENABLED_DEFAULT
    _attr_name = "SoC power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:chip"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_soc_power"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(SENSOR_SOC_POWER)


class PS4CPUPowerSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = _PLUGIN_SENSORS_ENABLED_DEFAULT
    _attr_name = "CPU power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cpu-64-bit"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_cpu_power"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(SENSOR_CPU_POWER)


class PS4GPUPowerSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = _PLUGIN_SENSORS_ENABLED_DEFAULT
    _attr_name = "GPU power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:gpu"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_gpu_power"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(SENSOR_GPU_POWER)


class PS4TotalPowerSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    _attr_entity_registry_enabled_default = _PLUGIN_SENSORS_ENABLED_DEFAULT
    _attr_name = "Total power"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:flash"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_total_power"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get(SENSOR_TOTAL_POWER)


class PS4FanDutySensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True  # enabled by default (live sensors payload)
    _attr_name = "Fan duty"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_fan_duty"
        self._attr_device_info = ps4_device_info(entry)
        self._entry_id = entry.entry_id

    @property
    def available(self) -> bool:
        return super().available and live_sensors.is_live(self.hass, self._entry_id)

    @property
    def native_value(self) -> int | None:
        val = (self.coordinator.data or {}).get(SENSOR_FAN_DUTY)
        return int(val) if val is not None else None


class PS4FWVersionSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # from DDP system-version; on by default
    _attr_name = "Firmware version"
    _attr_icon = "mdi:tag"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_fw_version"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get(SENSOR_FW_VERSION)


class PS4HWModelSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # from the console-info payload; on by default
    _attr_name = "Hardware model"
    _attr_icon = "mdi:sony-playstation"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_hw_model"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get(SENSOR_HW_MODEL)


class PS4ConsoleIDSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True
    # no source: a payload only gets a masked IDPS (a placeholder). Only the unfinished PS4State plugin would fill
    # this.
    _attr_entity_registry_enabled_default = _PLUGIN_SENSORS_ENABLED_DEFAULT
    _attr_name = "Console ID"
    _attr_icon = "mdi:identifier"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_console_id"
        self._attr_device_info = ps4_device_info(entry)

    @property
    def native_value(self) -> str | None:
        return (self.coordinator.data or {}).get(SENSOR_CONSOLE_ID)


# ── Play time (play_time.py keeps it; these show it) ────────────────────────────

class _PlayTimeUpdates:
    """Mixin: refresh on play_time's signal; read that console's book."""

    _entry_id: str

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()  # type: ignore[misc]
        self.async_on_remove(async_dispatcher_connect(  # type: ignore[attr-defined]
            self.hass, play_time.signal_updated(self._entry_id), self.async_write_ha_state))  # type: ignore[attr-defined]

    def _book(self) -> play_time.PlayTimeBook | None:
        rt = play_time.runtime(self.hass, self._entry_id)  # type: ignore[attr-defined]
        return rt.book if rt else None


def _hours(seconds: float) -> float:
    """In hours with no display precision, HA's frontend shows 47 min as "0h 47m" and 187 as "3h 7m"
    (in minutes it shows "187m"; hours with precision 0 show "3h")."""
    return round(seconds / 3600, 4)


class _PlayTimeSensor(_PlayTimeUpdates, SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_native_unit_of_measurement = UnitOfTime.HOURS

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry_id = entry.entry_id
        self._attr_unique_id = f"{DOMAIN}_{entry.data[CONF_PS4_HOST]}_{self._attr_translation_key}"
        self._attr_device_info = ps4_device_info(entry)


class PS4PlayTimeTodaySensor(_PlayTimeSensor):
    """Hours played today (local day), all games. Back to 0 at midnight, which HA's statistics read as a meter
    reset, so the statistics-graph card charts play time per day / week / month from it."""

    _attr_translation_key = "play_time_today"
    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        book = self._book()
        return _hours(book.today_seconds(dt_util.utcnow())) if book else None


class PS4PlayTimeLast7DaysSensor(_PlayTimeSensor):
    """Hours played today + the 6 days before (what the panel calls "Last 7 days")."""

    _attr_translation_key = "play_time_7_days"
    _days = 7

    @property
    def native_value(self) -> float | None:
        book = self._book()
        return _hours(book.last_days_seconds(dt_util.utcnow(), self._days)) if book else None


class PS4PlayTimeLast30DaysSensor(PS4PlayTimeLast7DaysSensor):
    """Hours played today + the 29 days before."""

    _attr_translation_key = "play_time_30_days"
    _days = 30


class PS4CurrentSessionSensor(_PlayTimeSensor):
    """Hours played in the open session (paused while away, ends 30 min after the game was left); 0 when none."""

    _attr_translation_key = "current_session"

    @property
    def native_value(self) -> float | None:
        book = self._book()
        return _hours(book.session_seconds()) if book else None

    @property
    def extra_state_attributes(self) -> dict:
        book = self._book()
        o = book.open if book else None
        return {
            SENSOR_TITLE_ID: o["title_id"] if o else None,
            "game":          o.get("name") if o else None,
            "started":       o["start"] if o else None,
            "playing":       bool(book and o and book.active == o["title_id"]),
        }
