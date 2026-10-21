"""Firmware, hardware model and serial on the device page (no Console ID: payloads get a masked IDPS).
The MAC from DDP `host-id` as the device's network connection.

Runs the real console_info.py against minimal HA stand-ins (no HA install needed); DDP, the payload send, the
device registry and .storage are faked. Model string verbatim from an 11.00 Pro's live probe.
"""
import ast
import asyncio
import logging
from pathlib import Path
import re
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_console_info_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


class DeviceRegistry:
    """get_or_create merges connections, like HA's; update with new_connections replaces them."""

    def __init__(self):
        self.calls = []
        self.updates = []
        self.connections = set()

    def async_get_or_create(self, **kwargs):
        self.calls.append(kwargs)
        self.connections |= kwargs.get("connections", set())
        return types.SimpleNamespace(id="D1", connections=set(self.connections))

    def async_update_device(self, device_id, new_connections):
        self.updates.append(new_connections)
        self.connections = set(new_connections)


REGISTRY = DeviceRegistry()
SAVED = {}


class Store:
    loaded = {}

    def __init__(self, hass, version, key, private=False):
        self.key = key
        self.private = private

    async def async_load(self):
        return Store.loaded.get(self.key)

    def async_delay_save(self, data_func, delay=0):
        SAVED[self.key] = dict(data_func())

    async def async_remove(self):
        SAVED[self.key] = "removed"


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.helpers")
def format_mac(mac):  # HA's rule for the bare 12-hex form DDP and DHCP use
    mac = mac.lower()
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2)) if len(mac) == 12 else mac


_module("homeassistant.helpers.device_registry", DeviceInfo=dict, async_get=lambda hass: REGISTRY,
        CONNECTION_NETWORK_MAC="mac", format_mac=format_mac)
_module("homeassistant.helpers.storage", Store=Store)

SENDS = []


async def fake_send_bin_tcp(host, port, filepath, timeout=30.0):
    SENDS.append((host, port, filepath))


_TITLE_ID_RE = re.compile(r"^[A-Z]{4}\d{5}$")


def fake_is_real_game_title_id(value):  # same rule as __init__.py
    return bool(value) and bool(_TITLE_ID_RE.fullmatch(value.strip().upper())) and not value.upper().startswith("NPXS")


DDP_STATUS = []


def fake_apply_ddp_status(hass, entry_id, reply):
    """Stand-in for KlogStateMachine.on_ddp_status (the real one: test_klog_power_state.py)."""
    DDP_STATUS.append((entry_id, reply.code))
    data = hass.data[const.DOMAIN][entry_id]["coordinator"].data
    tid = reply.headers.get("running-app-titleid")
    if reply.code == 200 and tid and not data.get("title_id"):
        data["title_id"] = tid


pkg = _module(PKG, _send_bin_tcp=fake_send_bin_tcp, _is_real_game_title_id=fake_is_real_game_title_id,
              async_apply_ddp_status=fake_apply_ddp_status)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

console_info = importlib.import_module(f"{PKG}.console_info")
rest = importlib.import_module(f"{PKG}.rest")
ddp = importlib.import_module(f"{PKG}.ddp")
const = importlib.import_module(f"{PKG}.const")

MODEL = "CUH-7215B B01X"
SERIAL = "MF12345678"
LINE = f"<118>ps4_goldhen console-info: model={MODEL};serial={SERIAL}"
# what the first build printed and cached; the masked IDPS (a placeholder) must not come back
OLD_LINE = LINE + ";idps=00000001018402130000000000000001"
HOST_ID = "00005E005301"  # an 11.00 Pro's DDP host-id (DDP SRCH)
MAC = "00:00:5e:00:53:01"

SEARCHES = []
REPLY = {"value": None}


async def fake_search(host, *args, **kwargs):
    SEARCHES.append(host)
    if isinstance(REPLY["value"], Exception):
        raise REPLY["value"]
    return REPLY["value"]


console_info.async_search = fake_search
rest.async_search = fake_search


def reply(code=200, system_version="11008001", host_id=HOST_ID, running=None):
    headers = {"system-version": system_version}
    if host_id is not None:
        headers["host-id"] = host_id
    if running:
        headers["running-app-titleid"] = running
    return ddp.DDPReply(code, headers)


class Coordinator:
    def __init__(self, data):
        self.data = data

    def async_set_updated_data(self, data):
        self.data = data


class Entry:
    entry_id = "E1"
    data = {"ps4_host": "192.0.2.1"}

    def __init__(self):
        self.tasks = []

    def async_create_background_task(self, hass, coro, name):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task


class StateMachine:
    def __init__(self, connected=True):
        self.klog_connected = connected


def make_hass(power="on", title_id=None, connected=True):
    entry = Entry()
    data = {
        "host": "192.0.2.1",
        "binloader_port": 9090,
        "klog_state_machine": StateMachine(connected),
        "klog_data": {},
        "coordinator": Coordinator({"power_state": power, "title_id": title_id}),
    }
    hass = types.SimpleNamespace(
        data={const.DOMAIN: {"E1": data}},
        config_entries=types.SimpleNamespace(async_get_entry=lambda eid: entry if eid == "E1" else None),
    )
    return hass, entry, data


def reset():
    REGISTRY.calls.clear()
    REGISTRY.updates.clear()
    REGISTRY.connections = set()
    SAVED.clear()
    SENDS.clear()
    SEARCHES.clear()
    DDP_STATUS.clear()
    Store.loaded = {}
    REPLY["value"] = reply()


async def setup(hass, entry):
    await console_info.async_setup(hass, entry)


class ParseTests(unittest.TestCase):
    def test_payload_line(self):
        self.assertEqual(console_info.parse_info_line(LINE), {"model": MODEL, "serial": SERIAL})
        self.assertEqual(console_info.parse_info_line(OLD_LINE), {"model": MODEL, "serial": SERIAL})

    def test_not_the_line(self):
        for line in (
            "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90007]",
            "<118>ps4_goldhen console-info-probe: wrote /data/ps4_goldhen/console-info-probe.txt",
            "<118>ps4_goldhen rest-mode: entering rest mode",
        ):
            with self.subTest(line=line):
                self.assertIsNone(console_info.parse_info_line(line))

    def test_bad_fields_dropped(self):
        line = "ps4_goldhen console-info: model=CUH-7215B;serial=bad serial!;color=red"
        self.assertEqual(console_info.parse_info_line(line), {"model": "CUH-7215B"})
        self.assertEqual(console_info.parse_info_line("ps4_goldhen console-info: "), {})

    def test_family(self):
        self.assertEqual(console_info.model_family(MODEL), "PlayStation 4 Pro")
        self.assertEqual(console_info.model_family("CUH-2216A"), "PlayStation 4 Slim")
        self.assertEqual(console_info.model_family("CUH-1215A"), "PlayStation 4")
        self.assertEqual(console_info.model_family(None), "PlayStation 4")

    def test_device_fields(self):
        fields = console_info.device_fields({"model": MODEL, "serial": SERIAL, "firmware": "11.00"})
        self.assertEqual(fields, {
            "model": "PlayStation 4 Pro", "model_id": "CUH-7215B", "hw_version": "B01X",
            "serial_number": SERIAL, "sw_version": "11.00",
        })
        # unknown fields aren't sent, so a cold start can't blank what the device page already shows
        self.assertEqual(console_info.device_fields({}), {"model": "PlayStation 4"})
        self.assertEqual(console_info.device_fields({"mac": MAC})["connections"], {("mac", MAC)})

    def test_mac_of(self):
        self.assertEqual(console_info.mac_of(reply()), MAC)
        self.assertEqual(console_info.mac_of(reply(host_id="00005e005302")), "00:00:5e:00:53:02")
        for bad in ("00005E00530", "00005E005301AA", "00:00:5E:00:53:01", "ZZ005E005301", "", None):
            with self.subTest(host_id=bad):
                self.assertIsNone(console_info.mac_of(reply(host_id=bad)))
        self.assertIsNone(console_info.mac_of(None))


class LineTests(unittest.TestCase):
    def setUp(self):
        reset()
        REPLY["value"] = None  # DDP quiet: these are about the payload's line, not the setup-time search

    def test_line_fills_device_page_and_sensors(self):
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        with self.assertLogs(console_info._LOGGER, logging.INFO) as logs:
            self.assertTrue(console_info.async_handle_line(hass, "E1", LINE))
        created = REGISTRY.calls[-1]
        self.assertEqual(created["identifiers"], {(const.DOMAIN, "E1")})
        self.assertEqual((created["model"], created["model_id"], created["serial_number"]),
                         ("PlayStation 4 Pro", "CUH-7215B", SERIAL))
        self.assertEqual(data["coordinator"].data[const.SENSOR_HW_MODEL], MODEL)
        self.assertEqual(SAVED[f"{const.DOMAIN}.console_info.E1"], {"model": MODEL, "serial": SERIAL})
        self.assertNotIn(SERIAL, "\n".join(logs.output))  # the serial stays out of the log

    def test_other_lines_untouched(self):
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        before = len(REGISTRY.calls)
        self.assertFalse(console_info.async_handle_line(hass, "E1", "<118>[SL] AppFocusChanged [A] -> [B]"))
        self.assertEqual(len(REGISTRY.calls), before)

    def test_cache_shown_at_setup(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "firmware": "11.00"}}
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        self.assertEqual(REGISTRY.calls[-1]["sw_version"], "11.00")
        self.assertEqual(REGISTRY.calls[-1]["model"], "PlayStation 4 Pro")

    def test_cached_idps_purged(self):
        key = f"{const.DOMAIN}.console_info.E1"
        Store.loaded = {key: {"model": MODEL, "serial": SERIAL, "idps": "00000001018402130000000000000001"}}
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        self.assertEqual(SAVED[key], {"model": MODEL, "serial": SERIAL})
        self.assertNotIn("idps", data["console_info"]["info"])
        self.assertNotIn(const.SENSOR_CONSOLE_ID, data["coordinator"].data)

    def test_store_is_private(self):
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        self.assertTrue(data["console_info"]["store"].private)

    def test_klog_loop_drops_the_line_before_any_event(self):
        # In _klog_listener_task the console-info check is the first thing each line meets, and it skips the rest
        tree = ast.parse((COMP / "__init__.py").read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_klog_listener_task")
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For) and ast.unparse(n.iter) == "lines[:-1]")
        first_if = next(s for s in loop.body if isinstance(s, ast.If))
        self.assertIn("console_info.async_handle_line(hass, entry_id, line)", ast.unparse(first_if.test))
        self.assertIsInstance(first_if.body[0], ast.Continue)
        fire = next(s for s in loop.body if "signal_klog_line" in ast.unparse(s))
        self.assertLess(loop.body.index(first_if), loop.body.index(fire))


class AutoReadTests(unittest.TestCase):
    def setUp(self):
        reset()
        console_info._AUTO_DELAY = 0
        console_info._AUTO_RECHECK = 0
        console_info._AUTO_WAIT = 0

    def run_connect(self, hass, entry, connects=1):
        async def go():
            await setup(hass, entry)
            for _ in range(connects):
                console_info.async_on_klog_connected(hass, "E1")
                await asyncio.gather(*entry.tasks)
        asyncio.run(go())

    def test_first_connect_reads_firmware_and_sends_once(self):
        hass, entry, data = make_hass()
        self.run_connect(hass, entry, connects=2)
        self.assertEqual(SENDS, [("192.0.2.1", 9090, str(console_info.INFO_PAYLOAD))])
        self.assertEqual(REGISTRY.calls[-1]["sw_version"], "11.00")
        self.assertEqual(data["coordinator"].data[const.SENSOR_FW_VERSION], "11.00")

    def test_cached_console_gets_no_payload(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "serial": SERIAL, "mac": MAC}}
        hass, entry, data = make_hass()
        self.run_connect(hass, entry)
        self.assertEqual(SENDS, [])
        self.assertEqual(SEARCHES, ["192.0.2.1"] * 2)  # setup + connect: DDP for the status and the MAC

    def test_never_mid_game_or_in_rest(self):
        for kwargs in ({"title_id": "CUSA90007"}, {"power": "rest"}, {"connected": False}):
            reset()
            with self.subTest(**kwargs):
                hass, entry, data = make_hass(**kwargs)
                self.run_connect(hass, entry)
                self.assertEqual(SENDS, [])

    def test_button_read_during_the_wait_cancels_the_auto_send(self):
        # Read console info pressed mid-game while the automatic read waits for the game to end
        console_info._AUTO_WAIT = 60
        hass, entry, data = make_hass(title_id="CUSA90007")

        async def go():
            await setup(hass, entry)
            console_info.async_on_klog_connected(hass, "E1")
            await asyncio.sleep(0)
            self.assertTrue(console_info.async_handle_line(hass, "E1", LINE))  # the button's answer
            data["coordinator"].data["title_id"] = None  # game closed
            await asyncio.gather(*entry.tasks)
        asyncio.run(go())
        self.assertEqual(SENDS, [])

    def test_ddp_game_blocks_the_auto_read(self):
        # after an HA restart klog has no title until the next AppFocusChanged (on an 11.00 Pro the automatic
        # read went out mid-game); DDP's running game closes that gap
        REPLY["value"] = reply(running="CUSA90005")
        hass, entry, data = make_hass()
        self.run_connect(hass, entry)
        self.assertEqual(SENDS, [])
        self.assertEqual(DDP_STATUS, [("E1", 200)] * 2)  # setup + connect

    def test_home_screen_counts_as_idle(self):
        hass, entry, data = make_hass(title_id="NPXS20001")
        self.run_connect(hass, entry)
        self.assertEqual(len(SENDS), 1)

    def test_unconfirmed_firmware_blocks_quietly(self):
        REPLY["value"] = reply(system_version="13008001")
        hass, entry, data = make_hass()
        self.run_connect(hass, entry)
        self.assertEqual(SENDS, [])
        self.assertEqual(REGISTRY.calls[-1]["sw_version"], "13.00")  # shown, just not sent to

    def test_ddp_error_tolerated(self):
        REPLY["value"] = OSError("network unreachable")
        hass, entry, data = make_hass()
        self.run_connect(hass, entry)
        self.assertEqual(SENDS, [])


class MacTests(unittest.TestCase):
    """The MAC from DDP lands on the device (HA's DHCP watcher and router trackers match on it)."""

    def setUp(self):
        reset()

    def run_setup(self, hass, entry):
        async def go():
            await setup(hass, entry)
            await asyncio.gather(*entry.tasks)
        asyncio.run(go())

    def test_setup_reads_the_mac_from_ddp(self):
        # resting consoles answer DDP too, so no klog connect is needed
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "serial": SERIAL}}
        hass, entry, data = make_hass(power="rest", connected=False)
        self.run_setup(hass, entry)
        self.assertEqual(SEARCHES, ["192.0.2.1"])
        self.assertEqual(REGISTRY.calls[-1]["connections"], {("mac", MAC)})
        self.assertEqual(REGISTRY.calls[-1]["identifiers"], {(const.DOMAIN, "E1")})
        self.assertEqual(SAVED[f"{const.DOMAIN}.console_info.E1"],
                         {"model": MODEL, "serial": SERIAL, "firmware": "11.00", "mac": MAC})
        self.assertEqual(SENDS, [])

    def test_cached_mac_shown_when_ddp_is_quiet(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "mac": MAC}}
        REPLY["value"] = None  # fully off
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)
        self.assertEqual(SEARCHES, ["192.0.2.1"])
        self.assertEqual(REGISTRY.calls[-1]["connections"], {("mac", MAC)})
        self.assertEqual(REGISTRY.connections, {("mac", MAC)})
        self.assertEqual(REGISTRY.updates, [])  # nothing to replace

    def test_console_swap_replaces_the_mac(self):
        # Configure pointed the entry at the other Pro: the old MAC must go, or DHCP would move the entry back
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "mac": MAC}}
        REPLY["value"] = reply(host_id="00005E005302")
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)
        self.assertEqual(REGISTRY.connections, {("mac", "00:00:5e:00:53:02")})
        self.assertEqual(SAVED[f"{const.DOMAIN}.console_info.E1"]["mac"], "00:00:5e:00:53:02")

    def test_cached_firmware_kept_until_the_button(self):
        # firmware + model are read when the console is added, then only by Read console info
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "serial": SERIAL, "firmware": "9.00",
                                                            "mac": MAC}}
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)  # the console answers 11.00
        self.assertEqual(data["console_info"]["info"]["firmware"], "9.00")
        self.assertEqual(data["coordinator"].data[const.SENSOR_FW_VERSION], "9.00")
        asyncio.run(console_info.async_read(hass, entry))  # the button
        self.assertEqual(data["console_info"]["info"]["firmware"], "11.00")

    def test_console_swap_takes_its_firmware(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "firmware": "9.00", "mac": MAC}}
        REPLY["value"] = reply(host_id="00005E005302")
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)
        self.assertEqual(data["console_info"]["info"]["firmware"], "11.00")

    def test_bad_host_id_keeps_the_firmware(self):
        REPLY["value"] = reply(host_id="not-a-mac")
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)
        self.assertNotIn("connections", REGISTRY.calls[-1])
        self.assertEqual(REGISTRY.calls[-1]["sw_version"], "11.00")
        self.assertNotIn("mac", data["console_info"]["info"])

    def test_no_reply_changes_nothing(self):
        REPLY["value"] = None  # fully off
        hass, entry, data = make_hass()
        self.run_setup(hass, entry)
        self.assertEqual(SEARCHES, ["192.0.2.1"])
        self.assertEqual(data["console_info"]["info"], {})
        self.assertNotIn(f"{const.DOMAIN}.console_info.E1", SAVED)

    def test_entry_gone_before_the_reply(self):
        hass, entry, data = make_hass()

        async def search_then_unload(host, *args, **kwargs):
            hass.data[const.DOMAIN].pop("E1")  # unloaded while the search was out
            return reply()

        async def go():
            await setup(hass, entry)
            await asyncio.gather(*entry.tasks)

        console_info.async_search = search_then_unload
        try:
            asyncio.run(go())
        finally:
            console_info.async_search = fake_search
        self.assertEqual(SAVED, {})

    def test_klog_connect_also_fills_the_mac(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "serial": SERIAL, "mac": MAC}}
        REPLY["value"] = reply(host_id="00005E005302")  # what a swapped console would answer
        hass, entry, data = make_hass()

        async def go():
            await setup(hass, entry)
            console_info.async_on_klog_connected(hass, "E1")
            await asyncio.gather(*entry.tasks)
        asyncio.run(go())
        self.assertEqual(data["console_info"]["info"]["mac"], "00:00:5e:00:53:02")
        self.assertEqual(SEARCHES, ["192.0.2.1"] * 2)


class ButtonReadTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_needs_klog_for_the_answer(self):
        hass, entry, data = make_hass(connected=False)
        asyncio.run(setup(hass, entry))
        SEARCHES.clear()  # setup's own DDP search for the MAC
        with self.assertRaises(HomeAssistantError):
            asyncio.run(console_info.async_read(hass, entry))
        self.assertEqual((SEARCHES, SENDS), ([], []))

    def test_button_sends_even_when_cached(self):
        Store.loaded = {f"{const.DOMAIN}.console_info.E1": {"model": MODEL, "serial": SERIAL}}
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        asyncio.run(console_info.async_read(hass, entry))
        self.assertEqual(len(SENDS), 1)

    def test_preflight_names_the_action(self):
        REPLY["value"] = reply(system_version="13008001")
        hass, entry, data = make_hass()
        asyncio.run(setup(hass, entry))
        with self.assertRaisesRegex(HomeAssistantError, "^Reading console info isn't confirmed for PS4 firmware 13.00"):
            asyncio.run(console_info.async_read(hass, entry))


class PayloadFileTests(unittest.TestCase):
    def test_payload_is_shipped_and_kept_out_of_the_panel_list(self):
        data = console_info.INFO_PAYLOAD.read_bytes()
        self.assertEqual(data[:4], b"\x7fELF")
        for needed in (b"sceKernelGetHwModelName", b"sceKernelGetHwSerialNumber", b"libkernel_sys.sprx",
                       console_info.MARKER.encode()):
            with self.subTest(needed=needed):
                self.assertIn(needed, data)
        self.assertNotIn(b"sceKernelGetIdPs", data)
        self.assertFalse((COMP / "bundled_payloads" / console_info.INFO_PAYLOAD.name).exists())

    def test_source_prints_the_parsed_format(self):
        src = (ROOT / "payload_src/console-info/main.c").read_text()
        self.assertIn(f'"{console_info.MARKER} "', src)
        for key in ("model=", "serial="):
            self.assertIn(key, src)
        self.assertNotIn("idps=", src)


if __name__ == "__main__":
    unittest.main()
