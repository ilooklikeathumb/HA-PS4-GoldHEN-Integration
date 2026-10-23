"""PS4 Game cover image entity — cover follows the running game.

Runs the real image.py against minimal HA stand-ins (no HA install needed).
"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_cover_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


@dataclass
class Image:
    content_type: str
    content: bytes


class ImageEntity:
    """Mirrors HA's ImageEntity parts that image.py relies on."""

    _attr_content_type = "image/jpeg"
    _attr_image_last_updated = None
    _attr_image_url = None
    _cached_image = None
    entity_id = "image.ps4_game_cover"

    def __init__(self, hass):
        self.access_tokens = ["tok"]
        self.cdn_fetches = 0

    @property
    def entity_picture(self):
        return f"/api/image_proxy/{self.entity_id}?token={self.access_tokens[-1]}"

    @property
    def state(self):
        return None if self._attr_image_last_updated is None else self._attr_image_last_updated.isoformat()

    async def async_image(self):
        if self._cached_image:
            return self._cached_image.content
        self.cdn_fetches += 1
        self._cached_image = Image("image/jpeg", b"cdn:" + self._attr_image_url.encode())
        return self._cached_image.content


class CoordinatorEntity:
    available = True

    def __init__(self, coordinator):
        self.coordinator = coordinator

    def _handle_coordinator_update(self):
        self.writes = getattr(self, "writes", 0) + 1


class Hass:
    def __init__(self, data):
        self.data = data
        self.executor_calls = []

    async def async_add_executor_job(self, fn, *args):
        self.executor_calls.append(args)
        return fn(*args)


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.image", Image=Image, ImageEntity=ImageEntity)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)
_module("homeassistant.helpers.device_registry", DeviceInfo=dict)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: datetime.now(timezone.utc))

FTP_FETCHES = []
DISK_CACHE = {}  # /config/ps4/covers/<console>: the real _read_or_fetch_cover reads it first, under the FTP lock


def fake_read_or_fetch_cover(host, ftp_port, console, tid, cover_path):
    if (console, tid) in DISK_CACHE:
        return DISK_CACHE[(console, tid)]
    FTP_FETCHES.append((host, ftp_port, console, tid, cover_path))
    DISK_CACHE[(console, tid)] = b"png:" + tid.encode()
    return DISK_CACHE[(console, tid)]


pkg = _module(PKG, _read_or_fetch_cover=fake_read_or_fetch_cover)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

image_mod = importlib.import_module(f"{PKG}.image")
const = importlib.import_module(f"{PKG}.const")


class Coordinator:
    def __init__(self, data):
        self.data = data


class Entry:
    entry_id = "E1"
    data = {const.CONF_PS4_HOST: "192.0.2.1"}


def make(coordinator_data, game_map=None):
    entry_data = {"host": "192.0.2.1", "ftp_port": 2121, "game_map": game_map or {}}
    hass = Hass({const.DOMAIN: {"E1": entry_data}})
    coordinator = Coordinator(coordinator_data)
    entity = image_mod.PS4GameCoverImage(hass, coordinator, Entry())
    entity.hass = hass
    return entity, coordinator, entry_data, hass


def run(coro):
    return asyncio.run(coro)


class GameCoverTests(unittest.TestCase):
    def setUp(self):
        FTP_FETCHES.clear()
        DISK_CACHE.clear()

    def test_home_screen_has_no_picture(self):
        e, *_ = make({})
        self.assertIsNone(e.entity_picture)
        self.assertIsNone(e.state)  # HA shows "Unknown"
        self.assertIsNone(run(e.async_image()))

    def test_game_uses_console_icon(self):
        e, *_ = make({"title_id": "cusa90003"}, {"CUSA90003": {"cover": "/user/appmeta/CUSA90003/icon0.png"}})
        self.assertIsNone(e._attr_image_url)
        self.assertTrue(e.entity_picture.startswith("/api/image_proxy/"))
        self.assertIsNotNone(e.state)
        self.assertEqual(run(e.async_image()), b"png:CUSA90003")
        self.assertEqual(e._attr_content_type, "image/png")
        self.assertEqual(FTP_FETCHES, [("192.0.2.1", 2121, "E1", "CUSA90003", "/user/appmeta/CUSA90003/icon0.png")])
        run(e.async_image())  # second view = in-memory cache
        self.assertEqual(len(FTP_FETCHES), 1)
        self.assertEqual(e.cdn_fetches, 0, "never a URL fetch")

    def test_cache_is_per_console(self):  # the same title can have a different icon on another console
        DISK_CACHE[("OTHER", "CUSA90003")] = b"other console's icon"
        e, *_ = make({"title_id": "CUSA90003"})
        self.assertEqual(run(e.async_image()), b"png:CUSA90003")
        self.assertEqual([f[2:4] for f in FTP_FETCHES], [("E1", "CUSA90003")])

    def test_default_cover_path_when_app_db_lacks_title(self):
        e, *_ = make({"title_id": "CUSA00001"})
        run(e.async_image())
        self.assertEqual(FTP_FETCHES[0][4], "/user/appmeta/CUSA00001/icon0.png")

    def test_rest_and_off_hide_the_cover(self):
        for power in (const.POWER_REST, const.POWER_OFF):
            with self.subTest(power=power):
                e, *_ = make({"title_id": "CUSA90003", "power_state": power})
                self.assertIsNone(e.entity_picture)
                self.assertIsNone(e.state)
                self.assertIs(e.available, False)

    def test_available_while_on_or_unknown(self):
        for data in ({"power_state": const.POWER_ON}, {"power_state": const.POWER_ON, "title_id": "CUSA90003"}, {}):
            with self.subTest(data=data):
                self.assertIs(make(data)[0].available, True)

    def test_game_change_clears_cache_and_restamps(self):
        e, coordinator, *_ = make({"title_id": "CUSA90003"})
        run(e.async_image())
        first = e._attr_image_last_updated
        coordinator.data = {"title_id": "CUSA90009"}
        e._handle_coordinator_update()
        self.assertIsNone(e._cached_image)
        self.assertGreaterEqual(e._attr_image_last_updated, first)
        self.assertEqual(run(e.async_image()), b"png:CUSA90009")
        coordinator.data = {}
        e._handle_coordinator_update()  # back to Home Screen
        self.assertIsNone(e.entity_picture)
        self.assertIsNone(e.state)

    def test_same_game_update_keeps_cache(self):
        e, coordinator, *_ = make({"title_id": "CUSA90003"})
        run(e.async_image())
        stamp = e._attr_image_last_updated
        e._handle_coordinator_update()  # e.g. a klog line for the same game
        self.assertIsNotNone(e._cached_image)
        self.assertEqual(e._attr_image_last_updated, stamp)

    def test_app_db_refresh_keeps_the_cover(self):  # an app.db pull for the same game neither clears nor restamps
        e, coordinator, entry_data, _ = make({"title_id": "CUSA90003"})
        run(e.async_image())
        stamp = e._attr_image_last_updated
        entry_data["game_map"] = {"CUSA90003": {"name": "Game C", "cover": "/user/appmeta/CUSA90003/icon0.png"}}
        e._handle_coordinator_update()
        self.assertIsNone(e._attr_image_url)
        self.assertIsNotNone(e._cached_image)
        self.assertEqual(e._attr_image_last_updated, stamp)

    def test_fetch_failure_is_no_image(self):
        e, *_ = make({"title_id": "CUSA90003"})
        original = image_mod._read_or_fetch_cover
        image_mod._read_or_fetch_cover = lambda *a: (_ for _ in ()).throw(OSError("FTP refused"))
        try:
            self.assertIsNone(run(e.async_image()))
        finally:
            image_mod._read_or_fetch_cover = original

    def test_concurrent_views_fetch_once(self):
        e, *_ = make({"title_id": "CUSA90003"})

        async def two_views():
            return await asyncio.gather(e.async_image(), e.async_image())

        self.assertEqual(run(two_views()), [b"png:CUSA90003", b"png:CUSA90003"])
        self.assertEqual(len(FTP_FETCHES), 1)


if __name__ == "__main__":
    unittest.main()
