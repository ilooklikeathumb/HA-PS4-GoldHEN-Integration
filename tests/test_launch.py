"""Launch / close game — the payload's title ID slot, and the checks before it is sent.

Runs the real launch.py (+ rest.py's preflight) against minimal HA stand-ins (no HA install needed); DDP and the
payload send are faked.
"""
import asyncio
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_launch_test_pkg"


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


REFUSALS = []  # how many sends the fake BinLoader still refuses


async def fake_send_bin_bytes(host, port, data, name, timeout=30.0):
    if REFUSALS and REFUSALS[0] > 0:
        REFUSALS[0] -= 1
        raise HomeAssistantError("Connection to PS4 BinLoader failed") from ConnectionRefusedError(111, "refused")
    SENDS.append((host, port, data, name))


async def fake_send_bin_tcp(*args, **kwargs):
    raise AssertionError("launch sends bytes, not a file")


pkg = _module(PKG, _send_bin_bytes=fake_send_bin_bytes, _send_bin_tcp=fake_send_bin_tcp)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

launch = importlib.import_module(f"{PKG}.launch")
rest = importlib.import_module(f"{PKG}.rest")
ddp = importlib.import_module(f"{PKG}.ddp")
const = importlib.import_module(f"{PKG}.const")

MARKER = b"PS4GH_TITLE_ID_\x00"
ELF = b"\x7fELF-head-" + MARKER + b"-tail"
REPLY = {"value": ddp.DDPReply(200, {"system-version": "11008001"})}


async def fake_search(host, *args, **kwargs):
    return REPLY["value"]


rest.async_search = fake_search


class FakePayloadFile:
    data = ELF

    def read_bytes(self):
        return self.data

    name = "launch-app.elf"


launch.LAUNCH_PAYLOAD = FakePayloadFile()


class Coordinator:
    def __init__(self, data):
        self.data = data


class Entry:
    entry_id = "E1"


class Hass:
    def __init__(self, coordinator_data):
        self.data = {const.DOMAIN: {"E1": {
            "host": "192.0.2.1", "binloader_port": 9090, "coordinator": Coordinator(coordinator_data),
        }}}

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


def slot(data):
    i = ELF.index(MARKER)
    return data[i:i + len(MARKER)]


def run(coro):
    return asyncio.run(coro)


class BuildPayloadTests(unittest.TestCase):
    def test_launch_slot(self):
        self.assertEqual(slot(launch.build_payload(ELF, "CUSA90001", close_first=False)), b"CUSA90001" + b"\0" * 7)
        self.assertEqual(slot(launch.build_payload(ELF, "CUSA90001", close_first=True)), b"CUSA90001\0K" + b"\0" * 5)

    def test_close_only_slot(self):
        self.assertEqual(slot(launch.build_payload(ELF, None, close_first=True)), b"\0" * 10 + b"K" + b"\0" * 5)

    def test_resume_only_slot(self):
        self.assertEqual(slot(launch.build_payload(ELF, None, close_first=False, resume_only=True)),
                         b"\0" * 10 + b"R" + b"\0" * 5)
        with self.assertRaises(HomeAssistantError):  # resume picks the suspended game itself
            launch.build_payload(ELF, "CUSA90001", close_first=False, resume_only=True)

    def test_rest_of_elf_unchanged(self):
        out = launch.build_payload(ELF, "CUSA90001", close_first=True)
        self.assertEqual(len(out), len(ELF))
        self.assertTrue(out.startswith(b"\x7fELF-head-") and out.endswith(b"-tail"))

    def test_bad_title_id(self):
        for tid in ("CUSA9000", "cusa90001", "CUSA900010", "CUSA90001\0K", "../../x"):
            with self.subTest(tid=tid), self.assertRaises(HomeAssistantError):
                launch.build_payload(ELF, tid, close_first=False)

    def test_damaged_elf(self):
        for elf in (b"no marker", ELF + MARKER):
            with self.subTest(elf=elf), self.assertRaises(HomeAssistantError):
                launch.build_payload(elf, "CUSA90001", close_first=False)

    def test_bundled_elf_has_one_marker(self):
        self.assertEqual((COMP / "launch_payload" / "launch-app.elf").read_bytes().count(MARKER), 1)


class LaunchTests(unittest.TestCase):
    def setUp(self):
        SENDS.clear()
        REPLY["value"] = ddp.DDPReply(200, {"system-version": "11008001"})

    def test_switch_closes_running_game(self):
        run(launch.async_launch_title(Hass({"power_state": "on", "title_id": "CUSA90004"}), Entry(), "cusa90001"))
        self.assertEqual(len(SENDS), 1)
        host, port, data, name = SENDS[0]
        self.assertEqual((host, port, name), ("192.0.2.1", 9090, "launch-app.elf"))
        self.assertEqual(slot(data), b"CUSA90001\0K" + b"\0" * 5)

    def test_from_home_screen(self):
        run(launch.async_launch_title(Hass({"power_state": "on"}), Entry(), "CUSA90001"))
        self.assertEqual(slot(SENDS[0][2]), b"CUSA90001\0K" + b"\0" * 5)  # K is harmless with nothing running

    def test_same_game_running_still_sent(self):  # it may be in the background; the payload resumes it
        run(launch.async_launch_title(Hass({"power_state": "on", "title_id": "CUSA90001"}), Entry(), "CUSA90001"))
        self.assertEqual(slot(SENDS[0][2]), b"CUSA90001\0K" + b"\0" * 5)

    def test_refusals_send_nothing(self):
        cases = [
            ({"power_state": "rest"}, "CUSA90001"),
            ({"power_state": "off"}, "CUSA90001"),
            ({"power_state": "on"}, "not-a-title"),
        ]
        for data, tid in cases:
            with self.subTest(data=data, tid=tid), self.assertRaises(HomeAssistantError):
                run(launch.async_launch_title(Hass(data), Entry(), tid))
        self.assertEqual(SENDS, [])

    def test_unconfirmed_firmware_sends_nothing(self):
        REPLY["value"] = ddp.DDPReply(200, {"system-version": "13008001"})
        with self.assertRaises(HomeAssistantError):
            run(launch.async_launch_title(Hass({"power_state": "on"}), Entry(), "CUSA90001"))
        self.assertEqual(SENDS, [])

    def test_close_game(self):
        run(launch.async_close_game(Hass({"power_state": "on", "title_id": "CUSA90001"}), Entry()))
        self.assertEqual(slot(SENDS[0][2]), b"\0" * 10 + b"K" + b"\0" * 5)


class AfterWakeTests(unittest.TestCase):
    """Right after HA's wake + login klog can still say rest; DDP decides"""

    def setUp(self):
        SENDS.clear()
        REPLY["value"] = ddp.DDPReply(200, {"system-version": "12008001"})
        self.saved_wait = (launch.PAYLOADER_WAIT, launch.PAYLOADER_RETRY)
        launch.PAYLOADER_WAIT, launch.PAYLOADER_RETRY = 0.2, 0.01

    def tearDown(self):
        REFUSALS.clear()
        launch.PAYLOADER_WAIT, launch.PAYLOADER_RETRY = self.saved_wait

    def test_resume_suspended_with_klog_still_in_rest(self):
        run(launch.async_resume_suspended(Hass({"power_state": "rest"}), Entry()))
        self.assertEqual(len(SENDS), 1)
        self.assertEqual(slot(SENDS[0][2]), b"\0" * 10 + b"R" + b"\0" * 5)

    def test_launch_after_wake_with_klog_still_in_rest(self):
        run(launch.async_launch_title(Hass({"power_state": "rest"}), Entry(), "CUSA90006", after_wake=True))
        self.assertEqual(slot(SENDS[0][2]), b"CUSA90006\0K" + b"\0" * 5)  # the payload never closes the picked game

    def test_after_wake_still_needs_ddp_on_and_known_firmware(self):
        for reply in (ddp.DDPReply(620, {"system-version": "12008001"}), None,
                      ddp.DDPReply(200, {"system-version": "13008001"})):
            REPLY["value"] = reply
            with self.subTest(reply=reply), self.assertRaises(HomeAssistantError):
                run(launch.async_resume_suspended(Hass({"power_state": "rest"}), Entry()))
        self.assertEqual(SENDS, [])

    # — the send goes out at LOGIN, before GoldHEN's BinLoader listens again —

    def test_refused_after_wake_retried(self):
        REFUSALS.append(3)
        run(launch.async_resume_suspended(Hass({"power_state": "rest"}), Entry()))
        self.assertEqual(len(SENDS), 1)

    def test_refused_after_wake_gives_up(self):
        REFUSALS.append(10_000)
        with self.assertRaises(HomeAssistantError):
            run(launch.async_resume_suspended(Hass({"power_state": "rest"}), Entry()))
        self.assertEqual(SENDS, [])

    def test_refused_while_on_not_retried(self):  # BinLoader switched off in GoldHEN: tell the user at once
        REFUSALS.append(1)
        with self.assertRaises(HomeAssistantError):
            run(launch.async_launch_title(Hass({"power_state": "on"}), Entry(), "CUSA90006"))
        self.assertEqual(REFUSALS, [0])


if __name__ == "__main__":
    unittest.main()
