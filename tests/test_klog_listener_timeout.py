"""A klog socket killed by TCP keepalive must not spin the event loop.
A silent klog socket while the state says rest → DDP check → reconnect if the console is on.
A silent klog socket while the state says on → DDP check → not responding after two no-replies (frozen).

Seen live: HA was OOM-killed (~5 GB) 1-2 min after a console went fully off / froze, three times. A console
that loses power never closes the socket, so keepalive gives up ~60 s later with ETIMEDOUT. Python raises that
as TimeoutError, the same class asyncio.wait_for raises, so the read loop took it for "klog is quiet" and read again.
The dead StreamReader re-raises at once without suspending: the loop never yielded, HA logged nothing, memory ran away.
"""
import ast
import asyncio
import contextlib
import errno
import logging
from pathlib import Path
import socket
from types import SimpleNamespace
from typing import Any
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components/ps4_goldhen/__init__.py"
MAX_READS = 50  # the buggy loop hits this in well under a millisecond


class Spin(BaseException):
    """Not an Exception, so the listener's `except Exception` can't swallow it."""


class Stop(BaseException):
    pass


def load_listener(fake_asyncio, async_search) -> dict[str, Any]:
    """Exec the klog listener and its helpers from __init__.py against stubs — no HA needed."""
    keep = [
        node for node in ast.parse(INIT.read_text()).body
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in ("_klog_listener_task", "_klog_power_tick", "_klog_stale_check", "_klog_frozen_check"))
        or (isinstance(node, ast.Assign) and any(
            getattr(t, "id", "") in ("_STALE_KLOG_CHECK", "_FROZEN_SILENCE") for t in node.targets))
    ]
    noop = SimpleNamespace(async_on_klog_connected=lambda *a: None, async_handle_line=lambda *a: False)
    ns: dict[str, Any] = {
        "asyncio": fake_asyncio, "contextlib": contextlib, "errno": errno, "socket": socket,
        "_LOGGER": logging.getLogger("klog-listener-test"), "DOMAIN": "ps4_goldhen",
        "async_dispatcher_send": lambda *a: None, "signal_klog_line": lambda entry_id: entry_id,
        "console_info": noop, "live_sensors": noop, "_parse_klog_line": lambda *a: False,
        "HomeAssistant": Any, "DataUpdateCoordinator": Any,
        "async_search": async_search, "POWER_ON": "on", "POWER_REST": "rest", "POWER_OFF": "off",
    }
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    return ns


class DeadReader(asyncio.StreamReader):
    """A real StreamReader whose socket died: read() raises the stored error immediately, like asyncio's own."""

    def __init__(self, exc: BaseException) -> None:
        super().__init__()
        self.reads = 0
        self.set_exception(exc)

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if self.reads > MAX_READS:
            raise Spin(f"read() called {self.reads} times on a dead socket")
        return await super().read(n)


class FakeWriter:
    def get_extra_info(self, name):
        return None

    def close(self):
        pass

    async def wait_closed(self):
        pass


class StateMachine:
    klog_connected = False
    unreachable_calls = 0
    ddp_on_calls = 0
    heard_calls = 0
    not_responding = False

    def __init__(self, power_state=None):
        self.power_state = power_state
        self.silence_codes = []

    def on_klog_heard(self):
        self.heard_calls += 1
        return False

    def on_ddp_silence_check(self, code):  # the real rule: two no-replies in a row
        self.silence_codes.append(code)
        if code is None and self.silence_codes[-2:] == [None, None] and not self.not_responding:
            self.not_responding = True
            return True
        return False

    def on_ddp_on(self):
        self.ddp_on_calls += 1
        self.power_state = "on"
        return True

    def on_klog_connected(self, now):
        pass

    def tick(self, now):
        return False

    def snapshot(self):
        return {}

    def on_klog_unreachable(self):
        self.unreachable_calls += 1
        return False


class QuietReader(asyncio.StreamReader):
    """A live but silent console: reads just wait; EOF after `quiet_reads` so the test ends. Read number `talk_at`
    gets a klog line instead."""

    def __init__(self, quiet_reads: int, talk_at: int | None = None) -> None:
        super().__init__()
        self.reads = 0
        self._quiet_reads = quiet_reads
        self._talk_at = talk_at

    async def read(self, n: int = -1) -> bytes:
        self.reads += 1
        if self.reads > self._quiet_reads:
            self.feed_eof()
        elif self.reads == self._talk_at:
            self.feed_data(b"<118>[SceShellCore] a line\n")
        return await super().read(n)


async def run_listener(
    exc: BaseException | None, quiet_reads: int = 0, power_state: str | None = None, ddp_reply=None,
    talk_at: int | None = None,
) -> SimpleNamespace:
    """One connection whose reads fail with `exc` (None = silent socket), then stop at the first reconnect sleep.

    Each quiet read moves a fake clock by the listener's 30 s; `ddp_reply` is what the stub DDP `SRCH` returns (an
    exception instance is raised instead).
    """
    reader = DeadReader(exc) if exc is not None else QuietReader(quiet_reads, talk_at)
    run = SimpleNamespace(reader=reader, connects=0, beats=0, searches=[], delays=[], pushed=[])
    clock = [1000.0]

    async def open_connection(host, port):
        run.connects += 1
        return reader, FakeWriter()

    real_sleep = asyncio.sleep

    async def sleep(delay):  # the only sleep in the listener: "Reconnecting to klog in Ns"
        run.delays.append(delay)
        raise Stop

    async def wait_for(coro, timeout):
        try:
            return await asyncio.wait_for(coro, 0.01)  # the listener's 30 s, shortened
        except asyncio.TimeoutError:
            clock[0] += timeout
            raise

    async def async_search(host):
        run.searches.append(clock[0])
        if isinstance(ddp_reply, BaseException):
            raise ddp_reply
        return ddp_reply

    fake = SimpleNamespace(
        open_connection=open_connection, sleep=sleep, wait_for=wait_for,
        TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError,
    )
    ns = load_listener(fake, async_search)
    loop = asyncio.get_running_loop()
    run.machine = StateMachine(power_state)
    hass = SimpleNamespace(
        loop=SimpleNamespace(time=lambda: clock[0]),
        bus=SimpleNamespace(async_fire=lambda *a: None),
        data={"ps4_goldhen": {"e1": {"klog_state_machine": run.machine, "klog_data": {}}}},
    )
    coordinator = SimpleNamespace(data={}, async_set_updated_data=run.pushed.append)

    async def heartbeat():
        while True:
            run.beats += 1
            await real_sleep(0)

    hb = loop.create_task(heartbeat())
    await real_sleep(0)
    beats_before = run.beats
    try:
        await ns["_klog_listener_task"](hass, "e1", "192.0.2.1", 3232, coordinator)
    except Stop:
        pass
    finally:
        hb.cancel()
    run.beats -= beats_before
    return run


class KeepaliveTimeoutTests(unittest.TestCase):
    def test_etimedout_is_a_timeout_error(self):
        # the root of it: the socket's ETIMEDOUT and wait_for's timeout are the same class
        self.assertIsInstance(OSError(errno.ETIMEDOUT, "Connection timed out"), TimeoutError)
        self.assertIs(asyncio.TimeoutError, TimeoutError)

    def test_keepalive_death_leaves_the_socket(self):
        exc = OSError(errno.ETIMEDOUT, "Connection timed out")
        run = asyncio.run(run_listener(exc))
        self.assertEqual(run.reader.reads, 1, "re-read a dead socket instead of reconnecting")
        self.assertEqual(run.connects, 1)

    def test_reset_by_peer_leaves_the_socket(self):
        # rest mode: the console resets the socket — this always worked, keep it that way
        run = asyncio.run(run_listener(ConnectionResetError(errno.ECONNRESET, "reset")))
        self.assertEqual(run.reader.reads, 1)

    def test_quiet_socket_stays_connected(self):
        # A quiet klog connection isn't dead (an idle console logs little); HA keeps it open.
        run = asyncio.run(run_listener(None, quiet_reads=3))
        self.assertEqual(run.reader.reads, 4, "gave up on a quiet socket")  # 3 timeouts, then the test's EOF
        self.assertEqual(run.connects, 1)
        self.assertEqual(run.delays, [10])


class OutageLogTests(unittest.TestCase):
    """A console on another subnet, fully off: connect timeouts every 20 s until the router answers "unreachable".
    Without the fix, each one logged a WARNING with no message."""

    def run_outage(self, failures, then_connect=False):
        attempts = [0]

        async def go():
            async def open_connection(host, port):
                attempts[0] += 1
                if attempts[0] <= failures:
                    raise asyncio.TimeoutError()
                if attempts[0] == failures + 1 and then_connect:
                    reader = asyncio.StreamReader()
                    reader.feed_eof()  # connected, then the console closes it
                    return reader, FakeWriter()
                raise OSError(errno.ETIMEDOUT, "Operation timed out")

            async def sleep(delay):
                if attempts[0] >= failures + 3:
                    raise Stop

            async def wait_for(coro, timeout):
                return await coro

            fake = SimpleNamespace(open_connection=open_connection, sleep=sleep, wait_for=wait_for,
                                   TimeoutError=asyncio.TimeoutError, CancelledError=asyncio.CancelledError)
            ns = load_listener(fake, None)
            machine = StateMachine()
            hass = SimpleNamespace(loop=SimpleNamespace(time=lambda: 1000.0),
                                   data={"ps4_goldhen": {"e1": {"klog_state_machine": machine, "klog_data": {}}}})
            coordinator = SimpleNamespace(data={}, async_set_updated_data=lambda data: None)
            with contextlib.suppress(Stop):
                await ns["_klog_listener_task"](hass, "e1", "192.0.2.1", 3232, coordinator)

        with self.assertLogs("klog-listener-test", level="DEBUG") as logs:
            asyncio.run(go())
        return [r for r in logs.records if "Klog connection error" in r.getMessage()]

    def test_one_warning_per_outage(self):
        records = self.run_outage(failures=5)
        self.assertEqual(len(records), 8)
        self.assertEqual([r.levelname for r in records], ["WARNING"] + ["DEBUG"] * 7)
        self.assertIn(":3232: TimeoutError (until it connects", records[0].getMessage())  # not an empty message

    def test_new_outage_after_a_connect_warns_again(self):
        records = self.run_outage(failures=2, then_connect=True)
        self.assertEqual([r.levelname for r in records], ["WARNING", "DEBUG", "WARNING", "DEBUG"])
        self.assertIn("Operation timed out", records[2].getMessage())


def reply(code: int) -> SimpleNamespace:
    return SimpleNamespace(code=code)


class StaleKlogTests(unittest.TestCase):
    """Stale klog while the state says rest / off. Quiet reads are 30 s each: checks fall at 60, 120, 180 s of silence."""

    def test_rest_but_ddp_on_reconnects(self):
        # woken from deep rest without HA: klog socket open but silent, HA stuck on rest
        run = asyncio.run(run_listener(None, quiet_reads=10, power_state="rest", ddp_reply=reply(200)))
        self.assertEqual(run.reader.reads, 2, "no DDP check after 60 s of silence")
        self.assertEqual(len(run.searches), 1)
        self.assertEqual(run.machine.ddp_on_calls, 1)
        self.assertEqual(run.machine.power_state, "on")
        self.assertTrue(run.pushed, "the new power state never reached the coordinator")
        self.assertEqual(run.delays, [1], "should reconnect at once, not after 10 s")

    def test_rest_and_ddp_rest_keeps_the_socket(self):
        # deep rest on an open socket: one SRCH a minute, nothing else changes
        run = asyncio.run(run_listener(None, quiet_reads=6, power_state="rest", ddp_reply=reply(620)))
        self.assertEqual(run.reader.reads, 7)  # 6 timeouts, then the test's EOF
        self.assertEqual(len(run.searches), 3, "not rate-limited to one SRCH per 60 s")
        self.assertEqual(run.machine.ddp_on_calls, 0)
        self.assertEqual(run.machine.power_state, "rest")
        self.assertEqual(run.delays, [10])

    def test_no_ddp_reply_or_error_keeps_the_socket(self):
        for answer in (None, OSError(errno.ENETUNREACH, "unreachable")):
            with self.subTest(answer=answer):
                run = asyncio.run(run_listener(None, quiet_reads=4, power_state="rest", ddp_reply=answer))
                self.assertEqual(len(run.searches), 2)
                self.assertEqual(run.machine.power_state, "rest")
                self.assertEqual(run.delays, [10])

    def test_off_but_ddp_on_reconnects(self):
        run = asyncio.run(run_listener(None, quiet_reads=10, power_state="off", ddp_reply=reply(200)))
        self.assertEqual(run.machine.power_state, "on")
        self.assertEqual(run.delays, [1])

    def test_on_or_unknown_never_asks_ddp(self):
        # a quiet console that's on is normal (no game running); unknown waits for the 90 s heartbeat rule
        for power in ("on", None):
            with self.subTest(power=power):
                run = asyncio.run(run_listener(None, quiet_reads=6, power_state=power, ddp_reply=reply(200)))
                self.assertEqual(run.searches, [])
                self.assertEqual(run.delays, [10])


class FrozenKlogTests(unittest.TestCase):
    """Silent klog while the state says on. Checks fall every 60 s; DDP is asked from 300 s of silence."""

    def test_no_ddp_reply_twice_is_not_responding(self):
        run = asyncio.run(run_listener(None, quiet_reads=12, power_state="on", ddp_reply=None))
        self.assertEqual(run.searches, [1300.0, 1360.0])
        self.assertEqual(run.machine.silence_codes, [None, None])
        self.assertTrue(run.machine.not_responding)
        self.assertTrue(run.pushed, "not responding never reached the coordinator")
        self.assertEqual(run.delays, [10], "the socket stays open (a frozen console still answers keepalives)")

    def test_ddp_answers_keeps_it_on(self):  # a quiet console that's fine: one SRCH a minute
        run = asyncio.run(run_listener(None, quiet_reads=14, power_state="on", ddp_reply=reply(200)))
        self.assertEqual(run.searches, [1300.0, 1360.0, 1420.0])
        self.assertEqual(run.machine.silence_codes, [200, 200, 200])
        self.assertFalse(run.machine.not_responding)

    def test_ddp_error_is_not_a_miss(self):
        run = asyncio.run(run_listener(None, quiet_reads=12, power_state="on",
                                       ddp_reply=OSError(errno.ENETUNREACH, "unreachable")))
        self.assertEqual(len(run.searches), 2)
        self.assertEqual(run.machine.silence_codes, [])

    def test_unknown_power_never_asks(self):
        run = asyncio.run(run_listener(None, quiet_reads=12, power_state=None, ddp_reply=None))
        self.assertEqual(run.searches, [])

    def test_klog_data_restarts_the_silence(self):
        # a line after 210 s: the next 180 s of silence stay under the 300 s
        run = asyncio.run(run_listener(None, quiet_reads=14, power_state="on", ddp_reply=None, talk_at=8))
        self.assertEqual(run.machine.heard_calls, 1)
        self.assertEqual(run.searches, [])


if __name__ == "__main__":
    unittest.main()
