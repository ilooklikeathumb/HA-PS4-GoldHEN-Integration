"""Log the paired user in after an HA wake, with a short Remote Play session.

Rest logs every user out on the PS4, so an HA wake lands on the Welcome / choose-user screen and launches fail. A
Remote Play control session counts as a controller: the console logs in the account the pairing belongs to (HA's pairing
saved its regist key + RP-Key) and ShellUI goes to the home screen. No video stream is opened. The login stays after
the session closes; a controller turned on later is assigned to that user, and games launched from HA run as them.
Tested live on a PS4 Pro (FW 12.00, controller off).

Protocol (TCP 9295, Sony's Remote Play service; no GoldHEN port):
1. `GET /sie/ps4/rp/sess/init`, headers `RP-RegistKey` (hex of the 8-character regist key) + `RP-Version`, sent with
   `Connection: close` → header `RP-Nonce` (16 bytes, base64). We close as soon as the reply header is in.
2. Pause `CTRL_GAP`, then `GET /sie/ps4/rp/sess/ctrl` with `RP-Auth` (regist key bytes, NUL-padded to 16), `RP-Did`,
   `RP-OSType`, `RP-StartBitrate` encrypted, in that order → `200`; the socket becomes the control channel. No reply
   within `CTRL_REPLY_TIMEOUT` → close, reconnect and send the same request once more.
3. Control messages: 8-byte header (u32 BE payload length, u16 BE type, u16 0) + encrypted payload. The console sends
   `LOGIN` (type 5, payload byte 0: 0 = logged in) unasked, then `SESSION_ID`, then a heartbeat (0xfe) every 5 s,
   answered with 0x1fe. `LOGIN_PIN_REQ` (type 4) = the user has a passcode, not supported here.
Timing (measured): the init already works at DDP 200, ~11 s before the console is WORKING. Right after answering it,
the console closes its listener for a moment: a ctrl connect 0.3-1.3 ms after the init got a RST (refused) 3 times
out of 3. The init had already claimed the session, so Remote Play then answered `403` (reason `80108b10`, "in use")
for ~30 s. chiaki-ng pauses ~10 ms ("PS4 doesn't always react right away"), and a 0.5 s pause always worked. So:
pause CTRL_GAP, retry a refused ctrl connect for a few seconds (a refused SYN never reaches the service, the claimed
session still waits), resend a silent ctrl once (chiaki-ng), and after any other failure wait the lock out once
instead of retrying into it.
Crypto: each encrypted buffer is AES-128-CFB with key = seed(RP-Key, nonce) and IV = HMAC-SHA256(nonce', counter)[:16];
separate send / receive counters. Ported from playactor (dhleong/playactor @ 93ee087, ISC license, Copyright (c)
Daniel Leong): `src/remoteplay/{session,codec,protocol}.ts`, `crypto/{base,modern}.ts`. The PS4 header set (no
`RP-StreamingType`, OS type NUL-terminated) follows what chiaki-ng sends; no chiaki-ng code is used.
Nothing secret is logged.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import struct
import time

from .ddp import async_search, async_wakeup
from .rp_keys import AUTH_NONCE_KEY_PS4, AUTH_SEED_KEY_PS4
from .rp_regist import _HMAC_KEYS, RP_10, RP_PORT, _aes_cfb, generate_iv, rp_version_for

_LOGGER = logging.getLogger(__name__)

_DID_PREFIX = bytes([0x00, 0x18, 0x00, 0x00, 0x00, 0x07, 0x00, 0x40, 0x00, 0x80])
_DID_SUFFIX = bytes(6)
_OS_TYPE = b"Win10.0.0\x00"

MSG_LOGIN_PIN_REQ = 0x04
MSG_LOGIN = 0x05
MSG_SESSION_ID = 0x33
MSG_HEARTBEAT_REQ = 0xFE
MSG_HEARTBEAT_REP = 0x1FE

_IN_USE = "80108b10"
_REASONS = {"80108b11": "rp_version", "80108b15": "crash"}

# How long to wait for each part. The console sent LOGIN within 1 s of the ctrl reply in both live runs.
HTTP_TIMEOUT = 10.0
LOGIN_TIMEOUT = 15.0
# Pause between the init close and the ctrl connect: 0.5 s in both manual logins; ~1 ms was refused 3/3 (bench).
CTRL_GAP = 0.5
# A refused ctrl connect (listener not back yet) is retried every CTRL_REFUSED_RETRY s for CTRL_REFUSED_WINDOW s.
CTRL_REFUSED_RETRY = 0.2
CTRL_REFUSED_WINDOW = 3.0
# No ctrl reply within this long → resend once on a new connection (chiaki-ng: 5 s, one resend).
CTRL_REPLY_TIMEOUT = 5.0
# Keep the session open this long after LOGIN so the console finishes the login (ShellUI takes ~1 s to reach the
# home screen); the user stays logged in after the close (3, 10 and 40 s tested on a 12.00 Pro). Not shorter by
# sending at LOGIN: a session closing with the game in front shows the "Remote Play disconnected" notice over it.
HOLD_AFTER_LOGIN = 3.0
# After the wake: the console answered DDP 200 ~13 s after the WAKEUP; bounded. chiaki-ng polls every 0.5 s.
WAIT_ON_TIMEOUT = 60.0
WAIT_ON_INTERVAL = 1.0
# DDP still says rest (620) this long after a WAKEUP → send it again, like chiaki-ng does while it sees standby. A wake
# that lands while the console hands its network to the standby chip (~1 min after a rest) is lost (seen on a
# FW 11.00 Pro: 620 for 60 s); a wake that works leaves 620 within ~2 s.
REWAKE_AFTER = 5.0
# Retries. Port 9295 refusing the connection = the service isn't listening yet: nothing started, so retry every
# RETRY_WAIT s for RETRY_WINDOW s after DDP 200. Any other failure may have armed the console's ~30 s Remote Play lock:
# wait LOCK_WAIT once, then (refusals aside, for RETRY_WINDOW_AFTER_LOCK s) one last try. Never retry inside the lock.
RETRY_WAIT = 1.0
RETRY_WINDOW = 45.0
LOCK_WAIT = 32.0
RETRY_WINDOW_AFTER_LOCK = 10.0
_LOCK_REASONS = ("no_session", "no_login", "in_use")


class RpSessionError(Exception):
    """Login failed; `reason` is a short key: not_on, not_listening, in_use, no_session, refused, rp_version, crash,
    passcode, login_failed, no_login."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


# — Crypto (playactor crypto/modern.ts + crypto/base.ts, PS4 / Remote Play 10) ———————————————————————————————


def transform_server_nonce(server_nonce: bytes) -> bytes:
    off = (server_nonce[0] >> 3) * 0x70
    return bytes(((server_nonce[i] + 0x36 + i) ^ AUTH_NONCE_KEY_PS4[off + i]) & 0xFF for i in range(16))


def auth_seed(rp_key: bytes, server_nonce: bytes) -> bytes:
    off = (server_nonce[7] >> 3) * 0x70
    return bytes((((AUTH_SEED_KEY_PS4[off + i] ^ rp_key[i]) + 0x21 + i) ^ server_nonce[i]) & 0xFF for i in range(16))


class SessionCrypto:
    """A fresh CFB stream per encrypted buffer; the IV comes from a per-direction counter."""

    def __init__(self, rp_key: bytes, server_nonce: bytes, hmac_key: bytes = _HMAC_KEYS[RP_10]) -> None:
        if len(rp_key) != 16 or len(server_nonce) != 16:
            raise ValueError("RP-Key and nonce must be 16 bytes")
        self._key = auth_seed(rp_key, server_nonce)
        self._nonce = transform_server_nonce(server_nonce)
        self._hmac_key = hmac_key
        self._send = 0
        self._recv = 0

    def encrypt(self, data: bytes) -> bytes:
        iv = generate_iv(self._hmac_key, self._nonce, self._send)
        self._send += 1
        return _aes_cfb(self._key, iv, data, decrypt=False)

    def decrypt(self, data: bytes) -> bytes:
        iv = generate_iv(self._hmac_key, self._nonce, self._recv)
        self._recv += 1
        return _aes_cfb(self._key, iv, data, decrypt=True)


def ctrl_headers(crypto: SessionCrypto, regist_key: str, did_middle: bytes | None = None) -> dict[str, str]:
    """The ctrl request's RP headers. Encryption order = counter order; keep it."""
    enc = lambda data: base64.b64encode(crypto.encrypt(data)).decode()  # noqa: E731
    did = _DID_PREFIX + (did_middle if did_middle is not None else os.urandom(16)) + _DID_SUFFIX
    return {
        "RP-Auth": enc(regist_key.encode("ascii").ljust(16, b"\x00")),
        "RP-Version": RP_10,
        "RP-Did": enc(did),
        "RP-ControllerType": "3",
        "RP-ClientType": "11",
        "RP-OSType": enc(_OS_TYPE),
        "RP-ConPath": "1",
        "RP-StartBitrate": enc(bytes(4)),
    }


# — Network ————————————————————————————————————————————————————————————————————————————————————————————————————


def _request(host: str, port: int, path: str, headers: dict[str, str], connection: str) -> bytes:
    lines = [
        f"GET {path} HTTP/1.1",
        f"Host: {host}:{port}",
        "User-Agent: remoteplay Windows",
        f"Connection: {connection}",
        "Content-Length: 0",  # playactor: a GET without it gets 403
        *(f"{k}: {v}" for k, v in headers.items()),
    ]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


async def _http_exchange(
    host: str, port: int, path: str, request: bytes, timeout: float
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int, dict[str, str]]:
    """Connect, send `request`, read the reply header. The caller closes the writer."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), HTTP_TIMEOUT)
    try:
        writer.write(request)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout)
        status, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise RpSessionError("no_session", f"{path}: not an HTTP reply")
        reply_headers = {}
        for line in header_lines:
            key, sep, value = line.partition(":")
            if sep:
                reply_headers[key.strip().lower()] = value.strip()
        return reader, writer, int(parts[1]), reply_headers
    except BaseException:
        writer.close()
        raise


def _refusal(path: str, code: int, headers: dict[str, str]) -> RpSessionError:
    reason = headers.get("rp-application-reason", "").lower().removeprefix("0x")
    key = "in_use" if reason == _IN_USE else _REASONS.get(reason, "refused")
    return RpSessionError(key, f"{path}: HTTP {code}, RP-Application-Reason {reason or 'none'}")


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1)
    except (OSError, TimeoutError):
        pass


class _Trace:
    """Debug timeline of one login, in ms from `t0` (DDP 200 in the after-wake flow). Nothing secret."""

    def __init__(self, host: str, t0: float | None = None) -> None:
        self.host = host
        self.t0 = time.monotonic() if t0 is None else t0

    def __call__(self, step: str) -> None:
        _LOGGER.debug("Remote Play %s +%d ms: %s", self.host, (time.monotonic() - self.t0) * 1000, step)


async def _async_init(host: str, port: int, regist_key: str, trace: _Trace) -> bytes:
    path = "/sie/ps4/rp/sess/init"
    headers = {"RP-RegistKey": regist_key.encode("ascii").hex(), "RP-Version": RP_10}
    trace("init: connecting")
    try:
        reader, writer, code, reply = await _http_exchange(
            host, port, path, _request(host, port, path, headers, "close"), HTTP_TIMEOUT
        )
    except ConnectionRefusedError as err:
        raise RpSessionError("not_listening", f"{path}: connection refused") from err
    # like chiaki-ng: close as soon as the reply header is in, don't wait for the console to close first
    await _close(writer)
    trace(f"init: HTTP {code}, closed")
    if code != 200:
        raise _refusal(path, code, reply)
    try:
        nonce = base64.b64decode(reply.get("rp-nonce", ""), validate=True)
    except ValueError as err:
        raise RpSessionError("no_session", "malformed RP-Nonce") from err
    if len(nonce) != 16:
        raise RpSessionError("no_session", f"RP-Nonce is {len(nonce)} bytes")
    return nonce


async def _async_ctrl(host: str, port: int, regist_key: str, rp_key: bytes, nonce: bytes, trace: _Trace) -> None:
    path = "/sie/ps4/rp/sess/ctrl"
    crypto = SessionCrypto(rp_key, nonce)
    request = _request(host, port, path, ctrl_headers(crypto, regist_key), "keep-alive")
    loop = asyncio.get_running_loop()
    refused_until = loop.time() + CTRL_REFUSED_WINDOW
    resent = False
    while True:
        trace("ctrl: connecting")
        try:
            reader, writer, code, reply = await _http_exchange(host, port, path, request, CTRL_REPLY_TIMEOUT)
            break
        except ConnectionRefusedError:
            # the listener isn't back yet after the init; the session the init claimed still waits for this request
            if loop.time() + CTRL_REFUSED_RETRY > refused_until:
                raise
            trace("ctrl: refused, retrying")
            await asyncio.sleep(CTRL_REFUSED_RETRY)
        except TimeoutError:
            # chiaki-ng: the console sometimes doesn't answer the first ctrl request; same request, new connection
            trace(f"ctrl: no reply within {CTRL_REPLY_TIMEOUT:.0f} s")
            if resent:
                raise
            resent = True
    trace(f"ctrl: HTTP {code}")
    try:
        if code != 200:
            raise _refusal(path, code, reply)
        if "rp-server-type" in reply:
            crypto.decrypt(base64.b64decode(reply["rp-server-type"]))  # first receive counter; not needed otherwise
        await _async_wait_for_login(reader, writer, crypto, trace)
    finally:
        await _close(writer)
        trace("ctrl: closed")


async def _async_wait_for_login(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, crypto: SessionCrypto, trace: _Trace
) -> None:
    """Read control messages until LOGIN, answer heartbeats, then hold HOLD_AFTER_LOGIN seconds."""
    loop = asyncio.get_running_loop()
    login_deadline = loop.time() + LOGIN_TIMEOUT
    hold_until: float | None = None
    while True:
        deadline = hold_until if hold_until is not None else login_deadline
        left = deadline - loop.time()
        if left <= 0:
            if hold_until is not None:
                return
            raise RpSessionError("no_login", f"no LOGIN within {LOGIN_TIMEOUT:.0f} s")
        try:
            head = await asyncio.wait_for(reader.readexactly(8), left)
            length, msg_type, _ = struct.unpack(">IHH", head)
            raw = await asyncio.wait_for(reader.readexactly(length), HTTP_TIMEOUT) if length else b""
        except TimeoutError:
            continue
        except asyncio.IncompleteReadError as err:
            if hold_until is not None:
                return  # the console closed the session after the login: fine
            raise RpSessionError("no_session", "the console closed the control channel") from err
        payload = crypto.decrypt(raw) if length else b""
        if msg_type == MSG_HEARTBEAT_REQ:
            writer.write(struct.pack(">IHH", 0, MSG_HEARTBEAT_REP, 0))  # header only = sent unencrypted
            await writer.drain()
        elif msg_type == MSG_LOGIN_PIN_REQ:
            raise RpSessionError("passcode", "this user has a login passcode")
        elif msg_type == MSG_LOGIN and hold_until is None:
            if not payload or payload[0] != 0:
                raise RpSessionError("login_failed", f"LOGIN result {payload[:1].hex() or 'empty'}")
            trace(f"LOGIN ok, holding {HOLD_AFTER_LOGIN:.0f} s")
            hold_until = loop.time() + HOLD_AFTER_LOGIN
        else:
            _LOGGER.debug("Remote Play: console message %#x, %d bytes", msg_type, length)


_NETWORK_ERRORS = (OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError)


async def async_login(
    host: str, regist_key: str, rp_key: str, *, port: int = RP_PORT, t0: float | None = None
) -> None:
    """One Remote Play session: init → pause → ctrl → wait for LOGIN → hold → close. Raises RpSessionError."""
    try:
        rp_key_bytes = bytes.fromhex(rp_key)
    except ValueError as err:
        raise RpSessionError("refused", "stored RP-Key is not hex") from err
    trace = _Trace(host, t0)
    try:
        nonce = await _async_init(host, port, regist_key, trace)
    except _NETWORK_ERRORS as err:
        raise RpSessionError("no_session", f"init: {type(err).__name__}: {err}") from err
    await asyncio.sleep(CTRL_GAP)
    try:
        await _async_ctrl(host, port, regist_key, rp_key_bytes, nonce, trace)
    except _NETWORK_ERRORS as err:
        raise RpSessionError("no_session", f"ctrl: {type(err).__name__}: {err}") from err


async def async_wait_until_on(
    host: str, timeout: float | None = None, interval: float | None = None, regist_key: str | None = None
):
    """Poll DDP SRCH until the console answers 200 (on). Returns that reply, or None at the timeout.

    With `regist_key`, a WAKEUP was just sent: resend it while DDP keeps saying rest (REWAKE_AFTER)."""
    timeout = WAIT_ON_TIMEOUT if timeout is None else timeout
    interval = WAIT_ON_INTERVAL if interval is None else interval
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last_wake = loop.time()
    while True:
        try:
            reply = await async_search(host, timeout=min(interval, 2.0))
        except OSError:
            reply = None
        if reply is not None and reply.code == 200:
            return reply
        if regist_key and reply is not None and reply.code == 620 and loop.time() - last_wake >= REWAKE_AFTER:
            _LOGGER.debug("Remote Play %s: still in rest %.0f s after the wake; sending WAKEUP again", host, REWAKE_AFTER)
            try:
                await async_wakeup(host, regist_key)
            except (OSError, ValueError) as err:
                _LOGGER.debug("Remote Play %s: WAKEUP resend failed: %s", host, err)
            last_wake = loop.time()
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(interval)


async def async_login_after_wake(host: str, regist_key: str, rp_key: str, *, port: int = RP_PORT) -> float:
    """Wait for the console to be on, then log the paired user in. Retries a refused port every RETRY_WAIT s; after
    any other failure, waits the ~30 s Remote Play lock out once and tries one last time.

    Returns the seconds it took. Raises RpSessionError (the last failure) when it can't log in.
    """
    started = time.monotonic()
    reply = await async_wait_until_on(host, regist_key=regist_key)
    if reply is None:
        raise RpSessionError("not_on", f"no DDP 200 within {WAIT_ON_TIMEOUT:.0f} s of the wake")
    if rp_version_for(reply.system_version) != RP_10:
        raise RpSessionError("rp_version", f"firmware {reply.firmware} uses an older Remote Play (FW 8+ needed)")
    on_at = time.monotonic()
    _LOGGER.debug("Remote Play %s: DDP 200 %.1f s after the wake", host, on_at - started)
    deadline = on_at + RETRY_WINDOW
    waited_lock = False
    attempt = 0
    while True:
        attempt += 1
        try:
            await async_login(host, regist_key, rp_key, port=port, t0=on_at)
            return time.monotonic() - started
        except RpSessionError as err:
            if err.reason == "not_listening" and time.monotonic() + RETRY_WAIT < deadline:
                wait = RETRY_WAIT
            elif err.reason in _LOCK_REASONS and not waited_lock:
                waited_lock = True
                wait = LOCK_WAIT
                deadline = time.monotonic() + LOCK_WAIT + RETRY_WINDOW_AFTER_LOCK
            else:
                raise
            _LOGGER.debug("Remote Play login try %d failed (%s); retrying in %.0f s", attempt, err, wait)
            await asyncio.sleep(wait)
