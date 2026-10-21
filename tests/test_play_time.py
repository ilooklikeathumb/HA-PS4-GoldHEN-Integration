"""Play time per console / game / session (play_time.py) and its two sensors.

Runs the real play_time.py + game_session.py against minimal HA stand-ins. The end-to-end test replays the console_a
Pro's klog capture (test_game_session.CAPTURE) through the real KlogStateMachine.
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_play_time_test_pkg"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_game_session import CAPTURE, NAMES, GAME_D, GAME_E, OTHER, at  # noqa: E402
from test_klog_power_state import KlogStateMachine  # noqa: E402

LOCAL = ZoneInfo("America/Los_Angeles")
NOW = [datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)]
_SAVED: dict = {}
CONNECTED: dict = {}   # signal -> [callbacks]
INTERVALS: list = []
LISTEN_ONCE: list = []
COMMANDS: list = []


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    _SAVED.setdefault(name, sys.modules.get(name))
    sys.modules[name] = mod
    return mod


class Store:
    saved: dict = {}
    delayed: dict = {}

    def __init__(self, hass, version, key, private=False):
        self.key = key

    async def async_load(self):
        return copy.deepcopy(Store.saved.get(self.key))

    async def async_save(self, data):
        Store.saved[self.key] = json.loads(json.dumps(data))  # must be JSON, like HA's

    def async_delay_save(self, data_func, delay):
        Store.delayed[self.key] = (data_func, delay)

    async def async_remove(self):
        Store.saved.pop(self.key, None)

    @classmethod
    def flush(cls):
        for key, (fn, _delay) in list(cls.delayed.items()):
            cls.saved[key] = json.loads(json.dumps(fn()))
        cls.delayed.clear()


def _send(hass, signal, *args):
    for fn in list(CONNECTED.get(signal, [])):
        fn(*args)


def _connect(hass, signal, fn):
    CONNECTED.setdefault(signal, []).append(fn)
    return lambda: CONNECTED[signal].remove(fn)


def _interval(hass, fn, period):
    INTERVALS.append(fn)
    return lambda: INTERVALS.remove(fn)


def _passthrough_cmd(schema):
    def wrap(fn):
        fn.schema = schema
        return fn
    return wrap


class _Entity:
    hass = None

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, fn):
        pass

    def async_write_ha_state(self):
        pass


class _Any:
    def __getattr__(self, name):
        return name


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.websocket_api", websocket_command=_passthrough_cmd, require_admin=lambda f: f,
        async_response=lambda f: f, async_register_command=lambda hass, cmd: COMMANDS.append(cmd),
        ActiveConnection=object)
_module("homeassistant.components.sensor", SensorEntity=_Entity, SensorDeviceClass=_Any(), SensorStateClass=_Any())
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.const", EVENT_HOMEASSISTANT_STOP="homeassistant_stop", UnitOfTemperature=_Any(),
        UnitOfPower=_Any(), UnitOfTime=_Any())
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=Exception)
_module("homeassistant.helpers")
_module("homeassistant.helpers.dispatcher", async_dispatcher_send=_send, async_dispatcher_connect=_connect)
_module("homeassistant.helpers.event", async_track_time_interval=_interval, async_track_state_change_event=None,
        async_call_later=None)  # live_sensors imports it
_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.helpers.entity", EntityCategory=_Any())
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module("homeassistant.helpers.restore_state", RestoreEntity=type("RestoreEntity", (), {}))
_module("homeassistant.helpers.update_coordinator", CoordinatorEntity=type("CoordinatorEntity", (), {}))
_module("homeassistant.helpers.device_registry", DeviceInfo=dict)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: NOW[0], as_local=lambda d: d.astimezone(LOCAL),
        parse_datetime=datetime.fromisoformat)
_module("voluptuous", Required=lambda key: key)
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
pt = importlib.import_module(f"{PKG}.play_time")
gs = importlib.import_module(f"{PKG}.game_session")
sensor_mod = importlib.import_module(f"{PKG}.sensor")
for _name, _old in _SAVED.items():
    if _old is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _old

T0 = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)  # 13:00 local


def book(data=None):
    return pt.PlayTimeBook(data, to_local=lambda d: d.astimezone(LOCAL))


def mins(n):
    return timedelta(minutes=n)


class BookTests(unittest.TestCase):
    def test_counts_while_in_focus(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        for i in range(1, 6):
            b.accrue(T0 + mins(i))
        b.stop(T0 + mins(5) + timedelta(seconds=30))
        self.assertEqual(b.games[GAME_D]["seconds"], 330)
        self.assertEqual(b.today_seconds(T0), 330)
        self.assertEqual(b.session_seconds(), 330)
        self.assertIsNone(b.active)

    def test_dip_under_30_min_is_the_same_session_and_the_gap_not_counted(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.stop(T0 + mins(10))
        self.assertFalse(b.expire(T0 + mins(39)))
        self.assertFalse(b.start(GAME_D, "Game D", T0 + mins(39)))
        b.stop(T0 + mins(49))
        self.assertEqual(b.session_seconds(), 20 * 60)
        self.assertEqual(b.open["start"], T0.isoformat())
        self.assertEqual(b.sessions, [])

    def test_30_min_away_ends_the_session(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.stop(T0 + mins(10))
        self.assertTrue(b.expire(T0 + mins(41)))
        self.assertIsNone(b.open)
        self.assertEqual(b.sessions, [{"title_id": GAME_D, "name": "Game D", "start": T0.isoformat(),
                                       "end": (T0 + mins(10)).isoformat(), "seconds": 600}])
        self.assertEqual(b.games[GAME_D]["sessions"], 1)

    def test_coming_back_late_without_a_tick_still_splits(self):  # expire runs on the tick; start checks too
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.stop(T0 + mins(10))
        self.assertTrue(b.start(GAME_D, "Game D", T0 + mins(45)))
        self.assertEqual(len(b.sessions), 1)
        self.assertEqual(b.open["start"], (T0 + mins(45)).isoformat())

    def test_another_game_ends_the_session_at_once(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.stop(T0 + mins(10))
        self.assertTrue(b.start(GAME_E, "Game E", T0 + mins(11)))
        self.assertEqual([s["title_id"] for s in b.sessions], [GAME_D])
        self.assertEqual(b.open["title_id"], GAME_E)

    def test_switch_without_a_left_still_counts_the_first_game(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.start(GAME_E, "Game E", T0 + mins(3))
        self.assertEqual(b.sessions[0]["seconds"], 180)

    def test_split_at_local_midnight(self):
        b = book()
        start = datetime(2026, 10, 17, 6, 50, tzinfo=timezone.utc)  # 23:50 local
        b.start(GAME_D, "Game D", start)
        b.accrue(start + mins(10))
        b.accrue(start + mins(20))
        self.assertEqual(b.days, {"2026-10-16": {GAME_D: 600}, "2026-10-17": {GAME_D: 600}})
        self.assertEqual(b.today_seconds(start + mins(20)), 600)
        self.assertEqual(b.games[GAME_D]["seconds"], 1200)

    def test_a_clock_jump_counts_at_most_10_min(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.accrue(T0 + timedelta(hours=3))
        self.assertEqual(b.session_seconds(), 600)
        b.accrue(T0 + timedelta(hours=3, minutes=1))
        self.assertEqual(b.session_seconds(), 660)

    def test_time_going_backwards_counts_nothing(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        self.assertFalse(b.accrue(T0 - mins(5)))
        self.assertEqual(b.session_seconds(), 0)

    def test_empty_session_is_not_kept(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.stop(T0)
        b.expire(T0 + mins(31))
        self.assertEqual(b.sessions, [])
        self.assertEqual(b.summary(T0 + mins(31))["games"], [])

    def test_restart_mid_session(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.accrue(T0 + mins(10))
        data = json.loads(json.dumps(b.to_store()))
        b2 = book(data)  # HA back 5 min later; the game is seen again after klog connects
        self.assertIsNone(b2.active)
        self.assertFalse(b2.accrue(T0 + mins(15)))  # not counting until game_session says so
        self.assertFalse(b2.start(GAME_D, "Game D", T0 + mins(15)))
        b2.accrue(T0 + mins(20))
        self.assertEqual(b2.session_seconds(), 15 * 60)  # 10 + 5: the 5 min HA was down aren't counted

    def test_restart_after_30_min_closes_on_load(self):
        b = book()
        b.start(GAME_D, "Game D", T0)
        b.accrue(T0 + mins(10))
        b2 = book(json.loads(json.dumps(b.to_store())))
        self.assertTrue(b2.expire(T0 + mins(45)))
        self.assertEqual(b2.sessions[0]["seconds"], 600)

    def test_prune_and_cap(self):
        b = book({"games": {}, "days": {"2025-01-01": {GAME_D: 5}, "2026-10-01": {GAME_D: 7}}, "sessions": [], "open": None})
        b.prune(T0)
        self.assertEqual(list(b.days), ["2026-10-01"])
        for i in range(pt.SESSIONS_KEPT + 5):
            b.start(GAME_D if i % 2 else GAME_E, "x", T0 + mins(2 * i))
            b.accrue(T0 + mins(2 * i + 1))
        self.assertEqual(len(b.sessions), pt.SESSIONS_KEPT)

    def test_summary(self):
        b = book({"games": {GAME_E: {"name": "Game E", "seconds": 3000, "sessions": 2,
                                    "first": "2026-09-01T20:00:00+00:00", "last": "2026-10-01T20:00:00+00:00"}},
                  "days": {"2026-10-01": {GAME_E: 3000}}, "sessions": [
                      {"title_id": GAME_E, "name": "Game E", "start": "2026-10-01T19:10:00+00:00",
                       "end": "2026-10-01T20:00:00+00:00", "seconds": 3000}], "open": None})
        b.start(GAME_D, "Game D", T0)
        for i in range(1, 31):
            b.accrue(T0 + mins(i))
        s = b.summary(T0 + mins(30))
        self.assertEqual((s["today"], s["week"], s["month"], s["total"]), (1800, 1800, 4800, 4800))
        self.assertEqual(s["since"], "2026-09-01T20:00:00+00:00")
        self.assertEqual([(g["title_id"], g["sessions"], g["week"]) for g in s["games"]], [(GAME_D, 1, 1800), (GAME_E, 2, 0)])
        self.assertEqual([(x["title_id"], x["open"], x["playing"]) for x in s["sessions"]],
                         [(GAME_D, True, True), (GAME_E, False, False)])
        json.dumps(s)


def replay_into_book(lines):
    sm, tracker, b = KlogStateMachine(), gs.GameSessionTracker(), book()
    sm.on_klog_connected(0.0)
    for hms, line in lines:
        sm.ingest(line)
        for kind, p in tracker.update(sm.snapshot(), at(hms), NAMES.get):
            if kind == "started":
                b.start(p["title_id"], p["name"], p["started"])
            elif not p["background"]:
                b.stop(p["ended"])
    return b


class CaptureTests(unittest.TestCase):
    def test_console_a_capture(self):
        b = replay_into_book(CAPTURE)
        self.assertEqual({tid: g["seconds"] for tid, g in b.games.items()}, {GAME_D: 676, GAME_E: 46, OTHER: 62})
        # Game D: dips + a rest under 30 min = one session; it ends when Game E starts. Same for Game E.
        self.assertEqual([(s["title_id"], s["seconds"], s["start"][11:19], s["end"][11:19]) for s in b.sessions],
                         [(GAME_D, 676, "00:10:00", "00:23:26"), (GAME_E, 46, "00:23:32", "00:27:38")])
        self.assertEqual((b.open["title_id"], b.open["seconds"]), (OTHER, 62))
        self.assertEqual(b.days, {"2026-10-15": {GAME_D: 676, GAME_E: 46, OTHER: 62}})  # 17:10–17:28 local


class Hass:
    def __init__(self):
        self.data = {"ps4_goldhen": {"E1": {}}}
        self.bus = types.SimpleNamespace(async_listen_once=lambda event, fn: LISTEN_ONCE.append(fn) or (lambda: None))


class Entry:
    entry_id = "E1"
    data = {"ps4_host": "192.0.2.121"}

    def __init__(self):
        self.unloads = []

    def async_on_unload(self, fn):
        self.unloads.append(fn)


class Connection:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result):
        self.results.append(result)

    def send_error(self, msg_id, code, message):
        self.errors.append((code, message))


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class GlueTests(unittest.TestCase):
    def setUp(self):
        Store.saved.clear()
        Store.delayed.clear()
        CONNECTED.clear()
        INTERVALS.clear()
        LISTEN_ONCE.clear()
        NOW[0] = T0
        self.hass, self.entry = Hass(), Entry()
        run(pt.async_setup(self.hass, self.entry))
        self.rt = pt.runtime(self.hass, "E1")
        self.updates = []
        _connect(self.hass, pt.signal_updated("E1"), lambda: self.updates.append(1))

    def started(self, tid, when):
        _send(self.hass, gs.signal_game_started("E1"), {"entry_id": "E1", "title_id": tid, "name": NAMES.get(tid),
                                                        "started": when})

    def left(self, tid, when, background=False):
        _send(self.hass, gs.signal_game_left("E1"), {"entry_id": "E1", "title_id": tid, "name": NAMES.get(tid),
                                                     "started": None, "ended": when, "reason": "home_screen",
                                                     "background": background})

    def tick(self, when):
        NOW[0] = when
        for fn in list(INTERVALS):
            fn(when)

    def test_signals_tick_and_save(self):
        self.started(GAME_D, T0)
        self.tick(T0 + mins(1))
        self.tick(T0 + mins(2))
        self.assertEqual(self.rt.book.session_seconds(), 120)
        self.assertTrue(self.updates)
        fn, delay = Store.delayed["ps4_goldhen.play_time.E1"]
        self.assertLess(delay, pt.TICK.total_seconds())  # lands before the next tick restarts the timer
        Store.flush()
        self.assertEqual(Store.saved["ps4_goldhen.play_time.E1"]["open"]["seconds"], 120)

    def test_background_left_adds_nothing(self):
        self.started(GAME_D, T0)
        self.left(GAME_D, T0 + mins(5))
        self.left(GAME_D, T0 + mins(9), background=True)  # rest from the home screen
        self.assertEqual(self.rt.book.session_seconds(), 300)

    def test_tick_expires_and_saves_soon(self):
        self.started(GAME_D, T0)
        self.left(GAME_D, T0 + mins(5))
        self.tick(T0 + mins(36))
        self.assertIsNone(self.rt.book.open)
        self.assertEqual(Store.delayed["ps4_goldhen.play_time.E1"][1], 1)

    def test_midnight_tick_updates_the_sensor_without_play(self):
        n = len(self.updates)
        self.tick(datetime(2026, 10, 17, 7, 0, 30, tzinfo=timezone.utc))  # 00:00:30 local
        self.assertEqual(len(self.updates), n + 1)

    def test_unload_writes_before_the_reload_reads(self):
        self.started(GAME_D, T0)
        NOW[0] = T0 + mins(1) + timedelta(seconds=30)
        run(pt.async_unload(self.hass, self.entry))
        self.assertEqual(Store.saved["ps4_goldhen.play_time.E1"]["open"]["seconds"], 90)

    def test_stop_counts_up_to_now(self):
        self.started(GAME_D, T0)
        NOW[0] = T0 + timedelta(seconds=45)
        LISTEN_ONCE[-1](None)
        Store.flush()
        self.assertEqual(Store.saved["ps4_goldhen.play_time.E1"]["open"]["seconds"], 45)

    def test_setup_closes_a_stale_session(self):
        self.started(GAME_D, T0)
        self.tick(T0 + mins(10))
        Store.flush()
        for fn in self.entry.unloads:
            fn()
        NOW[0] = T0 + timedelta(hours=2)
        hass, entry = Hass(), Entry()
        run(pt.async_setup(hass, entry))
        self.assertIsNone(pt.runtime(hass, "E1").book.open)
        self.assertEqual(pt.runtime(hass, "E1").book.sessions[0]["seconds"], 600)

    def test_ws_summary(self):
        self.started(GAME_D, T0)
        NOW[0] = T0 + mins(3)
        conn = Connection()
        run(pt._ws_play_time(self.hass, conn, {"id": 1, "entry_id": "E1"}))
        self.assertEqual(conn.results[0]["today"], 180)
        self.assertEqual(conn.results[0]["sessions"][0]["playing"], True)
        run(pt._ws_play_time(self.hass, conn, {"id": 2, "entry_id": "nope"}))
        self.assertEqual(conn.errors, [("play_time_error", "Unknown PS4")])

    def test_ws_registered_once(self):
        n = len(COMMANDS)
        second = Entry()
        second.entry_id = "E2"
        self.hass.data["ps4_goldhen"]["E2"] = {}
        run(pt.async_setup(self.hass, second))  # the second console: same HA, no second registration
        self.assertEqual(len(COMMANDS), n)
        self.assertEqual(pt._ws_play_time.schema["type"], "ps4_goldhen/play_time")

    def test_remove(self):
        Store.saved["ps4_goldhen.play_time.E1"] = {"games": {}}
        run(pt.async_remove(self.hass, self.entry))
        self.assertNotIn("ps4_goldhen.play_time.E1", Store.saved)

    def test_sensors(self):
        today = sensor_mod.PS4PlayTimeTodaySensor(self.entry)
        session = sensor_mod.PS4CurrentSessionSensor(self.entry)
        for ent in (today, session):
            ent.hass = self.hass
        self.assertEqual((today.native_value, session.native_value), (0, 0))
        self.assertEqual(session.extra_state_attributes, {"title_id": None, "game": None, "started": None,
                                                          "playing": False})
        self.started(GAME_D, T0)
        self.tick(T0 + mins(1) + timedelta(seconds=30))
        self.assertEqual((today.native_value, session.native_value), (0.025, 0.025))  # hours (1.5 min)
        self.assertEqual(session.extra_state_attributes, {"title_id": GAME_D, "game": NAMES[GAME_D],
                                                          "started": T0.isoformat(), "playing": True})
        self.left(GAME_D, T0 + mins(2))
        self.assertFalse(session.extra_state_attributes["playing"])
        self.assertEqual(today._attr_unique_id, "ps4_goldhen_192.0.2.121_play_time_today")
        self.assertEqual(today._attr_state_class, "TOTAL_INCREASING")
        self.assertIsNone(getattr(session, "_attr_state_class", None))
        other = sensor_mod.PS4PlayTimeTodaySensor(types.SimpleNamespace(entry_id="E9", data={"ps4_host": "x"}))
        other.hass = self.hass
        self.assertIsNone(other.native_value)

    def test_sensors_in_hours_with_no_display_precision(self):  # HA's frontend then writes "3h 7m"
        week = sensor_mod.PS4PlayTimeLast7DaysSensor(self.entry)
        month = sensor_mod.PS4PlayTimeLast30DaysSensor(self.entry)
        today = sensor_mod.PS4PlayTimeTodaySensor(self.entry)
        session = sensor_mod.PS4CurrentSessionSensor(self.entry)
        for ent in (week, month, today, session):
            ent.hass = self.hass
            self.assertEqual(ent._attr_native_unit_of_measurement, "HOURS")  # the stub names enum members
            self.assertIsNone(getattr(ent, "_attr_suggested_display_precision", None))
        self.assertEqual([e._attr_unique_id for e in (week, month)],
                         ["ps4_goldhen_192.0.2.121_play_time_7_days", "ps4_goldhen_192.0.2.121_play_time_30_days"])
        self.assertIsNone(getattr(week, "_attr_state_class", None))  # statistics come from Play time today only
        b = pt.runtime(self.hass, "E1").book
        today_iso = b.local_day(T0).isoformat()
        b.days.update({today_iso: {GAME_D: 3600.0}, (b.local_day(T0) - timedelta(days=6)).isoformat(): {GAME_E: 7200.0},
                       (b.local_day(T0) - timedelta(days=7)).isoformat(): {GAME_E: 1800.0},
                       (b.local_day(T0) - timedelta(days=29)).isoformat(): {GAME_D: 900.0},
                       (b.local_day(T0) - timedelta(days=30)).isoformat(): {GAME_D: 99999.0}})
        self.assertEqual((today.native_value, week.native_value, month.native_value), (1.0, 3.0, 3.75))  # NOW = T0
        self.assertEqual(b.last_days_seconds(T0, 1), b.today_seconds(T0))
        self.assertEqual(sensor_mod._hours(11244), 3.1233)  # a 12.00 Pro's 187.4 min


if __name__ == "__main__":
    unittest.main()
