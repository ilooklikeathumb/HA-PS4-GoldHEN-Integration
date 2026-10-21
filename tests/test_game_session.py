"""Which game is being played → game_started / game_left signals (play time + save backup listen).

The klog lines are verbatim from a 12.00 Pro
and fed through the real KlogStateMachine.
"""
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_game_session_test_pkg"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_klog_power_state import KlogStateMachine, WAKE  # noqa: E402

_SAVED: dict = {}
SENT: list = []


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    _SAVED.setdefault(name, sys.modules.get(name))
    sys.modules[name] = mod
    return mod


NOW = [datetime(2026, 10, 16, 0, 0, tzinfo=timezone.utc)]
_module("homeassistant")
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda hass, sig, payload: SENT.append((sig, payload)))
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: NOW[0])
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
gs = importlib.import_module(f"{PKG}.game_session")
for _name, _old in _SAVED.items():
    if _old is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _old

GAME_D, GAME_E, OTHER = "CUSA90005", "CUSA90006", "CUSA90003"
NAMES = {GAME_D: "GAME D", GAME_E: "Game E Plus"}

# (UTC time, line) — the capture, in order. Wake + Game D at 00:10 added so a game is in focus when it starts.
CAPTURE = [
    ("00:10:00", WAKE[2]),
    ("00:10:00", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90005]"),
    ("00:18:52", "<118>[SL] AppFocusChanged [CUSA90005] -> [NPXS20001]"),
    ("00:18:55", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90005]"),
    ("00:19:22", "<118>[SceSystemStateMgr] State Change: MAIN_ON_STANDBY"),
    ("00:19:28", "<118>[SL] AppFocusChanged [CUSA90005] -> [invalid_title]"),
    ("00:19:35", "<118>[SL] AppFocusChanged [invalid_title] -> [NPXS20001]"),
    ("00:19:45", "<118>[SceSystemStateMgr] State Change: SUSPEND_ON_GOING"),
    ("00:21:22", "<118>[SceSystemStateMgr] State Change: WORKING"),
    ("00:21:23", "<118>[SceLncService] launchApp(CUSA90005)"),
    ("00:21:23", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90005]"),
    ("00:23:12", "<118>[SL] AppFocusChanged [CUSA90005] -> [NPXS20001]"),
    ("00:23:17", "<118>[SceLncService] launchApp(CUSA90005)"),
    ("00:23:18", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90005]"),
    ("00:23:26", "<118>[SL] AppFocusChanged [CUSA90005] -> [NPXS20001]"),
    ("00:23:26", "<118>[BGFT] [641] GameStopped(CUSA90005, 2) start"),
    ("00:23:26", "<118>[BGFT] [643] GameStopped(CUSA90005, 2) end"),
    ("00:23:27", "<118>[SceLncService] launchApp(CUSA90006)"),
    ("00:23:32", "<118>[BGFT] [606] GameWillStart(CUSA90006, 2) start"),
    ("00:23:33", "<118>[BGFT] [608] GameWillStart(CUSA90006, 2) end"),
    ("00:23:34", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90006]"),
    ("00:24:16", "<118>[SL] AppFocusChanged [CUSA90006] -> [NPXS20001]"),
    ("00:24:21", "<118>[SceSystemStateMgr] State Change: MAIN_ON_STANDBY"),
    ("00:24:30", "<118>[SL] AppFocusChanged [NPXS20001] -> [NPXS20001]"),
    ("00:24:34", "<118>[SceSystemStateMgr] State Change: SUSPEND_ON_GOING"),
    ("00:27:36", "<118>[SceSystemStateMgr] State Change: WORKING"),
    ("00:27:36", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90006]"),
    ("00:27:38", "<118>[SL] AppFocusChanged [CUSA90006] -> [NPXS20001]"),
    ("00:27:39", "<118>[BGFT] [641] GameStopped(CUSA90006, 2) start"),
    ("00:27:39", "<118>[BGFT] [643] GameStopped(CUSA90006, 2) end"),
    ("00:27:39", "<118>[SceLncService] launchApp(CUSA90003)"),
    ("00:27:43", "<118>[BGFT] [606] GameWillStart(CUSA90003, 2) start"),
    ("00:27:45", "<118>[BGFT] [608] GameWillStart(CUSA90003, 2) end"),
    ("00:27:49", "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90003]"),
    ("00:28:45", "<118>[SL] AppFocusChanged [CUSA90003] -> [NPXS20001]"),
    ("00:28:46", "<118>[BGFT] [641] GameStopped(CUSA90003, 2) start"),
    ("00:28:46", "<118>[BGFT] [643] GameStopped(CUSA90003, 2) end"),
]


def at(hms: str) -> datetime:
    h, m, s = (int(x) for x in hms.split(":"))
    return datetime(2026, 10, 16, h, m, s, tzinfo=timezone.utc)


def replay(lines, per_second=False):
    """Klog lines → state machine → tracker. per_second: lines of the same second arrive in one read (one update)."""
    sm, tracker, events = KlogStateMachine(), gs.GameSessionTracker(), []
    sm.on_klog_connected(0.0)
    groups: list[tuple[str, list[str]]] = []
    for hms, line in lines:
        if per_second and groups and groups[-1][0] == hms:
            groups[-1][1].append(line)
        else:
            groups.append((hms, [line]))
    for hms, chunk in groups:
        for line in chunk:
            sm.ingest(line)
        for kind, payload in tracker.update(sm.snapshot(), at(hms), NAMES.get):
            events.append((hms, kind, payload["title_id"], payload.get("reason"), payload.get("background")))
    return sm, tracker, events


class SuspendedTitleTests(unittest.TestCase):
    def test_ps_button_leaves_the_game_suspended(self):
        sm, _, _ = replay(CAPTURE[:3])
        self.assertIsNone(sm.current_title_id)
        self.assertEqual(sm.snapshot()["suspended_title_id"], GAME_D)

    def test_rest_keeps_it_and_resuming_clears_it(self):
        sm, _, _ = replay(CAPTURE[:8])  # through SUSPEND_ON_GOING
        self.assertEqual(sm.suspended_title_id, GAME_D)
        sm.ingest(CAPTURE[8][1])  # WORKING
        self.assertEqual(sm.suspended_title_id, GAME_D)
        sm.ingest(CAPTURE[10][1])  # Game D back in focus
        self.assertIsNone(sm.suspended_title_id)

    def test_game_stopped_clears_it(self):
        sm, _, _ = replay(CAPTURE[:15])
        self.assertEqual(sm.suspended_title_id, GAME_D)
        self.assertTrue(sm.ingest(CAPTURE[15][1]))  # GameStopped(Game D) start: a change, so HA gets an update
        self.assertIsNone(sm.suspended_title_id)
        self.assertFalse(sm.ingest(CAPTURE[16][1]))  # … end: nothing left to clear

    def test_game_stopped_of_another_title_keeps_it(self):
        sm, _, _ = replay(CAPTURE[:3])
        self.assertFalse(sm.ingest("<118>[BGFT] [641] GameStopped(CUSA90006, 2) start"))
        self.assertEqual(sm.suspended_title_id, GAME_D)

    def test_game_close_line_closes_the_background_game(self):
        sm, _, _ = replay(CAPTURE[:3])
        self.assertTrue(sm.ingest("<118>[SceShellCore] Game Close detected"))
        self.assertIsNone(sm.suspended_title_id)

    def test_close_while_in_focus_is_not_suspended(self):
        sm, _, _ = replay(CAPTURE[:2])
        sm.ingest("<118>[SceShellCore] Game Close detected")
        self.assertIsNone(sm.current_title_id)
        self.assertIsNone(sm.suspended_title_id)

    def test_another_game_replaces_it(self):
        sm, _, _ = replay(CAPTURE[:15])
        sm.ingest(CAPTURE[18][1])  # GameWillStart(Game E), as if GameStopped(Game D) was missed
        self.assertEqual(sm.current_title_id, GAME_E)
        self.assertIsNone(sm.suspended_title_id)

    def test_shutdown_clears_it(self):
        sm, _, _ = replay(CAPTURE[:3])
        sm.ingest("<118>[SceSystemStateMgr] State Change: SHUTDOWN_ON_GOING")
        self.assertIsNone(sm.suspended_title_id)

    def test_rest_straight_from_the_game_suspends_it(self):
        sm, _, _ = replay(CAPTURE[:2] + [CAPTURE[4]])
        self.assertEqual(sm.power_state, "rest")
        self.assertEqual(sm.suspended_title_id, GAME_D)


class CaptureReplayTests(unittest.TestCase):
    EXPECTED = [
        ("00:10:00", "started", GAME_D, None, None),
        ("00:18:52", "left", GAME_D, "home_screen", False),
        ("00:18:55", "started", GAME_D, None, None),
        ("00:19:22", "left", GAME_D, "rest", False),
        ("00:21:23", "started", GAME_D, None, None),
        ("00:23:12", "left", GAME_D, "home_screen", False),
        ("00:23:18", "started", GAME_D, None, None),
        ("00:23:26", "left", GAME_D, "home_screen", False),
        ("00:23:26", "left", GAME_D, "closed", True),
        ("00:23:32", "started", GAME_E, None, None),
        ("00:24:16", "left", GAME_E, "home_screen", False),
        ("00:24:21", "left", GAME_E, "rest", True),
        ("00:27:36", "started", GAME_E, None, None),
        ("00:27:38", "left", GAME_E, "home_screen", False),
        ("00:27:39", "left", GAME_E, "closed", True),
        ("00:27:43", "started", OTHER, None, None),
        ("00:28:45", "left", OTHER, "home_screen", False),
        ("00:28:46", "left", OTHER, "closed", True),
    ]

    def test_line_by_line(self):
        _, _, events = replay(CAPTURE)
        self.assertEqual(events, self.EXPECTED)

    def test_same_second_lines_in_one_read(self):
        """Focus → home and GameStopped in one read: one left, reason closed (foreground)."""
        _, _, events = replay(CAPTURE, per_second=True)
        self.assertIn(("00:23:26", "left", GAME_D, "closed", False), events)
        self.assertNotIn(("00:23:26", "left", GAME_D, "home_screen", False), events)
        self.assertEqual(len([e for e in events if e[2] == GAME_D and e[1] == "left"]), 4)

    def test_payload_times_and_names(self):
        tracker = gs.GameSessionTracker()
        sm = KlogStateMachine()
        sm.on_klog_connected(0.0)
        out = []
        for hms, line in CAPTURE[:3]:
            sm.ingest(line)
            out += tracker.update(sm.snapshot(), at(hms), NAMES.get)
        self.assertEqual(out[0], ("started", {"title_id": GAME_D, "name": NAMES[GAME_D], "started": at("00:10:00")}))
        self.assertEqual(out[1], ("left", {"title_id": GAME_D, "name": NAMES[GAME_D], "started": at("00:10:00"),
                                           "ended": at("00:18:52"), "reason": "home_screen", "background": False}))

    def test_background_rest_carries_the_last_focus_times(self):
        tracker = gs.GameSessionTracker()
        sm = KlogStateMachine()
        sm.on_klog_connected(0.0)
        out = []
        for hms, line in CAPTURE[:23]:  # through Game E → home, then MAIN_ON_STANDBY
            sm.ingest(line)
            out += tracker.update(sm.snapshot(), at(hms), NAMES.get)
        kind, payload = out[-1]
        self.assertEqual((kind, payload["reason"], payload["background"]), ("left", "rest", True))
        self.assertEqual((payload["started"], payload["ended"]), (at("00:23:32"), at("00:24:16")))


class TrackerRuleTests(unittest.TestCase):
    T0 = datetime(2026, 10, 16, 20, 0, tzinfo=timezone.utc)

    def data(self, **kw):
        return {"power_state": "on", "title_id": None, "klog_connected": True, "state_reason": "x",
                "suspended_title_id": None, **kw}

    def test_klog_drop_mid_game_is_klog_lost_then_started_again(self):
        t = gs.GameSessionTracker()
        t.update(self.data(title_id=GAME_D), self.T0, NAMES.get)
        out = t.update(self.data(title_id=GAME_D, klog_connected=False), self.T0 + timedelta(minutes=5), NAMES.get)
        self.assertEqual([(k, p["reason"]) for k, p in out], [("left", "klog_lost")])
        self.assertIsNone(t.segment)
        out = t.update(self.data(title_id=GAME_D), self.T0 + timedelta(minutes=6), NAMES.get)
        self.assertEqual([k for k, _ in out], ["started"])

    def test_switch_straight_to_another_game(self):
        t = gs.GameSessionTracker()
        t.update(self.data(title_id=GAME_D), self.T0, NAMES.get)
        out = t.update(self.data(title_id=GAME_E), self.T0 + timedelta(minutes=1), NAMES.get)
        self.assertEqual([(k, p["title_id"], p.get("reason")) for k, p in out],
                         [("left", GAME_D, "switched"), ("started", GAME_E, None)])

    def test_off(self):
        t = gs.GameSessionTracker()
        t.update(self.data(title_id=GAME_D), self.T0, NAMES.get)
        out = t.update(self.data(power_state="off"), self.T0 + timedelta(minutes=1), NAMES.get)
        self.assertEqual([(k, p["reason"], p["background"]) for k, p in out], [("left", "off", False)])

    def test_suspended_game_gone_with_shutdown(self):
        t = gs.GameSessionTracker()
        t.update(self.data(suspended_title_id=GAME_D), self.T0, NAMES.get)
        out = t.update(self.data(power_state="off"), self.T0 + timedelta(minutes=1), NAMES.get)
        self.assertEqual([(p["title_id"], p["reason"], p["background"]) for _, p in out], [(GAME_D, "off", True)])
        self.assertIsNone(out[0][1]["started"])  # HA never saw it in focus (e.g. after a restart)
        self.assertEqual(out[0][1]["name"], NAMES[GAME_D])

    def test_suspended_game_replaced_by_a_launch(self):
        t = gs.GameSessionTracker()
        t.update(self.data(suspended_title_id=GAME_D), self.T0, NAMES.get)
        out = t.update(self.data(title_id=GAME_E), self.T0 + timedelta(minutes=1), NAMES.get)
        self.assertEqual([(k, p["title_id"], p.get("reason"), p.get("background")) for k, p in out],
                         [("left", GAME_D, "switched", True), ("started", GAME_E, None, None)])

    def test_klog_drop_with_a_suspended_game_says_nothing(self):
        t = gs.GameSessionTracker()
        t.update(self.data(suspended_title_id=GAME_D), self.T0, NAMES.get)
        self.assertEqual(t.update(self.data(suspended_title_id=GAME_D, klog_connected=False), self.T0, NAMES.get), [])

    def test_unknown_power_with_a_game_counts(self):  # like Status: playing unless rest / off
        t = gs.GameSessionTracker()
        out = t.update(self.data(power_state=None, title_id=GAME_D), self.T0, NAMES.get)
        self.assertEqual([k for k, _ in out], ["started"])

    def test_no_klog_no_game(self):  # HA start: DDP may know the game before klog connects
        t = gs.GameSessionTracker()
        self.assertEqual(t.update(self.data(title_id=GAME_D, klog_connected=False), self.T0, NAMES.get), [])


class Hass:
    def __init__(self):
        self.data = {"ps4_goldhen": {"E1": {"game_map": {GAME_D: {"name": "Game D from app.db"}}}}}


class Entry:
    entry_id = "E1"

    def __init__(self):
        self.unloads = []

    def async_on_unload(self, fn):
        self.unloads.append(fn)


class Coordinator:
    def __init__(self):
        self.data, self.listeners = {}, []

    def async_add_listener(self, fn):
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn)

    def push(self, **data):
        self.data = {**self.data, **data}
        for fn in list(self.listeners):
            fn()


class GlueTests(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        self.hass, self.entry, self.coord = Hass(), Entry(), Coordinator()
        gs.async_setup(self.hass, self.entry, self.coord)

    def test_signals_per_console_with_entry_id_and_app_db_name(self):
        self.coord.push(power_state="on", title_id=GAME_D, klog_connected=True)
        sig, payload = SENT[-1]
        self.assertEqual(sig, "ps4_goldhen_game_started_E1")
        self.assertEqual((payload["entry_id"], payload["title_id"], payload["name"]), ("E1", GAME_D, "Game D from app.db"))
        self.assertEqual(gs.current(self.hass, "E1")["title_id"], GAME_D)
        self.coord.push(title_id=None, suspended_title_id=GAME_D)
        self.assertEqual(SENT[-1][0], "ps4_goldhen_game_left_E1")
        self.assertIsNone(gs.current(self.hass, "E1"))
        self.assertEqual(gs.suspended(self.hass, "E1"), GAME_D)

    def test_name_falls_back_to_klog_data(self):
        self.coord.push(power_state="on", title_id=GAME_E, game_name="Game E (klog)", klog_connected=True)
        self.assertEqual(SENT[-1][1]["name"], "Game E (klog)")

    def test_unload_sends_nothing(self):
        self.coord.push(power_state="on", title_id=GAME_D, klog_connected=True)
        n = len(SENT)
        for fn in self.entry.unloads:
            fn()
        self.coord.push(title_id=None)
        self.assertEqual(len(SENT), n)

    def test_unknown_console(self):
        self.assertIsNone(gs.current(self.hass, "nope"))
        self.assertIsNone(gs.suspended(self.hass, "nope"))


if __name__ == "__main__":
    unittest.main()
