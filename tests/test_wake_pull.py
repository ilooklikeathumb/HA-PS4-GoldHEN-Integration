"""The app.db pull after a wake, and the cover sync after every pull.

A wake (rest / off → on) brings the next pull forward, but only once the console has been on with someone logged in
for _WAKE_SETTLE s; then the covers: changed ones fetched again, missing ones fetched in batches (one FTP session
each). Runs the real functions from __init__.py (extracted, like test_db_refresh) with real asyncio and short timings.
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

_NAMES = {"_watch_wake", "_settled_after_wake", "_wait_next_pull", "_async_sync_covers",
          "_WAKE_SETTLE", "_WAKE_CHECK", "_WAKE_PULL_COOLDOWN", "_COVERS_PER_SESSION"}

INTERVAL, SETTLE, CHECK, COOLDOWN = 0.6, 0.15, 0.01, 0.3  # stand-ins for 3600 / 90 / 5 / 600 s


def load(**fakes) -> dict[str, Any]:
    ns: dict[str, Any] = {"_LOGGER": logging.getLogger("wake-pull-test"), "Any": Any, "asyncio": asyncio,
                          "HomeAssistant": object, "DataUpdateCoordinator": object, "callback": lambda f: f}
    exec(compile(CONST.read_text(), str(CONST), "exec"), ns)
    keep = [node for node in ast.parse(INIT.read_text()).body
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _NAMES)
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") in _NAMES for t in node.targets))]
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    ns.update(DB_REFRESH_INTERVAL=INTERVAL, _WAKE_SETTLE=SETTLE, _WAKE_CHECK=CHECK, _WAKE_PULL_COOLDOWN=COOLDOWN)
    ns.update(fakes)
    return ns


NS = load()
POWER_ON, POWER_REST, POWER_OFF = NS["POWER_ON"], NS["POWER_REST"], NS["POWER_OFF"]


def entry(power=POWER_ON, no_user=False) -> dict[str, Any]:
    return {"host": "192.0.2.1", "ftp_port": 2121, "db_wake": asyncio.Event(),
            "klog_state_machine": types.SimpleNamespace(power_state=power, no_user_logged_in=no_user)}


class WatchWakeTests(unittest.TestCase):
    def test_rest_or_off_to_on_wakes_the_task(self):
        async def go():
            data = entry()
            coordinator = types.SimpleNamespace(data={})
            hass = types.SimpleNamespace(data={"ps4_goldhen": {"e1": data}}, loop=asyncio.get_running_loop())
            seen = []
            for power in (None, POWER_ON, POWER_ON, POWER_REST, POWER_ON, POWER_OFF, POWER_ON):
                coordinator.data = {"power_state": power}
                NS["_watch_wake"](hass, "e1", coordinator)
                seen.append(data["db_wake"].is_set())
                data["db_wake"].clear()
            return seen, data

        seen, data = asyncio.run(go())
        # HA start (unknown → on) and staying on aren't wakes; rest → on and off → on are
        self.assertEqual(seen, [False, False, False, False, True, False, True])
        self.assertIn("db_woke_at", data)

    def test_unloaded_entry_is_ignored(self):
        hass = types.SimpleNamespace(data={"ps4_goldhen": {}}, loop=None)
        NS["_watch_wake"](hass, "gone", types.SimpleNamespace(data={"power_state": POWER_ON}))


class WaitNextPullTests(unittest.TestCase):
    def wait(self, data, during=None):
        """Seconds _wait_next_pull took; `during(data)` runs alongside it (a wake, a login…)."""
        async def go():
            loop = asyncio.get_running_loop()
            start = loop.time()
            side = asyncio.ensure_future(during(data)) if during else None
            await NS["_wait_next_pull"](data)
            if side:
                side.cancel()
            return loop.time() - start
        return asyncio.run(go())

    @staticmethod
    def wake_after(delay, then=None):
        async def during(data):
            await asyncio.sleep(delay)
            data["db_woke_at"] = asyncio.get_running_loop().time()
            data["db_wake"].set()
            if then:
                await then(data)
        return during

    def test_no_wake_waits_the_interval(self):
        self.assertGreaterEqual(self.wait(entry()), INTERVAL)

    def test_wake_pulls_once_settled(self):
        took = self.wait(entry(), self.wake_after(0.05))
        self.assertGreaterEqual(took, 0.05 + SETTLE)
        self.assertLess(took, INTERVAL, "sooner than the hourly pull")

    def test_choose_user_screen_holds_it_until_someone_logs_in(self):
        async def log_in(data):
            await asyncio.sleep(0.2)
            data["klog_state_machine"].no_user_logged_in = False

        took = self.wait(entry(no_user=True), self.wake_after(0.02, log_in))
        self.assertGreaterEqual(took, 0.02 + 0.2 + SETTLE, "the settle time counts from the login")
        self.assertLess(took, INTERVAL)

    def test_back_to_rest_before_it_settles_waits_the_interval(self):
        async def rest(data):
            await asyncio.sleep(SETTLE / 3)
            data["klog_state_machine"].power_state = POWER_REST

        self.assertGreaterEqual(self.wait(entry(), self.wake_after(0.02, rest)), INTERVAL)

    def test_wake_soon_after_a_pull_waits_the_interval(self):
        async def pull_then_wake(data):
            data["db_last_pull"] = asyncio.get_running_loop().time()  # the cooldown counts from here
            await self.wake_after(0.02)(data)

        self.assertGreaterEqual(self.wait(entry(), pull_then_wake), INTERVAL)

    def test_hourly_pull_never_in_a_wakes_first_seconds(self):
        ns = load(DB_REFRESH_INTERVAL=0.02)

        async def go():
            data = entry(power=POWER_REST)  # the wake didn't settle (no early pull) but the hour is up
            loop = asyncio.get_running_loop()
            data["db_woke_at"] = loop.time()
            start = loop.time()
            await ns["_wait_next_pull"](data)
            return loop.time() - start

        self.assertGreaterEqual(asyncio.run(go()), SETTLE)


class FakeHass:
    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


class SyncCoversTests(unittest.TestCase):
    def setup(self, missing, listed=None, fail=None, on_run=None):
        runs = []

        async def run(hass, data, func, *args, wait=None):  # ftp_session.run (test_ftp_session.py has the lock)
            runs.append((func.__name__, args))
            if on_run:
                on_run(data, len(runs))
            if fail and func.__name__ == fail:
                raise OSError("refused")
            return 0

        def refresh_cached_covers(host, port, console):
            return 0

        def fetch_covers(host, port, console, tids):
            return len(tids)

        def missing_covers(console, tids):
            return [tid for tid in tids if tid in missing]

        titles = listed if listed is not None else missing
        ns = load(ftp_session=types.SimpleNamespace(run=run), _refresh_cached_covers=refresh_cached_covers,
                  _fetch_covers=fetch_covers, _missing_covers=missing_covers, _COVERS_PER_SESSION=2,
                  ps4_db=types.SimpleNamespace(title_list=lambda game_map: [(tid, tid) for tid in titles]))
        return ns, runs

    def sync(self, ns, data):
        asyncio.run(ns["_async_sync_covers"](FakeHass(), data, "E1", {}))

    def test_check_then_missing_covers_in_batches(self):
        ns, runs = self.setup(["T1", "T2", "T3", "T4", "T5"], listed=["T0", "T1", "T2", "T3", "T4", "T5"])
        self.sync(ns, entry())
        self.assertEqual([name for name, _ in runs],
                         ["refresh_cached_covers", "fetch_covers", "fetch_covers", "fetch_covers"])
        self.assertEqual(runs[0][1], ("192.0.2.1", 2121, "E1"))
        self.assertEqual([args[3] for name, args in runs[1:]], [["T1", "T2"], ["T3", "T4"], ["T5"]],
                         "one FTP session per batch, the FTP lock free in between")

    def test_nothing_missing_is_just_the_check(self):
        ns, runs = self.setup([], listed=["T0"])
        self.sync(ns, entry())
        self.assertEqual([name for name, _ in runs], ["refresh_cached_covers"])

    def test_console_asleep_stops_between_batches(self):
        def rest_after_first_batch(data, n):
            if n == 2:
                data["klog_state_machine"].power_state = POWER_REST

        ns, runs = self.setup(["T1", "T2", "T3", "T4"], on_run=rest_after_first_batch)
        self.sync(ns, entry())
        self.assertEqual([name for name, _ in runs], ["refresh_cached_covers", "fetch_covers"])

    def test_failure_is_a_debug_line(self):
        for fail in ("refresh_cached_covers", "fetch_covers"):
            with self.subTest(fail=fail):
                ns, runs = self.setup(["T1", "T2", "T3"], fail=fail)
                with self.assertNoLogs("wake-pull-test", level="WARNING"):
                    self.sync(ns, entry())
                self.assertEqual(runs[-1][0], fail, "nothing after the failure")


if __name__ == "__main__":
    unittest.main()
