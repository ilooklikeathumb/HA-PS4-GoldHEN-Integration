"""Current game shows Rest Mode / Off; state_classification + restore come from the power state.

Runs the real sensor.py against minimal HA stand-ins (no HA install needed).
"""
import asyncio
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_current_game_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, fn):
        pass


class RestoreEntity:
    last_state = None

    async def async_get_last_state(self):
        return self.last_state


class _Any:
    def __getattr__(self, name):
        return name


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.sensor", SensorDeviceClass=_Any(), SensorEntity=object, SensorStateClass=_Any())
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.const", UnitOfTemperature=_Any(), UnitOfPower=_Any(), UnitOfTime=_Any(),
        EVENT_HOMEASSISTANT_STOP="homeassistant_stop")  # sensor.py imports play_time
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity", EntityCategory=_Any())
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module("homeassistant.helpers.event", async_track_state_change_event=lambda *a: None,
        async_track_time_interval=lambda *a: None, async_call_later=lambda *a: None)  # sensor.py imports live_sensors
_module("homeassistant.exceptions", HomeAssistantError=Exception)
_module("homeassistant.helpers.restore_state", RestoreEntity=RestoreEntity)
_module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)
_module("homeassistant.helpers.device_registry", DeviceInfo=dict)
_module("homeassistant.helpers.dispatcher", async_dispatcher_connect=lambda *a: None,
        async_dispatcher_send=lambda *a: None)
_module("homeassistant.helpers.storage", Store=object)
_module("homeassistant.components.websocket_api", websocket_command=lambda schema: (lambda f: f),
        require_admin=lambda f: f, async_response=lambda f: f, async_register_command=lambda *a: None,
        ActiveConnection=object)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: None, as_local=lambda d: d, parse_datetime=lambda s: s)
_module("voluptuous", Required=lambda key: key)

pkg = _module(PKG)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

sensor_mod = importlib.import_module(f"{PKG}.sensor")
const = importlib.import_module(f"{PKG}.const")


class Coordinator:
    def __init__(self, data):
        self.data = data

    def async_set_updated_data(self, data):
        self.data = data


class Entry:
    entry_id = "E1"
    data = {const.CONF_PS4_HOST: "192.0.2.1"}


class States:
    def get(self, entity_id):
        return None


class Hass:
    def __init__(self, data):
        self.data = data
        self.states = States()


class LastState:
    def __init__(self, state, attributes=None):
        self.state = state
        self.attributes = attributes or {}


class Machine:
    def __init__(self):
        self.restored = "untouched"

    def restore_power_state(self, power):
        self.restored = power

    def snapshot(self):
        return {}


def make(data, pi_state=None):
    machine = Machine()
    entry_data = {"game_map": {}, "klog_state_machine": machine, "klog_data": {}}
    e = sensor_mod.PS4CurrentGameSensor(Coordinator(data), Entry())
    e.hass = Hass({const.DOMAIN: {"E1": entry_data}})
    e._pi_state = pi_state
    return e, machine


class CurrentGameStateTests(unittest.TestCase):
    def test_rest_and_off_text(self):
        for power, text, cls in (("rest", "Rest Mode", "rest"), ("off", "Off", "off")):
            with self.subTest(power=power):
                e, _ = make({"power_state": power, "title_id": "CUSA90003", "game_name": "Game C"})
                self.assertEqual(e.native_value, text)
                self.assertEqual(e.extra_state_attributes["state_classification"], cls)

    def test_not_responding_text(self):  # frozen mid-game: the game's name would say it's still playing
        e, _ = make({"power_state": "on", "not_responding": True, "title_id": "CUSA90003", "game_name": "Game C"})
        self.assertEqual(e.native_value, "Not responding")
        self.assertEqual(e.extra_state_attributes["state_classification"], "not_responding")
        e, _ = make({"power_state": "rest", "not_responding": True})  # rest / off win
        self.assertEqual(e.native_value, "Rest Mode")

    def test_running_console_unchanged(self):
        e, _ = make({"power_state": "on"})
        self.assertEqual(e.native_value, const.HOME_SCREEN)
        self.assertEqual(e.extra_state_attributes["state_classification"], "home_screen")
        e, _ = make({"power_state": "on", "title_id": "CUSA90003", "game_name": "Game C"})
        self.assertEqual(e.native_value, "Game C")
        self.assertEqual(e.extra_state_attributes["state_classification"], "game")
        e, _ = make({"title_id": "CUSA90003"})  # power unknown, name unknown
        self.assertEqual(e.native_value, "CUSA90003")
        self.assertEqual(e.extra_state_attributes["state_classification"], "game")

    def test_pi_sensor_rest_and_offline(self):
        for pi, text, cls in (("rest", "Rest Mode", "rest"), ("offline", "Off", "off")):
            with self.subTest(pi=pi):
                e, _ = make({"power_state": "on"}, pi_state=pi)
                self.assertEqual(e.native_value, text)
                self.assertEqual(e.extra_state_attributes["state_classification"], cls)

    def test_klog_detail_attributes_not_recorded(self):
        e, _ = make({"power_state": "on"})
        noisy = {"state_signal_line", "state_reason", "pending_title_id", "power_trigger"}
        self.assertEqual(e._unrecorded_attributes, noisy)
        self.assertLessEqual(noisy, e.extra_state_attributes.keys())  # still on the entity, only not recorded
        self.assertNotIn("state_classification", e._unrecorded_attributes)  # dashboards + history read this one


class RestoreTests(unittest.TestCase):
    def restore(self, last):
        e, machine = make({})
        e.last_state = last
        asyncio.run(e.async_added_to_hass())
        return machine.restored

    def test_restore_uses_power_attribute(self):
        # n/a = the state text an older version stored; the attribute decides
        self.assertEqual(self.restore(LastState("n/a", {"power_state": "rest"})), "rest")
        self.assertEqual(self.restore(LastState("n/a", {"power_state": "off"})), "off")

    def test_restore_from_state_text(self):
        self.assertEqual(self.restore(LastState("Rest Mode")), "rest")
        self.assertEqual(self.restore(LastState("Off")), "off")

    def test_running_last_state_restores_nothing(self):
        self.assertIsNone(self.restore(LastState("PlayStation Home Screen", {"power_state": None})))


def make_status(data):
    machine = Machine()
    e = sensor_mod.PS4StatusSensor(Coordinator(data), Entry())
    e.hass = Hass({const.DOMAIN: {"E1": {"game_map": {}, "klog_state_machine": machine, "klog_data": {}}}})
    return e, machine


class StatusSensorTests(unittest.TestCase):
    """Status = playing / home_screen / rest / off, same rules as media_player.ps4."""

    def test_states(self):
        game = {"title_id": "CUSA90003", "game_name": "Game C"}
        for data, expected in (
            ({"power_state": "on", **game}, "playing"),
            ({"power_state": None, **game}, "playing"),  # klog not settled yet, but a game is running
            ({"power_state": "on"}, "home_screen"),
            ({"power_state": "rest", **game}, "rest"),
            ({"power_state": "off", **game}, "off"),
            ({"power_state": None}, None),  # unknown, like the media player
            ({"power_state": "on", "not_responding": True, **game}, "not_responding"),
            ({"power_state": "on", "not_responding": True}, "not_responding"),
            ({"power_state": "rest", "not_responding": True}, "rest"),
            ({"power_state": "on", "not_responding": False, **game}, "playing"),
            ({}, None),
        ):
            with self.subTest(data=data):
                self.assertEqual(make_status(data)[0].native_value, expected)

    def test_options_cover_every_state(self):
        e, _ = make_status({})
        self.assertEqual(e._attr_options, ["playing", "home_screen", "rest", "off", "not_responding"])

    def test_translations_name_every_state(self):
        import json
        for path in ("strings.json", "translations/en.json"):
            with self.subTest(path=path):
                status = json.loads((COMP / path).read_text())["entity"]["sensor"]["status"]
                self.assertEqual(
                    status["state"],
                    {"playing": "Playing", "home_screen": "Home screen", "rest": "Rest mode", "off": "Off",
                     "not_responding": "Not responding"},
                )
        icons = json.loads((COMP / "icons.json").read_text())["entity"]["sensor"]["status"]["state"]
        self.assertEqual(set(icons), {"playing", "home_screen", "rest", "off", "not_responding"})

    def restore(self, state):
        e, machine = make_status({})
        e.last_state = LastState(state)
        asyncio.run(e.async_added_to_hass())
        return machine.restored

    def test_restore(self):
        # Current game can be disabled now, so Status carries the power state restore too
        self.assertEqual(self.restore("rest"), "rest")
        self.assertEqual(self.restore("off"), "off")
        self.assertIsNone(self.restore("playing"))
        self.assertIsNone(self.restore("unknown"))

    def test_no_last_state_restores_nothing(self):
        e, machine = make_status({})
        asyncio.run(e.async_added_to_hass())
        self.assertEqual(machine.restored, "untouched")


if __name__ == "__main__":
    unittest.main()
