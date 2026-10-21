"""Rest mode — checks before the rest payload is sent, and the payload itself.

Runs the real rest.py against minimal HA stand-ins (no HA install needed); DDP and the payload send are faked.
"""
import asyncio
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_rest_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)

SENDS = []


async def fake_send_bin_tcp(host, port, filepath, timeout=30.0):
    SENDS.append((host, port, filepath))


pkg = _module(PKG, _send_bin_tcp=fake_send_bin_tcp)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

rest = importlib.import_module(f"{PKG}.rest")
ddp = importlib.import_module(f"{PKG}.ddp")
const = importlib.import_module(f"{PKG}.const")

SEARCHES = []
REPLY = {"value": None}


async def fake_search(host, *args, **kwargs):
    SEARCHES.append(host)
    if isinstance(REPLY["value"], Exception):
        raise REPLY["value"]
    return REPLY["value"]


rest.async_search = fake_search


class Coordinator:
    def __init__(self, data):
        self.data = data


class Entry:
    entry_id = "E1"


def hass_with(power_state):
    data = {"host": "192.0.2.1", "binloader_port": 9090, "coordinator": Coordinator({"power_state": power_state})}
    return types.SimpleNamespace(data={const.DOMAIN: {"E1": data}})


def reply(code=200, system_version="12008001"):
    return ddp.DDPReply(code, {"system-version": system_version} if system_version else {})


def enter_rest(power_state):
    asyncio.run(rest.async_enter_rest(hass_with(power_state), Entry()))


class FirmwareTests(unittest.TestCase):
    def test_firmware_of(self):
        self.assertEqual(ddp.firmware_of("12008001"), "12.00")
        self.assertEqual(ddp.firmware_of("11008001"), "11.00")
        self.assertEqual(ddp.firmware_of("07550001"), "7.55")
        for bad in (None, "", "1200", "12.00", "abcdefgh"):
            with self.subTest(value=bad):
                self.assertIsNone(ddp.firmware_of(bad))

    def test_confirmed_firmware(self):
        # an 11.00 Pro, a 12.00 Pro, a 12.02 Slim
        self.assertLessEqual({"11.00", "12.00", "12.02"}, rest.CONFIRMED_FIRMWARE)
        self.assertNotIn("13.00", rest.CONFIRMED_FIRMWARE)  # SDK lists it unchecked


class EnterRestTests(unittest.TestCase):
    def setUp(self):
        SENDS.clear()
        SEARCHES.clear()
        REPLY["value"] = reply()

    def test_sends_rest_payload_to_this_console(self):
        enter_rest("on")
        self.assertEqual(SEARCHES, ["192.0.2.1"])
        self.assertEqual(SENDS, [("192.0.2.1", 9090, str(rest.REST_PAYLOAD))])

    def test_unknown_klog_power_still_sends_when_ddp_says_on(self):
        enter_rest(None)  # klog not connected yet; DDP 200 is the authority
        self.assertEqual(len(SENDS), 1)

    def test_refused_in_rest_or_off_without_touching_the_console(self):
        for power in ("rest", "off"):
            with self.subTest(power=power), self.assertRaises(HomeAssistantError):
                enter_rest(power)
        self.assertEqual(SEARCHES, [])
        self.assertEqual(SENDS, [])

    def test_ddp_checks_block_the_send(self):
        cases = {
            "no reply": None,
            "rest (620)": reply(620),
            "unconfirmed firmware": reply(system_version="13008001"),
            "no firmware": reply(system_version=None),
            "DDP error": OSError("network unreachable"),
        }
        for name, value in cases.items():
            REPLY["value"] = value
            with self.subTest(case=name), self.assertRaises(HomeAssistantError):
                enter_rest("on")
        self.assertEqual(SENDS, [])


class PayloadFileTests(unittest.TestCase):
    def test_payload_is_shipped_and_kept_out_of_the_panel_list(self):
        data = rest.REST_PAYLOAD.read_bytes()
        self.assertEqual(data[:4], b"\x7fELF")
        self.assertIn(b"sceSystemStateMgrEnterStandby", data)
        self.assertIn(b"libSceSystemService.sprx", data)
        # bundled_payloads/ is copied to the panel's list, where a send would skip the firmware check
        self.assertFalse((COMP / "bundled_payloads" / rest.REST_PAYLOAD.name).exists())
        self.assertNotEqual(rest.REST_PAYLOAD.parent.name, "bundled_payloads")


if __name__ == "__main__":
    unittest.main()
