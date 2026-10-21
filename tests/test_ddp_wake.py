"""DDP WAKEUP packet + regist key parsing (ddp.py has no HA imports)."""
import asyncio
import importlib.util
from pathlib import Path
import socket
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("ps4_ddp_wake", ROOT / "custom_components/ps4_goldhen/ddp.py")
ddp = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = ddp  # dataclasses (DDPReply) look their module up
_spec.loader.exec_module(ddp)

# a made-up key, never a real console's
KEY = "1a2b3c4d"


class ParseRegistKeyTests(unittest.TestCase):
    def test_accepted_forms(self):
        for raw in (
            KEY,
            "1A2B3C4D",
            f"  {KEY}\n",
            f'"{KEY}"',
            f"@ByteArray({KEY}\\0\\0\\0\\0\\0\\0\\0\\0)",  # chiaki-ng settings export, \0 escapes as text
            f"@ByteArray({KEY}" + "\x00" * 8 + ")",      # real NULs
            KEY + "\x00" * 8,
        ):
            with self.subTest(raw=raw):
                self.assertEqual(ddp.parse_regist_key(raw), KEY)

    def test_rejected(self):
        for raw in ("", "1a2b3c4", "1a2b3c4d5", "1a2b3c4g", "@ByteArray()", "0x1a2b3c4d", "1a2b 3c4d"):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    ddp.parse_regist_key(raw)


class WakeupPacketTests(unittest.TestCase):
    def test_exact_bytes(self):
        # credential = key read as hex, sent as decimal (0x1a2b3c4d = 439041101)
        self.assertEqual(
            ddp.wakeup_packet(KEY),
            b"WAKEUP * HTTP/1.1\n"
            b"client-type:vr\n"
            b"auth-type:R\n"
            b"model:w\n"
            b"app-type:r\n"
            b"user-credential:439041101\n"
            b"device-discovery-protocol-version:00020020\n"
            b"\x00",
        )

    def test_max_and_leading_zero_keys(self):
        self.assertIn(b"user-credential:4294967295\n", ddp.wakeup_packet("ffffffff"))
        self.assertIn(b"user-credential:10\n", ddp.wakeup_packet("0000000a"))

    def test_bad_key_raises(self):
        with self.assertRaises(ValueError):
            ddp.wakeup_packet("nope")


class AsyncWakeupTests(unittest.TestCase):
    def test_sends_one_datagram_to_the_console(self):
        rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rx.bind(("127.0.0.1", 0))
        rx.settimeout(2)
        try:
            asyncio.run(ddp.async_wakeup("127.0.0.1", f"@ByteArray({KEY}\\0\\0)", port=rx.getsockname()[1]))
            data, _ = rx.recvfrom(1024)
        finally:
            rx.close()
        self.assertEqual(data, ddp.wakeup_packet(KEY))

    def test_bad_key_sends_nothing(self):
        with self.assertRaises(ValueError):
            asyncio.run(ddp.async_wakeup("127.0.0.1", "nope", port=9))


if __name__ == "__main__":
    unittest.main()
