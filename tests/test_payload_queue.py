"""Payload sends (`_send_bin_bytes` in __init__.py): one at a time per console, a gap between two sends to the same
console, none between consoles. Sends go to loopback TCP servers standing in for BinLoader; no PS4 access required."""
import ast
import asyncio
import logging
from pathlib import Path
import socket
import unittest

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components/ps4_goldhen/__init__.py"
NAMES = {"_PAYLOAD_GAP", "_payload_queues", "_payload_last"}
GAP = 0.2  # s; the module's own gap, shortened for the tests


class HomeAssistantError(Exception):
    pass


def load():
    """_send_bin_bytes and its three module-level names, run in their own namespace."""
    keep = [node for node in ast.parse(INIT.read_text()).body
            if (isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_bin_bytes")
            or (isinstance(node, (ast.Assign, ast.AnnAssign))
                and {getattr(t, "id", "") for t in (node.targets if isinstance(node, ast.Assign) else [node.target])}
                & NAMES)]
    ns = {"asyncio": asyncio, "_LOGGER": logging.getLogger("payload-queue-test"),
          "HomeAssistantError": HomeAssistantError}
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    ns["_PAYLOAD_GAP"] = GAP
    return ns


class BinLoader:
    """Records each connection: when it opened, when the sender closed it, what it carried."""

    def __init__(self):
        self.received = []

    async def handle(self, reader, writer):
        loop = asyncio.get_running_loop()
        opened = loop.time()
        body = await reader.read()
        self.received.append((opened, loop.time(), body))
        writer.close()

    async def start(self, host):
        self.server = await asyncio.start_server(self.handle, host, 0)
        return self.server.sockets[0].getsockname()[1]


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PayloadQueueTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = load()
        self.send = self.ns["_send_bin_bytes"]

    async def settle(self, loader, count):
        for _ in range(200):
            if len(loader.received) >= count:
                return
            await asyncio.sleep(0.01)

    async def test_sends_to_one_console_go_one_at_a_time_in_order_with_the_gap(self):
        loader = BinLoader()
        port = await loader.start("127.0.0.1")
        await asyncio.gather(*(self.send("127.0.0.1", port, str(n).encode(), f"p{n}.bin") for n in (1, 2, 3)))
        await self.settle(loader, 3)
        loader.server.close()
        self.assertEqual([body for *_, body in loader.received], [b"1", b"2", b"3"], "first come, first sent")
        for (_, ended, _), (opened, _, _) in zip(loader.received, loader.received[1:]):
            self.assertGreaterEqual(opened - ended, GAP * 0.9, "the next send waits the gap")

    async def test_two_consoles_do_not_wait_for_each_other(self):
        a, b = BinLoader(), BinLoader()
        port_a, port_b = await a.start("127.0.0.1"), await b.start("127.0.0.2")
        loop = asyncio.get_running_loop()
        await self.send("127.0.0.1", port_a, b"a", "a.bin")
        started = loop.time()
        await self.send("127.0.0.2", port_b, b"b", "b.bin")
        self.assertLess(loop.time() - started, GAP / 2, "another console's last send doesn't delay this one")
        await self.settle(b, 1)
        a.server.close(); b.server.close()
        self.assertEqual([body for *_, body in b.received], [b"b"])

    async def test_a_failed_send_still_counts_for_the_gap(self):
        loader = BinLoader()
        port = await loader.start("127.0.0.1")
        with self.assertRaises(HomeAssistantError):
            await self.send("127.0.0.1", free_port(), b"x", "x.bin")  # nothing listens there
        failed_at = asyncio.get_running_loop().time()
        await self.send("127.0.0.1", port, b"ok", "ok.bin")
        await self.settle(loader, 1)
        loader.server.close()
        self.assertGreaterEqual(loader.received[0][0] - failed_at, GAP * 0.9)
        self.assertFalse(self.ns["_payload_queues"]["127.0.0.1"].locked(), "the queue is free again")


if __name__ == "__main__":
    unittest.main(verbosity=2)
