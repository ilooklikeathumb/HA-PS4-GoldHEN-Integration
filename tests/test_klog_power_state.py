"""Console power state (on / rest / off) from SceSystemStateMgr klog lines.

Lines below are verbatim from a console's klog (PS4 Pro, FW 11.00, GoldHEN 2.4b18.12).
"""
import ast
from collections import deque
import logging
from pathlib import Path
import re
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components/ps4_goldhen/__init__.py"
CONST = ROOT / "custom_components/ps4_goldhen/const.py"


def load_state_machine():
    """Exec const.py, the module-level regexes/constants and KlogStateMachine from __init__.py — no HA needed."""
    ns: dict[str, Any] = {"re": re, "deque": deque, "Any": Any, "_LOGGER": logging.getLogger("klog-power-test")}
    exec(compile(CONST.read_text(), str(CONST), "exec"), ns)
    keep = []
    for node in ast.parse(INIT.read_text()).body:
        if isinstance(node, ast.Assign) and all(
            isinstance(t, ast.Name) and t.id.startswith(("_KLOG_", "_SYSTEM_STATE", "_REST_", "_HOME_SCREEN", "_TITLE_ID"))
            for t in node.targets
        ):
            keep.append(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "_is_real_game_title_id":
            keep.append(node)
        elif isinstance(node, ast.ClassDef) and node.name == "KlogStateMachine":
            keep.append(node)
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    return ns


NS = load_state_machine()
KlogStateMachine = NS["KlogStateMachine"]
GRACE = NS["_REST_HEARTBEAT_GRACE"]

WAKE = [
    "<118>[SceSystemStateMgr] Wake Up Request",
    "<118>[SceSystemStateMgr] Trigger Code: REMOTE_PLAY",
    "<118>[SceSystemStateMgr] State Change: WORKING",
]
GAME_FOCUS = "<118>[SL] AppFocusChanged [NPXS20001] -> [CUSA90007]"
ENTER_REST = [
    "<118>[SceSystemStateMgr] Enter Standby Mode Request from ShellUI",
    "<118>[SceSystemStateMgr] Keeping MAIN_ON_STANDBY (BGFT 1, SaveData 27, Update 0, BgDailyChecker 0, EventService 0, PatchChecker 0, vshUpdate 0, appInstaller 0)",
    "<118>[SceSystemStateMgr] USB Charge: on (no timeout)",
    "<118>[SceSystemStateMgr] State Change: MAIN_ON_STANDBY",
    "<118>[SceSystemStateMgr] Trigger Code: MISC",
]
SHELLUI_RELAUNCH_IN_REST = "<118>[SL] AppFocusChanged [NPXS20001] -> [NPXS20001]"
KEEPING = "<118>[SceSystemStateMgr] Keeping MAIN_ON_STANDBY (BGFT 1, SaveData 0, Update 0, BgDailyChecker 0, EventService 0, PatchChecker 0, vshUpdate 0, appInstaller 0)"
IDLE = "<118>[SceSystemStateMgr] No user input for 60 seconds"
DEEP_REST = "<118>[SceSystemStateMgr] State Change: SUSPEND_ON_GOING"
POWER_MODE = "<118>[SceSystemStateMgr] Power Mode Change: NAVIGATION"
SHUTDOWN = "<118>[SceSystemStateMgr] State Change: SHUTDOWN_ON_GOING"


def feed(sm, lines):
    for line in lines:
        sm.ingest(line)


class PowerStateTests(unittest.TestCase):
    def setUp(self):
        self.sm = KlogStateMachine()
        self.sm.klog_connected = True
        self.sm.on_klog_connected(0.0)

    def test_wake_then_game_then_rest_clears_game(self):
        feed(self.sm, WAKE + [GAME_FOCUS])
        self.assertEqual(self.sm.power_state, "on")
        self.assertEqual(self.sm.power_trigger, "REMOTE_PLAY")
        self.assertEqual(self.sm.current_title_id, "CUSA90007")
        feed(self.sm, ENTER_REST)
        snap = self.sm.snapshot()
        self.assertEqual(snap["power_state"], "rest")
        self.assertIsNone(snap["title_id"])
        self.assertEqual(snap["power_trigger"], "MISC")

    def test_shellui_relaunch_in_rest_does_not_flip_to_on(self):
        feed(self.sm, ENTER_REST + [SHELLUI_RELAUNCH_IN_REST, IDLE, KEEPING])
        self.assertEqual(self.sm.power_state, "rest")
        self.assertIsNone(self.sm.current_title_id)

    def test_idle_timer_is_not_a_power_signal(self):
        feed(self.sm, WAKE + [IDLE])
        self.assertEqual(self.sm.power_state, "on")
        feed(self.sm, ENTER_REST + [IDLE])
        self.assertEqual(self.sm.power_state, "rest")

    def test_deep_rest_stays_rest_after_klog_drops(self):
        feed(self.sm, ENTER_REST + [DEEP_REST])
        self.sm.klog_connected = False  # socket reset by the PS4 ~40 s later
        self.assertFalse(self.sm.tick(1000.0))
        self.assertEqual(self.sm.power_state, "rest")

    def test_keeping_line_alone_means_rest(self):  # HA (re)connects mid-rest
        self.assertTrue(self.sm.ingest(KEEPING))
        self.assertEqual(self.sm.power_state, "rest")
        self.assertFalse(self.sm.ingest(KEEPING))  # repeat every 60 s: no change event

    def test_power_mode_change_means_on(self):
        feed(self.sm, ENTER_REST)
        feed(self.sm, [POWER_MODE])
        self.assertEqual(self.sm.power_state, "on")

    def test_game_focus_means_on_even_if_working_line_missed(self):
        feed(self.sm, ENTER_REST)
        self.assertTrue(self.sm.ingest(GAME_FOCUS))
        self.assertEqual(self.sm.power_state, "on")
        self.assertEqual(self.sm.current_title_id, "CUSA90007")

    def test_shutdown_means_off(self):
        feed(self.sm, WAKE + [GAME_FOCUS, SHUTDOWN])
        self.assertEqual(self.sm.power_state, "off")
        self.assertIsNone(self.sm.current_title_id)

    def test_live_shutdown_stays_off(self):  # the 90 s rule must not flip a live-seen OFF back to on
        feed(self.sm, WAKE + [GAME_FOCUS, SHUTDOWN, "<118>[SceSystemStateMgr] Trigger Code: MISC"])
        self.assertFalse(self.sm.tick(10 * GRACE))
        self.assertEqual(self.sm.power_state, "off")

    def test_unknown_state_name_ignored(self):
        feed(self.sm, WAKE)
        self.assertFalse(self.sm.ingest("<118>[SceSystemStateMgr] State Change: INITIALIZING"))
        self.assertEqual(self.sm.power_state, "on")


class ChooseUserScreenTests(unittest.TestCase):
    """Lines verbatim from a 12.00 Pro's klog: HA wake → Remote Play login."""

    WELCOME = [
        "<118>[SceSystemStateMgr] State Change: WORKING",
        "<118>[SceLncService] Num. of logged-in users is 0",
        "<118>[AutoLogin] Start",
        "<118>[AutoLogin] End(2)",
        "<118>[Theme/I] : OnLogin : ffffffff",
        "<118>[UserSelectScene] CurrentSceneState : NoDevice",
    ]
    RP_LOGIN = "<118>[Theme/I] : OnLogin : 10000001"
    LAUNCH_COUNT = "<118>[SceLncService] Num. of logged-in users is 1"

    def setUp(self):
        self.sm = KlogStateMachine()
        self.sm.klog_connected = True
        self.sm.on_klog_connected(0.0)

    def test_wake_lands_on_choose_user_screen(self):
        feed(self.sm, ENTER_REST + self.WELCOME)
        snap = self.sm.snapshot()
        self.assertEqual(snap["power_state"], "on")
        self.assertTrue(snap["no_user_logged_in"])

    def test_login_clears_it(self):
        feed(self.sm, self.WELCOME)
        self.assertTrue(self.sm.ingest(self.RP_LOGIN))  # changed → coordinator update
        self.assertFalse(self.sm.no_user_logged_in)

    def test_logged_in_count_clears_it(self):
        feed(self.sm, self.WELCOME + [self.LAUNCH_COUNT])
        self.assertFalse(self.sm.no_user_logged_in)

    def test_nobody_onlogin_is_not_a_login(self):
        feed(self.sm, self.WELCOME[:2])
        self.assertFalse(self.sm.ingest("<118>[Theme/I] : OnLogin : ffffffff"))
        self.assertTrue(self.sm.no_user_logged_in)

    def test_rest_resets_it(self):
        feed(self.sm, self.WELCOME + ENTER_REST)
        self.assertFalse(self.sm.no_user_logged_in)


class HeartbeatGraceTests(unittest.TestCase):
    """Wake from deep rest: 'State Change: WORKING' can be emitted before klog reconnects."""

    def setUp(self):
        self.sm = KlogStateMachine()
        feed(self.sm, ENTER_REST + [DEEP_REST])
        self.sm.klog_connected = False

    def reconnect(self, at):
        self.sm.klog_connected = True
        self.sm.on_klog_connected(at)

    def test_reconnect_without_rest_heartbeat_becomes_on(self):
        self.reconnect(100.0)
        self.assertFalse(self.sm.tick(100.0 + GRACE - 1))
        self.assertEqual(self.sm.power_state, "rest")
        self.assertTrue(self.sm.tick(100.0 + GRACE))
        self.assertEqual(self.sm.power_state, "on")
        self.assertEqual(self.sm.last_reason, "klog_up_no_rest_heartbeat")

    def test_reconnect_into_main_on_standby_stays_rest(self):  # maintenance wake / download in rest
        self.reconnect(100.0)
        self.sm.ingest(KEEPING)
        self.assertFalse(self.sm.tick(100.0 + 10 * GRACE))
        self.assertEqual(self.sm.power_state, "rest")

    def test_heartbeat_before_reconnect_does_not_count(self):
        self.sm.ingest(KEEPING)
        self.reconnect(100.0)
        self.assertTrue(self.sm.tick(100.0 + GRACE))

    def test_grace_covers_the_60s_heartbeat(self):
        self.assertGreater(GRACE, 60)


class UnreachableTests(unittest.TestCase):
    """Fully off = no ARP reply → connect fails with EHOSTUNREACH. Rest still answers and refuses (111)."""

    def test_unreachable_means_off(self):
        sm = KlogStateMachine()
        feed(sm, WAKE + [GAME_FOCUS])
        sm.klog_connected = False
        self.assertTrue(sm.on_klog_unreachable())
        self.assertEqual(sm.power_state, "off")
        self.assertIsNone(sm.current_title_id)
        self.assertFalse(sm.on_klog_unreachable())  # retried every 10 s: no change event

    def test_cold_boot_after_off_becomes_on(self):  # power on + GoldHEN loaded → klog up, no rest heartbeat
        sm = KlogStateMachine()
        sm.on_klog_unreachable()
        sm.klog_connected = True
        sm.on_klog_connected(100.0)
        self.assertTrue(sm.tick(100.0 + GRACE))
        self.assertEqual(sm.power_state, "on")


class StaleKlogTests(unittest.TestCase):
    """DDP said on while klog still said rest (woke from deep rest without HA)."""

    def test_ddp_on_after_deep_rest_means_on(self):
        sm = KlogStateMachine()
        feed(sm, WAKE + [GAME_FOCUS] + ENTER_REST + [DEEP_REST])
        self.assertTrue(sm.on_ddp_on())
        self.assertEqual(sm.power_state, "on")
        self.assertEqual(sm.last_reason, "ddp_on_klog_stale")
        self.assertIsNone(sm.current_title_id)  # the game comes back with klog's next AppFocusChanged
        self.assertFalse(sm.on_ddp_on())  # already on: no change event

    def test_rest_after_reconnect_still_wins(self):
        sm = KlogStateMachine()
        feed(sm, ENTER_REST)
        sm.on_ddp_on()
        sm.on_klog_connected(100.0)
        feed(sm, [KEEPING])
        self.assertEqual(sm.power_state, "rest")


class DdpStatusTests(unittest.TestCase):
    """Power + running game from DDP at setup / klog connect (title lost after an HA restart)."""

    def test_restart_mid_game_gets_the_title(self):
        sm = KlogStateMachine()
        sm.on_klog_connected(100.0)
        self.assertTrue(sm.on_ddp_status(200, "CUSA90005"))
        self.assertEqual((sm.power_state, sm.current_title_id), ("on", "CUSA90005"))
        self.assertEqual(sm.last_reason, "ddp_running_app")

    def test_home_screen_only_power(self):
        sm = KlogStateMachine()
        self.assertTrue(sm.on_ddp_status(200, None))
        self.assertEqual((sm.power_state, sm.current_title_id), ("on", None))
        self.assertEqual(sm.last_reason, "ddp_on_at_connect")
        self.assertFalse(sm.on_ddp_status(200, None))  # nothing new: no update event
        self.assertFalse(sm.on_ddp_status(200, "NPXS20001"))  # a system app isn't a game

    def test_klog_title_wins(self):
        sm = KlogStateMachine()
        feed(sm, WAKE + [GAME_FOCUS])
        self.assertFalse(sm.on_ddp_status(200, "CUSA90005"))
        self.assertEqual(sm.current_title_id, "CUSA90007")

    def test_rest_ignored(self):
        sm = KlogStateMachine()
        sm.restore_power_state("rest")
        self.assertFalse(sm.on_ddp_status(620, None))
        self.assertEqual(sm.power_state, "rest")

    def test_rest_seen_on_this_connection_wins(self):
        sm = KlogStateMachine()
        sm.on_klog_connected(100.0)
        feed(sm, [KEEPING])
        self.assertFalse(sm.on_ddp_status(200, "CUSA90005"))  # a stale reply racing the rest line
        self.assertEqual((sm.power_state, sm.current_title_id), ("rest", None))

    def test_restored_rest_overridden_by_ddp_on(self):
        # HA restarted during rest, then a controller woke the console before klog reconnected
        sm = KlogStateMachine()
        sm.restore_power_state("rest")
        sm.on_klog_connected(100.0)
        self.assertTrue(sm.on_ddp_status(200, "CUSA90006"))
        self.assertEqual((sm.power_state, sm.current_title_id), ("on", "CUSA90006"))


class RestoreTests(unittest.TestCase):
    def test_restore_rest_only_when_unknown(self):
        sm = KlogStateMachine()
        sm.restore_power_state("rest")
        self.assertEqual(sm.power_state, "rest")
        sm2 = KlogStateMachine()
        feed(sm2, WAKE)
        sm2.restore_power_state("rest")
        self.assertEqual(sm2.power_state, "on")

    def test_restore_ignores_none_and_on(self):
        sm = KlogStateMachine()
        sm.restore_power_state(None)
        sm.restore_power_state("on")
        self.assertIsNone(sm.power_state)


if __name__ == "__main__":
    unittest.main()
