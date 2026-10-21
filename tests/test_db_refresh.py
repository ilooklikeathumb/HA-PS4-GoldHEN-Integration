"""The hourly app.db refresh skips the FTP pull while the console is in rest / off.

Runs the real _db_refresh_task from __init__.py (extracted, like test_klog_power_state) against stand-ins.
"""
import ast
import asyncio
import logging
from pathlib import Path
import types
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components/ps4_goldhen/__init__.py"
CONST = ROOT / "custom_components/ps4_goldhen/const.py"


class _StopLoop(Exception):
    pass


def load_task(db, on_short_sleep=None):
    """_db_refresh_task with its own asyncio stand-in. The hourly sleep stops the loop; the start-up wait's short
    sleeps return at once (on_short_sleep runs first, so a test can make the power state arrive mid-wait)."""
    ns: dict[str, Any] = {"_LOGGER": logging.getLogger("db-refresh-test"), "ps4_db": db,
                          "HomeAssistant": object, "DataUpdateCoordinator": object, "short_sleeps": []}
    exec(compile(CONST.read_text(), str(CONST), "exec"), ns)

    async def sleep(delay):
        if delay == ns["DB_REFRESH_INTERVAL"]:
            raise _StopLoop
        ns["short_sleeps"].append(delay)
        if on_short_sleep:
            on_short_sleep()

    ns["asyncio"] = types.SimpleNamespace(sleep=sleep, CancelledError=asyncio.CancelledError)

    async def run(hass, data, func, *args, wait=None):  # ftp_session.run without the lock (test_ftp_session.py has it)
        ns["locked_runs"].append(func)
        return await hass.async_add_executor_job(func, *args)

    ns["locked_runs"] = []
    ns["ftp_session"] = types.SimpleNamespace(run=run)
    keep = [node for node in ast.parse(INIT.read_text()).body
            if (isinstance(node, ast.AsyncFunctionDef) and node.name == "_db_refresh_task")
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_DB_POWER_WAIT" for t in node.targets))]
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    return ns


class FakeDb:
    def __init__(self, cached=None):
        self.cached = cached
        self.pulls = []
        self.cache_reads = []

    def download_and_parse(self, host, port):
        self.pulls.append((host, port))
        return {"CUSA00001": {"name": "Pulled"}}

    def load_cached(self, host):
        self.cache_reads.append(host)
        return self.cached


class FakeHass:
    def __init__(self, entry_data):
        self.data = {"ps4_goldhen": {"e1": entry_data}}

    async def async_add_executor_job(self, func, *args):
        return func(*args)


class FakeCoordinator:
    data = {}

    def async_set_updated_data(self, data):
        self.data = data


def run_one_round(power, game_map=None, cached=None, power_after_wait=None):
    """Start-up wait + one loop round; returns the fake db, the entry data and the task's namespace."""
    db = FakeDb(cached)
    machine = types.SimpleNamespace(power_state=power)

    def arrive():
        if power_after_wait is not None:
            machine.power_state = power_after_wait

    ns = load_task(db, arrive)
    entry_data = {"host": "192.0.2.10", "ftp_port": 2121, "klog_data": {}, "klog_state_machine": machine}
    if game_map is not None:
        entry_data["game_map"] = game_map

    try:
        asyncio.run(ns["_db_refresh_task"](FakeHass(entry_data), "e1", FakeCoordinator()))
    except _StopLoop:
        pass
    else:
        raise AssertionError("the loop didn't reach its sleep")
    return db, entry_data, ns


CONSTS: dict[str, Any] = {}
exec(compile(CONST.read_text(), str(CONST), "exec"), CONSTS)
POWER_ON, POWER_REST, POWER_OFF = CONSTS["POWER_ON"], CONSTS["POWER_REST"], CONSTS["POWER_OFF"]


class DbRefreshTests(unittest.TestCase):
    def test_on_pulls(self):
        db, entry_data, ns = run_one_round(POWER_ON)
        self.assertEqual(db.pulls, [("192.0.2.10", 2121)])
        self.assertEqual(ns["locked_runs"], [db.download_and_parse], "the pull runs under the FTP lock")
        self.assertEqual(entry_data["game_map"], {"CUSA00001": {"name": "Pulled"}})

    def test_known_power_needs_no_wait(self):
        for power in (POWER_ON, POWER_REST):
            with self.subTest(power=power):
                _, _, ns = run_one_round(power)
                self.assertEqual(ns["short_sleeps"], [])

    def test_start_waits_for_rest_then_skips(self):  # HA start: the power state arrives a few seconds in
        db, _, ns = run_one_round(None, power_after_wait=POWER_REST)
        self.assertEqual(ns["short_sleeps"], [2])
        self.assertEqual(db.pulls, [])

    def test_power_never_known_pulls_after_the_wait(self):  # klog + DDP silent: pull anyway after the wait
        db, _, ns = run_one_round(None)
        self.assertEqual(sum(ns["short_sleeps"]), ns["_DB_POWER_WAIT"])
        self.assertEqual(len(db.pulls), 1)

    def test_rest_and_off_skip_the_pull(self):
        for power in (POWER_REST, POWER_OFF):
            with self.subTest(power=power):
                db, entry_data, _ = run_one_round(power, game_map={"CUSA00002": {"name": "Kept"}})
                self.assertEqual(db.pulls, [])
                self.assertEqual(db.cache_reads, [])  # a map is loaded already: nothing to do
                self.assertEqual(entry_data["game_map"], {"CUSA00002": {"name": "Kept"}})

    def test_skip_with_no_map_loads_the_disk_cache(self):
        db, entry_data, _ = run_one_round(POWER_REST, cached={"CUSA00003": {"name": "Cached"}})
        self.assertEqual(db.pulls, [])
        self.assertEqual(db.cache_reads, ["192.0.2.10"])
        self.assertEqual(entry_data["game_map"], {"CUSA00003": {"name": "Cached"}})

    def test_skip_with_no_cache_leaves_no_map(self):
        db, entry_data, _ = run_one_round(POWER_OFF)
        self.assertEqual(db.pulls, [])
        self.assertNotIn("game_map", entry_data)


if __name__ == "__main__":
    unittest.main()
