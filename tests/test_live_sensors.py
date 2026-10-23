"""Live CPU / SoC temperature and fan duty from the resident sensors payload, behind a switch.
The fan target (its `thr` field + fan.py's payload), the Fan control switch and its slider.

Runs the real live_sensors.py / fan.py against minimal HA stand-ins (no HA install needed); DDP, the payload send and
the clock are faked. Line format verbatim from what payload_src/sensors/main.c and payload_src/fan/main.c print;
values from an 11.00 Pro's live probe (a game running) and a 12.00 Pro's fan probe.
"""
import ast
import asyncio
from pathlib import Path
import re
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_live_sensors_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


TIMERS = []


def track_time_interval(hass, action, interval):
    TIMERS.append((action, interval))
    return lambda: None


LATER = []  # [delay, action, cancelled] per async_call_later


def call_later(hass, delay, action):
    timer = [delay, action, False]
    LATER.append(timer)

    def cancel():
        timer[2] = True
    return cancel


_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda fn: fn)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.helpers")
_module("homeassistant.helpers.device_registry", DeviceInfo=dict, async_get=lambda hass: None)
class Store:
    """.storage stand-in: one dict per key, kept across 'restarts' within a test (SAVED)."""
    SAVED: dict = {}

    def __init__(self, hass, version, key, **kwargs):
        self.key = key

    async def async_load(self):
        return Store.SAVED.get(self.key)

    def async_delay_save(self, data_func, delay=0):
        Store.SAVED[self.key] = data_func()

    async def async_remove(self):
        Store.SAVED.pop(self.key, None)


_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.helpers.event", async_track_time_interval=track_time_interval, async_call_later=call_later)

SENDS = []


async def fake_send_bin_tcp(host, port, filepath, timeout=30.0):
    SENDS.append(Path(filepath).name)


SENT_BYTES = []
ON_BYTES_SEND = []  # callbacks run after a _send_bin_bytes, e.g. the console printing the fan line


async def fake_send_bin_bytes(host, port, data, name, timeout=30.0):
    SENDS.append(name)
    SENT_BYTES.append(data)
    for cb in ON_BYTES_SEND:
        cb()


_TITLE_ID_RE = re.compile(r"^[A-Z]{4}\d{5}$")


def fake_is_real_game_title_id(value):  # same rule as __init__.py
    return bool(value) and bool(_TITLE_ID_RE.fullmatch(value.strip().upper())) and not value.upper().startswith("NPXS")


pkg = _module(PKG, _send_bin_tcp=fake_send_bin_tcp, _send_bin_bytes=fake_send_bin_bytes,
              _is_real_game_title_id=fake_is_real_game_title_id)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

live = importlib.import_module(f"{PKG}.live_sensors")
rest = importlib.import_module(f"{PKG}.rest")
ddp = importlib.import_module(f"{PKG}.ddp")
const = importlib.import_module(f"{PKG}.const")
fan = importlib.import_module(f"{PKG}.fan")

LINE = "<118>[ScePartyDaemon] ps4_goldhen sensors: cpu=88;soc=88;soc0=82;duty=482;thr=83;ver=2"
OLD_LINE = "<118>[ScePartyDaemon] ps4_goldhen sensors: cpu=88;soc=88;soc0=82;duty=482"  # build 1
FAN_SET = "<118>ps4_goldhen fan: state=set;thr=72;was=83"
FAN_FAILED = "<118>ps4_goldhen fan: state=failed;step=set;errno=22;thr=83"
STARTED = "<118>[ScePartyDaemon] ps4_goldhen sensors: state=started"
STOPPED = "<118>[ScePartyDaemon] ps4_goldhen sensors: state=stopped"
REPLY = {"value": None}
CLOCK = [1000.0]


async def fake_search(host, *args, **kwargs):
    return REPLY["value"]


rest.async_search = fake_search


def reply(code=200, system_version="11008001"):
    return ddp.DDPReply(code, {"system-version": system_version})


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

    def async_on_unload(self, func):
        pass


class StateMachine:
    def __init__(self, connected=True):
        self.klog_connected = connected


def make_hass(power="on", title_id=None, connected=True, model="CUH-7215B B01X"):
    """A console HA has set up; by default a PS4 Pro CUH-7215B (factory fan target 83) whose console info was read."""
    entry = Entry()
    data = {
        "host": "192.0.2.1",
        "binloader_port": 9090,
        "klog_state_machine": StateMachine(connected),
        "klog_data": dict.fromkeys(live.SENSOR_KEYS),
        "coordinator": Coordinator({"power_state": power, "title_id": title_id}),
    }
    if model is not None:
        data["console_info"] = {"info": {"model": model}}
    async def executor(fn, *args):
        return fn(*args)

    hass = types.SimpleNamespace(
        data={const.DOMAIN: {"E1": data}},
        loop=types.SimpleNamespace(time=lambda: CLOCK[0],
                                   create_future=lambda: asyncio.get_running_loop().create_future()),
        config_entries=types.SimpleNamespace(async_get_entry=lambda eid: entry if eid == "E1" else None),
        async_add_executor_job=executor,
    )
    live.async_setup(hass, entry)
    return hass, entry, data


def reset():
    SENDS.clear()
    SENT_BYTES.clear()
    ON_BYTES_SEND.clear()
    TIMERS.clear()
    LATER.clear()
    Store.SAVED.clear()
    CLOCK[0] = 1000.0
    REPLY["value"] = reply()


def run(coro_fn):
    """Run a test body inside an event loop, then let its background sends finish."""
    async def go():
        entry = await coro_fn()
        if entry is not None:
            await asyncio.gather(*entry.tasks)
    asyncio.run(go())


class ParseTests(unittest.TestCase):
    def test_payload_line(self):
        self.assertEqual(live.parse_line(LINE), {"cpu": 88, "soc": 88, "soc0": 82, "duty": 482, "thr": 83, "ver": 2})
        self.assertEqual(live.readings(live.parse_line(LINE)), {
            const.SENSOR_CPU_TEMP: 88, const.SENSOR_SOC_TEMP: 88, const.SENSOR_FAN_DUTY: 47,  # 482 of 1024
            const.SENSOR_FAN_THRESHOLD: 83,
        })
        self.assertEqual(live.parse_line(OLD_LINE), {"cpu": 88, "soc": 88, "soc0": 82, "duty": 482})

    def test_console_line_without_process_tag(self):
        # /dev/console lines reach klog as "<118>ps4_goldhen …", klog_puts ones as "<118>[ScePartyDaemon] ps4_goldhen …"
        self.assertEqual(live.parse_line("<118>ps4_goldhen sensors: cpu=70;duty=154"), {"cpu": 70, "duty": 154})

    def test_not_the_line(self):
        for line in (
            "<118>[ScePartyDaemon] ps4_goldhen sensors-probe: r=1 t=1792027601 cpu=88/0 freq=2100000000",
            "<118>ps4_goldhen console-info: model=CUH-7215B B01X",
            "<118>[SceShellUI] I/PSM.UI : UI system timer elapsed 01:16:00",
        ):
            with self.subTest(line=line):
                self.assertIsNone(live.parse_line(line))

    def test_bad_fields_dropped(self):
        line = "ps4_goldhen sensors: cpu=-49;soc=200;duty=2000;fan=50;soc0=abc;cpu2=5;thr=0;ver=0"
        self.assertEqual(live.parse_line(line), {})
        self.assertEqual(live.parse_line("ps4_goldhen sensors: cpu=52;duty=0"), {"cpu": 52, "duty": 0})

    def test_state_lines(self):
        self.assertEqual(live.parse_line(STARTED), {"state": "started"})
        self.assertEqual(live.parse_line("ps4_goldhen sensors: state=exploded"), {})


class LineTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_line_feeds_sensors(self):
        hass, entry, data = make_hass()
        self.assertTrue(live.async_handle_line(hass, "E1", LINE))
        self.assertEqual(data["coordinator"].data[const.SENSOR_CPU_TEMP], 88)
        self.assertEqual(data["coordinator"].data[const.SENSOR_FAN_DUTY], 47)
        self.assertTrue(live.is_live(hass, "E1"))

    def test_other_lines_untouched(self):
        hass, entry, data = make_hass()
        self.assertFalse(live.async_handle_line(hass, "E1", "<118>[SL] AppFocusChanged [A] -> [B]"))
        self.assertFalse(live.is_live(hass, "E1"))

    def test_stale_reading_goes_unavailable(self):
        hass, entry, data = make_hass()
        live.async_handle_line(hass, "E1", LINE)
        CLOCK[0] += live.FRESH_FOR - 1
        live.async_check(hass, entry)
        self.assertTrue(live.is_live(hass, "E1"))  # klog is late in bursts: still current
        CLOCK[0] += 1
        self.assertFalse(live.is_live(hass, "E1"))
        live.async_check(hass, entry)
        self.assertIsNone(data["coordinator"].data[const.SENSOR_CPU_TEMP])  # no old value left behind

    def test_a_field_missing_from_a_line_goes_unavailable(self):
        """Each reading is as current as the last line: a field the line lacks (or an out-of-range value) goes
        unavailable, the others stay."""
        hass, entry, data = make_hass()
        live.async_handle_line(hass, "E1", LINE)
        for line in (LINE.replace("soc=88;", ""), LINE.replace("soc=88", "soc=200")):
            with self.subTest(line=line):
                CLOCK[0] += 30
                live.async_handle_line(hass, "E1", line)
                self.assertIsNone(data["coordinator"].data[const.SENSOR_SOC_TEMP])
                self.assertFalse(live.is_live(hass, "E1", const.SENSOR_SOC_TEMP))
                self.assertTrue(live.is_live(hass, "E1", const.SENSOR_CPU_TEMP))
                self.assertEqual(data["coordinator"].data[const.SENSOR_CPU_TEMP], 88)
        live.async_handle_line(hass, "E1", LINE.replace("thr=83;", ""))
        self.assertIsNone(fan.console_value(hass, "E1"), "the fan target too")
        live.async_handle_line(hass, "E1", LINE)
        self.assertTrue(live.is_live(hass, "E1", const.SENSOR_SOC_TEMP))
        self.assertEqual(fan.console_value(hass, "E1"), 83)

    def test_each_sensor_checks_its_own_field(self):
        tree = ast.parse((COMP / "sensor.py").read_text())
        keys = [ast.unparse(c.args[2]) for c in ast.walk(tree)
                if isinstance(c, ast.Call) and ast.unparse(c.func) == "live_sensors.is_live"]
        self.assertEqual(sorted(keys), ["SENSOR_CPU_TEMP", "SENSOR_FAN_DUTY", "SENSOR_SOC_TEMP"])

    def test_rest_or_off_is_unavailable(self):
        hass, entry, data = make_hass()
        live.async_handle_line(hass, "E1", LINE)
        data["coordinator"].data["power_state"] = "rest"
        self.assertFalse(live.is_live(hass, "E1"))

    def test_wake_does_not_bring_back_the_reading_from_before_rest(self):
        hass, entry, data = make_hass()
        live.async_handle_line(hass, "E1", LINE)
        data["coordinator"].data["power_state"] = "rest"
        CLOCK[0] += 60
        live.async_check(hass, entry)
        data["coordinator"].data["power_state"] = "on"  # woken 2 min after the last line: still inside FRESH_FOR
        self.assertFalse(live.is_live(hass, "E1"))
        self.assertIsNone(data["coordinator"].data[const.SENSOR_CPU_TEMP])
        live.async_handle_line(hass, "E1", LINE)  # the copy resumes
        self.assertTrue(live.is_live(hass, "E1"))

    def test_klog_loop_drops_the_line_before_any_event(self):
        tree = ast.parse((COMP / "__init__.py").read_text())
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_klog_listener_task")
        loop = next(n for n in ast.walk(fn) if isinstance(n, ast.For) and ast.unparse(n.iter) == "lines[:-1]")
        first_if = next(s for s in loop.body if isinstance(s, ast.If))
        self.assertIn("live_sensors.async_handle_line(hass, entry_id, line)", ast.unparse(first_if.test))
        self.assertIsInstance(first_if.body[0], ast.Continue)
        fire = next(s for s in loop.body if "signal_klog_line" in ast.unparse(s))
        self.assertLess(loop.body.index(first_if), loop.body.index(fire))

    def test_checked_every_minute(self):
        make_hass()
        self.assertEqual([interval.total_seconds() for _, interval in TIMERS], [60])


class AutoStartTests(unittest.TestCase):
    def setUp(self):
        reset()

    def connect_then_wait(self, seconds, **kwargs):
        hass, entry, data = make_hass(**kwargs)

        async def body():
            live.async_on_klog_connected(hass, "E1")
            CLOCK[0] += seconds
            live.async_check(hass, entry)
            return entry
        run(body)
        return hass, entry, data

    def test_starts_when_nothing_heard_after_connect(self):
        self.connect_then_wait(live.START_AFTER_CONNECT - 1)
        self.assertEqual(SENDS, [])
        reset()
        self.connect_then_wait(live.START_AFTER_CONNECT)
        self.assertEqual(SENDS, ["ps4-sensors.elf"])

    def test_running_copy_gets_no_second_send(self):
        hass, entry, data = make_hass()

        async def body():
            live.async_on_klog_connected(hass, "E1")
            CLOCK[0] += 40
            live.async_handle_line(hass, "E1", LINE)  # e.g. after an HA restart: the copy is still running
            CLOCK[0] += live.START_AFTER_CONNECT
            live.async_check(hass, entry)
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_idle_home_screen_gap_keeps_it_live(self):
        # seen live: klog held the payload's lines 5 min 03 s on the idle home screen
        hass, entry, data = make_hass()

        async def body():
            live.async_on_klog_connected(hass, "E1")
            live.async_handle_line(hass, "E1", LINE)
            for _ in range(6):
                CLOCK[0] += 60
                live.async_check(hass, entry)
            self.assertTrue(live.is_live(hass, "E1"))
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_silent_copy_restarted_at_most_every_ten_minutes(self):
        hass, entry, data = make_hass()

        async def body():
            live.async_on_klog_connected(hass, "E1")
            live.async_handle_line(hass, "E1", LINE)
            CLOCK[0] += live.FRESH_FOR
            live.async_check(hass, entry)  # went silent (reboot, crash): start again
            await asyncio.gather(*entry.tasks)
            CLOCK[0] += 60
            live.async_check(hass, entry)  # still silent, but the last send was a minute ago
            return entry
        run(body)
        self.assertEqual(SENDS, ["ps4-sensors.elf"])

    def test_never_mid_game_in_rest_or_switched_off(self):
        for kwargs in ({"title_id": "CUSA90007"}, {"power": "rest"}, {"connected": False}):
            reset()
            with self.subTest(**kwargs):
                self.connect_then_wait(live.START_AFTER_CONNECT, **kwargs)
                self.assertEqual(SENDS, [])
        reset()
        hass, entry, data = make_hass()

        async def body():
            await live.async_set_enabled(hass, entry, False, send=False)  # restored "off"
            live.async_on_klog_connected(hass, "E1")
            CLOCK[0] += live.START_AFTER_CONNECT
            live.async_check(hass, entry)
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_copy_from_an_older_build_is_replaced_once(self):
        hass, entry, data = make_hass()

        async def body():
            live.async_on_klog_connected(hass, "E1")
            live.async_handle_line(hass, "E1", OLD_LINE)  # still the patch-19 copy after the update
            await asyncio.gather(*entry.tasks)
            CLOCK[0] += 30
            live.async_handle_line(hass, "E1", OLD_LINE)  # the next line from it: no second send within RETRY
            live.async_handle_line(hass, "E1", LINE)
            return entry
        run(body)
        self.assertEqual(SENDS, ["ps4-sensors.elf"])
        self.assertEqual(data["coordinator"].data[const.SENSOR_CPU_TEMP], 88)  # its readings count meanwhile

    def test_older_build_mid_game_waits(self):
        hass, entry, data = make_hass(title_id="CUSA90007")
        run(lambda: _handle(hass, entry, OLD_LINE))
        self.assertEqual(SENDS, [])

    def test_timed_check_starts_it_without_the_minute_tick(self):
        # one check is timed for the moment the wait is up, so the temps start without waiting for the next
        # minute check (or a switch off / on)
        self.assertLessEqual(live.START_AFTER_CONNECT, 60)
        hass, entry, data = make_hass()

        async def body():
            live.async_on_klog_connected(hass, "E1")
            [(delay, action, cancelled)] = LATER
            self.assertFalse(cancelled)
            self.assertGreater(delay, live.START_AFTER_CONNECT)  # the check sees the wait as over
            CLOCK[0] += delay
            action(None)
            return entry
        run(body)
        self.assertEqual(SENDS, ["ps4-sensors.elf"])

    def test_reconnect_replaces_the_timed_check(self):
        hass, entry, data = make_hass()
        live.async_on_klog_connected(hass, "E1")
        CLOCK[0] += 30
        live.async_on_klog_connected(hass, "E1")  # klog dropped and came back
        self.assertEqual([t[2] for t in LATER], [True, False])
        unload = []
        entry.async_on_unload = unload.append  # entry unloaded: the pending check goes too
        live.async_setup(hass, entry)
        live.async_on_klog_connected(hass, "E1")
        for fn in unload:
            fn()
        self.assertTrue(LATER[-1][2])

    def test_timed_check_mid_game_sends_nothing(self):
        hass, entry, data = make_hass(title_id="CUSA90007")

        async def body():
            live.async_on_klog_connected(hass, "E1")
            delay, action, _ = LATER[0]
            CLOCK[0] += delay
            action(None)
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_unconfirmed_firmware_blocks_quietly(self):
        REPLY["value"] = reply(system_version="13008001")
        self.connect_then_wait(live.START_AFTER_CONNECT)
        self.assertEqual(SENDS, [])


class SwitchTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_on_by_default(self):
        hass, entry, data = make_hass()
        self.assertTrue(live.is_enabled(hass, "E1"))

    def test_off_stops_the_payload_and_clears_readings(self):
        hass, entry, data = make_hass()

        async def body():
            live.async_handle_line(hass, "E1", LINE)
            await live.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(SENDS, ["ps4-sensors-stop.elf"])
        self.assertIsNone(data["coordinator"].data[const.SENSOR_CPU_TEMP])
        self.assertFalse(live.is_live(hass, "E1"))
        live.async_handle_line(hass, "E1", LINE)  # a late line from before the stop
        self.assertFalse(live.is_live(hass, "E1"))

    def test_on_sends_now_even_mid_game(self):
        hass, entry, data = make_hass(title_id="CUSA90007")
        run(lambda: live.async_set_enabled(hass, entry, True, send=True))
        self.assertEqual(SENDS, ["ps4-sensors.elf"])

    def test_off_in_rest_stops_it_on_the_next_line(self):
        hass, entry, data = make_hass(power="rest")

        async def body():
            await live.async_set_enabled(hass, entry, False, send=True)
            self.assertEqual(SENDS, [])  # nothing to send to in rest
            data["coordinator"].data["power_state"] = "on"  # woke up, the copy kept running
            live.async_handle_line(hass, "E1", LINE)
            await asyncio.gather(*entry.tasks)
            live.async_handle_line(hass, "E1", LINE)  # 30 s later: no second stop
            return entry
        run(body)
        self.assertEqual(SENDS, ["ps4-sensors-stop.elf"])

    def test_on_needs_klog_for_the_readings(self):
        hass, entry, data = make_hass(connected=False)
        with self.assertRaises(HomeAssistantError):
            asyncio.run(live.async_set_enabled(hass, entry, True, send=True))
        self.assertEqual(SENDS, [])


async def _handle(hass, entry, line):
    live.async_handle_line(hass, "E1", line)
    return entry


class FanThresholdTests(unittest.TestCase):
    def setUp(self):
        reset()

    def test_threshold_from_the_sensors_line(self):
        hass, entry, data = make_hass()
        live.async_handle_line(hass, "E1", LINE)
        self.assertEqual(data["coordinator"].data[const.SENSOR_FAN_THRESHOLD], 83)
        data["coordinator"].data["power_state"] = "rest"
        live.async_check(hass, entry)
        self.assertIsNone(data["coordinator"].data[const.SENSOR_FAN_THRESHOLD])  # cleared with the rest

    def test_fan_lines_parsed_and_dropped(self):
        self.assertEqual(fan.parse_line(FAN_SET), {"state": "set", "thr": 72, "was": 83})
        self.assertEqual(fan.parse_line(FAN_FAILED), {"state": "failed", "step": "set", "errno": 22, "thr": 83})
        for line in ("<118>ps4_goldhen fanprobe: 8F08 sel=0 rc=0", "<118>ps4_goldhen fanset: SET thr=60", LINE):
            with self.subTest(line=line):
                self.assertIsNone(fan.parse_line(line))
        hass, entry, data = make_hass()
        self.assertTrue(live.async_handle_line(hass, "E1", FAN_SET))  # dropped before klog events
        self.assertEqual(data["coordinator"].data[const.SENSOR_FAN_THRESHOLD], 72)
        self.assertIsNone(data["coordinator"].data[const.SENSOR_CPU_TEMP])  # not a sensors reading

    def test_build_payload(self):
        elf = b"\x7fELF..." + fan._MARKER + b"..."
        self.assertEqual(fan.build_payload(elf, 72), b"\x7fELF..." + b"72" + b"\0" * 14 + b"...")
        for bad in (49, 84, 0):
            with self.subTest(threshold=bad), self.assertRaises(HomeAssistantError):
                fan.build_payload(elf, bad)
        with self.assertRaises(HomeAssistantError):
            fan.build_payload(b"\x7fELF no marker", 72)

    def set_threshold(self, value, line=None, **kwargs):
        hass, entry, data = make_hass(**kwargs)
        if line:
            ON_BYTES_SEND.append(lambda: live.async_handle_line(hass, "E1", line))
        error = None

        async def body():
            nonlocal error
            try:
                await fan.async_set_threshold(hass, entry, value)
            except HomeAssistantError as err:
                error = err
        asyncio.run(body())
        return data, error

    def test_set_confirmed_by_the_payload(self):
        data, error = self.set_threshold(72, FAN_SET)
        self.assertIsNone(error)
        self.assertEqual(SENDS, ["ps4-fan.elf"])
        self.assertEqual(SENT_BYTES[0].count(b"72" + b"\0" * 14), 1)
        self.assertNotIn(fan._MARKER, SENT_BYTES[0])
        self.assertEqual(data["coordinator"].data[const.SENSOR_FAN_THRESHOLD], 72)
        self.assertIsNone(data["fan"]["waiter"])

    def test_payload_failure_is_an_error_and_shows_what_it_holds(self):
        data, error = self.set_threshold(72, FAN_FAILED)
        self.assertIn("failed at set", str(error))
        self.assertEqual(data["coordinator"].data[const.SENSOR_FAN_THRESHOLD], 83)

    def test_no_line_is_an_error(self):
        timeout, fan.CONFIRM_TIMEOUT = fan.CONFIRM_TIMEOUT, 0.01
        try:
            data, error = self.set_threshold(72)
        finally:
            fan.CONFIRM_TIMEOUT = timeout
        self.assertIn("didn't confirm", str(error))
        self.assertIsNone(data["fan"]["waiter"])

    def test_nothing_sent_in_rest_without_klog_or_on_unconfirmed_firmware(self):
        for kwargs in ({"power": "rest"}, {"power": "off"}, {"connected": False}):
            reset()
            with self.subTest(**kwargs):
                data, error = self.set_threshold(72, FAN_SET, **kwargs)
                self.assertIsInstance(error, HomeAssistantError)
                self.assertEqual(SENDS, [])
        reset()
        REPLY["value"] = reply(system_version="13008001")
        data, error = self.set_threshold(72, FAN_SET)
        self.assertIn("isn't confirmed", str(error))
        self.assertEqual(SENDS, [])

    def test_out_of_range_never_sent(self):
        data, error = self.set_threshold(49, FAN_SET)
        self.assertIsInstance(error, HomeAssistantError)
        self.assertEqual(SENDS, [])

    def test_community_fan_payload_not_bundled(self):
        # its zeroed 10-byte SET wipes channel 0's other fields; Fan control replaces it
        self.assertFalse((COMP / "bundled_payloads" / "fan-threshold.bin").exists())
        self.assertNotIn("fan-threshold", (COMP / "frontend/ps4-goldhen-panel.js").read_text())

    def test_payload_and_source_agree(self):
        elf = fan.FAN_PAYLOAD.read_bytes()
        self.assertEqual(elf[:4], b"\x7fELF")
        self.assertEqual(elf.count(fan._MARKER), 1)
        self.assertIn(b"/dev/icc_fan", elf)
        self.assertFalse((COMP / "bundled_payloads" / fan.FAN_PAYLOAD.name).exists())
        src = (ROOT / "payload_src/fan/main.c").read_text()
        self.assertIn(f'volatile char target[16] = "{fan._MARKER[:-1].decode()}";', src)
        self.assertIn(f'#define MARKER            "{fan.LINE_MARKER} "', src)
        self.assertIn(f"#define MIN_THRESHOLD     {fan.MIN_THRESHOLD}", src)
        self.assertIn(f"#define MAX_THRESHOLD     {fan.MAX_THRESHOLD}", src)
        for key in ("state=set;thr=%d;was=%d", "state=failed;step=%s;errno=%d"):
            self.assertIn(key, src)


def _line_for(value, was=83):
    return f"<118>ps4_goldhen fan: state=set;thr={value};was={was}"


class FanControlTests(unittest.TestCase):
    """The Fan control switch (off = back to what the console held before) and the slider HA holds while it's on."""

    def setUp(self):
        reset()

    def hass_answering(self, **kwargs):
        """A console that takes every send: the payload's line carries the target written into the ELF."""
        hass, entry, data = make_hass(**kwargs)
        offset = fan.FAN_PAYLOAD.read_bytes().index(fan._MARKER)

        def answer():
            value = int(SENT_BYTES[-1][offset:offset + 16].rstrip(b"\0"))
            live.async_handle_line(hass, "E1", _line_for(value, was=fan.console_value(hass, "E1") or 83))
        ON_BYTES_SEND.append(answer)
        return hass, entry, data

    def sent_values(self):
        offset = fan.FAN_PAYLOAD.read_bytes().index(fan._MARKER)
        return [int(b[offset:offset + 16].rstrip(b"\0")) for b in SENT_BYTES]

    def test_off_by_default_and_hands_off(self):
        hass, entry, data = self.hass_answering()
        self.assertFalse(fan.is_enabled(hass, "E1"))
        self.assertEqual(fan.wanted(hass, "E1"), 83, "the slider starts at the model's factory target")
        run(lambda: _handle(hass, entry, LINE.replace("thr=83", "thr=70")))  # e.g. set by the PS4 Temperature app
        self.assertEqual(SENDS, [])
        self.assertEqual(fan.console_value(hass, "E1"), 70)

    def test_slider_while_off_only_stores(self):
        hass, entry, data = self.hass_answering()
        asyncio.run(fan.async_set_wanted(hass, entry, 72, send=True))
        self.assertEqual((SENDS, fan.wanted(hass, "E1")), ([], 72))

    def test_on_sends_the_slider_and_off_sends_back_what_it_held(self):
        hass, entry, data = self.hass_answering()

        async def body():
            live.async_handle_line(hass, "E1", LINE)  # console holds 83
            await fan.async_set_wanted(hass, entry, 72, send=True)
            await fan.async_set_enabled(hass, entry, True, send=True)
            await fan.async_set_wanted(hass, entry, 65, send=True)  # on: the slider applies at once
            await fan.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [72, 65, 83])
        self.assertEqual(fan.console_value(hass, "E1"), 83)
        self.assertFalse(data["fan"]["restore_pending"])

    def test_no_send_when_the_console_already_holds_it(self):
        hass, entry, data = self.hass_answering()

        async def body():
            live.async_handle_line(hass, "E1", LINE)  # 83
            await fan.async_set_enabled(hass, entry, True, send=True)  # slider still 83
            await fan.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_on_reapplies_after_drift_at_most_every_ten_minutes(self):
        hass, entry, data = self.hass_answering()

        async def body():
            await fan.async_set_wanted(hass, entry, 72, send=False)
            await fan.async_set_enabled(hass, entry, True, send=False)  # restored "on" after an HA restart
            live.async_handle_line(hass, "E1", LINE)  # console holds 83 (unplugged since)
            await asyncio.gather(*entry.tasks)
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=72"))  # took it
            CLOCK[0] += 60
            live.async_handle_line(hass, "E1", LINE)  # drifted again a minute later: wait out AUTO_RETRY
            CLOCK[0] += fan.AUTO_RETRY
            live.async_handle_line(hass, "E1", LINE)
            await asyncio.gather(*entry.tasks)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [72, 72])

    def test_no_automatic_send_mid_game(self):
        hass, entry, data = self.hass_answering(title_id="CUSA90003")

        async def body():
            await fan.async_set_wanted(hass, entry, 72, send=False)
            await fan.async_set_enabled(hass, entry, True, send=False)
            live.async_handle_line(hass, "E1", LINE)
            return entry
        run(body)
        self.assertEqual(SENDS, [])

    def test_slim_off_goes_back_to_its_own_target_not_83(self):
        # Off must not send the Pro's 83 to a Slim or an original PS4 (their own targets are 78-80)
        hass, entry, data = self.hass_answering()

        async def body():
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))
            await fan.async_set_wanted(hass, entry, 67, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)
            self.assertEqual(fan.off_value(hass, "E1"), 79)  # the slider's attribute
            await fan.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [67, 79])
        self.assertEqual(fan.console_value(hass, "E1"), 79)
        self.assertIsNone(fan.off_value(hass, "E1"))

    def test_on_in_rest_learns_it_from_the_first_send(self):
        hass, entry, data = self.hass_answering(power="rest")

        async def body():
            await fan.async_set_wanted(hass, entry, 70, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)  # in rest: nothing known, nothing sent
            self.assertIsNone(fan.off_value(hass, "E1"))
            data["coordinator"].data["power_state"] = "on"
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))  # woke: holds 79 → HA sends 70
            await asyncio.gather(*entry.tasks)
            self.assertEqual(fan.off_value(hass, "E1"), 79)  # from the line's was=79
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=70"))
            await fan.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [70, 79])

    def test_kept_across_an_ha_restart(self):
        hass, entry, data = self.hass_answering()

        async def body():
            await fan.async_load(hass, entry)
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))
            await fan.async_set_wanted(hass, entry, 65, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)
            return entry
        run(body)
        SENT_BYTES.clear()
        ON_BYTES_SEND.clear()
        hass, entry, data = self.hass_answering()  # restarted: switch + slider restore without a send

        async def after():
            await fan.async_load(hass, entry)
            await fan.async_set_wanted(hass, entry, 65, send=False)
            await fan.async_set_enabled(hass, entry, True, send=False)
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=65"))
            await fan.async_set_enabled(hass, entry, False, send=True)
            await fan.async_remove(hass, entry)  # entry deleted: the stored value goes too
            return entry
        run(after)
        self.assertEqual(self.sent_values(), [79])
        self.assertEqual(Store.SAVED, {})

    def test_off_in_rest_restores_when_the_console_reports(self):
        hass, entry, data = self.hass_answering()

        async def body():
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))
            await fan.async_set_wanted(hass, entry, 72, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)
            data["coordinator"].data["power_state"] = "rest"
            await fan.async_set_enabled(hass, entry, False, send=True)  # in rest: nothing to send to
            self.assertEqual(self.sent_values(), [72])
            data["coordinator"].data["power_state"] = "on"
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=72"))
            await asyncio.gather(*entry.tasks)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [72, 79])
        self.assertFalse(data["fan"]["restore_pending"])

    def test_off_in_rest_survives_an_ha_restart(self):
        """Off while the console rests, then HA restarts before it wakes: the console still gets its own target back."""
        hass, entry, data = self.hass_answering()

        async def body():
            await fan.async_load(hass, entry)
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))
            await fan.async_set_wanted(hass, entry, 72, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)
            data["coordinator"].data["power_state"] = "rest"
            await fan.async_set_enabled(hass, entry, False, send=True)  # pending: the console sleeps
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [72])
        SENT_BYTES.clear()
        ON_BYTES_SEND.clear()
        hass, entry, data = self.hass_answering(power="rest")  # restarted while the console rests

        async def after():
            await fan.async_load(hass, entry)
            await fan.async_set_enabled(hass, entry, False, send=False)  # the switch restores off
            self.assertEqual(fan.off_value(hass, "E1"), 79)
            data["coordinator"].data["power_state"] = "on"
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=72"))  # woke, still on HA's 72
            await asyncio.gather(*entry.tasks)
            return entry
        run(after)
        self.assertEqual(self.sent_values(), [79])
        self.assertFalse(data["fan"]["restore_pending"])
        self.assertIsNone(Store.SAVED[f"{const.DOMAIN}.fan.E1"].get("restore_to"), "landed: nothing owed any more")

    def test_on_again_before_the_off_landed_keeps_the_own_value(self):
        hass, entry, data = self.hass_answering()

        async def body():
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=79"))
            await fan.async_set_wanted(hass, entry, 70, send=False)
            await fan.async_set_enabled(hass, entry, True, send=True)
            data["coordinator"].data["power_state"] = "rest"
            await fan.async_set_enabled(hass, entry, False, send=True)  # pending: the console sleeps
            data["coordinator"].data["power_state"] = "on"
            await fan.async_set_enabled(hass, entry, True, send=True)  # it still holds 70 (HA's), not its own
            self.assertEqual(fan.off_value(hass, "E1"), 79)
            await fan.async_set_enabled(hass, entry, False, send=True)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [70, 79])

    def test_off_when_ha_changed_nothing_sends_nothing(self):
        for power in ("rest", "on"):
            reset()
            with self.subTest(power=power):
                hass, entry, data = self.hass_answering(power=power)

                async def body():
                    await fan.async_set_enabled(hass, entry, True, send=False)  # e.g. turned on, then off at once
                    await fan.async_set_enabled(hass, entry, False, send=True)
                    data["coordinator"].data["power_state"] = "on"
                    live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=70"))  # another tool's value
                    return entry
                run(body)
                self.assertEqual(SENDS, [])
                self.assertFalse(data["fan"]["restore_pending"])

    def test_failed_off_retries_later(self):
        hass, entry, data = make_hass()
        ON_BYTES_SEND.append(lambda: live.async_handle_line(hass, "E1", FAN_FAILED))

        async def body():
            data["fan"].update(enabled=True, before=79)
            live.async_handle_line(hass, "E1", LINE.replace("thr=83", "thr=72"))
            with self.assertRaises(HomeAssistantError):
                await fan.async_set_enabled(hass, entry, False, send=True)
            self.assertTrue(data["fan"]["restore_pending"])
            self.assertEqual(fan.off_value(hass, "E1"), 79)
            return entry
        run(body)

    def test_slider_range(self):
        hass, entry, data = make_hass()
        for bad in (49, 84):
            with self.subTest(value=bad), self.assertRaises(HomeAssistantError):
                asyncio.run(fan.async_set_wanted(hass, entry, bad, send=False))
        self.assertEqual((fan.MIN_THRESHOLD, fan.MAX_THRESHOLD, fan.max_threshold(hass, "E1")), (50, 83, 83))

    def test_slider_stops_at_the_models_factory_target(self):
        for model, top in (("CUH-7215B B01X", 83), ("CUH-7216B", 83), ("CUH-7016B", 79), ("CUH-7116B", 79),
                           ("CUH-2215B", 80), ("CUH-2015A", 80), ("CUH-1116A", 80), ("CUH-1215A B01", 78),
                           ("CUH-2116B", 78), (None, 78)):
            with self.subTest(model=model):
                hass, entry, data = make_hass(model=model)
                self.assertEqual(fan.max_threshold(hass, "E1"), top)
                self.assertEqual(fan.wanted(hass, "E1"), top, "the slider starts at the top")
                with self.assertRaises(HomeAssistantError):
                    asyncio.run(fan.async_set_wanted(hass, entry, top + 1, send=False))
                asyncio.run(fan.async_set_wanted(hass, entry, top, send=False))
                self.assertEqual(fan.wanted(hass, "E1"), top)

    def test_restored_value_above_the_cap_holds_the_cap(self):
        # a Slim's saved 83 is above its cap: Fan control holds 80, its factory target
        hass, entry, data = self.hass_answering(model="CUH-2215B")
        fan.restore_wanted(hass, "E1", 83)
        self.assertEqual(fan.wanted(hass, "E1"), 80)

        async def body():
            live.async_handle_line(hass, "E1", LINE)  # holds 83
            await fan.async_set_enabled(hass, entry, True, send=True)
            return entry
        run(body)
        self.assertEqual(self.sent_values(), [80])

    def test_cap_follows_the_console_info_read(self):
        hass, entry, data = make_hass(model=None)  # set up, console info not read yet
        self.assertEqual(fan.wanted(hass, "E1"), 78)
        data["console_info"] = {"info": {"model": "CUH-7215B B01X"}}
        self.assertEqual((fan.max_threshold(hass, "E1"), fan.wanted(hass, "E1")), (83, 83))
        fan.restore_wanted(hass, "E1", 49)  # out of range: ignored
        self.assertEqual(fan.wanted(hass, "E1"), 83)


class PayloadFileTests(unittest.TestCase):
    def test_payloads_are_shipped_and_kept_out_of_the_panel_list(self):
        data = live.SENSORS_PAYLOAD.read_bytes()
        self.assertEqual(data[:4], b"\x7fELF")
        for needed in (b"sceKernelGetCpuTemperature", b"sceKernelGetSocSensorTemperature",
                       b"sceKernelGetCurrentFanDuty", b"libkernel_sys.sprx", live.MARKER.encode(),
                       b"/data/ps4_goldhen/sensors.run", b"/dev/icc_fan"):
            with self.subTest(needed=needed):
                self.assertIn(needed, data)
        self.assertNotIn(b"sceKernelGetSocPowerConsumption", data)  # power waits (buffer-size trap)
        stop = live.STOP_PAYLOAD.read_bytes()
        self.assertEqual(stop[:4], b"\x7fELF")
        self.assertIn(b"/data/ps4_goldhen/sensors.run", stop)
        for path in (live.SENSORS_PAYLOAD, live.STOP_PAYLOAD):
            self.assertFalse((COMP / "bundled_payloads" / path.name).exists())

    def test_source_prints_the_parsed_format(self):
        src = (ROOT / "payload_src/sensors/main.c").read_text()
        self.assertIn(f'#define MARKER      "{live.MARKER} "', src)
        for key in ("cpu=", "soc=", "soc0=", "duty=", "thr=", "ver=", "state=started", "state=stopped"):
            self.assertIn(key, src)
        self.assertIn(f"#define DUTY_SCALE  {live.DUTY_SCALE}", src)
        self.assertIn(f"#define VERSION     {live.PAYLOAD_VERSION}", src)
        # through /dev/console (klog delivers it at once), klog_puts only as the fallback inside say()
        self.assertIn('open("/dev/console"', src)
        self.assertEqual(src.count("klog_puts("), 1)


if __name__ == "__main__":
    unittest.main()
