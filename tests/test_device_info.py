"""Every entity joins the PS4 device; plugin sensors and payload buttons start disabled.

AST checks only — no HA install needed.
"""
import ast
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"

# Sourced from PS4StateJSON.prx, which isn't working yet (README, plugin/README.md)
PLUGIN_SENSORS = {
    "PS4SoCPowerSensor", "PS4CPUPowerSensor", "PS4GPUPowerSensor", "PS4TotalPowerSensor",
    "PS4ConsoleIDSensor",  # a payload only gets a masked IDPS, so still plugin-only
}
# fed by our live sensors payload now, enabled by default (unavailable without a current reading)
LIVE_SENSORS = {"PS4CPUTempSensor", "PS4SoCTempSensor", "PS4FanDutySensor"}
# DDP + the console-info payload; on by default
CONSOLE_INFO_SENSORS = {"PS4FWVersionSensor", "PS4HWModelSensor"}
# works, but off by default: reads not_polled unless the FTP poll is on
QUIET_SENSORS = {"PS4FTPStatusSensor"}
# Rest mode works but stays off by default (the media player is the main control)
PAYLOAD_BUTTONS = {"PS4StandbyButton"}
# klog-backed (works) + FTP connection health + cover art + power / wake
# + Read console info + Live temperatures switch + Close game
WORKING = {"PS4CurrentGameSensor", "PS4StatusSensor", "PS4GameCoverImage", "PS4MediaPlayer", "PS4ConsoleInfoButton",
           "PS4LiveSensorsSwitch", "PS4CloseGameButton",
           "PS4FanControlSwitch", "PS4FanTargetNumber"} | LIVE_SENSORS | CONSOLE_INFO_SENSORS  # fan control
# exist only while screenshots are on (panel tab); enabled when they exist
SCREENSHOT_ENTITIES = {"PS4LatestScreenshotImage", "PS4ImportLatestScreenshotButton"}
# play time, from play_time.py
# the four play time sensors (today, 7 days, 30 days, current session) share _PlayTimeSensor, which sets
# the device; test_play_time checks each one's value
PLAY_TIME_SENSORS = {"_PlayTimeSensor"}


def entity_classes(path: Path) -> dict[str, ast.ClassDef]:
    tree = ast.parse(path.read_text())
    return {
        n.name: n for n in tree.body
        if isinstance(n, ast.ClassDef)
        and any(isinstance(b, ast.Name) and b.id in ("SensorEntity", "ButtonEntity", "ImageEntity", "MediaPlayerEntity", "SwitchEntity",
                                                    "NumberEntity", "RestoreNumber")
                for b in n.bases)
    }


def class_attrs(cls: ast.ClassDef) -> dict[str, ast.expr]:
    return {
        t.id: n.value
        for n in cls.body if isinstance(n, ast.Assign)
        for t in n.targets if isinstance(t, ast.Name)
    }


def init_sets(cls: ast.ClassDef, attr: str) -> ast.expr | None:
    for n in cls.body:
        if isinstance(n, ast.FunctionDef) and n.name == "__init__":
            for s in ast.walk(n):
                if isinstance(s, ast.Assign):
                    for t in s.targets:
                        if (isinstance(t, ast.Attribute) and t.attr == attr
                                and isinstance(t.value, ast.Name) and t.value.id == "self"):
                            return s.value
    return None


def module_constant(path: Path, name: str):
    for n in ast.parse(path.read_text()).body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in n.targets):
            return ast.literal_eval(n.value)
    raise KeyError(name)


SENSORS = entity_classes(COMP / "sensor.py")
BUTTONS = entity_classes(COMP / "button.py")
IMAGES = entity_classes(COMP / "image.py")
MEDIA_PLAYERS = entity_classes(COMP / "media_player.py")
SWITCHES = entity_classes(COMP / "switch.py")
NUMBERS = entity_classes(COMP / "number.py")
SCREENSHOTS = entity_classes(COMP / "screenshot_entities.py")
ALL = {**SENSORS, **BUTTONS, **IMAGES, **MEDIA_PLAYERS, **SWITCHES, **NUMBERS, **SCREENSHOTS}


def enabled_default(name: str):
    """Resolve _attr_entity_registry_enabled_default for a class; None = not set (HA default True)."""
    value = class_attrs(ALL[name]).get("_attr_entity_registry_enabled_default")
    if value is None:
        return None
    if isinstance(value, ast.Name):
        src = COMP / ("sensor.py" if name in SENSORS else "button.py" if name in BUTTONS else
                      "image.py" if name in IMAGES else "media_player.py")
        return module_constant(src, value.id)
    return ast.literal_eval(value)


class DeviceInfoTests(unittest.TestCase):
    def test_entity_inventory_matches(self):
        # A new entity class must be sorted into one of the groups above
        self.assertEqual(set(ALL), PLUGIN_SENSORS | QUIET_SENSORS | PAYLOAD_BUTTONS | WORKING | SCREENSHOT_ENTITIES
                         | PLAY_TIME_SENSORS)

    def test_every_entity_joins_the_device(self):
        for name, cls in ALL.items():
            with self.subTest(entity=name):
                value = init_sets(cls, "_attr_device_info")
                self.assertIsNotNone(value, f"{name} sets no _attr_device_info")
                self.assertEqual(ast.unparse(value), "ps4_device_info(entry)")

    def test_unique_id_and_entity_name(self):
        # device_info needs a unique_id; has_entity_name gives "PS4 <name>" friendly names
        for name, cls in ALL.items():
            with self.subTest(entity=name):
                self.assertIsNotNone(init_sets(cls, "_attr_unique_id"))
                self.assertTrue(ast.literal_eval(class_attrs(cls)["_attr_has_entity_name"]))

    def test_plugin_sensors_and_payload_buttons_disabled_by_default(self):
        for name in PLUGIN_SENSORS | QUIET_SENSORS | PAYLOAD_BUTTONS:
            with self.subTest(entity=name):
                self.assertIs(enabled_default(name), False)

    def test_working_entities_enabled(self):
        for name in WORKING | SCREENSHOT_ENTITIES | PLAY_TIME_SENSORS:
            with self.subTest(entity=name):
                self.assertIn(enabled_default(name), (None, True))

    def test_ftp_status_is_diagnostic(self):
        value = class_attrs(SENSORS["PS4FTPStatusSensor"])["_attr_entity_category"]
        self.assertEqual(ast.unparse(value), "EntityCategory.DIAGNOSTIC")

    def test_current_game_shows_icon_not_cover(self):
        # the cover lives in image.ps4_game_cover; Current game keeps mdi:sony-playstation
        cls = SENSORS["PS4CurrentGameSensor"]
        self.assertNotIn("entity_picture", {n.name for n in cls.body if isinstance(n, ast.FunctionDef)})
        self.assertEqual(ast.literal_eval(class_attrs(cls)["_attr_icon"]), "mdi:sony-playstation")
        # the cover endpoint is gone, so no attribute points at it (dashboards use the media player)
        self.assertNotIn("cover_url", ast.unparse(cls))

    def test_no_unauthenticated_http_view(self):
        # every HTTP view needs auth
        for path in COMP.glob("*.py"):
            src = path.read_text().replace("requires_auth=False", "requires_auth = False")
            self.assertNotIn("requires_auth = False", src, path.name)
            self.assertNotIn("/api/ps4_goldhen/cover", src, path.name)

    def test_device_identity(self):
        fn = next(
            n for n in ast.parse((COMP / "entity.py").read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "ps4_device_info"
        )
        call = next(s for s in ast.walk(fn) if isinstance(s, ast.Call) and getattr(s.func, "id", "") == "DeviceInfo")
        kw = {k.arg: ast.unparse(k.value) for k in call.keywords}
        # entry_id, not host: an IP change must keep the same device
        self.assertEqual(kw["identifiers"], "{(DOMAIN, entry.entry_id)}")
        self.assertNotIn("connections", kw)  # no MAC: would merge with UniFi's client device
        # console_info.py owns model / firmware / serial; a fixed value here would overwrite them
        for field in ("model", "model_id", "sw_version", "hw_version", "serial_number"):
            self.assertNotIn(field, kw)
        # the console's own name from setup, else "PS4" (older entries)
        self.assertEqual(kw["name"], "entry.data.get(CONF_CONSOLE_NAME) or 'PS4'")
        url = module_constant(COMP / "entity.py", "_PANEL_CONFIGURATION_URL")
        panel_path = module_constant(COMP / "__init__.py", "_PANEL_URL_PATH")
        self.assertEqual(url, f"homeassistant://{panel_path}")


if __name__ == "__main__":
    unittest.main()
