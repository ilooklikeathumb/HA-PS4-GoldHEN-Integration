"""PS4 save backup to /share: settings (Manual by default, no reload), device-page entities that come
and go, manual passes (Automatic was removed: old settings load as Manual, its switch goes), open games never copied, the
torn-copy guard, the storage limit, Keep, zips, Send to PS4 (Apollo's Fake USB folder) and the panel's list.

Runs the real save_backup.py + save_files.py + game_session.py against minimal HA stand-ins and the fake console from
test_save_files.py (a 12.00 Pro's user).
"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_save_backup_test_pkg"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capture_files import FakeFTP  # noqa: E402
from test_save_files import (OTHER, GAME_C, UID, WritableFTP, add_save_db, epoch, save_console,  # noqa: E402
                             write_save)

_SAVED: dict = {}  # sys.modules entries replaced while save_backup.py imports; put back after (other tests' stubs)
NOW = datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc)


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    _SAVED.setdefault(name, sys.modules.get(name))
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


class Store:
    saved: dict = {}

    def __init__(self, hass, version, key, private=False):
        self.key = key

    async def async_load(self):
        return copy.deepcopy(Store.saved.get(self.key))

    async def async_save(self, data):
        Store.saved[self.key] = copy.deepcopy(data)

    async def async_remove(self):
        Store.saved.pop(self.key, None)


class EntityRegistry:
    def __init__(self):
        self.entities, self.removed, self.by_unique = {}, [], {}

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.by_unique.get((domain, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.entities.pop(entity_id, None)


class DeviceRegistry:
    def __init__(self):
        self.names = {}

    def entries_for(self, entry_id):
        if entry_id not in self.names:
            return []
        name, by_user = self.names[entry_id]
        return [types.SimpleNamespace(identifiers={("ps4_goldhen", entry_id)}, name=name, name_by_user=by_user)]


ENTITY_REG, DEVICE_REG = EntityRegistry(), DeviceRegistry()
LATER, TICKS, SENT = [], [], []
CONNECTED: dict = {}


def _connect(hass, sig, fn):
    CONNECTED.setdefault(sig, []).append(fn)
    return lambda: CONNECTED[sig].remove(fn)


def _send(hass, sig, *args):
    SENT.append(sig)
    for fn in list(CONNECTED.get(sig, [])):
        fn(*args)


def _call_later(hass, delay, action):
    LATER.append((delay, action))
    return lambda: LATER.remove((delay, action)) if (delay, action) in LATER else None


def _track_interval(hass, action, interval):
    TICKS.append((interval, action))
    return lambda: None


class _Entity:
    """Like HA's Entity: self.hass is None until the platform adds it."""
    hass = None
    writes = 0

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, fn):
        pass

    def async_write_ha_state(self):
        self.writes += 1


def _passthrough(*_a, **_k):
    return lambda fn: fn


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.websocket_api", websocket_command=_passthrough, require_admin=lambda f: f,
        async_response=lambda f: f, async_register_command=lambda hass, cmd: None, ActiveConnection=object)
_module("homeassistant.components.http", HomeAssistantView=object)
_module("homeassistant.components.sensor", SensorEntity=_Entity,
        SensorDeviceClass=types.SimpleNamespace(ENUM="enum", TIMESTAMP="timestamp"))
_module("homeassistant.components.button", ButtonEntity=_Entity)
_module("homeassistant.components.switch", SwitchEntity=_Entity)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_registry", async_get=lambda hass: ENTITY_REG)
_module("homeassistant.helpers.device_registry", DeviceInfo=dict, async_get=lambda hass: DEVICE_REG,
        async_entries_for_config_entry=lambda registry, entry_id: registry.entries_for(entry_id))
_module("homeassistant.helpers.dispatcher", async_dispatcher_send=_send, async_dispatcher_connect=_connect)
_module("homeassistant.helpers.event", async_call_later=_call_later, async_track_time_interval=_track_interval)
_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: NOW, now=lambda: NOW, get_default_time_zone=lambda: timezone.utc,
        parse_datetime=lambda s: datetime.fromisoformat(s.replace("Z", "+00:00")))
_module("aiohttp")
_module("aiohttp.web", Response=object, FileResponse=object, Request=object, StreamResponse=object)
sys.modules["aiohttp"].web = sys.modules["aiohttp.web"]


class _Vol:
    def __getattr__(self, name):
        return lambda *a, **k: (name, a[0] if a else None)


_SAVED.setdefault("voluptuous", sys.modules.get("voluptuous"))
sys.modules["voluptuous"] = _Vol()

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
sb = importlib.import_module(f"{PKG}.save_backup")
sf = importlib.import_module(f"{PKG}.save_files")
cf = importlib.import_module(f"{PKG}.capture_files")
gs = importlib.import_module(f"{PKG}.game_session")
const = importlib.import_module(f"{PKG}.const")
ents = importlib.import_module(f"{PKG}.save_backup_entities")
for _name, _old in _SAVED.items():
    if _old is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _old

LATEST = "Latest/PS4/SAVEDATA/0000000000000000"
OLDER = "2026-10-16 014221"  # Game C SAVEDATAPlayer's first copy, in History after PanelActions' setup


def item(title, slot=None, stamp=None):
    """One ticked thing in the panel: a game, a save's newest copy, or an older version."""
    return {"title_id": title, **({"slot": slot} if slot else {}), **({"stamp": stamp} if stamp else {})}


class Hass:
    def __init__(self, tmp: Path):
        self.data = {}
        self.config = types.SimpleNamespace(path=lambda *p: str(tmp.joinpath("config", *p)))
        self.http = types.SimpleNamespace(register_view=lambda view: None)

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)

    def async_create_task(self, coro):
        return asyncio.ensure_future(coro)


class Entry:
    def __init__(self, entry_id):
        self.entry_id = entry_id
        self.data = {}  # an older entry: no console name (ps4_device_info reads it)
        self.unloads, self.tasks = [], []

    def async_on_unload(self, fn):
        self.unloads.append(fn)

    def async_create_background_task(self, hass, coro, name):
        task = asyncio.ensure_future(coro)
        self.tasks.append(task)
        return task


class Coordinator:
    def __init__(self):
        self.data, self.listeners = {"power_state": "on"}, []

    def async_add_listener(self, fn):
        self.listeners.append(fn)
        return lambda: self.listeners.remove(fn)

    def push(self, **data):
        self.data = {**self.data, **data}
        for fn in list(self.listeners):
            fn()


class FakeEntity:
    def __init__(self, kind):
        self.kind, self.entity_id, self.hass = kind, None, None


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        Store.saved.clear()
        ENTITY_REG.entities.clear()
        ENTITY_REG.removed.clear()
        ENTITY_REG.by_unique.clear()
        DEVICE_REG.names = {"console_a": ("PS4", "PS4 PRO A"), "console_b": ("PS4", "PS4 PRO B")}
        for registry in (LATER, TICKS, SENT):
            registry.clear()
        CONNECTED.clear()
        self._orig = (sb.SHARE_ROOT, sf.disk_room, cf.connect)
        sb.SHARE_ROOT = tmp / "share"
        sb.SHARE_ROOT.mkdir()
        self.room = 10 << 30
        sf.disk_room = lambda path, floor: self.room
        self.console = save_console()
        self.ftp_sessions = []
        self.ftp_class = FakeFTP

        def _connect_ftp(host, port, **kw):
            ftp = self.ftp_class(self.console)
            self.ftp_sessions.append(ftp)
            return ftp

        cf.connect = _connect_ftp
        self.hass = Hass(tmp)
        self.root = sb.SHARE_ROOT / "PS4 Saves"
        self.entry, self.coordinator = await self.add_console("console_a")

    async def asyncTearDown(self):
        sb.SHARE_ROOT, sf.disk_room, cf.connect = self._orig
        self.tmp.cleanup()

    async def add_console(self, entry_id):
        entry = Entry(entry_id)
        coordinator = Coordinator()
        self.hass.data.setdefault(const.DOMAIN, {})[entry_id] = {
            "host": "192.0.2.121", "ftp_port": 2121, "coordinator": coordinator,
            "game_map": {GAME_C: {"name": "Game C"}},
        }
        gs.async_setup(self.hass, entry, coordinator)
        await sb.async_setup(self.hass, entry, coordinator)
        self.added = getattr(self, "added", {})
        for platform in ("button", "sensor"):
            def _add(entities, platform=platform, entry_id=entry_id):
                for i, entity in enumerate(entities):
                    entity.entity_id = f"{platform}.{entry_id}_{i}"
                    ENTITY_REG.entities[entity.entity_id] = entity
            sb.register_platform(self.hass, entry, platform, _add,
                                 lambda platform=platform: [FakeEntity(platform) for _ in range(2 if platform == "sensor" else 1)])
        return entry, coordinator

    def rt(self, entry_id="console_a"):
        return sb.runtime(self.hass, entry_id)

    @property
    def user(self):
        return self.root / "PS4 PRO A" / "player1"

    async def turn_on(self, mode=sb.MODE_MANUAL, **changes):
        await sb.async_set_settings(self.hass, "console_a", {"mode": mode, **changes})

    async def drain(self, entry=None):
        for _ in range(3):
            await asyncio.sleep(0)
        tasks = (entry or self.entry).tasks
        while tasks:
            await tasks.pop(0)

    async def back_up(self, title=None, slot=None):
        """Everything, one game, or one save (the PS4 pane's ticked saves)."""
        items = [{"title_id": title, **({"slot": slot} if slot else {})}] if title else None
        await sb.async_start_backup(self.hass, "console_a", UID if items else None, items)
        await self.drain()

    def files(self, base=None):
        base = base or self.user
        return sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file()) if base.exists() else []


class Settings(Base):
    async def test_manual_by_default_folder_pinned_entities_no_ftp(self):
        rt = self.rt()
        self.assertEqual(rt.settings["mode"], sb.MODE_MANUAL)
        self.assertEqual(rt.settings["limit_gb"], 5.0)
        self.assertEqual(rt.settings["folder"], "PS4 PRO A")
        self.assertEqual(sb.console_dir(self.hass, rt), self.root / "PS4 PRO A")
        self.assertEqual(Store.saved[f"{const.DOMAIN}.saves.console_a"]["folder"], "PS4 PRO A", "pinned on the first start")
        self.assertEqual(sorted(ENTITY_REG.entities),
                         ["button.console_a_0", "sensor.console_a_0", "sensor.console_a_1"])
        self.assertEqual((self.ftp_sessions, LATER), ([], []), "Manual: nothing runs by itself")

    async def test_off_removes_the_entities_and_refuses_actions(self):
        rt = self.rt()
        await sb.async_set_settings(self.hass, "console_a", {"mode": sb.MODE_OFF})
        self.assertEqual(ENTITY_REG.entities, {})
        self.assertEqual(rt.settings["folder"], "PS4 PRO A", "the folder stays pinned")
        for action in (sb.async_list(self.hass, "console_a"), sb.async_start_backup(self.hass, "console_a"),
                       sb.async_send(self.hass, "console_a", UID)):
            with self.assertRaises(HomeAssistantError):
                await action
        self.assertEqual(self.ftp_sessions, [])
        await sb.async_set_settings(self.hass, "console_a", {"mode": sb.MODE_MANUAL})
        self.assertEqual(len(ENTITY_REG.entities), 3)

    async def test_a_saved_off_stays_off(self):
        Store.saved[f"{const.DOMAIN}.saves.console_b"] = {"mode": sb.MODE_OFF}
        await self.add_console("console_b")
        self.assertEqual((self.rt("console_b").settings["mode"], self.rt("console_b").settings["folder"]),
                         (sb.MODE_OFF, None))
        self.assertFalse(any(e.startswith(("button.console_b", "sensor.console_b")) for e in ENTITY_REG.entities))

    async def test_a_saved_automatic_loads_as_manual_and_its_switch_goes(self):
        Store.saved[f"{const.DOMAIN}.saves.console_b"] = {"mode": "automatic", "pending": [GAME_C], "folder": "PS4 PRO B"}
        ENTITY_REG.by_unique[("switch", "ps4_goldhen_console_b_automatic_save_backup")] = "switch.console_b_automatic"
        await self.add_console("console_b")
        rt = self.rt("console_b")
        self.assertEqual(rt.settings["mode"], sb.MODE_MANUAL)
        self.assertNotIn("pending", rt.settings)
        self.assertIn("switch.console_b_automatic", ENTITY_REG.removed)
        self.assertNotIn("pending", sb.settings_view(self.hass, rt))
        self.coordinator.push(power_state="rest")
        self.coordinator.push(power_state="on")
        self.assertEqual((LATER, self.ftp_sessions), ([], []), "power-on starts nothing")
        self.assertEqual(sb.MODES, (sb.MODE_OFF, sb.MODE_MANUAL))

    async def test_two_consoles_with_the_same_name_get_their_own_folders(self):
        DEVICE_REG.names["console_b"] = ("PS4", "PS4 PRO A")
        entry, _ = await self.add_console("console_b")
        await self.turn_on()
        await sb.async_set_settings(self.hass, "console_b", {"mode": sb.MODE_MANUAL})
        self.assertEqual(self.rt("console_b").settings["folder"], "PS4 PRO A (2)")

    async def test_removing_the_entry_keeps_the_backups(self):
        await self.turn_on()
        await self.back_up()
        zipped = await sb.async_zip(self.hass, "console_a", UID)
        await sb.async_remove(self.hass, self.entry)
        self.assertNotIn(f"{const.DOMAIN}.saves.console_a", Store.saved)
        self.assertFalse((sb.downloads_dir(self.hass, "console_a") / zipped["name"]).exists())
        self.assertTrue((self.user / LATEST / GAME_C / "SAVEDATAPlayer").is_file())


class ManualBackup(Base):
    async def test_first_backup_copies_every_slot_in_apollos_layout(self):
        await self.turn_on()
        await self.back_up()
        self.assertEqual(self.files(), [
            f"{LATEST}/{GAME_C}/SAVEDATAPlayer", f"{LATEST}/{GAME_C}/SAVEDATAPlayer.bin",
            f"{LATEST}/{GAME_C}/sce_sdmemory", f"{LATEST}/{GAME_C}/sce_sdmemory.bin",
            f"{LATEST}/{OTHER}/SAVEDATA01", f"{LATEST}/{OTHER}/SAVEDATA01.bin",
        ])
        rt = self.rt()
        self.assertEqual(rt.settings["users"], {UID: "player1"})
        self.assertEqual(rt.settings["last"]["state"], sb.STATUS_UP_TO_DATE)
        self.assertEqual((rt.settings["last"]["checked"], rt.settings["last"]["copied"]), (3, 3))
        self.assertEqual(rt.settings["last_success"], NOW.isoformat())
        self.assertEqual(sb.status(rt), sb.STATUS_UP_TO_DATE)
        self.assertEqual(rt.usage, {"bytes": 396 + 1096 + 736, "versions": 0})
        self.assertFalse(rt.op_lock.locked())
        self.assertFalse((self.root / "PS4 PRO A" / sf.STAGING).exists())

    async def test_storage_used_grows_during_the_pass(self):
        # a long pass: the panel's "X of N GB" grows as copies land, not only at the end
        await self.turn_on()
        seen = []
        rt = self.rt()
        CONNECTED.setdefault(sb.signal("console_a"), []).append(
            lambda *a: seen.append((rt.running, rt.progress["done"], rt.usage["bytes"])))
        await self.back_up()
        mid = [(done, used) for running, done, used in seen if running and done]
        self.assertEqual(mid, [(2, 396 + 1096), (3, 396 + 1096 + 736)])  # one batch per game

    async def test_nothing_changed_means_listings_only(self):
        await self.turn_on()
        await self.back_up()
        before = len(self.ftp_sessions)
        await self.back_up()
        sessions = self.ftp_sessions[before:]
        self.assertEqual(len(sessions), 1, "one walk, no copy sessions")
        self.assertEqual(sessions[0].retrieved, [], "no downloads, and the user's name is already known")
        self.assertEqual(self.rt().settings["last"]["copied"], 0)

    async def test_a_rewritten_slot_moves_the_old_copy_to_history(self):
        await self.turn_on()
        await self.back_up()
        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016030000", "20261016011910")
        await self.back_up(GAME_C)
        self.assertEqual((self.user / LATEST / GAME_C / "SAVEDATAPlayer").read_bytes(), b"Q" * 300)
        version = self.user / "History/Game C (CUSA90003)/SAVEDATAPlayer/2026-10-16 014221"
        self.assertEqual((version / "PS4/SAVEDATA/0000000000000000" / GAME_C / "SAVEDATAPlayer").read_bytes(), b"P" * 300)
        self.assertEqual(self.rt().usage["versions"], 1)
        self.assertEqual(self.rt().settings["last"]["scope"], [GAME_C])

    async def test_one_game_lists_only_that_game(self):
        await self.turn_on()
        before = len(self.ftp_sessions)
        await self.back_up(OTHER)
        walk = self.ftp_sessions[before]
        self.assertNotIn(f"{sf.HOME_ROOT}/{UID}/savedata/{GAME_C}", walk.listings)
        self.assertIn(f"{sf.HOME_ROOT}/{UID}/savedata/{OTHER}", walk.listings)
        self.assertEqual(self.files(), [f"{LATEST}/{OTHER}/SAVEDATA01", f"{LATEST}/{OTHER}/SAVEDATA01.bin"])

    async def test_ticked_games_only(self):
        """The panel's Select → Back up N."""
        await self.turn_on()
        before = len(self.ftp_sessions)
        await sb.async_start_backup(self.hass, "console_a", UID, [{"title_id": OTHER}])
        await self.drain()
        walk = self.ftp_sessions[before]
        self.assertNotIn(f"{sf.HOME_ROOT}/{UID}/savedata/{GAME_C}", walk.listings)
        self.assertEqual(self.files(), [f"{LATEST}/{OTHER}/SAVEDATA01", f"{LATEST}/{OTHER}/SAVEDATA01.bin"])
        with self.assertRaises(HomeAssistantError):
            await sb.async_start_backup(self.hass, "console_a", UID, [{"title_id": "../etc"}])

    async def test_a_slot_written_during_the_copy_is_dropped_and_waits(self):
        await self.turn_on()
        target = sf.image_remote(UID, GAME_C, "SAVEDATAPlayer")
        console = self.console

        class SavingFTP(FakeFTP):
            def retrbinary(self, cmd, callback, blocksize=8192):
                super().retrbinary(cmd, callback, blocksize)
                if cmd.endswith(target):  # the game saves while HA copies
                    write_save(console, UID, GAME_C, "SAVEDATAPlayer", b"R" * 300, "20261016014500", "20261016011910")

        self.ftp_class = SavingFTP
        await self.back_up()
        rt = self.rt()
        self.assertIsNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))
        self.assertIsNotNone(sf.local_slot(self.user, GAME_C, "sce_sdmemory"))
        self.assertEqual((rt.settings["last"]["copied"], rt.settings["last"]["retried"]), (2, 1))
        self.ftp_class = FakeFTP
        await self.back_up()
        self.assertEqual((self.user / LATEST / GAME_C / "SAVEDATAPlayer").read_bytes(), b"R" * 300)

    async def test_ftp_failure_is_recorded_and_raised_once(self):
        await self.turn_on()

        def _refuse(host, port, **kw):
            raise ConnectionRefusedError("refused")

        cf.connect = _refuse
        await self.back_up()
        rt = self.rt()
        self.assertEqual(rt.settings["last"]["state"], sb.STATUS_FAILED)
        self.assertIn("refused", rt.settings["last"]["error"])
        self.assertIsNone(rt.settings["last_success"])
        self.assertEqual(sb.status(rt), sb.STATUS_FAILED)
        self.assertFalse(rt.op_lock.locked())

    async def test_busy_and_sleeping_console_are_refused(self):
        await self.turn_on()
        await self.rt().op_lock.acquire()
        with self.assertRaises(HomeAssistantError):
            await sb.async_start_backup(self.hass, "console_a")
        self.rt().op_lock.release()
        self.coordinator.data["power_state"] = "rest"
        with self.assertRaises(HomeAssistantError):
            await sb.async_start_backup(self.hass, "console_a")
        with self.assertRaises(HomeAssistantError):
            await sb.async_start_backup(self.hass, "console_a", UID, [{"title_id": "../etc"}])

    async def test_users_with_the_same_name_get_their_own_folders(self):
        self.console.add_dir(f"{sf.HOME_ROOT}/10000002", "20260421213050")
        self.console.add_file(f"{sf.HOME_ROOT}/10000002/username.dat", b"player1\0", "20260421213050")
        self.console.add_dir(f"{sf.HOME_ROOT}/10000002/savedata", "20260421213050")
        write_save(self.console, "10000002", OTHER, "SAVEDATA01", b"o" * 50, "20261016000000")
        await self.turn_on()
        await self.back_up()
        self.assertEqual(self.rt().settings["users"], {UID: "player1", "10000002": "player1 (10000002)"})
        self.assertTrue((self.root / "PS4 PRO A" / "player1 (10000002)" / LATEST / OTHER / "SAVEDATA01").is_file())


class Limit(Base):
    async def test_newest_first_until_the_limit(self):
        await self.turn_on(limit_gb=1500 / (1 << 30))  # sce_sdmemory 1096 + Player 396 fit, SAVEDATA01 736 doesn't
        await self.back_up()
        rt = self.rt()
        self.assertEqual(rt.settings["last"]["state"], sb.STATUS_LIMIT)
        self.assertEqual((rt.settings["last"]["copied"], rt.settings["last"]["skipped"]), (2, 1))
        self.assertIsNone(sf.local_slot(self.user, OTHER, "SAVEDATA01"))
        self.assertEqual(sb.status(rt), sb.STATUS_LIMIT)
        self.assertIsNone(rt.settings["last_success"])

    async def test_old_unkept_history_makes_room_latest_never_does(self):
        await self.turn_on()
        await self.back_up(OTHER)
        for i, modify in enumerate(("20261016010000", "20261016011000")):
            write_save(self.console, UID, OTHER, "SAVEDATA01", bytes([65 + i]) * 640, modify, "20261015193539")
            await self.back_up(OTHER)
        self.assertEqual(self.rt().usage, {"bytes": 3 * 736, "versions": 2})
        oldest = self.user / "History" / OTHER / "SAVEDATA01" / "2026-10-16 005531"
        sf.set_kept(oldest, True)
        await sb.async_set_settings(self.hass, "console_a", {"limit_gb": (3 * 736 + 100) / (1 << 30)})
        write_save(self.console, UID, OTHER, "SAVEDATA01", b"Z" * 640, "20261016012000", "20261015193539")
        await self.back_up(OTHER)
        history = sorted(p.name for p in (self.user / "History" / OTHER / "SAVEDATA01").iterdir())
        self.assertEqual(history, ["2026-10-16 005531", "2026-10-16 011000"],
                         "01:00 (oldest unkept) went; the kept 00:55 stayed; 01:10 moved out of Latest")
        self.assertEqual(self.rt().settings["last"]["state"], sb.STATUS_UP_TO_DATE)

    async def test_disk_floor_counts_too(self):
        await self.turn_on()
        self.room = 500
        await self.back_up()
        last = self.rt().settings["last"]
        self.assertEqual((last["copied"], last["skipped"]), (1, 2), "only Player (396) fits in 500 bytes")
        self.assertIsNotNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))


class OpenGames(Base):
    """A game in focus or suspended behind the home screen may have its save mounted: never copied."""

    def play(self, title=None, **data):
        self.coordinator.push(power_state="on", klog_connected=True, **{const.SENSOR_TITLE_ID: title}, **data)

    async def test_manual_skips_the_game_in_focus(self):
        self.play(GAME_C)
        await self.back_up()
        rt = self.rt()
        self.assertIsNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))
        self.assertIsNotNone(sf.local_slot(self.user, OTHER, "SAVEDATA01"))
        self.assertEqual((rt.settings["last"]["state"], rt.settings["last"]["open"]),
                         (sb.STATUS_GAME_OPEN, ["Game C"]))
        self.assertEqual(sb.status(rt), sb.STATUS_GAME_OPEN)
        self.assertIsNone(rt.settings["last_success"], "not everything is backed up")
        self.play(None, state_reason="game_close_detected")
        await self.back_up()
        self.assertIsNotNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))
        self.assertEqual(sb.status(rt), sb.STATUS_UP_TO_DATE)

    async def test_the_suspended_game_counts_as_open(self):
        self.play(None, suspended_title_id=OTHER)
        await self.back_up(OTHER)
        self.assertIsNone(sf.local_slot(self.user, OTHER, "SAVEDATA01"))
        self.assertEqual(self.rt().settings["last"]["open"], [OTHER], "no name known: the title ID")

    async def test_a_game_started_during_the_pass_is_skipped(self):
        calls = []

        def _open(hass, entry_id):
            calls.append(entry_id)
            return set() if len(calls) == 1 else {GAME_C}

        orig, sb._open_titles = sb._open_titles, _open
        try:
            await self.back_up()
        finally:
            sb._open_titles = orig
        self.assertIsNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))
        self.assertIsNotNone(sf.local_slot(self.user, OTHER, "SAVEDATA01"))
        self.assertEqual(self.rt().progress, {"done": 3, "total": 3})


class PanelActions(Base):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on()
        await self.back_up()
        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016030000", "20261016011910")
        await self.back_up(GAME_C)

    async def test_list_is_what_ha_holds(self):
        listing = await sb.async_list(self.hass, "console_a")
        (user,) = listing["users"]
        self.assertEqual((user["uid"], user["name"]), (UID, "player1"))
        game_c = next(g for g in user["games"] if g["title_id"] == GAME_C)
        self.assertEqual(game_c["game"], "Game C")
        player = next(s for s in game_c["slots"] if s["slot"] == "SAVEDATAPlayer")
        self.assertEqual(player["saved"], "2026-10-16T03:00:00Z")
        self.assertEqual(player["versions"], [{"stamp": OLDER, "bytes": 396, "kept": False, "name": None}])
        self.assertEqual((player["name"], player["on_ps4"], game_c["on_ps4"]), (None, True, True),
                         "the passes read the console: it's there; no savedata.db on this fake console: no name")
        self.assertEqual([g["title_id"] for g in user["games"]], [GAME_C, OTHER], "most recently saved first")
        self.assertEqual(game_c["bytes"], 396 + 396 + 1096)

    async def test_keep_marks_a_version(self):
        await sb.async_keep(self.hass, "console_a", UID, GAME_C, "SAVEDATAPlayer", "2026-10-16 014221", True)
        version = self.user / "History/Game C (CUSA90003)/SAVEDATAPlayer/2026-10-16 014221"
        self.assertTrue((version / sf.KEEP_MARK).exists())
        listing = await sb.async_list(self.hass, "console_a")
        game_c = next(g for g in listing["users"][0]["games"] if g["title_id"] == GAME_C)
        self.assertTrue(game_c["slots"][0]["versions"][0]["kept"])
        await sb.async_keep(self.hass, "console_a", UID, GAME_C, "SAVEDATAPlayer", "2026-10-16 014221", False)
        self.assertFalse((version / sf.KEEP_MARK).exists())
        for bad in (("deadbeef", GAME_C, "SAVEDATAPlayer", "2026-10-16 014221"),
                    (UID, GAME_C, "SAVEDATAPlayer", "2026-10-16 000000"),
                    (UID, GAME_C, "../x", "2026-10-16 014221")):
            with self.assertRaises(HomeAssistantError):
                await sb.async_keep(self.hass, "console_a", *bad, True)

    async def test_zips_unpack_onto_a_usb_stick(self):
        everything = await sb.async_zip(self.hass, "console_a", UID)
        self.assertEqual(everything["name"], "PS4 PRO A player1 all games latest.zip")
        self.assertEqual(everything["path"],
                         "/api/ps4_goldhen/saves/console_a/download?file=PS4%20PRO%20A%20player1%20all%20games%20latest.zip")
        with zipfile.ZipFile(sb.downloads_dir(self.hass, "console_a") / everything["name"]) as zf:
            self.assertEqual(len(zf.namelist()), 6)
            self.assertEqual(zf.read(f"PS4/SAVEDATA/0000000000000000/{GAME_C}/SAVEDATAPlayer"), b"Q" * 300)
        one = await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C, "SAVEDATAPlayer", OLDER)])
        self.assertEqual(one["name"], f"PS4 PRO A player1 Game C {OLDER}.zip")
        with zipfile.ZipFile(sb.downloads_dir(self.hass, "console_a") / one["name"]) as zf:
            self.assertEqual(zf.read(f"PS4/SAVEDATA/0000000000000000/{GAME_C}/SAVEDATAPlayer"), b"P" * 300)
        game = await sb.async_zip(self.hass, "console_a", UID, [item(OTHER)])
        with zipfile.ZipFile(sb.downloads_dir(self.hass, "console_a") / game["name"]) as zf:
            self.assertEqual(sorted(zf.namelist()), [f"PS4/SAVEDATA/0000000000000000/{OTHER}/SAVEDATA01",
                                                     f"PS4/SAVEDATA/0000000000000000/{OTHER}/SAVEDATA01.bin"])
        self.assertTrue(any(d == sb.ZIP_MAX_AGE_S + 60 for d, _ in LATER), "an hour later the zips go")
        for bad in ([{"title_id": GAME_C, "stamp": OLDER}], [item("CUSA99999")], [item(GAME_C, "../x")],
                    [item(GAME_C, "SAVEDATAPlayer", "2026-10-16 000000")]):
            with self.assertRaises(HomeAssistantError):
                await sb.async_zip(self.hass, "console_a", UID, bad)

    async def test_single_saves_one_version_each(self):
        """Tick saves, not just games. Apollo's USB layout holds one copy of each save."""
        mixed = await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C, "sce_sdmemory"),
                                                             item(GAME_C, "SAVEDATAPlayer", OLDER)])
        self.assertEqual(mixed["name"], "PS4 PRO A player1 Game C saves.zip")
        with zipfile.ZipFile(sb.downloads_dir(self.hass, "console_a") / mixed["name"]) as zf:
            self.assertEqual(sorted(zf.namelist()), [f"PS4/SAVEDATA/0000000000000000/{GAME_C}/{n}" for n in (
                "SAVEDATAPlayer", "SAVEDATAPlayer.bin", "sce_sdmemory", "sce_sdmemory.bin")])
            self.assertEqual(zf.read(f"PS4/SAVEDATA/0000000000000000/{GAME_C}/SAVEDATAPlayer"), b"P" * 300, "the older one")
        newest = await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C, "SAVEDATAPlayer"), item(OTHER)])
        self.assertEqual(newest["name"], "PS4 PRO A player1 2 games latest.zip")
        with self.assertRaisesRegex(HomeAssistantError, "one version per save"):
            await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C, "SAVEDATAPlayer"),
                                                         item(GAME_C, "SAVEDATAPlayer", OLDER)])
        with self.assertRaisesRegex(HomeAssistantError, "one version per save"):
            await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C), item(GAME_C, "SAVEDATAPlayer", OLDER)])
        self.assertEqual(await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C), item(GAME_C, "sce_sdmemory")]),
                         await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C)]), "the same save twice is fine")

    async def test_one_zip_per_console_and_the_disk_floor(self):
        """A new zip deletes the console's previous one; one that doesn't fit is refused."""
        await sb.async_zip(self.hass, "console_a", UID, [item(OTHER)])
        two = await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C), item(OTHER)])
        self.assertEqual(two["name"], "PS4 PRO A player1 2 games latest.zip")
        folder = sb.downloads_dir(self.hass, "console_a")
        self.assertEqual([p.name for p in folder.iterdir()], [two["name"]], "the previous zip went")
        with zipfile.ZipFile(folder / two["name"]) as zf:
            self.assertEqual(len(zf.namelist()), 6)
        one = await sb.async_zip(self.hass, "console_a", UID, [item(GAME_C)])
        self.assertEqual(one["name"], "PS4 PRO A player1 Game C latest.zip")
        self.room = 10
        with self.assertRaises(HomeAssistantError) as ctx:
            await sb.async_zip(self.hass, "console_a", UID)
        self.assertIn("free space", str(ctx.exception))

    def fake_usb(self):
        return {p: d for p, d in self.console.files.items() if p.startswith("/data/")}

    async def test_send_to_ps4_then_remove(self):
        self.ftp_class = WritableFTP
        self.console.add_dir("/data", "20260101000000")
        fake = f"{sf.FAKE_USB_SAVES}/{GAME_C}"
        sent = await sb.async_send(self.hass, "console_a", UID, [item(GAME_C)])
        self.assertEqual(sorted(self.fake_usb()), [f"{fake}/SAVEDATAPlayer", f"{fake}/SAVEDATAPlayer.bin",
                                                   f"{fake}/sce_sdmemory", f"{fake}/sce_sdmemory.bin"])
        self.assertEqual(self.console.files[f"{fake}/SAVEDATAPlayer"], b"Q" * 300, "the newest copy")
        self.assertEqual((sent["target"], sent["games"], sent["files"], sent["bytes"], sent["version"]),
                         ("PS4 PRO A", ["Game C"], 4, 396 + 1096, None))
        self.assertEqual(Store.saved[f"{const.DOMAIN}.saves.console_a"]["sent"]["games"], ["Game C"])
        self.assertEqual(sb.settings_view(self.hass, self.rt())["sent"]["user"], "player1")
        written = [p for ftp in self.ftp_sessions for p in getattr(ftp, "stored", [])]
        self.assertTrue(written and all(p.startswith(sf.FAKE_USB_SAVES + "/") for p in written))

        await sb.async_send(self.hass, "console_a", UID, [item(GAME_C, "SAVEDATAPlayer", OLDER)])
        self.assertEqual(sorted(self.fake_usb()), [f"{fake}/SAVEDATAPlayer", f"{fake}/SAVEDATAPlayer.bin"],
                         "the older version replaces what was sent before")
        self.assertEqual(self.console.files[f"{fake}/SAVEDATAPlayer"], b"P" * 300)
        self.assertEqual(self.rt().settings["sent"]["version"], OLDER)
        self.assertEqual(self.rt().settings["sent"]["saves"],
                         [{"title_id": GAME_C, "slot": "SAVEDATAPlayer", "stamp": OLDER, "name": None}])
        self.assertEqual(self.rt().console["fake_usb"], {GAME_C: ["SAVEDATAPlayer"]}, "the PS4 pane shows it at once")

        await sb.async_send(self.hass, "console_a", UID, [item(OTHER)])  # Select → Send N
        other = f"{sf.FAKE_USB_SAVES}/{OTHER}"
        self.assertEqual(sorted(self.fake_usb()), [f"{other}/SAVEDATA01", f"{other}/SAVEDATA01.bin"])
        self.assertEqual(self.rt().settings["sent"]["games"], [OTHER])

        self.assertEqual(await sb.async_clear_sent(self.hass, "console_a"), 2)
        self.assertEqual(self.fake_usb(), {})
        self.assertIsNone(self.rt().settings["sent"])
        self.assertEqual(self.rt().console["fake_usb"], {})

    async def test_send_all_games_to_the_other_console(self):
        self.ftp_class = WritableFTP
        self.console.add_dir("/data", "20260101000000")
        await self.add_console("console_b")
        sent = await sb.async_send(self.hass, "console_a", UID, target="console_b")
        self.assertEqual((sent["target"], sent["files"]), ("PS4 PRO B", 6))
        self.assertEqual(self.rt("console_b").settings["sent"]["from"], "PS4 PRO A")
        self.assertIsNone(self.rt().settings["sent"])

    async def test_send_refuses_a_sleeping_or_busy_target_and_cleans_up_a_failure(self):
        self.ftp_class = WritableFTP
        self.console.add_dir("/data", "20260101000000")
        self.coordinator.push(power_state="rest")
        with self.assertRaisesRegex(HomeAssistantError, "rest mode or off"):
            await sb.async_send(self.hass, "console_a", UID, [item(GAME_C)])
        self.coordinator.push(power_state="on")
        await self.rt().op_lock.acquire()
        with self.assertRaisesRegex(HomeAssistantError, "Busy"):
            await sb.async_send(self.hass, "console_a", UID, [item(GAME_C)])
        self.rt().op_lock.release()
        sessions = len(self.ftp_sessions)
        self.console.fail_stor = {f"{sf.FAKE_USB_SAVES}/{GAME_C}/sce_sdmemory"}
        with self.assertRaisesRegex(HomeAssistantError, "Send to PS4 stopped: dropped"):
            await sb.async_send(self.hass, "console_a", UID, [item(GAME_C)])
        self.assertEqual(self.fake_usb(), {}, "nothing half sent stays for Apollo")
        self.assertIsNone(self.rt().settings["sent"])
        self.assertGreater(len(self.ftp_sessions), sessions)
        with self.assertRaises(HomeAssistantError):
            await sb.async_send(self.hass, "console_a", UID, [item(GAME_C)], target="nowhere")

    async def test_settings_view(self):
        view = sb.settings_view(self.hass, self.rt())
        self.assertEqual(view["mode"], sb.MODE_MANUAL)
        self.assertEqual(view["folder"], str(self.root / "PS4 PRO A"))
        self.assertEqual(view["status"], sb.STATUS_UP_TO_DATE)
        self.assertEqual((view["limit_min_gb"], view["limit_max_gb"]), (0.5, 50.0))
        self.assertEqual(view["power_state"], "on")


class Panes(Base):
    """The PS4 pane (a console read, each save against HA's newest copy), single saves, names, Delete
    from HA."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on()
        add_save_db(self.console, UID)

    def statuses(self, view):
        return {(g["title_id"], s["slot"]): s["status"] for u in view["users"] for g in u["games"] for s in g["slots"]}

    async def test_console_read_then_backup_then_change(self):
        view = await sb.async_console(self.hass, "console_a")
        self.assertEqual(len(self.ftp_sessions), 1, "one FTP session")
        self.assertEqual(self.ftp_sessions[0].retrieved, [f"{sf.HOME_ROOT}/{UID}/username.dat",
                                                          sf.SAVE_DB_REMOTE.format(uid=UID)], "no save downloads")
        self.assertEqual(set(self.statuses(view).values()), {"not_in_ha"})
        (user,) = view["users"]
        self.assertEqual((user["uid"], user["name"], view["read_at"], view["asleep"]), (UID, "player1", NOW.isoformat(), False))
        game_c = next(g for g in user["games"] if g["title_id"] == GAME_C)
        self.assertEqual([(s["slot"], s["name"]) for s in game_c["slots"]],
                         [("sce_sdmemory", "(Autosave) Chapter 1 (100.0%)"), ("SAVEDATAPlayer", "Player")],
                         "named, the most recently saved first")
        self.assertEqual(game_c["slots"][1]["bytes"], 396)

        await self.back_up()
        before = len(self.ftp_sessions)
        view = await sb.async_console(self.hass, "console_a")
        self.assertEqual(len(self.ftp_sessions), before, "the pass refreshed the read: no new session")
        self.assertEqual(set(self.statuses(view).values()), {"in_ha"})

        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016030000", "20261016011910")
        write_save(self.console, UID, GAME_C, "SAVEDATANew", b"n" * 10, "20261016030500")
        view = await sb.async_console(self.hass, "console_a", refresh=True)
        self.assertEqual(len(self.ftp_sessions), before + 1)
        self.assertEqual(self.statuses(view), {(GAME_C, "SAVEDATAPlayer"): "changed", (GAME_C, "sce_sdmemory"): "in_ha",
                                               (GAME_C, "SAVEDATANew"): "not_in_ha", (OTHER, "SAVEDATA01"): "in_ha"})

    async def test_stale_after_five_minutes_and_never_while_asleep_or_busy(self):
        await sb.async_console(self.hass, "console_a")
        self.rt().console["mono"] -= sb.CONSOLE_FRESH_S + 1
        await sb.async_console(self.hass, "console_a")
        self.assertEqual(len(self.ftp_sessions), 2, "stale: read again")
        self.rt().console["mono"] -= sb.CONSOLE_FRESH_S + 1
        self.coordinator.push(power_state="rest")
        view = await sb.async_console(self.hass, "console_a", refresh=True)
        self.assertEqual(len(self.ftp_sessions), 2, "asleep: no FTP")
        self.assertTrue(view["asleep"])
        self.assertTrue(view["users"], "the last read, with its time")
        self.coordinator.push(power_state="on")
        await self.rt().op_lock.acquire()
        await sb.async_console(self.hass, "console_a", refresh=True)
        self.rt().op_lock.release()
        self.assertEqual(len(self.ftp_sessions), 2, "a pass or a send is running: the last read")

        self.rt().console = None  # HA restarted while the PS4 sleeps
        self.coordinator.push(power_state="off")
        view = await sb.async_console(self.hass, "console_a")
        self.assertEqual((view["read_at"], view["users"], len(self.ftp_sessions)), (None, [], 2))

    async def test_open_games_and_the_fake_usb_folder(self):
        self.coordinator.push(power_state="on", klog_connected=True, **{const.SENSOR_TITLE_ID: GAME_C})
        self.console.add_dir("/data", "20260101000000")
        for path in ("/data/fakeusb", "/data/fakeusb/PS4", "/data/fakeusb/PS4/SAVEDATA", sf.FAKE_USB_SAVES,
                     f"{sf.FAKE_USB_SAVES}/{OTHER}"):
            self.console.add_dir(path, "20261016120000")
        for name in ("SAVEDATA01", "SAVEDATA01.bin"):
            self.console.add_file(f"{sf.FAKE_USB_SAVES}/{OTHER}/{name}", b"x", "20261016120000")
        view = await sb.async_console(self.hass, "console_a")
        games = {g["title_id"]: g for g in view["users"][0]["games"]}
        self.assertEqual((games[GAME_C]["open"], games[OTHER]["open"]), (True, False))
        self.assertEqual(view["fake_usb"], [{"title_id": OTHER, "game": OTHER, "slots": [{"slot": "SAVEDATA01", "name": None}]}])

    async def test_ticked_saves_only(self):
        await self.back_up(GAME_C, "sce_sdmemory")
        self.assertEqual(self.files(), [f"{LATEST}/{GAME_C}/sce_sdmemory", f"{LATEST}/{GAME_C}/sce_sdmemory.bin",
                                        f"Latest/{sf.LATEST_META}"])
        last = self.rt().settings["last"]
        self.assertEqual((last["checked"], last["copied"], last["already"]), (1, 1, 0))
        self.assertIsNone(self.rt().settings["last_success"], "one save isn't everything backed up")
        await self.back_up(GAME_C, "sce_sdmemory")
        last = self.rt().settings["last"]
        self.assertEqual((last["checked"], last["copied"], last["already"]), (1, 0, 1), "already in HA")
        await self.back_up()
        self.assertEqual(self.rt().settings["last_success"], NOW.isoformat())
        with self.assertRaisesRegex(HomeAssistantError, "older version"):
            await sb.async_start_backup(self.hass, "console_a", UID, [item(GAME_C, "sce_sdmemory", OLDER)])
        with self.assertRaisesRegex(HomeAssistantError, "user"):
            await sb.async_start_backup(self.hass, "console_a", "../x", [item(GAME_C)])

    async def test_names_on_the_ha_pane_and_saves_gone_from_the_ps4(self):
        self.hass.data[const.DOMAIN]["console_a"]["game_map"] = {}  # Game C uninstalled: its saves still name it
        await self.back_up()
        self.assertEqual(sf.latest_meta(self.user)[GAME_C]["SAVEDATAPlayer"]["name"], "Player")
        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016030000", "20261016011910")
        add_save_db(self.console, UID, [(GAME_C, "SAVEDATAPlayer", "Game C", "Chapter 3"),
                                        (GAME_C, "sce_sdmemory", "Game C", "Autosave")])
        await self.back_up(GAME_C)
        del self.console.files[sf.image_remote(UID, OTHER, "SAVEDATA01")]  # the game's save was deleted on the PS4
        self.console.dirs[sf.title_remote(UID, OTHER)].pop("sdimg_SAVEDATA01")
        await sb.async_console(self.hass, "console_a", refresh=True)
        (user,) = (await sb.async_list(self.hass, "console_a"))["users"]
        games = {g["title_id"]: g for g in user["games"]}
        self.assertEqual(games[GAME_C]["game"], "Game C")
        player = next(s for s in games[GAME_C]["slots"] if s["slot"] == "SAVEDATAPlayer")
        self.assertEqual((player["name"], player["versions"][0]["name"]), ("Chapter 3", "Player"),
                         "the older copy keeps the name it had")
        self.assertEqual((games[OTHER]["on_ps4"], games[OTHER]["slots"][0]["on_ps4"], games[GAME_C]["on_ps4"]),
                         (False, False, True))

    async def test_delete_from_ha_only(self):
        await self.back_up()
        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016030000", "20261016011910")
        await self.back_up(GAME_C)
        console_files = dict(self.console.files)
        sessions = len(self.ftp_sessions)
        result = await sb.async_delete(self.hass, "console_a", UID, [item(GAME_C, "SAVEDATAPlayer"),
                                                                 item(GAME_C, "SAVEDATAPlayer", OLDER)])
        self.assertEqual(result, {"deleted": 2})
        self.assertIsNone(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"))
        self.assertFalse((self.user / "History").exists() and any((self.user / "History").rglob("SAVEDATAPlayer")))
        self.assertIsNotNone(sf.local_slot(self.user, GAME_C, "sce_sdmemory"))
        self.assertEqual((len(self.ftp_sessions), self.console.files), (sessions, console_files), "the PS4 untouched")
        self.assertEqual(self.rt().usage["versions"], 0)
        for bad in ([], [item(GAME_C, "SAVEDATAPlayer")], [item(GAME_C, "SAVEDATAPlayer", OLDER)]):
            with self.assertRaises(HomeAssistantError):
                await sb.async_delete(self.hass, "console_a", UID, bad)
        await self.rt().op_lock.acquire()
        with self.assertRaisesRegex(HomeAssistantError, "Busy"):
            await sb.async_delete(self.hass, "console_a", UID, [item(OTHER)])
        self.rt().op_lock.release()


class RealEntities(Base):
    """The real classes, built the way HA does: self.hass is None until added."""

    async def test_button_sensors(self):
        await self.turn_on()
        button = ents.PS4BackUpSavesNowButton(self.entry)
        status = ents.PS4SaveBackupStatusSensor(self.entry)
        last = ents.PS4LastSaveBackupSensor(self.entry)
        self.assertFalse(hasattr(ents, "PS4AutomaticSaveBackupSwitch"), "no Automatic switch")
        for entity in (button, status, last):
            entity.hass = self.hass
            await entity.async_added_to_hass()
        self.assertEqual(status.native_value, sb.STATUS_NEVER)
        self.assertIsNone(last.native_value)
        await button.async_press()
        await self.drain()
        self.assertEqual(status.native_value, sb.STATUS_UP_TO_DATE)
        self.assertEqual(last.native_value, NOW)
        attrs = status.extra_state_attributes
        self.assertEqual((attrs["copied"], attrs["mode"], attrs["progress"]), (3, sb.MODE_MANUAL, None))
        self.assertNotIn("waiting", attrs)

    def test_progress_not_recorded(self):
        status = ents.PS4SaveBackupStatusSensor(self.entry)
        self.assertEqual(status._unrecorded_attributes, {"progress"})  # still on the entity, only not recorded
        self.assertNotIn("last_finished", status._unrecorded_attributes)  # history reads the pass results


if __name__ == "__main__":
    unittest.main()
