"""PS4 screenshots in HA: settings (off by default, no reload), device-page entities that come and go,
Latest screenshot, Import latest, Sync (all / game / newest / selected) within the storage limit, latest-shot checks.
Copies are manual only: there is no "Automatic" mode.

Runs the real screenshots.py + capture_files.py against minimal HA stand-ins and the fake console from
test_capture_files.py (the 3 shots seen on a 12.00 Pro).
"""
import asyncio
import copy
from datetime import datetime, timezone
import importlib
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_screenshots_test_pkg"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capture_files import FakeConsole, FakeFTP, live_like_console  # noqa: E402


_SAVED: dict = {}  # sys.modules entries replaced while screenshots.py imports; put back after (other tests' stubs)


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
        self.entities, self.removed, self.unique_ids = {}, [], {}  # unique_ids: (domain, unique_id) -> entity_id

    def async_get(self, entity_id):
        return self.entities.get(entity_id)

    def async_get_entity_id(self, domain, platform, unique_id):
        return self.unique_ids.get((domain, unique_id))

    def async_remove(self, entity_id):
        self.removed.append(entity_id)
        self.entities.pop(entity_id, None)
        self.unique_ids = {k: v for k, v in self.unique_ids.items() if v != entity_id}


class DeviceRegistry:
    def __init__(self):
        self.names = {}  # entry_id -> (name, name_by_user)

    def entries_for(self, entry_id):
        if entry_id not in self.names:
            return []
        name, by_user = self.names[entry_id]
        return [types.SimpleNamespace(identifiers={("ps4_goldhen", entry_id)}, name=name, name_by_user=by_user)]


ENTITY_REG, DEVICE_REG = EntityRegistry(), DeviceRegistry()
SIGNALS, LATER, CONNECTED = [], [], []


class _Entity:
    """Like HA's Entity: self.hass is None until the platform adds it (an image bug found live)."""
    hass = None
    writes = 0

    async def async_added_to_hass(self):
        pass

    def async_on_remove(self, fn):
        pass

    def async_write_ha_state(self):
        self.writes += 1


class ImageEntity(_Entity):
    _cached_image = None
    _attr_image_last_updated = None

    def __init__(self, hass):  # HA's sets tokens + an HTTP client here, not self.hass
        self.access_tokens = ["tok"]

    @property
    def entity_picture(self):
        return f"/api/image_proxy/x?token={self.access_tokens[-1]}"


def _call_later(hass, delay, action):
    LATER.append((delay, action))
    return lambda: LATER.remove((delay, action)) if (delay, action) in LATER else None


def _passthrough(*_a, **_k):
    return lambda fn: fn


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.websocket_api", websocket_command=_passthrough, require_admin=lambda f: f,
        async_response=lambda f: f, async_register_command=lambda hass, cmd: None, ActiveConnection=object)
_module("homeassistant.components.http", HomeAssistantView=object)
_module("homeassistant.components.image", ImageEntity=ImageEntity)
_module("homeassistant.components.sensor", SensorEntity=_Entity, SensorDeviceClass=types.SimpleNamespace(ENUM="enum"))
_module("homeassistant.components.button", ButtonEntity=_Entity)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_registry", async_get=lambda hass: ENTITY_REG)
_module("homeassistant.helpers.device_registry", DeviceInfo=dict, async_get=lambda hass: DEVICE_REG,
        async_entries_for_config_entry=lambda registry, entry_id: registry.entries_for(entry_id))
_module("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda hass, sig: SIGNALS.append(sig),
        async_dispatcher_connect=lambda hass, sig, fn: CONNECTED.append((sig, fn)) or (lambda: None))
_module("homeassistant.helpers.event", async_call_later=_call_later)
_module("homeassistant.helpers.storage", Store=Store)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: datetime(2026, 10, 16, 4, 0, tzinfo=timezone.utc),
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
ss = importlib.import_module(f"{PKG}.screenshots")
cf = importlib.import_module(f"{PKG}.capture_files")
const = importlib.import_module(f"{PKG}.const")
ents = importlib.import_module(f"{PKG}.screenshot_entities")
for _name, _old in _SAVED.items():
    if _old is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _old

HOME = "NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg"
GAME_D_LATE = "NPXS20001/CUSA90005/3e9/20261015_111203_00205776.jpg"
GAME_D_EARLY = "NPXS20001/CUSA90005/375/20261015_110310_00680394.jpg"


class Hass:
    def __init__(self, tmp: Path):
        self.data = {}
        self.config = types.SimpleNamespace(media_dirs={"local": str(tmp / "media")}, path=lambda *p: str(tmp.joinpath(*p)))
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
        self.data, self.listeners = {}, []

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
        ENTITY_REG.unique_ids.clear()
        DEVICE_REG.names = {"console_a": ("PS4", "PS4 PRO A"), "console_b": ("PS4", "PS4 PRO B")}
        SIGNALS.clear()
        LATER.clear()
        self._orig = (ss.CACHE_ROOT, ss._disk_room, cf.connect)
        ss.CACHE_ROOT = tmp / "config" / "ps4" / "screenshots"
        ss._disk_room = lambda path: 10 << 30
        self.console = live_like_console()
        self.ftp_sessions = []

        def _connect(host, port, **kw):
            ftp = FakeFTP(self.console)
            self.ftp_sessions.append(ftp)
            return ftp

        cf.connect = _connect
        self.hass = Hass(tmp)
        self.media = tmp / "media" / "PS4 Screenshots"
        self.entry = await self.add_console("console_a")

    async def asyncTearDown(self):
        ss.CACHE_ROOT, ss._disk_room, cf.connect = self._orig
        self.tmp.cleanup()

    async def add_console(self, entry_id):
        entry = Entry(entry_id)
        coordinator = Coordinator()
        self.hass.data.setdefault(const.DOMAIN, {})[entry_id] = {
            "host": "192.0.2.121", "ftp_port": 2121, "coordinator": coordinator,
            "game_map": {"CUSA90005": {"name": "GAME D"}},
        }
        await ss.async_setup(self.hass, entry, coordinator)
        self.added = getattr(self, "added", {})
        for platform in ("image", "button"):
            def _add(entities, platform=platform, entry_id=entry_id):
                for i, entity in enumerate(entities):
                    entity.entity_id = f"{platform}.{entry_id}_{i}"
                    ENTITY_REG.entities[entity.entity_id] = entity
                    self.added.setdefault(entry_id, []).append(entity.entity_id)
            ss.register_platform(self.hass, entry, platform, _add, lambda platform=platform: [FakeEntity(platform)])
        return entry

    def rt(self, entry_id="console_a"):
        return ss.runtime(self.hass, entry_id)

    async def turn_on(self, entry_id="console_a", **changes):
        await ss.async_set_settings(self.hass, entry_id, {"enabled": True, **changes})
        LATER.clear()

    def copies(self, console="PS4 PRO A"):
        root = self.media / console
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()) if root.exists() else []


class Settings(Base):
    async def test_off_by_default_no_entities_no_ftp(self):
        self.assertFalse(self.rt().settings["enabled"])
        self.assertEqual(self.rt().settings["mode"], ss.MODE_ON_REQUEST)
        self.assertEqual(self.rt().settings["limit_gb"], 2.0)
        self.assertEqual(ENTITY_REG.entities, {})
        self.assertEqual(self.ftp_sessions, [])
        with self.assertRaises(HomeAssistantError):
            await ss.async_list(self.hass, "console_a", 0, 24)

    async def test_turning_on_pins_the_console_folder_and_adds_entities(self):
        await ss.async_set_settings(self.hass, "console_a", {"enabled": True})
        rt = self.rt()
        self.assertEqual(rt.settings["folder"], "PS4 PRO A")
        self.assertEqual(ss.console_dir(self.hass, rt), self.media / "PS4 PRO A")
        self.assertEqual(sorted(ENTITY_REG.entities), ["button.console_a_0", "image.console_a_0"])
        self.assertEqual(Store.saved[f"{const.DOMAIN}.screenshots.console_a"]["enabled"], True)
        self.assertEqual([d for d, _ in LATER], [1.0], "one check soon, to fill the Latest screenshot")
        DEVICE_REG.names["console_a"] = ("PS4", "Renamed")
        await ss.async_set_settings(self.hass, "console_a", {"limit_gb": 3.0})
        self.assertEqual(rt.settings["folder"], "PS4 PRO A", "renaming the device later doesn't split the copies")

    async def test_same_name_gets_a_suffix(self):
        DEVICE_REG.names["console_b"] = ("PS4", "PS4 PRO A")
        await self.add_console("console_b")
        await self.turn_on("console_a")
        await self.turn_on("console_b")
        self.assertEqual(self.rt("console_b").settings["folder"], "PS4 PRO A (2)")

    async def test_off_removes_entities_without_a_reload(self):
        await self.turn_on()
        self.assertEqual(len(ENTITY_REG.entities), 2)
        await ss.async_set_settings(self.hass, "console_a", {"enabled": False})
        self.assertEqual(ENTITY_REG.entities, {})

    async def test_settings_survive_a_restart(self):
        await self.turn_on(limit_gb=5.0)
        del self.hass.data[const.DOMAIN]["console_a"]["screenshots"]
        await self.add_console("console_a")
        rt = self.rt()
        self.assertEqual((rt.settings["enabled"], rt.settings["mode"], rt.settings["limit_gb"]), (True, "on_request", 5.0))

    async def test_a_saved_never_reads_as_on_request(self):
        """Never is gone (viewing no longer needs a copy); a saved one keeps the Sync button."""
        Store.saved[f"{const.DOMAIN}.screenshots.console_b"] = {"enabled": True, "mode": "never", "folder": "PS4 PRO B"}
        await self.add_console("console_b")
        self.assertEqual(self.rt("console_b").settings["mode"], ss.MODE_ON_REQUEST)
        self.assertIn("button.console_b_0", ENTITY_REG.entities)
        self.assertEqual(ss.MODES, (ss.MODE_ON_REQUEST,))

    async def test_the_removed_sync_status_sensor_leaves_the_registry(self):
        ENTITY_REG.entities["sensor.console_a_ps4_pro_a_screenshot_sync_status"] = FakeEntity("sensor")
        ENTITY_REG.unique_ids[("sensor", "ps4_goldhen_console_a_screenshot_sync_status")] = (
            "sensor.console_a_ps4_pro_a_screenshot_sync_status")
        del self.hass.data[const.DOMAIN]["console_a"]["screenshots"]
        await self.add_console("console_a")
        self.assertIn("sensor.console_a_ps4_pro_a_screenshot_sync_status", ENTITY_REG.removed)
        self.assertNotIn("sensor.console_a_ps4_pro_a_screenshot_sync_status", ENTITY_REG.entities)

    async def test_a_saved_automatic_setting_reads_as_on_request(self):
        Store.saved[f"{const.DOMAIN}.screenshots.console_a"] = {"enabled": True, "mode": "automatic", "folder": "PS4 PRO A"}
        del self.hass.data[const.DOMAIN]["console_a"]["screenshots"]
        await self.add_console("console_a")
        self.assertEqual(self.rt().settings["mode"], ss.MODE_ON_REQUEST)
        self.assertNotIn("automatic", ss.MODES)

    async def test_remove_entry_drops_settings_and_thumbnails_keeps_copies(self):
        await self.turn_on()
        await ss.async_list(self.hass, "console_a", 0, 24)
        await ss.async_import_latest(self.hass, self.entry)
        await ss.async_remove(self.hass, self.entry)
        self.assertNotIn(f"{const.DOMAIN}.screenshots.console_a", Store.saved)
        self.assertFalse((ss.CACHE_ROOT / "console_a").exists())
        self.assertEqual(len(self.copies()), 1)


class ListAndLatest(Base):
    async def test_page_has_thumbnails_games_and_in_ha(self):
        await self.turn_on()
        page = await ss.async_list(self.hass, "console_a", 0, 2)
        self.assertTrue(page["more"])
        self.assertEqual([s["rel"] for s in page["shots"]], [HOME, GAME_D_LATE])
        home, game_d = page["shots"]
        self.assertEqual((home["game"], game_d["game"]), ("Home screen", "GAME D"))
        self.assertTrue(home["thumb"].startswith("data:image/jpeg;base64,"))
        self.assertTrue(game_d["trophy"])
        self.assertFalse(home["in_ha"])
        rest = await ss.async_list(self.hass, "console_a", 2, 2)
        self.assertEqual(([s["rel"] for s in rest["shots"]], rest["more"]), ([GAME_D_EARLY], False))

    async def test_latest_is_saved_signalled_and_served_from_the_thumbnail(self):
        await self.turn_on()
        SIGNALS.clear()
        await ss.async_list(self.hass, "console_a", 0, 1)
        self.assertEqual(self.rt().settings["latest"]["rel"], HOME)
        self.assertIn(ss.signal("console_a"), SIGNALS)
        self.assertEqual(await ss.async_latest_thumb(self.hass, "console_a"), b"t" * 11)
        SIGNALS.clear()
        await ss.async_list(self.hass, "console_a", 0, 1)
        self.assertEqual(SIGNALS, [], "same latest shot: no state write")

    async def test_one_games_page(self):  # the Content tab: one game's shots, the tab's list and latest left alone
        await self.turn_on()
        before = len(self.ftp_sessions)
        page = await ss.async_list(self.hass, "console_a", 0, 6, "CUSA90005")
        self.assertEqual([s["rel"] for s in page["shots"]], [GAME_D_LATE, GAME_D_EARLY])
        self.assertTrue(all(s["thumb"] for s in page["shots"]))
        self.assertFalse(page["more"])
        walk = self.ftp_sessions[before]
        self.assertNotIn(f"{cf.PHOTO_ROOT}/NPXS20001/NPXS20001", walk.listings, "no other game's folder")
        self.assertIsNone(self.rt().shown)
        self.assertIsNone(self.rt().settings["latest"])
        self.assertIsNone(page["read_at"])
        await ss.async_list(self.hass, "console_a", 0, 24)  # the tab's read
        self.hass.data[const.DOMAIN]["console_a"]["coordinator"].data = {"power_state": "rest"}
        before = len(self.ftp_sessions)
        page = await ss.async_list(self.hass, "console_a", 0, 6, "CUSA90005")
        self.assertEqual((page["shots"], page["asleep"]), ([], True), "asleep: none (HA's last read is the newest only)")
        self.assertEqual(len(self.ftp_sessions), before, "no FTP to a sleeping console")
        self.assertEqual(len((await ss.async_list(self.hass, "console_a", 0, 24))["shots"]), 3, "the tab's read stays")

    async def test_thumbnail_cache_is_capped(self):
        await self.turn_on()
        for i in range(30):
            self.console.add_shot("CUSA00009", f"b{i}", f"20261016_0900{i:02d}_00000000.jpg", f"202610161300{i:02d}")
        old_cap, ss.THUMB_CAP = ss.THUMB_CAP, 10
        try:
            await ss.async_list(self.hass, "console_a", 0, 24)
        finally:
            ss.THUMB_CAP = old_cap
        self.assertEqual(len(list((ss.CACHE_ROOT / "console_a" / "thumbs").iterdir())), 10)


class Import(Base):
    async def test_import_latest_copies_once_into_the_game_folder(self):
        await self.turn_on()
        await ss.async_import_latest(self.hass, self.entry)
        self.assertEqual(self.copies(), ["Home screen/20261015_214637_00434169.jpg"])
        rt = self.rt()
        self.assertEqual(rt.settings["last_sync"]["state"], ss.STATUS_SYNCED)
        self.assertEqual(rt.settings["last_sync"]["copied"], 1)
        self.assertEqual(rt.usage, {"count": 1, "bytes": 383363})
        await ss.async_import_latest(self.hass, self.entry)
        self.assertEqual(rt.settings["last_sync"]["copied"], 0, "already in HA: nothing copied again")
        self.assertFalse(rt.op_lock.locked())

    async def test_import_refused_over_the_limit(self):
        await self.turn_on()
        self.rt().settings["limit_gb"] = 100_000 / (1 << 30)  # ~100 KB: the 383 KB shot doesn't fit
        with self.assertRaises(HomeAssistantError) as ctx:
            await ss.async_import_latest(self.hass, self.entry)
        self.assertIn("limit", str(ctx.exception))
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.rt().settings["last_sync"]["state"], ss.STATUS_LIMIT)

    async def test_card_import(self):
        await self.turn_on()
        result = await ss.async_import_one(self.hass, "console_a", GAME_D_EARLY)
        self.assertTrue(result["in_ha"])
        self.assertEqual(self.copies(), ["GAME D/20261015_110310_00680394.jpg"])
        with self.assertRaises(HomeAssistantError):
            await ss.async_import_one(self.hass, "console_a", "../../etc/passwd.jpg")
        with self.assertRaises(HomeAssistantError):
            await ss.async_import_one(self.hass, "console_a", "NPXS20001/CUSA90005/375/20991231_000000_00000000.jpg")

    async def test_ftp_down_is_a_clear_error_and_releases_the_lock(self):
        await self.turn_on()

        def _down(host, port, **kw):
            raise ConnectionRefusedError(111, "Connection refused")

        cf.connect = _down
        with self.assertRaises(HomeAssistantError) as ctx:
            await ss.async_import_latest(self.hass, self.entry)
        self.assertIn("not reachable", str(ctx.exception))
        self.assertFalse(self.rt().op_lock.locked())


class Viewer(Base):
    """The viewer shows HA's copy, else reads the shot from the PS4 for that view only (no copy)."""

    class Resp:
        def __init__(self, *args, body=None, text=None, status=200, content_type=None, headers=None):
            self.args, self.body, self.text, self.status, self.content_type = args, body, text, status, content_type

    class Req(dict):
        def __init__(self, hass, rel, admin=True):
            super().__init__(hass_user=types.SimpleNamespace(is_admin=admin))
            self.app, self.query = {"hass": hass}, {"rel": rel}

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self._web = (ss.web.Response, ss.web.FileResponse)
        ss.web.Response = ss.web.FileResponse = self.Resp
        self.hass.data[const.DOMAIN]["console_a"]["coordinator"].data = {"power_state": "on"}

    async def asyncTearDown(self):
        ss.web.Response, ss.web.FileResponse = self._web
        await super().asyncTearDown()

    async def view(self, rel, admin=True):
        return await ss.PS4ScreenshotView().get(self.Req(self.hass, rel, admin), "console_a")

    async def test_ps4_only_shot_is_read_not_kept(self):
        await self.turn_on()
        before = len(self.ftp_sessions)
        resp = await self.view(GAME_D_EARLY)
        self.assertEqual((resp.status, resp.content_type), (200, "image/jpeg"))
        self.assertEqual(resp.body, self.console.files[f"{cf.PHOTO_ROOT}/{GAME_D_EARLY}"])
        self.assertEqual(self.copies(), [], "viewing never syncs")
        self.assertEqual(len(self.ftp_sessions), before + 1, "one FTP session")

    async def test_ha_copy_is_served_from_ha(self):
        await self.turn_on()
        await ss.async_import_one(self.hass, "console_a", GAME_D_EARLY)
        before = len(self.ftp_sessions)
        resp = await self.view(GAME_D_EARLY)
        self.assertEqual(resp.status, 200)
        self.assertTrue(str(resp.args[0]).endswith("20261015_110310_00680394.jpg"))
        self.assertEqual(len(self.ftp_sessions), before, "no FTP for a copy HA has")

    async def test_asleep_off_admin_and_size_cap(self):
        await self.turn_on()
        self.hass.data[const.DOMAIN]["console_a"]["coordinator"].data = {"power_state": "rest"}
        before = len(self.ftp_sessions)
        self.assertEqual((await self.view(GAME_D_EARLY)).status, 409)
        self.assertEqual(len(self.ftp_sessions), before, "no FTP to a sleeping console")
        self.hass.data[const.DOMAIN]["console_a"]["coordinator"].data = {"power_state": "on"}
        self.assertEqual((await self.view(GAME_D_EARLY, admin=False)).status, 403)
        self.assertEqual((await self.view("../../etc/passwd.jpg")).status, 404)
        orig, ss.VIEW_MAX_BYTES = ss.VIEW_MAX_BYTES, 10
        try:
            resp = await self.view(GAME_D_EARLY)
        finally:
            ss.VIEW_MAX_BYTES = orig
        self.assertEqual((resp.status, resp.text), (502, "Too big to show"))
        self.assertEqual(ss.settings_view(self.hass, self.rt())["power_state"], "on")
        await ss.async_set_settings(self.hass, "console_a", {"enabled": False})
        self.assertEqual((await self.view(GAME_D_EARLY)).status, 404)


class Sync(Base):
    async def run_sync(self, **scope):
        total = await ss.async_start_sync(self.hass, "console_a", scope)
        await asyncio.gather(*self.entry.tasks)
        return total

    async def test_plan_counts_new_and_room(self):
        await self.turn_on()
        await ss.async_import_latest(self.hass, self.entry)
        plan = await ss.async_plan(self.hass, "console_a")
        self.assertEqual((plan["total"], plan["in_ha"]), (3, 1))
        self.assertEqual([s["rel"] for s in plan["new"]], [GAME_D_LATE, GAME_D_EARLY])
        self.assertEqual(plan["new"][0]["game"], "GAME D")
        self.assertEqual(plan["used_bytes"], 383363)
        self.assertEqual(plan["room_bytes"], 2 * (1 << 30) - 383363)

    async def test_all_uses_the_dialogs_walk_and_copies_everything_new(self):
        await self.turn_on()
        await ss.async_plan(self.hass, "console_a")
        sessions = len(self.ftp_sessions)
        self.assertEqual(await self.run_sync(kind="all"), 3)
        self.assertEqual(len(self.copies()), 3)
        walks = [f for f in self.ftp_sessions[sessions:] if f.listings]
        self.assertEqual(walks, [], "the Sync right after its dialog doesn't walk the console again")
        self.assertEqual(self.rt().progress, {"done": 3, "total": 3})
        self.assertFalse(self.rt().op_lock.locked())

    async def test_game_newest_selected(self):
        await self.turn_on()
        self.assertEqual(await self.run_sync(kind="game", title_id="CUSA90005"), 2)
        self.assertEqual(self.copies(), ["GAME D/20261015_110310_00680394.jpg",
                                         "GAME D/20261015_111203_00205776.jpg"])
        self.assertEqual(await self.run_sync(kind="newest", count=5), 1, "newest N counts only new shots")
        self.assertEqual(len(self.copies()), 3)
        self.assertEqual(await self.run_sync(kind="selected", rels=[HOME, "../x.jpg"]), 0, "already in HA")

    async def test_one_game_walks_only_that_game(self):  # the Content tab's Sync screenshots, no dialog before it
        await self.turn_on()
        before = len(self.ftp_sessions)
        self.assertEqual(await self.run_sync(kind="game", title_id="CUSA90005"), 2)
        listed = [p for f in self.ftp_sessions[before:] for p in f.listings]
        self.assertIn(f"{cf.PHOTO_ROOT}/NPXS20001/CUSA90005", listed)
        self.assertNotIn(f"{cf.PHOTO_ROOT}/NPXS20001/NPXS20001", listed)
        self.assertNotIn(f"{cf.PHOTO_ROOT}/NPXS20001", listed, "the app's game folders aren't listed")

    async def test_limit_copies_the_newest_that_fit_and_says_so(self):
        await self.turn_on()
        self.rt().settings["limit_gb"] = 1_300_000 / (1 << 30)  # home (383 KB) + Game D late (886 KB) fit; early doesn't
        await self.run_sync(kind="all")
        self.assertEqual(len(self.copies()), 2)
        last = self.rt().settings["last_sync"]
        self.assertEqual((last["state"], last["copied"], last["skipped"]), (ss.STATUS_LIMIT, 2, 1))

    async def test_a_drop_mid_sync_is_recorded_and_unlocks(self):
        await self.turn_on()
        bad = f"{cf.PHOTO_ROOT}/{GAME_D_LATE}"
        real = cf.connect

        def _flaky(host, port, **kw):
            ftp = real(host, port)
            ftp.fail_retr.add(bad)
            return ftp

        cf.connect = _flaky
        await self.run_sync(kind="all")
        last = self.rt().settings["last_sync"]
        self.assertEqual((last["state"], last["copied"]), (ss.STATUS_FAILED, 1))
        self.assertIn("dropped", last["error"])
        self.assertFalse(self.rt().op_lock.locked())
        self.assertFalse(self.rt().syncing)
        self.assertEqual(self.copies(), ["Home screen/20261015_214637_00434169.jpg"], "no half file left behind")

    async def test_busy_while_a_sync_runs(self):
        await self.turn_on()
        await self.rt().op_lock.acquire()
        with self.assertRaises(HomeAssistantError):
            await ss.async_start_sync(self.hass, "console_a", {"kind": "all"})
        with self.assertRaises(HomeAssistantError):
            await ss.async_import_latest(self.hass, self.entry)
        self.rt().op_lock.release()

    async def test_reconnects_every_25_files(self):
        await self.turn_on()
        for i in range(30):
            self.console.add_shot("CUSA00009", f"b{i}", f"20261016_0900{i:02d}_00000000.jpg", f"202610161300{i:02d}")
        await ss.async_plan(self.hass, "console_a")
        sessions = len(self.ftp_sessions)
        await self.run_sync(kind="all")
        self.assertEqual(len(self.ftp_sessions) - sessions, 2, "33 files = 2 sessions: the cover fetch gets a turn")


class Delete(Base):
    """The panel's Delete asks every time: HA copy (default) and/or PS4 original (permanent)."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.turn_on()
        await ss.async_list(self.hass, "console_a", 0, 24)
        await self.run_all()

    async def run_all(self):
        await ss.async_start_sync(self.hass, "console_a", {"kind": "all"})
        await asyncio.gather(*self.entry.tasks)

    def on_ps4(self):
        return [s["rel"] for s in cf.all_shots(FakeFTP(self.console))]

    async def test_ha_copy_only_keeps_the_ps4_original(self):
        result = await ss.async_delete(self.hass, "console_a", [GAME_D_EARLY, HOME], ha=True, ps4=False)
        self.assertEqual(result, {"ha_deleted": 2, "ps4_deleted": 0})
        self.assertEqual(self.copies(), ["GAME D/20261015_111203_00205776.jpg"])
        self.assertEqual(len(self.on_ps4()), 3)
        self.assertEqual(self.rt().usage["count"], 1)
        page = await ss.async_list(self.hass, "console_a", 0, 24)
        self.assertEqual({s["rel"]: s["in_ha"] for s in page["shots"]}, {HOME: False, GAME_D_LATE: True, GAME_D_EARLY: False})

    async def test_ps4_original_only_keeps_the_ha_copy(self):
        result = await ss.async_delete(self.hass, "console_a", [GAME_D_EARLY], ha=False, ps4=True)
        self.assertEqual(result, {"ha_deleted": 0, "ps4_deleted": 1})
        self.assertEqual(self.on_ps4(), [HOME, GAME_D_LATE])
        self.assertEqual(len(self.copies()), 3, "HA's copy stays as the backup")
        thumbs = ss.CACHE_ROOT / "console_a" / "thumbs"
        self.assertFalse(cf.thumb_local(thumbs, GAME_D_EARLY).exists(), "its cached thumbnail goes")

    async def test_both_and_the_latest_moves_on(self):
        self.assertEqual(self.rt().settings["latest"]["rel"], HOME)
        result = await ss.async_delete(self.hass, "console_a", [HOME], ha=True, ps4=True)
        self.assertEqual(result, {"ha_deleted": 1, "ps4_deleted": 1})
        self.assertEqual(self.rt().settings["latest"]["rel"], GAME_D_LATE)
        self.assertNotIn("Home screen/20261015_214637_00434169.jpg", self.copies())

    async def test_refusals(self):
        with self.assertRaises(HomeAssistantError):
            await ss.async_delete(self.hass, "console_a", [HOME], ha=False, ps4=False)
        with self.assertRaises(HomeAssistantError):
            await ss.async_delete(self.hass, "console_a", ["../../etc/passwd.jpg"], ha=True, ps4=True)
        await self.rt().op_lock.acquire()
        with self.assertRaises(HomeAssistantError):
            await ss.async_delete(self.hass, "console_a", [HOME], ha=True, ps4=False)
        self.rt().op_lock.release()
        self.assertEqual(len(self.on_ps4()), 3)
        self.assertEqual(len(self.copies()), 3)

    async def test_ftp_down_still_deletes_the_ha_copy_and_unlocks(self):
        cf.connect = lambda host, port, **kw: (_ for _ in ()).throw(ConnectionRefusedError(111, "refused"))
        with self.assertRaises(HomeAssistantError):
            await ss.async_delete(self.hass, "console_a", [HOME], ha=True, ps4=True)
        self.assertEqual(self.rt().usage["count"], 2, "the HA side ran first and the count is fresh")
        self.assertFalse(self.rt().op_lock.locked())


class LatestChecks(Base):
    async def fire(self):
        delay, action = LATER.pop()
        action(None)
        await asyncio.gather(*self.entry.tasks)
        return delay

    async def test_triggers(self):
        coordinator = self.hass.data[const.DOMAIN]["console_a"]["coordinator"]
        coordinator.push(power_state="on")
        self.assertEqual(LATER, [], "off: nothing scheduled")
        await self.turn_on()
        coordinator.push(power_state="rest")
        coordinator.push(power_state="on")
        self.assertEqual([d for d, _ in LATER], [30.0])
        LATER.clear()
        coordinator.push(title_id="CUSA90005")
        self.assertEqual(LATER, [], "starting a game isn't a trigger")
        coordinator.push(title_id=None)
        self.assertEqual([d for d, _ in LATER], [10.0])

    async def test_on_request_only_refreshes_the_latest(self):
        coordinator = self.hass.data[const.DOMAIN]["console_a"]["coordinator"]
        await self.turn_on()
        coordinator.push(power_state="on")
        await self.fire()
        self.assertEqual(self.rt().settings["latest"]["rel"], HOME)
        self.assertEqual(self.copies(), [])

    async def test_leaving_a_game_refreshes_the_latest_but_copies_nothing(self):
        coordinator = self.hass.data[const.DOMAIN]["console_a"]["coordinator"]
        await self.turn_on()
        coordinator.push(power_state="on", title_id="CUSA90005")
        LATER.clear()
        coordinator.push(title_id=None)
        await self.fire()
        self.assertEqual(self.rt().settings["latest"]["rel"], HOME)
        self.assertEqual(self.copies(), [], "copies are manual only")

    async def test_no_ftp_to_a_resting_console(self):
        coordinator = self.hass.data[const.DOMAIN]["console_a"]["coordinator"]
        await self.turn_on()
        coordinator.push(power_state="on", title_id="CUSA90005")
        LATER.clear()
        coordinator.push(title_id=None, power_state="rest")
        sessions = len(self.ftp_sessions)
        await self.fire()
        self.assertEqual(len(self.ftp_sessions), sessions)


class RealEntities(Base):
    async def attach(self, entity):
        entity.hass = self.hass  # what HA's platform does, after __init__
        await entity.async_added_to_hass()
        return entity

    async def test_built_before_hass_is_set_then_follow_the_signal(self):
        await self.turn_on()
        image = ents.PS4LatestScreenshotImage(self.hass, self.entry)  # must not touch self.hass here
        button = ents.PS4ImportLatestScreenshotButton(self.entry)
        for entity in (image, button):
            await self.attach(entity)
        self.assertIsNone(image.entity_picture, "no shot known yet: the row shows the icon")
        await button.async_press()
        image._on_signal()
        self.assertEqual(image.extra_state_attributes["game"], "Home screen")
        self.assertTrue(image.extra_state_attributes["in_ha"])
        self.assertEqual(image._attr_image_last_updated.isoformat(), "2026-10-16T01:46:37+00:00")
        self.assertIsNotNone(image.entity_picture)
        self.assertEqual(await image.async_image(), b"t" * 11)
        self.assertEqual(self.rt().settings["last_sync"]["state"], ss.STATUS_SYNCED)
        self.assertFalse(hasattr(ents, "PS4ScreenshotSyncStatusSensor"), "the sync status sensor is gone")

    async def test_image_restored_after_a_restart_with_screenshots_on(self):
        await self.turn_on()
        await ss.async_list(self.hass, "console_a", 0, 1)
        image = await self.attach(ents.PS4LatestScreenshotImage(self.hass, self.entry))
        self.assertEqual(image.extra_state_attributes["title_id"], "NPXS20001")


class Asleep(Base):
    """A console in rest mode or off gets no FTP: the list shows HA's last read, the console actions refuse."""

    def power(self, state):
        self.hass.data[const.DOMAIN]["console_a"]["coordinator"].data = {"power_state": state}

    async def test_list_shows_the_last_read_without_ftp(self):
        await self.turn_on()
        self.power("on")
        awake = await ss.async_list(self.hass, "console_a", 0, 2)
        self.assertEqual((awake["asleep"], awake["read_at"]), (False, "2026-10-16T04:00:00+00:00"))
        await ss.async_import_one(self.hass, "console_a", GAME_D_LATE)
        before = len(self.ftp_sessions)
        for state in ("rest", "off"):
            self.power(state)
            page = await ss.async_list(self.hass, "console_a", 0, 2)
            self.assertEqual([s["rel"] for s in page["shots"]], [HOME, GAME_D_LATE])
            self.assertEqual((page["asleep"], page["power_state"], page["more"]), (True, state, False))
            self.assertEqual(page["read_at"], "2026-10-16T04:00:00+00:00")
            self.assertTrue(page["shots"][1]["in_ha"], "In HA comes from HA's folder, not the last read")
            self.assertTrue(page["shots"][0]["thumb"].startswith("data:image/jpeg;base64,"), "thumbnails from the cache")
        self.assertEqual(len(self.ftp_sessions), before, "no FTP to a sleeping console")

    async def test_show_more_pages_through_the_last_read(self):
        await self.turn_on()
        self.power("on")
        await ss.async_list(self.hass, "console_a", 0, 1)
        await ss.async_list(self.hass, "console_a", 1, 1)
        self.power("rest")
        before = len(self.ftp_sessions)
        first = await ss.async_list(self.hass, "console_a", 0, 1)
        second = await ss.async_list(self.hass, "console_a", 1, 1)
        self.assertEqual(([s["rel"] for s in first["shots"]], first["more"]), ([HOME], True))
        self.assertEqual(([s["rel"] for s in second["shots"]], second["more"]), ([GAME_D_LATE], False),
                         "the 3rd shot was never read: no page for it")
        self.assertEqual(len(self.ftp_sessions), before)

    async def test_nothing_read_yet_is_an_empty_page(self):
        await self.turn_on()
        self.power("rest")
        page = await ss.async_list(self.hass, "console_a", 0, 24)
        self.assertEqual((page["shots"], page["more"], page["asleep"], page["read_at"]), ([], False, True, None))
        self.assertEqual(self.ftp_sessions, [])

    async def test_unknown_power_still_reads(self):
        """Like save backup: only a console known to be in rest mode or off is skipped."""
        await self.turn_on()
        page = await ss.async_list(self.hass, "console_a", 0, 1)
        self.assertEqual((page["asleep"], len(page["shots"])), (False, 1))
        self.assertEqual(len(self.ftp_sessions), 1)

    async def test_console_actions_refuse(self):
        await self.turn_on()
        self.power("on")
        await ss.async_list(self.hass, "console_a", 0, 24)
        await ss.async_import_one(self.hass, "console_a", HOME)
        self.power("rest")
        before = len(self.ftp_sessions)
        calls = (
            lambda: ss.async_plan(self.hass, "console_a"),
            lambda: ss.async_start_sync(self.hass, "console_a", {"kind": "all"}),
            lambda: ss.async_import_one(self.hass, "console_a", GAME_D_LATE),
            lambda: ss.async_import_latest(self.hass, self.entry),
            lambda: ss.async_delete(self.hass, "console_a", [HOME], ha=True, ps4=True),
        )
        for call in calls:
            with self.assertRaises(HomeAssistantError) as ctx:
                await call()
            self.assertIn("rest mode or off: turn it on first", str(ctx.exception))
        self.assertEqual(len(self.ftp_sessions), before, "no FTP to a sleeping console")
        self.assertFalse(self.rt().op_lock.locked())
        self.assertEqual(self.copies(), ["Home screen/20261015_214637_00434169.jpg"], "a refused delete deletes nothing")
        done = await ss.async_delete(self.hass, "console_a", [HOME], ha=True, ps4=False)
        self.assertEqual(done, {"ha_deleted": 1, "ps4_deleted": 0}, "HA's copies can go while it sleeps")
        self.assertEqual(self.copies(), [])

    async def test_a_ps4_delete_leaves_the_last_read(self):
        await self.turn_on()
        self.power("on")
        await ss.async_list(self.hass, "console_a", 0, 24)
        await ss.async_delete(self.hass, "console_a", [GAME_D_LATE], ha=False, ps4=True)
        self.power("rest")
        page = await ss.async_list(self.hass, "console_a", 0, 24)
        self.assertNotIn(GAME_D_LATE, [s["rel"] for s in page["shots"]])

    async def test_turning_off_forgets_the_last_read(self):
        await self.turn_on()
        self.power("on")
        await ss.async_list(self.hass, "console_a", 0, 24)
        await ss.async_set_settings(self.hass, "console_a", {"enabled": False})
        self.assertIsNone(self.rt().shown)


class PanelErrors(Base):
    async def test_ws_errors_reach_the_panel(self):
        sent = {}

        class Conn:
            def send_result(self, msg_id, result):
                sent["result"] = result

            def send_error(self, msg_id, code, message):
                sent["error"] = (code, message)

        handler = ss._ws_handler(ss._ws_list)
        await handler(self.hass, Conn(), {"id": 1, "entry_id": "console_a", "offset": 0, "count": 24})
        self.assertEqual(sent["error"][0], "screenshots_error")
        self.assertIn("off for this PS4", sent["error"][1])


if __name__ == "__main__":
    unittest.main()
