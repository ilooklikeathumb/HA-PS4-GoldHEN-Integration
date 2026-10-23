"""The panel's Content tab backend (games.py): the cover grid's list and one game's page.

Runs the real games.py + db.py against minimal HA stand-ins and stand-ins of the modules it reads (play time, save
backup, screenshots, game session): what each title carries, the cover URL from the media player, one game's saves
only, the details read from the cached app.db for the logged-in user, the unknown-title error, the WS commands.
"""
import importlib
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_games_test_pkg"
_SAVED: dict = {}
COMMANDS: list = []


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    _SAVED.setdefault(name, sys.modules.get(name))
    sys.modules[name] = mod
    return mod


class HomeAssistantError(Exception):
    pass


def _passthrough_cmd(schema):
    def wrap(fn):
        fn.schema = schema
        return fn
    return wrap


class Book:
    """play_time's book: the summary the grid reads, title_summary the page reads."""
    def __init__(self):
        self.accrued = []

    def accrue(self, now):
        self.accrued.append(now)

    def summary(self, now):
        return {"games": [{"title_id": "CUSA90003", "name": "Game C", "seconds": 7200.0, "sessions": 3,
                           "first": "2026-09-01T20:00:00+00:00", "last": "2026-10-20T20:00:00+00:00", "week": 0}]}

    def title_summary(self, tid, now):
        return {"seconds": 7200.0 if tid == "CUSA90003" else 0, "title_id": tid}


SAVES = {"mode": "manual"}
SHOTS = {"enabled": True}
SUSPENDED = {"value": None}
INVENTORY = {"users": [
    {"uid": "1eb3d8e9", "name": "Player", "games": [
        {"title_id": "CUSA90003", "game": "Game C", "last_saved": "2026-10-20T21:00:00Z", "bytes": 300, "on_ps4": True,
         "slots": [{"slot": "SAVEDATA00", "name": "Chapter 3", "saved": "2026-10-20T21:00:00Z", "bytes": 100,
                    "versions": [{"stamp": "a"}, {"stamp": "b"}], "on_ps4": True}]},
        {"title_id": "CUSA90001", "game": "Game B", "last_saved": None, "bytes": 5, "on_ps4": None, "slots": []},
    ]},
    {"uid": "1eb3d8ea", "name": "Guest", "games": []},
]}
pt_rt = types.SimpleNamespace(book=Book())
sv_rt = types.SimpleNamespace(settings=SAVES)
ss_rt = types.SimpleNamespace(settings=SHOTS)


async def _saves_list(hass, entry_id):
    return INVENTORY


_module("homeassistant")
_module("homeassistant.components")
_module("homeassistant.components.websocket_api", websocket_command=_passthrough_cmd, require_admin=lambda f: f,
        async_response=lambda f: f, async_register_command=lambda hass, cmd: COMMANDS.append(cmd),
        ActiveConnection=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.util")
_module("homeassistant.util.dt", utcnow=lambda: "NOW")
_module("voluptuous", Required=lambda key: key)
pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
_module(f"{PKG}.play_time", runtime=lambda hass, entry_id: pt_rt)
_module(f"{PKG}.save_backup", MODE_OFF="off", runtime=lambda hass, entry_id: sv_rt, async_list=_saves_list,
        settings_view=lambda hass, rt: {"mode": rt.settings["mode"], "running": False})
_module(f"{PKG}.screenshots", runtime=lambda hass, entry_id: ss_rt,
        settings_view=lambda hass, rt: {"enabled": rt.settings["enabled"]})
_module(f"{PKG}.game_session", suspended=lambda hass, entry_id: SUSPENDED["value"])
games = importlib.import_module(f"{PKG}.games")
for _name, _old in _SAVED.items():
    if _old is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _old

GAME_MAP = {
    "CUSA90003": {"name": "Game C", "ui_category": "game", "category": "gd", "can_remove": True, "last_access": "2026-10-20"},
    "CUSA90001": {"name": "Game B", "ui_category": "game", "category": "gd", "can_remove": True, "last_access": "2026-10-01"},
    "APOL00004": {"name": "Apollo Save Tool", "ui_category": "app", "category": "gde", "can_remove": True,
                  "last_access": "2026-10-10"},
    "NPXS20102": {"name": "Internet Browser", "ui_category": "app", "category": "gdi", "can_remove": False,
                  "last_access": "2025-01-01"},
    "NPXS20111": {"name": "Library", "ui_category": "app", "category": "gdi", "can_remove": False, "last_access": ""},
}


class Player:
    def cover_url(self, tid):
        return f"/api/media_player_proxy/media_player.ps4/browse_media/game/{tid}?token=t0k"


class Hass:
    def __init__(self, player=True, user="1DFFC9A9"):
        self.data = {games.DOMAIN: {"console_a": {
            "host": "192.0.2.121", "game_map": GAME_MAP,
            "coordinator": types.SimpleNamespace(data={"login_user_id": user} if user else {}),
            **({"media_player_entity": Player()} if player else {}),
        }}}
        self.jobs = []

    async def async_add_executor_job(self, fn, *args):
        self.jobs.append((fn, args))
        return fn(*args)


class Connection:
    def __init__(self):
        self.results, self.errors = [], []

    def send_result(self, msg_id, result):
        self.results.append((msg_id, result))

    def send_error(self, msg_id, code, message):
        self.errors.append((msg_id, code, message))


class GamesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        SAVES["mode"], SHOTS["enabled"], SUSPENDED["value"] = "manual", True, None
        self.info_calls = []
        self._orig = games.db.title_info
        games.db.title_info = lambda host, tid, user: self.info_calls.append((host, tid, user)) or {"version": "1.02"}

    def tearDown(self):
        games.db.title_info = self._orig

    async def test_list_titles_play_and_covers(self):
        out = await games.async_list(Hass(), "console_a")
        # the Internet Browser (built in) isn't offered: HA can't close it
        self.assertEqual([t["title_id"] for t in out["titles"]], ["CUSA90003", "APOL00004", "CUSA90001"])
        c, apollo, b = out["titles"]
        self.assertEqual(c["play"], {"seconds": 7200.0, "sessions": 3, "last": "2026-10-20T20:00:00+00:00"})
        self.assertIsNone(b["play"])
        self.assertEqual((c["kind"], apollo["kind"]), ("game", "app"))
        self.assertEqual(c["cover"], "/api/media_player_proxy/media_player.ps4/browse_media/game/CUSA90003?token=t0k")
        self.assertEqual(pt_rt.book.accrued[-1], "NOW", "counted up to now first, like the Play time tab")
        no_player = await games.async_list(Hass(player=False), "console_a")
        self.assertIsNone(no_player["titles"][0]["cover"])  # before the media player is added

    async def test_detail(self):
        SUSPENDED["value"] = "CUSA90001"
        hass = Hass()
        d = await games.async_detail(hass, "console_a", " cusa90003 ")
        self.assertEqual((d["title_id"], d["name"], d["kind"]), ("CUSA90003", "Game C", "game"))
        self.assertEqual(self.info_calls, [("192.0.2.121", "CUSA90003", 0x1DFFC9A9)], "the logged-in user's folders")
        self.assertEqual(hass.jobs[0][1], ("192.0.2.121", "CUSA90003", 0x1DFFC9A9), "app.db read off the event loop")
        self.assertEqual(d["info"], {"version": "1.02"})
        self.assertEqual(d["play"]["seconds"], 7200.0)
        self.assertEqual(d["saves"], [{"uid": "1eb3d8e9", "user": "Player", "last_saved": "2026-10-20T21:00:00Z", "bytes": 300,
                                       "slots": [{"slot": "SAVEDATA00", "name": "Chapter 3", "saved": "2026-10-20T21:00:00Z",
                                                  "bytes": 100, "versions": 2, "on_ps4": True}]}])
        self.assertEqual(d["suspended"], {"title_id": "CUSA90001", "name": "Game B"})
        self.assertEqual((d["saves_settings"]["mode"], d["screenshot_settings"]["enabled"]), ("manual", True))
        self.assertTrue(d["cover"].endswith("/CUSA90003?token=t0k"))

    async def test_detail_saves_off_no_user_unknown_title(self):
        SAVES["mode"] = "off"
        d = await games.async_detail(Hass(user=None), "console_a", "CUSA90001")
        self.assertIsNone(d["saves"])
        self.assertEqual(d["saves_settings"]["mode"], "off")
        self.assertIsNone(d["suspended"])
        self.assertEqual(self.info_calls[-1], ("192.0.2.121", "CUSA90001", None))
        for tid in ("NPXS20102", "NPXS20111", "CUSA00000"):  # built in (the Browser too); not on this console
            with self.subTest(tid=tid), self.assertRaises(HomeAssistantError):
                await games.async_detail(Hass(), "console_a", tid)
        with self.assertRaises(HomeAssistantError):
            await games.async_list(Hass(), "console_b")

    async def test_ws_commands_registered_once_errors_as_ws_errors(self):
        hass = Hass()
        COMMANDS.clear()
        games.register_once(hass)
        games.register_once(hass)
        self.assertEqual([c.schema["type"] for c in COMMANDS], ["ps4_goldhen/games/list", "ps4_goldhen/games/detail"])
        conn = Connection()
        await COMMANDS[1](hass, conn, {"id": 7, "entry_id": "console_a", "title_id": "CUSA00000"})
        self.assertEqual(conn.errors, [(7, "games_error", "That game isn't in this PS4's game list")])
        await COMMANDS[0](hass, conn, {"id": 8, "entry_id": "console_a"})
        self.assertEqual(conn.results[0][0], 8)
        self.assertEqual(len(conn.results[0][1]["titles"]), 3)


if __name__ == "__main__":
    unittest.main()
