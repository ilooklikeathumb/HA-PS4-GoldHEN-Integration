"""Remote Play session login — crypto vectors, a fake PS4 control channel end to end, retries.

Needs `cryptography` (part of Home Assistant core). Outside HA: `uv run --no-project --with cryptography python -m
unittest tests/test_rp_session.py`. Crypto vectors come from playactor's tests (test/remoteplay/crypto/modern-test.ts).
"""
import asyncio
import base64
import importlib
from pathlib import Path
import struct
import sys
import time
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_rp_session_test_pkg"

_pkg = types.ModuleType(PKG)
_pkg.__path__ = [str(COMP)]
sys.modules[PKG] = _pkg

rs = importlib.import_module(f"{PKG}.rp_session")
ddp = importlib.import_module(f"{PKG}.ddp")

# made-up values, never a real console's
REGIST_KEY = "1a2b3c4d"
RP_KEY = "00112233445566778899aabbccddeeff"
NONCE = bytes(range(16))

# the values the integration ships with, before the tests shorten them
PRODUCTION = {name: getattr(rs, name) for name in ("CTRL_GAP", "CTRL_REPLY_TIMEOUT", "HOLD_AFTER_LOGIN", "LOCK_WAIT")}

rs.HOLD_AFTER_LOGIN = 0.05
rs.LOGIN_TIMEOUT = 1.0
rs.HTTP_TIMEOUT = 2.0
rs.CTRL_GAP = 0.05
rs.CTRL_REPLY_TIMEOUT = 0.3


def run(coro):
    return asyncio.run(coro)


class CryptoTests(unittest.TestCase):
    SERVER_NONCE = bytes([0xAE, 0x92, 0xE7, 0x64, 0x88, 0x26, 0x51, 0xEF, 0x89, 0x01, 0x8C, 0xFA, 0x69, 0x6C, 0x69, 0x38])
    AUTH_KEY = bytes([0x74, 0xA5, 0x9C, 0x96, 0x93, 0xC2, 0x08, 0x3B, 0xA6, 0xA8, 0x4B, 0xA0, 0x50, 0xFA, 0x8E, 0x5A])

    def test_nonce_transform_ps4_vector(self):
        self.assertEqual(rs.transform_server_nonce(self.SERVER_NONCE).hex().upper(), "92BEE219B9D5B1ABC6494577A421E9BD")

    def test_auth_seed_ps4_vector(self):
        self.assertEqual(rs.auth_seed(self.AUTH_KEY, self.SERVER_NONCE).hex().upper(), "67408C5E65665AD291A832EBE2D90ABB")

    def test_tables(self):
        self.assertEqual(len(rs.AUTH_NONCE_KEY_PS4), 32 * 0x70)
        self.assertEqual(len(rs.AUTH_SEED_KEY_PS4), 32 * 0x70)

    def test_counters_per_direction(self):
        client = rs.SessionCrypto(bytes.fromhex(RP_KEY), NONCE)
        console = rs.SessionCrypto(bytes.fromhex(RP_KEY), NONCE)
        first, second = client.encrypt(b"same text"), client.encrypt(b"same text")
        self.assertNotEqual(first, second)  # new IV per buffer
        self.assertEqual(console.decrypt(first), b"same text")
        self.assertEqual(console.decrypt(second), b"same text")

    def test_ctrl_headers(self):
        client = rs.SessionCrypto(bytes.fromhex(RP_KEY), NONCE)
        console = rs.SessionCrypto(bytes.fromhex(RP_KEY), NONCE)
        headers = rs.ctrl_headers(client, REGIST_KEY, did_middle=bytes(16))
        self.assertNotIn("RP-StreamingType", headers)  # PS4: like chiaki-ng
        plain = {k: console.decrypt(base64.b64decode(headers[k])) for k in ("RP-Auth", "RP-Did", "RP-OSType", "RP-StartBitrate")}
        self.assertEqual(plain["RP-Auth"], b"1a2b3c4d" + bytes(8))
        self.assertEqual(plain["RP-Did"], rs._DID_PREFIX + bytes(16) + rs._DID_SUFFIX)
        self.assertEqual(plain["RP-OSType"], b"Win10.0.0\x00")
        self.assertEqual(plain["RP-StartBitrate"], bytes(4))
        self.assertEqual(headers["RP-Version"], "10.0")


class FakePS4:
    """Sony's Remote Play service on 127.0.0.1: init + ctrl, then the control messages a real PS4 sent."""

    def __init__(self, *, init_code=200, init_reason=None, login=b"\x00", pin_request=False, close_after_login=False,
                 ctrl_fail_on_keepalive_init=True, reset_ctrl_within=0.0, silent_ctrls=0, init_hold_open=False,
                 listener_closed_after_init=0.0):
        self.init_code = init_code
        self.init_reason = init_reason
        self.login = login
        self.pin_request = pin_request
        self.close_after_login = close_after_login
        self.ctrl_fail_on_keepalive_init = ctrl_fail_on_keepalive_init
        # the fake console can: reset a ctrl that comes this soon after the init ended; ignore the first N ctrl
        # requests; keep the init connection open after the reply (only the client's close ends it)
        self.reset_ctrl_within = reset_ctrl_within
        self.silent_ctrls = silent_ctrls
        self.init_hold_open = init_hold_open
        # the real console refused (RST) a ctrl connect that came ~1 ms after the init
        self.listener_closed_after_init = listener_closed_after_init
        self.refused_window_done = False
        self.requests = []
        self.heartbeat_replies = 0
        self.last_init_connection = None
        self.init_ended_at = None
        self.init_client_closed_first = None
        self.auth = None
        self.login_sent_at = None
        self.ctrl_closed_at = None

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    def close(self):
        self.server.close()

    async def _reopen(self, delay):
        await asyncio.sleep(delay)
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", self.port)

    async def _handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:] if line)}
        self.requests.append((lines[0], headers))
        if lines[0].startswith("GET /sie/ps4/rp/sess/init "):
            self.last_init_connection = headers.get("connection")
            assert headers["rp-registkey"] == REGIST_KEY.encode().hex(), headers
            if self.init_code != 200:
                extra = f"RP-Application-Reason: {self.init_reason}\r\n" if self.init_reason else ""
                writer.write(f"HTTP/1.1 {self.init_code} Forbidden\r\n{extra}Content-Length: 0\r\n\r\n".encode())
            else:
                nonce = base64.b64encode(NONCE).decode()
                writer.write(f"HTTP/1.1 200 OK\r\nRP-Nonce: {nonce}\r\nRP-Version: 10.0\r\nContent-Length: 0\r\n\r\n".encode())
            await writer.drain()
            if self.init_hold_open:
                try:
                    self.init_client_closed_first = await asyncio.wait_for(reader.read(), 3) == b""
                except TimeoutError:
                    self.init_client_closed_first = False
            self.init_ended_at = time.monotonic()
            writer.close()
            if self.listener_closed_after_init and not self.refused_window_done:
                self.refused_window_done = True
                self.server.close()  # new connections are refused until it reopens
                asyncio.get_running_loop().create_task(self._reopen(self.listener_closed_after_init))
            return
        # ctrl: the real console reset this when the init connection had been keep-alive
        if self.ctrl_fail_on_keepalive_init and self.last_init_connection != "close":
            writer.transport.abort()
            return
        # ... and when it came straight after the init
        if self.init_ended_at is not None and time.monotonic() - self.init_ended_at < self.reset_ctrl_within:
            writer.transport.abort()
            return
        if self.silent_ctrls:
            self.silent_ctrls -= 1
            await reader.read()  # no reply; wait for the client to give up
            writer.close()
            return
        crypto = rs.SessionCrypto(bytes.fromhex(RP_KEY), NONCE)
        self.auth = crypto.decrypt(base64.b64decode(headers["rp-auth"]))
        for name in ("rp-did", "rp-ostype", "rp-startbitrate"):
            crypto.decrypt(base64.b64decode(headers[name]))
        if self.auth != REGIST_KEY.encode() + bytes(8):
            writer.write(b"HTTP/1.1 403 Forbidden\r\nRP-Application-Reason: 80108b09\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
            writer.close()
            return
        server_type = base64.b64encode(crypto.encrypt(b"\x01\x00\x00\x00")).decode()
        writer.write(f"HTTP/1.1 200 OK\r\nRP-Server-Type: {server_type}\r\nRP-Prohibit: 0\r\nContent-Length: 0\r\n\r\n".encode())

        def msg(msg_type, payload=b""):
            return struct.pack(">IHH", len(payload), msg_type, 0) + (crypto.encrypt(payload) if payload else b"")

        if self.pin_request:
            writer.write(msg(rs.MSG_LOGIN_PIN_REQ))
        else:
            writer.write(msg(rs.MSG_LOGIN, self.login) + msg(0x6, b"\x00\x01") + msg(rs.MSG_SESSION_ID, b"S" * 75)
                         + msg(rs.MSG_HEARTBEAT_REQ))
        await writer.drain()
        self.login_sent_at = time.monotonic()
        if self.close_after_login:
            writer.close()
            return
        try:
            while True:
                reply = await asyncio.wait_for(reader.readexactly(8), 2)
                if struct.unpack(">IHH", reply)[1] == rs.MSG_HEARTBEAT_REP:
                    self.heartbeat_replies += 1
        except (asyncio.IncompleteReadError, ConnectionError, TimeoutError):
            pass
        self.ctrl_closed_at = time.monotonic()
        writer.close()


async def _with_console(console, fn):
    await console.start()
    try:
        return await fn(console)
    finally:
        console.close()


class LoginTests(unittest.TestCase):
    def login(self, console):
        return run(_with_console(console, lambda c: rs.async_login("127.0.0.1", REGIST_KEY, RP_KEY, port=c.port)))

    def test_logs_in(self):
        console = FakePS4()
        self.login(console)
        self.assertEqual(console.last_init_connection, "close")
        self.assertEqual(console.auth, REGIST_KEY.encode() + bytes(8))
        self.assertEqual(console.heartbeat_replies, 1)
        self.assertTrue(all(h.get("content-length") == "0" for _, h in console.requests))

    def test_console_closing_after_login_is_fine(self):
        self.login(FakePS4(close_after_login=True))

    def test_in_use(self):
        with self.assertRaises(rs.RpSessionError) as ctx:
            self.login(FakePS4(init_code=403, init_reason="80108B10"))
        self.assertEqual(ctx.exception.reason, "in_use")

    def test_other_refusal(self):
        with self.assertRaises(rs.RpSessionError) as ctx:
            self.login(FakePS4(init_code=403, init_reason="80108b11"))
        self.assertEqual(ctx.exception.reason, "rp_version")

    def test_wrong_rp_key_refused(self):
        async def go(console):
            await rs.async_login("127.0.0.1", REGIST_KEY, "ff" * 16, port=console.port)

        with self.assertRaises(rs.RpSessionError) as ctx:
            run(_with_console(FakePS4(), go))
        self.assertEqual(ctx.exception.reason, "refused")

    def test_passcode_user(self):
        with self.assertRaises(rs.RpSessionError) as ctx:
            self.login(FakePS4(pin_request=True))
        self.assertEqual(ctx.exception.reason, "passcode")

    def test_login_failed(self):
        with self.assertRaises(rs.RpSessionError) as ctx:
            self.login(FakePS4(login=b"\x01"))
        self.assertEqual(ctx.exception.reason, "login_failed")

    def test_nothing_listening(self):
        async def go():
            server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            await rs.async_login("127.0.0.1", REGIST_KEY, RP_KEY, port=port)

        with self.assertRaises(rs.RpSessionError) as ctx:
            run(go())
        self.assertEqual(ctx.exception.reason, "not_listening")  # nothing started on the console: retried quickly

    # — match chiaki-ng on the wire —

    def test_shipped_timings(self):  # the values the live logins ran with
        self.assertEqual(PRODUCTION["CTRL_GAP"], 0.5)
        self.assertEqual(PRODUCTION["HOLD_AFTER_LOGIN"], 3.0)
        self.assertEqual(PRODUCTION["CTRL_REPLY_TIMEOUT"], 5.0)
        self.assertGreaterEqual(PRODUCTION["LOCK_WAIT"], 30.0)

    def test_init_closed_at_once(self):
        console = FakePS4(init_hold_open=True)
        started = time.monotonic()
        self.login(console)
        self.assertTrue(console.init_client_closed_first)
        self.assertLess(time.monotonic() - started, 1.0)  # no waiting for the console's close

    def test_pause_before_ctrl(self):
        saved = rs.CTRL_GAP
        try:
            rs.CTRL_GAP = 0.3
            self.login(FakePS4(reset_ctrl_within=0.2))
            rs.CTRL_GAP = 0
            with self.assertRaises(rs.RpSessionError) as ctx:
                self.login(FakePS4(reset_ctrl_within=0.2))
        finally:
            rs.CTRL_GAP = saved
        self.assertEqual(ctx.exception.reason, "no_session")
        self.assertIn("ctrl:", str(ctx.exception))  # the failed request is named

    def test_refused_ctrl_connect_retried(self):  # a real console: RST to a ctrl SYN ~1 ms after the init
        saved = rs.CTRL_GAP
        try:
            rs.CTRL_GAP = 0
            console = FakePS4(listener_closed_after_init=0.5)
            self.login(console)
        finally:
            rs.CTRL_GAP = saved
        self.assertEqual(sum("/sess/init " in line for line, _ in console.requests), 1)  # one init, one session
        self.assertEqual(console.auth, REGIST_KEY.encode() + bytes(8))

    def test_refused_ctrl_gives_up_after_the_window(self):
        saved = rs.CTRL_GAP, rs.CTRL_REFUSED_WINDOW
        try:
            rs.CTRL_GAP, rs.CTRL_REFUSED_WINDOW = 0, 0.3
            with self.assertRaises(rs.RpSessionError) as ctx:
                self.login(FakePS4(listener_closed_after_init=2.0))
        finally:
            rs.CTRL_GAP, rs.CTRL_REFUSED_WINDOW = saved
        self.assertEqual(ctx.exception.reason, "no_session")  # the init claimed the session: lock wait, not 1 s retries
        self.assertIn("ctrl:", str(ctx.exception))

    def test_silent_ctrl_resent_once(self):
        console = FakePS4(silent_ctrls=1)
        self.login(console)
        ctrls = [h for line, h in console.requests if "/sess/ctrl " in line]
        self.assertEqual(len(ctrls), 2)
        self.assertEqual(ctrls[0], ctrls[1])  # the same request, byte for byte
        self.assertEqual(console.auth, REGIST_KEY.encode() + bytes(8))

    def test_silent_ctrl_twice_gives_up(self):
        with self.assertRaises(rs.RpSessionError) as ctx:
            self.login(FakePS4(silent_ctrls=2))
        self.assertEqual(ctx.exception.reason, "no_session")
        self.assertIn("ctrl:", str(ctx.exception))

    def test_holds_after_login(self):
        saved = rs.HOLD_AFTER_LOGIN
        try:
            rs.HOLD_AFTER_LOGIN = 0.3
            console = FakePS4()
            self.login(console)
        finally:
            rs.HOLD_AFTER_LOGIN = saved
        self.assertGreaterEqual(console.ctrl_closed_at - console.login_sent_at, 0.25)


class AfterWakeTests(unittest.TestCase):
    def setUp(self):
        self.saved = (rs.async_search, rs.async_login, rs.RETRY_WAIT, rs.RETRY_WINDOW, rs.LOCK_WAIT,
                      rs.RETRY_WINDOW_AFTER_LOCK, rs.WAIT_ON_TIMEOUT, rs.WAIT_ON_INTERVAL, rs.async_wakeup,
                      rs.REWAKE_AFTER)
        rs.RETRY_WAIT, rs.RETRY_WINDOW, rs.LOCK_WAIT, rs.RETRY_WINDOW_AFTER_LOCK = 0.01, 0.2, 0.1, 0.1
        rs.WAIT_ON_TIMEOUT, rs.WAIT_ON_INTERVAL = 0.05, 0.01
        self.wakes = []

        async def wakeup(host, regist_key):
            self.wakes.append((host, regist_key))

        rs.async_wakeup = wakeup
        self.searches = []
        self.logins = []
        self.login_times = []

    def tearDown(self):
        (rs.async_search, rs.async_login, rs.RETRY_WAIT, rs.RETRY_WINDOW, rs.LOCK_WAIT, rs.RETRY_WINDOW_AFTER_LOCK,
         rs.WAIT_ON_TIMEOUT, rs.WAIT_ON_INTERVAL, rs.async_wakeup, rs.REWAKE_AFTER) = self.saved

    def gaps(self):
        return [b - a for a, b in zip(self.login_times, self.login_times[1:])]

    def fake_search(self, codes):
        async def search(host, timeout=3.0):
            self.searches.append(host)
            code = codes.pop(0) if codes else 200
            return None if code is None else ddp.DDPReply(code, {"system-version": "12008011"})

        rs.async_search = search

    def fake_login(self, failures):
        async def login(host, regist_key, rp_key, port=rs.RP_PORT, t0=None):
            self.logins.append((host, regist_key, rp_key))
            self.login_times.append(time.monotonic())
            if failures:
                raise rs.RpSessionError(failures.pop(0))

        rs.async_login = login

    def test_waits_for_on_then_logs_in(self):
        self.fake_search([620, None, 200])
        self.fake_login([])
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(len(self.searches), 3)
        self.assertEqual(self.logins, [("192.0.2.1", REGIST_KEY, RP_KEY)])

    def test_lost_wake_resent(self):  # the WAKEUP can get lost: DDP says 620 for 60 s
        rs.WAIT_ON_TIMEOUT, rs.REWAKE_AFTER = 1.0, 0.03
        self.fake_search([620] * 10 + [200])
        self.fake_login([])
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertGreaterEqual(len(self.wakes), 1)
        self.assertEqual(self.wakes[0], ("192.0.2.1", REGIST_KEY))
        self.assertEqual(len(self.logins), 1)

    def test_no_resend_while_waking(self):  # a wake that works: 620 briefly, no reply, then 200
        rs.WAIT_ON_TIMEOUT, rs.REWAKE_AFTER = 1.0, 5.0
        self.fake_search([620, None, None, 200])
        self.fake_login([])
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(self.wakes, [])

    def test_never_on(self):
        self.fake_search([620] * 100)
        self.fake_login([])
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "not_on")
        self.assertEqual(self.logins, [])

    def test_old_firmware(self):
        async def search(host, timeout=3.0):
            return ddp.DDPReply(200, {"system-version": "07550001"})

        rs.async_search = search
        self.fake_login([])
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "rp_version")

    def test_refused_until_remote_play_is_up(self):  # port not listening yet: nothing armed, quick retries
        self.fake_search([])
        self.fake_login(["not_listening"] * 8)
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(len(self.logins), 9)
        self.assertTrue(all(gap < rs.LOCK_WAIT for gap in self.gaps()))

    def test_failed_session_waits_out_the_lock(self):  # seen live: a retry 2 s after a reset hit 403 in use
        self.fake_search([])
        self.fake_login(["no_session"])
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(len(self.logins), 2)
        self.assertGreaterEqual(self.gaps()[0], rs.LOCK_WAIT)

    def test_lock_waited_once_only(self):
        self.fake_search([])
        self.fake_login(["no_session", "no_login"])
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "no_login")
        self.assertEqual(len(self.logins), 2)

    def test_in_use_waited_once_only(self):
        self.fake_search([])
        self.fake_login(["in_use", "in_use"])
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "in_use")
        self.assertEqual(len(self.logins), 2)
        self.assertGreaterEqual(self.gaps()[0], rs.LOCK_WAIT)

    def test_refused_after_the_lock_wait_retried(self):  # live: reset → refused → in use (service restarting)
        self.fake_search([])
        self.fake_login(["no_session", "not_listening", "not_listening"])
        run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(len(self.logins), 4)
        self.assertGreaterEqual(self.gaps()[0], rs.LOCK_WAIT)
        self.assertTrue(all(gap < rs.LOCK_WAIT for gap in self.gaps()[1:]))

    def test_gives_up_after_the_window(self):
        self.fake_search([])
        self.fake_login(["not_listening"] * 1000)
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "not_listening")
        self.assertGreater(len(self.logins), 3)
        self.assertLess(len(self.logins), 1000)

    def test_permanent_failure_not_retried(self):
        self.fake_search([])
        self.fake_login(["passcode"])
        with self.assertRaises(rs.RpSessionError) as ctx:
            run(rs.async_login_after_wake("192.0.2.1", REGIST_KEY, RP_KEY))
        self.assertEqual(ctx.exception.reason, "passcode")
        self.assertEqual(len(self.logins), 1)


if __name__ == "__main__":
    unittest.main()
