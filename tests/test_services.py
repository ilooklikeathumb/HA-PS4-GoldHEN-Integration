"""The `send_payload` and `close_title` actions: admin-only, which file, which console and port; no PS4 access required."""
import asyncio
import contextlib
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_issue1_connection import load_integration  # noqa: E402

CONSOLES = [{"ps4_host": "192.0.2.1", "binloader_port": 9090}, {"ps4_host": "192.0.2.2", "binloader_port": 9091}]


class ServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.integration, _flow, _transport = load_integration()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.payloads = os.path.join(self.tmp.name, "payloads")
        os.mkdir(self.payloads)
        os.mkdir(os.path.join(self.payloads, "sub"))
        for name in ("info.elf", os.path.join("sub", "info.elf")):
            Path(self.payloads, name).write_bytes(b"\x7fELF")
        Path(self.tmp.name, "secrets.yaml").write_text("x")
        os.symlink(os.path.join(self.tmp.name, "secrets.yaml"), os.path.join(self.payloads, "link.bin"))
        self.entries = [SimpleNamespace(entry_id=f"e{i}", data=data) for i, data in enumerate(CONSOLES)]
        self.admin, self.plain = {}, Mock()

    async def asyncSetUp(self):
        await self.setup_entry()
        self.sent = AsyncMock()
        for patcher in (patch.object(self.integration, "_send_bin_tcp", self.sent),
                        patch.object(self.integration, "PAYLOAD_DIR", self.payloads)):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def setup_entry(self):
        """async_setup_entry with HA's admin helper stubbed; the feature hooks have their own suites."""
        hass = SimpleNamespace(data={},
            config_entries=SimpleNamespace(async_forward_entry_setups=AsyncMock(),
                                           async_entries=lambda domain: self.entries),
            services=SimpleNamespace(has_service=lambda domain, name: name in self.admin, async_register=self.plain))
        for key in (state := self.integration._global(hass)):
            state[key] = True
        entry = SimpleNamespace(entry_id="e0", data=dict(CONSOLES[0]),
            async_create_background_task=lambda hass, coro, *, name: coro.close(),
            async_on_unload=lambda callback: None, add_update_listener=lambda fn: lambda: None)
        helper = ModuleType("homeassistant.helpers.service")
        helper.async_register_admin_service = lambda hass, domain, name, fn, schema: self.admin.setdefault(name, fn)
        features = ("console_info", "live_sensors", "screenshots", "play_time", "game_session", "save_backup")
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"homeassistant.helpers.service": helper}))
            for name in features:
                stack.enter_context(patch.object(getattr(self.integration, name), "async_setup", autospec=True))
            stack.enter_context(patch.object(self.integration.fan, "async_load", autospec=True))
            self.assertTrue(await asyncio.wait_for(self.integration.async_setup_entry(hass, entry), 1))
        self.hass = hass

    async def send(self, **data):
        await self.admin["send_payload"](SimpleNamespace(data={"timeout": 30, **data}))

    def test_both_are_registered_admin_only(self):
        # HA's admin wrapper raises Unauthorized for a non-admin user's call (WebSocket, REST, a script they start)
        self.assertEqual(set(self.admin), {"send_payload", "close_title"})
        self.plain.assert_not_called()

    async def test_registered_once(self):
        first = dict(self.admin)
        await self.setup_entry()
        self.assertEqual(self.admin, first)

    async def test_file_name_goes_to_the_named_console_on_its_port(self):
        await self.send(payload_file="info.elf", ps4_host="192.0.2.2")
        self.sent.assert_awaited_once_with("192.0.2.2", 9091, os.path.join(self.payloads, "info.elf"), 30)

    async def test_binloader_port_in_the_call_is_used(self):
        await self.send(payload_file="info.elf", ps4_host="192.0.2.2", binloader_port=9999)
        self.sent.assert_awaited_once_with("192.0.2.2", 9999, os.path.join(self.payloads, "info.elf"), 30)

    async def test_full_path_to_a_file_in_the_payload_folder_is_accepted(self):
        for name in (os.path.join(self.payloads, "info.elf"), os.path.join(self.payloads, ".", "info.elf")):
            with self.subTest(payload_file=name):
                self.sent.reset_mock()
                await self.send(payload_file=name, ps4_host="192.0.2.1")
                self.sent.assert_awaited_once_with("192.0.2.1", 9090, os.path.join(self.payloads, "info.elf"), 30)

    async def test_paths_outside_the_payload_folder_are_refused(self):
        # before: only the base name was used, so sub/info.elf or /elsewhere/info.elf sent payloads/info.elf
        for name in (os.path.join(self.tmp.name, "secrets.yaml"), "../secrets.yaml", "sub/info.elf",
                     os.path.join(self.payloads, "sub", "info.elf"), os.path.join(self.tmp.name, "info.elf"),
                     "../payloads/info.elf", "..", ".", "", "link.bin", os.path.join(self.payloads, "link.bin"),
                     os.path.join(self.payloads, "..", "secrets.yaml"), self.payloads, "/"):
            with self.subTest(payload_file=name):
                with self.assertRaisesRegex(RuntimeError, "must be a file name in"):
                    await self.send(payload_file=name, ps4_host="192.0.2.1")
        self.sent.assert_not_awaited()

    async def test_host_refusals_send_nothing(self):
        for host, message in (("192.0.2.99", "not a PS4 set up"), (None, "Several PS4s")):
            with self.subTest(host=host):
                with self.assertRaisesRegex(RuntimeError, message):
                    await self.send(payload_file="info.elf", **({"ps4_host": host} if host else {}))
        self.sent.assert_not_awaited()

    async def test_close_title_needs_a_console_of_this_integration(self):
        close = self.admin["close_title"]
        with self.assertRaisesRegex(RuntimeError, "not a PS4 set up"):
            await close(SimpleNamespace(data={"title_id": "CUSA00001", "ps4_host": "192.0.2.99"}))
        with self.assertRaisesRegex(RuntimeError, "isn't loaded"):
            await close(SimpleNamespace(data={"title_id": "CUSA00001", "ps4_host": "192.0.2.2"}))


if __name__ == "__main__":
    unittest.main()
