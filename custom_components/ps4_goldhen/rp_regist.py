"""Sony Remote Play registration ("pairing") with a PIN. No chiaki-ng needed.

Ported from playactor (dhleong/playactor @ 93ee087, ISC license, Copyright (c) Daniel Leong):
`src/remoteplay/registration.ts`, `crypto.ts`, `crypto/{base,modern,legacy}.ts`, `model.ts`. PS4 only. Transport
details (ephemeral UDP port, `SRC2` NUL-terminated, plain HTTP/1.1 POST) follow chiaki-ng's `lib/src/regist.c`, a
client known to work; no chiaki-ng code is used.

On the PS4: Settings → Remote Play Connection Settings → Add Device shows an 8-digit PIN for 300 s. Then:
1. DDP `SRCH` to UDP 987 → `system-version` picks the Remote Play version (FW ≥ 8: "10.0", 7.x: "9.0", older "8.0").
2. `SRC2` to UDP 9295, wait for `RES2`: the console rejects a registration that didn't search first.
3. HTTP POST `/sie/ps4/rp/sess/rgst` (Remote Play < 10: `/sce/rp/regist`) to TCP 9295. Body = 480 `A` bytes carrying
   the random client nonce at fixed offsets, then the record `Client-Type` + `Np-AccountId` encrypted with AES-128-CFB.
   Key = from the PIN; IV = HMAC-SHA256(nonce ‖ counter 0)[:16].
4. The 200 reply body, decrypted with the same key + IV, holds `PS4-RegistKey` (the wake key: 8 ASCII hex characters,
   hex-encoded), `RP-Key` (16 bytes, Remote Play session key), `PS4-Mac`, `PS4-Nickname`. A refusal carries
   `RP-Application-Reason`.
Only Sony's own services answer (987, 9295); no GoldHEN port is touched. Nothing secret is logged.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import logging
import os
import re
import socket

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .ddp import async_search, parse_regist_key
from .rp_keys import AERO_KEY_PS4, INIT_KEY_PS4

_LOGGER = logging.getLogger(__name__)

RP_PORT = 9295
NONCE_LENGTH = 16
PADDING_LENGTH = 480  # 0x1e0
SEARCH_TIMEOUT = 6.0
RESPONSE_TIMEOUT = 15.0

CLIENT_TYPE = "dabfa2ec873de5839bee8d3f4c0239c4282c07c25c6077a2931afcf0adc0d34f"
CLIENT_TYPE_LEGACY = "Windows"

# Remote Play protocol versions (playactor RemotePlayVersion; sent as the RP-Version header)
RP_8 = "8.0"
RP_9 = "9.0"
RP_10 = "10.0"

# The 16-byte constants below, like rp_keys.py's tables, were first published by chiaki (lib/src/rpcrypt.c, 2020-10);
# taken here from playactor, which copied them crediting chiaki. CREDITS.md has the details.
_HMAC_KEYS = {
    RP_10: bytes.fromhex("20d66f5904ea7c14e557ffc52e488ac8"),
    RP_9: bytes.fromhex("ac078883c83a1fe811463af39ee3e377"),
    RP_8: bytes.fromhex("ac078883c83a1fe811463af39ee3e377"),
}

# Remote Play < 10 ("legacy" crypto)
_LEGACY_AES_KEY = bytes.fromhex("3f1cc4b6dcbb3ecc50baedef9734c7c9")
_LEGACY_ECHO_B = bytes.fromhex("e1ec9c3addbd0885fc0e1d789032c004")

# RP-Application-Reason → error key (playactor model.ts errorCodes)
_REASONS = {
    "80108b09": "wrong_pin",  # "Registration failed, probably invalid PIN"
    "80108b02": "invalid_account",  # "Invalid PSN ID"
    "80108b10": "in_use",  # "Remote is already in use"
    "80108b15": "crash",  # "Remote Play on Console crashed"
    "80108b11": "rp_version",  # "RP-Version mismatch"
}


class RegistError(Exception):
    """Pairing failed; `reason` is a short key (no_console, resting, no_search_reply, wrong_pin, …)."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


@dataclass(frozen=True)
class Registration:
    regist_key: str = field(repr=False)  # 8 hex = what ddp.async_wakeup takes
    rp_key: str = field(repr=False)  # 32 hex, the Remote Play session key (the login after a wake)
    mac: str  # 12 hex
    nickname: str | None = None
    key_type: str | None = None


# — Input parsing ————————————————————————————————————————————————————————————————————————————————

_HEX16_RE = re.compile(r"^(?:0x)?([0-9a-fA-F]{16})$")


def parse_pin(value: str) -> str:
    """The 8-digit PIN from the Add Device screen; spaces / dashes allowed. ValueError otherwise."""
    pin = re.sub(r"[\s-]", "", value or "")
    if not re.fullmatch(r"\d{8}", pin):
        raise ValueError("PIN must be 8 digits")
    return pin


def parse_account_id(value: str) -> str:
    """Normalize an account ID to the base64 form the console expects (8 bytes, little-endian → 12 characters).

    Accepts that base64 form (chiaki-ng, psn-account-id script), 16 hex characters (Apollo Save Tool, the PS4
    SAVEDATA folder name), or the decimal number (PSN user_id). ValueError if it's none of them or zero.
    """
    raw = (value or "").strip()
    if m := _HEX16_RE.match(raw):
        number = int(m.group(1), 16)
    elif raw.isdigit():
        number = int(raw)
        if number >= 1 << 64:
            raise ValueError("account ID is larger than 64 bits")
    else:
        try:
            decoded = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as err:
            raise ValueError("account ID is not base64, hex or decimal") from err
        if len(decoded) != 8:
            raise ValueError("base64 account ID must decode to 8 bytes")
        number = int.from_bytes(decoded, "little")
    if number == 0:
        raise ValueError("account ID is zero")
    return base64.b64encode(number.to_bytes(8, "little")).decode()


def rp_version_for(system_version: str | None) -> str:
    """DDP `system-version` (e.g. `11008001` = FW 11.00) → Remote Play version. Unknown → 10.0 (any FW ≥ 8)."""
    try:
        number = int(system_version or "", 10)
    except ValueError:
        return RP_10
    if number >= 8_000_000:
        return RP_10
    if number >= 7_000_000:
        return RP_9
    return RP_8


# — Crypto ———————————————————————————————————————————————————————————————————————————————————————


@dataclass(frozen=True)
class _ModernTables:
    init_key: bytes
    aero_key: bytes
    wurzelbert: int


_PS4_TABLES = _ModernTables(INIT_KEY_PS4, AERO_KEY_PS4, 0x29)


def generate_iv(hmac_key: bytes, nonce: bytes, counter: int = 0) -> bytes:
    return hmac.new(hmac_key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()[:NONCE_LENGTH]


def modern_pin_seed(init_key: bytes, pin: int, init_key_off: int) -> bytes:
    seed = bytearray(init_key[(i * 0x20 + init_key_off) % len(init_key)] for i in range(16))
    for i, shift in enumerate((24, 16, 8, 0)):
        seed[0xC + i] ^= (pin >> shift) & 0xFF
    return bytes(seed)


def _modern_sign(tables: _ModernTables, nonce: bytes, padding: bytearray) -> None:
    off = padding[0] >> 3
    aero = bytes(((nonce[i] ^ tables.aero_key[i * 0x20 + off]) + tables.wurzelbert + i) & 0xFF for i in range(16))
    padding[0xC7:0xC7 + 8] = aero[8:16]
    padding[0x191:0x191 + 8] = aero[0:8]


def _legacy_pin_seed(pin: int) -> bytes:
    seed = bytearray(_LEGACY_AES_KEY)
    for i, shift in enumerate((24, 16, 8, 0)):
        seed[i] ^= (pin >> shift) & 0xFF
    return bytes(seed)


def _legacy_sign(nonce: bytes, padding: bytearray) -> None:
    for i in range(16):
        padding[0x11C + i] = ((nonce[i] - i - 0x29) & 0xFF) ^ _LEGACY_ECHO_B[i]


def _aes_cfb(key: bytes, iv: bytes, data: bytes, *, decrypt: bool) -> bytes:
    """AES-128-CFB (full 128-bit feedback, = Node's `aes-128-cfb`) on the plain AES block function.

    `modes.CFB` is deprecated in `cryptography` (moved to `hazmat.decrepit`, removal announced), so it isn't used.
    """
    block_cipher = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    out = bytearray()
    feedback = iv
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        result = bytes(a ^ b for a, b in zip(chunk, block_cipher.update(feedback)))
        out += result
        feedback = chunk if decrypt else result
    return bytes(out)


@dataclass(frozen=True)
class RegistCrypto:
    key: bytes
    iv: bytes
    preface: bytes

    def encrypt(self, data: bytes) -> bytes:
        return _aes_cfb(self.key, self.iv, data, decrypt=False)

    def decrypt(self, data: bytes) -> bytes:
        return _aes_cfb(self.key, self.iv, data, decrypt=True)


def regist_crypto(
    pin: str,
    nonce: bytes,
    rp_version: str = RP_10,
    *,
    tables: _ModernTables = _PS4_TABLES,
    hmac_key: bytes | None = None,
) -> RegistCrypto:
    """Key, IV and the signed 480-byte preface for one registration (playactor createCodecForPin)."""
    if len(nonce) != NONCE_LENGTH:
        raise ValueError("nonce must be 16 bytes")
    pin_number = int(pin, 10)
    padding = bytearray(b"A" * PADDING_LENGTH)
    iv = generate_iv(hmac_key or _HMAC_KEYS[rp_version], nonce)
    if rp_version == RP_10:
        key = modern_pin_seed(tables.init_key, pin_number, padding[0x18D] & 0x1F)
        _modern_sign(tables, nonce, padding)
    else:
        key = _legacy_pin_seed(pin_number)
        _legacy_sign(nonce, padding)
    return RegistCrypto(key, iv, bytes(padding))


def build_payload(crypto: RegistCrypto, account_id: str, rp_version: str = RP_10) -> bytes:
    """Preface + encrypted `Client-Type` / `Np-AccountId` record."""
    client_type = CLIENT_TYPE if rp_version == RP_10 else CLIENT_TYPE_LEGACY
    record = f"Client-Type: {client_type}\r\nNp-AccountId: {account_id}\r\n".encode()
    return crypto.preface + crypto.encrypt(record)


def parse_registration(body: bytes) -> Registration:
    """Decrypted reply body (`Key: Value` CRLF lines) → Registration. RegistError("invalid_response") if incomplete."""
    values: dict[str, str] = {}
    for line in body.decode("utf-8", errors="replace").split("\r\n"):
        key, sep, value = line.partition(":")
        if sep and value.strip():
            values[key.strip()] = value.strip()
    try:
        regist_key = parse_regist_key(bytes.fromhex(values["PS4-RegistKey"]).decode("ascii"))
        rp_key = values["RP-Key"].lower()
        mac = values["PS4-Mac"].lower()
        if len(bytes.fromhex(rp_key)) != NONCE_LENGTH or len(bytes.fromhex(mac)) != 6:
            raise ValueError
    except (KeyError, ValueError, UnicodeDecodeError) as err:
        raise RegistError("invalid_response", "missing or malformed PS4-RegistKey / RP-Key / PS4-Mac") from err
    return Registration(regist_key, rp_key, mac, values.get("PS4-Nickname"), values.get("RP-KeyType"))


# — Network ——————————————————————————————————————————————————————————————————————————————————————


class _SearchProtocol(asyncio.DatagramProtocol):
    def __init__(self, host_ip: str, found: asyncio.Future) -> None:
        self._host_ip = host_ip
        self._found = found

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] == self._host_ip and data.startswith(b"RES2") and not self._found.done():
            self._found.set_result(True)

    def error_received(self, exc: Exception) -> None:
        """ICMP errors: let the caller time out."""


async def _async_regist_search(host_ip: str, port: int, timeout: float, attempts: int = 3) -> bool:
    loop = asyncio.get_running_loop()
    found: asyncio.Future = loop.create_future()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _SearchProtocol(host_ip, found), local_addr=("0.0.0.0", 0)
    )
    try:
        for _ in range(attempts):
            transport.sendto(b"SRC2\x00", (host_ip, port))
            try:
                return await asyncio.wait_for(asyncio.shield(found), timeout / attempts)
            except TimeoutError:
                continue
        return False
    finally:
        transport.close()


async def _async_post(host_ip: str, port: int, path: str, rp_version: str, body: bytes, timeout: float) -> bytes:
    """One HTTP/1.1 POST; returns the 200 body. RegistError on a refusal or a malformed reply."""
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"HOST: {host_ip}\r\n"
        "User-Agent: remoteplay Windows\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"RP-Version: {rp_version}\r\n"
        "\r\n"
    ).encode() + body

    async def exchange() -> tuple[int, dict[str, str], bytes]:
        reader, writer = await asyncio.open_connection(host_ip, port)
        try:
            writer.write(request)
            await writer.drain()
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            parts = lines[0].split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                raise RegistError("invalid_response", "not an HTTP reply")
            code = int(parts[1])
            headers = {}
            for line in lines[1:]:
                key, sep, value = line.partition(":")
                if sep:
                    headers[key.strip().lower()] = value.strip()
            length = int(headers.get("content-length") or 0)
            payload = await reader.readexactly(length) if length else b""
            return code, headers, payload
        finally:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), timeout=1)
            except (OSError, TimeoutError):
                pass

    try:
        code, headers, payload = await asyncio.wait_for(exchange(), timeout)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as err:
        raise RegistError("invalid_response", type(err).__name__) from err
    except TimeoutError as err:
        raise RegistError("no_reply", "registration request timed out") from err
    if code != 200:
        reason = headers.get("rp-application-reason", "").lower().removeprefix("0x")
        raise RegistError(_REASONS.get(reason, "failed"), f"HTTP {code}, RP-Application-Reason {reason or 'none'}")
    if not payload:
        raise RegistError("invalid_response", "empty registration reply")
    return payload


async def async_register(
    host: str,
    pin: str,
    account_id: str,
    *,
    ddp_port: int = 987,
    rp_port: int = RP_PORT,
    search_timeout: float = SEARCH_TIMEOUT,
    response_timeout: float = RESPONSE_TIMEOUT,
    nonce: bytes | None = None,
) -> Registration:
    """Pair with the PS4 at `host` (it must be on, showing Add Device). Raises RegistError or OSError."""
    pin = parse_pin(pin)
    account_id = parse_account_id(account_id)
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, rp_port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    host_ip = infos[0][4][0]

    status = await async_search(host_ip, port=ddp_port)
    if status is None:
        raise RegistError("no_console", "no DDP reply")
    if status.code == 620:
        raise RegistError("resting", "console is in rest mode")
    rp_version = rp_version_for(status.system_version)
    _LOGGER.debug("Pairing: system-version %s → RP-Version %s", status.system_version, rp_version)

    if not await _async_regist_search(host_ip, rp_port, search_timeout):
        raise RegistError("no_search_reply", "no RES2 on UDP 9295")
    await asyncio.sleep(0.1)  # playactor: some consoles don't accept the request right after RES2

    crypto = regist_crypto(pin, nonce or os.urandom(NONCE_LENGTH), rp_version)
    path = "/sie/ps4/rp/sess/rgst" if rp_version == RP_10 else "/sce/rp/regist"
    payload = await _async_post(host_ip, rp_port, path, rp_version, build_payload(crypto, account_id, rp_version), response_timeout)
    return parse_registration(crypto.decrypt(payload))
