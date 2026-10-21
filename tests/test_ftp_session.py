"""The console's FTP lock (ftp_session.py): one HA FTP session at a time, a bounded wait for panel actions, and a
cancelled caller keeps the lock until its FTP thread ends. No PS4 access required."""
import asyncio
import contextlib
import importlib.util
from pathlib import Path
import sys
import threading
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "_ps4_ftp_session_test"


def load():
    ha = ModuleType("homeassistant"); ha.__path__ = []
    core = ModuleType("homeassistant.core"); core.HomeAssistant = object
    package = ModuleType(PKG); package.__path__ = [str(COMP)]
    with patch.dict(sys.modules, {"homeassistant": ha, "homeassistant.core": core, PKG: package}):
        spec = importlib.util.spec_from_file_location(f"{PKG}.ftp_session", COMP / "ftp_session.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    return module


fs = load()


class Hass:
    def __init__(self, data=None):
        self.data = {"ps4_goldhen": data or {}}

    def async_add_executor_job(self, func, *args):
        return asyncio.get_running_loop().run_in_executor(None, func, *args)


class FTPLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sessions_run_one_after_the_other(self):
        data, order = {}, []
        first_may_end = threading.Event()

        def first():
            order.append("first starts")
            first_may_end.wait(5)
            order.append("first ends")

        def second():
            order.append("second starts")

        hass = Hass()
        one = asyncio.create_task(fs.run(hass, data, first))
        await asyncio.sleep(0.05)
        two = asyncio.create_task(fs.run(hass, data, second))
        await asyncio.sleep(0.05)
        self.assertEqual(order, ["first starts"], "the second waits for the lock")
        first_may_end.set()
        await asyncio.gather(one, two)
        self.assertEqual(order, ["first starts", "first ends", "second starts"])
        self.assertFalse(fs.lock(data).locked())

    async def test_panel_wait_gives_up_with_ftp_busy(self):
        data = {}
        await fs.lock(data).acquire()  # a save backup batch holds it
        ran = []
        with self.assertRaises(fs.FTPBusy):
            await fs.run(Hass(), data, lambda: ran.append(1), wait=0.05)
        self.assertEqual(ran, [], "nothing ran")
        self.assertTrue(fs.lock(data).locked(), "the holder still has it")
        fs.lock(data).release()

    async def test_no_wait_means_waiting_its_turn(self):
        data = {}
        await fs.lock(data).acquire()
        task = asyncio.create_task(fs.run(Hass(), data, lambda: "done"))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        fs.lock(data).release()
        self.assertEqual(await task, "done")

    async def test_cancelled_caller_keeps_the_lock_until_the_thread_ends(self):
        data = {}
        thread_may_end = threading.Event()
        task = asyncio.create_task(fs.run(Hass(), data, lambda: thread_may_end.wait(5)))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(fs.lock(data).locked(), "the FTP thread still runs: no new session beside it")
        thread_may_end.set()
        for _ in range(100):
            if not fs.lock(data).locked():
                break
            await asyncio.sleep(0.01)
        self.assertFalse(fs.lock(data).locked(), "released when the thread ended")

    async def test_failure_releases_the_lock(self):
        data = {}

        def refused():
            raise ConnectionRefusedError("FTP refused")

        with self.assertRaises(ConnectionRefusedError):
            await fs.run(Hass(), data, refused)
        self.assertFalse(fs.lock(data).locked())

    async def test_session_logs_in_and_runs_the_work_under_the_lock(self):
        data = {"host": "192.0.2.1", "ftp_port": 2121}
        hass = Hass({"entry": data})
        seen = []

        @contextlib.contextmanager
        def connect(host, port):
            seen.append(("connect", host, port, fs.lock(data).locked()))
            yield SimpleNamespace(name="ftp")

        with patch.object(fs.cf, "connect", connect):
            result = await fs.session(hass, "entry", lambda ftp: ftp.name)
        self.assertEqual(result, "ftp")
        self.assertEqual(seen, [("connect", "192.0.2.1", 2121, True)])
        self.assertFalse(fs.lock(data).locked())


if __name__ == "__main__":
    unittest.main(verbosity=2)
