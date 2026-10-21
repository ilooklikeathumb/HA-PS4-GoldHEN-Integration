"""Sony Device Discovery Protocol (DDP) wake, UDP 987.

The PS4's standby stack answers DDP while the console rests, so one `WAKEUP` datagram turns it on — the same packet
chiaki-ng's Wake button sends. Its `user-credential` is the console's Remote Play registration key ("regist key":
8 hex characters, issued when a Remote Play client pairs) parsed as hex and sent as a decimal number. No reply comes
back; the caller sees the wake as the console's power state turning on. A fully-off PS4 has its network down and
can't be woken this way.

`async_search` sends `SRCH`, which the console answers itself — `200` on, `620` rest — with its
`system-version`; Remote Play pairing needs that to pick the protocol version, and the device page shows it as the
firmware.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import socket

DDP_PORT = 987
DDP_PROTOCOL_VERSION = "00020020"  # PS4
_SEARCH_LOCAL_PORTS = range(9303, 9320)  # chiaki-ng's discovery source ports; 0 = any free port

# chiaki-ng's settings export stores the key as `rp_regist_key=@ByteArray(1a2b3c4d\0\0\0\0\0\0\0\0)`
_BYTEARRAY_RE = re.compile(r"^@ByteArray\((.*)\)$", re.DOTALL)
_KEY_RE = re.compile(r"^[0-9a-fA-F]{8}$")
_SYSTEM_VERSION_RE = re.compile(r"^(\d{2})(\d{2})\d{4}$")


def firmware_of(system_version: str | None) -> str | None:
    """DDP `system-version` → firmware: `12008001` → `12.00`, `07550001` → `7.55`; None if it isn't one.

    HA core's ps4 integration slices `[1:4]`, which reads `11008001` as 1.00 — wrong from FW 10 on.
    """
    m = _SYSTEM_VERSION_RE.match(system_version or "")
    return f"{int(m.group(1))}.{m.group(2)}" if m else None


def parse_regist_key(value: str) -> str:
    """Normalize a pasted regist key to 8 lowercase hex characters; ValueError if it isn't one.

    Accepts the bare key or chiaki-ng's `@ByteArray(...)` form (NUL padding as `\\0` escapes or real NULs).
    """
    key = value.strip().strip('"')
    m = _BYTEARRAY_RE.match(key)
    if m:
        key = m.group(1)
    key = key.split("\\0", 1)[0].split("\x00", 1)[0].strip()
    if not _KEY_RE.match(key):
        raise ValueError("regist key must be 8 hex characters")
    return key.lower()


def wakeup_packet(regist_key: str) -> bytes:
    """The WAKEUP datagram, NUL-terminated like chiaki-ng's."""
    credential = int(parse_regist_key(regist_key), 16)
    return (
        "WAKEUP * HTTP/1.1\n"
        "client-type:vr\n"
        "auth-type:R\n"
        "model:w\n"
        "app-type:r\n"
        f"user-credential:{credential}\n"
        f"device-discovery-protocol-version:{DDP_PROTOCOL_VERSION}\n"
    ).encode() + b"\x00"


async def async_wakeup(host: str, regist_key: str, port: int = DDP_PORT) -> None:
    """Send one WAKEUP datagram to the console. Raises OSError if the host can't be resolved or sent to."""
    packet = wakeup_packet(regist_key)
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    transport, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, family=socket.AF_INET)
    try:
        transport.sendto(packet, infos[0][4])
    finally:
        transport.close()


# — SRCH (status + system version) ————————————————————————————————————————————————


@dataclass(frozen=True)
class DDPReply:
    code: int  # 200 = on, 620 = rest
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def system_version(self) -> str | None:
        return self.headers.get("system-version") or None

    @property
    def firmware(self) -> str | None:
        return firmware_of(self.system_version)


def parse_reply(data: bytes) -> DDPReply | None:
    """`HTTP/1.1 <code> <text>` then `key:value` lines; None if it isn't a DDP reply."""
    lines = data.rstrip(b"\x00").decode("utf-8", errors="replace").splitlines()
    if not lines or not lines[0].startswith("HTTP/1.1 "):
        return None
    try:
        code = int(lines[0].split(" ", 2)[1])
    except (IndexError, ValueError):
        return None
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return DDPReply(code, headers)


class _ReplyProtocol(asyncio.DatagramProtocol):
    def __init__(self, host_ip: str, reply: asyncio.Future) -> None:
        self._host_ip = host_ip
        self._reply = reply

    def datagram_received(self, data: bytes, addr) -> None:
        if addr[0] != self._host_ip or self._reply.done():
            return
        if (parsed := parse_reply(data)) is not None:
            self._reply.set_result(parsed)

    def error_received(self, exc: Exception) -> None:
        """ICMP errors (host unreachable): let the caller time out."""


async def async_search(host: str, timeout: float = 3.0, attempts: int = 2, port: int = DDP_PORT) -> DDPReply | None:
    """Send `SRCH` up to `attempts` times within `timeout` s; the first reply wins, None = no reply.

    The PS4 answers from another port, so the socket stays unconnected and filters on the host's address.
    """
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM)
    host_ip = infos[0][4][0]
    reply: asyncio.Future = loop.create_future()
    packet = f"SRCH * HTTP/1.1\ndevice-discovery-protocol-version:{DDP_PROTOCOL_VERSION}\n".encode() + b"\x00"

    transport = None
    for local_port in (*_SEARCH_LOCAL_PORTS, 0):
        try:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ReplyProtocol(host_ip, reply), local_addr=("0.0.0.0", local_port)
            )
            break
        except OSError:
            if local_port == 0:
                raise
    try:
        for _ in range(attempts):
            transport.sendto(packet, (host_ip, port))
            try:
                return await asyncio.wait_for(asyncio.shield(reply), timeout / attempts)
            except TimeoutError:
                continue
        return None
    finally:
        transport.close()
