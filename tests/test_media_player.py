"""PS4 media player — state from klog power, game + cover, wake routing, rest (turn
off), game list + browser + launch, Remote Play login after the wake.

Runs the real media_player.py against minimal HA stand-ins (no HA install needed). Needs `cryptography` (rp_session
imports rp_regist): `uv run --no-project --with cryptography python -m unittest tests/test_media_player.py`.
"""
import asyncio
import enum
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_media_player_test_pkg"


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    sys.modules[name] = mod
    return mod


class MediaPlayerEntityFeature(enum.IntFlag):
    TURN_ON = 128
    TURN_OFF = 256
    PLAY_MEDIA = 512
    SELECT_SOURCE = 2048
    BROWSE_MEDIA = 131072


class MediaClass(enum.StrEnum):
    APP = "app"
    DIRECTORY = "directory"
    GAME = "game"


class BrowseMedia:
    def __init__(self, **kwargs):
        self.children = None
        self.thumbnail = None
        self.__dict__.update(kwargs)


class BrowseError(Exception):
    pass


class MediaPlayerState(str, enum.Enum):
    OFF = "off"
    ON = "on"
    IDLE = "idle"
    PLAYING = "playing"
    STANDBY = "standby"


class MediaType(enum.StrEnum):  # HA: StrEnum, formats as "game"
    APP = "app"
    GAME = "game"


class MediaPlayerEntity:
    """Mirrors HA's MediaPlayerEntity image parts that media_player.py relies on."""

    _context = None

    @property
    def media_image_url(self):
        return None

    def get_browse_image_url(self, media_content_type, media_content_id, media_image_id=None):
        return f"/api/media_player_proxy/media_player.ps4/browse_media/{media_content_type}/{media_content_id}?token=t"


class CoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator

    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass


class HomeAssistantError(Exception):
    pass


class Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, data))


class Hass:
    def __init__(self, data):
        self.data = data
        self.services = Services()

    async def async_add_executor_job(self, fn, *args):
        return fn(*args)


_module("homeassistant")
_module("homeassistant.components")
_module(
    "homeassistant.components.media_player",
    BrowseError=BrowseError,
    BrowseMedia=BrowseMedia,
    MediaClass=MediaClass,
    MediaPlayerEntity=MediaPlayerEntity,
    MediaPlayerEntityFeature=MediaPlayerEntityFeature,
    MediaPlayerState=MediaPlayerState,
    MediaType=MediaType,
)
_module("homeassistant.config_entries", ConfigEntry=object)
_module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
_module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError)
_module("homeassistant.helpers")
_module("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_module("homeassistant.helpers.update_coordinator", CoordinatorEntity=CoordinatorEntity)
_module("homeassistant.helpers.device_registry", DeviceInfo=dict)

FTP_FETCHES = []
CACHED_COVERS: dict = {}  # HA's cover cache on disk: (console = entry ID, title ID) -> bytes


def fake_read_cached_cover(console, tid):
    return CACHED_COVERS.get((console, tid))


def fake_read_or_fetch_cover(host, ftp_port, console, tid, cover_path):
    FTP_FETCHES.append((host, ftp_port, console, tid, cover_path))
    return b"png:" + tid.encode()


async def fake_send_bin_tcp(*args, **kwargs):  # rest.py imports it; turn off is tested with async_enter_rest faked
    raise AssertionError("no payload send expected here")


pkg = _module(
    PKG, _read_or_fetch_cover=fake_read_or_fetch_cover, _read_cached_cover=fake_read_cached_cover,
    _send_bin_tcp=fake_send_bin_tcp, _send_bin_bytes=fake_send_bin_tcp
)
pkg.__path__ = [str(COMP)]
import importlib  # noqa: E402

mp_mod = importlib.import_module(f"{PKG}.media_player")
const = importlib.import_module(f"{PKG}.const")

KEY = "1a2b3c4d"  # made up
WAKES = []


async def fake_wakeup(host, key):
    WAKES.append((host, key))


mp_mod.async_wakeup = fake_wakeup

RP_KEY = "00112233445566778899aabbccddeeff"  # made up
DDP_CODES = []  # what the fake DDP SRCH answers next (None = no reply); empty = rest (620)
LOGINS = []
LOGIN_FAILURE = []


async def fake_search(host, timeout=3.0):
    code = DDP_CODES.pop(0) if DDP_CODES else 620
    return None if code is None else types.SimpleNamespace(code=code)


async def fake_login_after_wake(host, regist_key, rp_key):
    LOGINS.append((host, regist_key, rp_key))
    if LOGIN_FAILURE:
        raise mp_mod.RpSessionError(LOGIN_FAILURE.pop(0))
    return 17.0


mp_mod.async_search = fake_search
mp_mod.async_login_after_wake = fake_login_after_wake

RESTS = []


async def fake_enter_rest(hass, entry):
    RESTS.append(entry.entry_id)


mp_mod.async_enter_rest = fake_enter_rest

LAUNCHES = []
LAUNCHES_AFTER_WAKE = []  # the after_wake flag of each launch, same order as LAUNCHES
LAUNCHES_CLOSE_FIRST = []  # the close_first flag of each launch, same order as LAUNCHES


async def fake_launch_title(hass, entry, title_id, *, after_wake=False, close_first=True):
    LAUNCHES.append(title_id)
    LAUNCHES_AFTER_WAKE.append(after_wake)
    LAUNCHES_CLOSE_FIRST.append(close_first)


mp_mod.async_launch_title = fake_launch_title

RESUMES = []
RESUME_FAILURE = []


async def fake_resume_suspended(hass, entry):
    RESUMES.append(entry.entry_id)
    if RESUME_FAILURE:
        raise HomeAssistantError(RESUME_FAILURE.pop(0))


mp_mod.async_resume_suspended = fake_resume_suspended

LAYOUT_CALLS = []
LAYOUT = {}  # db.folder_layout's answer from the cached app.db; empty = no cache


def fake_folder_layout(host, user_id):
    LAYOUT_CALLS.append((host, user_id))
    return dict(LAYOUT) or None


mp_mod.folder_layout = fake_folder_layout


def game(name, last, ui="game", category="gd"):
    return {"name": name, "cover": None, "ui_category": ui, "category": category,
            "last_access": last}


GAMES = {
    "CUSA90003": game("Game C", "2026-10-15 01:23"),
    "CUSA90001": game("Game B", "2025-10-27 13:37"),
    "CUSA90004": game("Game A\u2122", "2026-10-13 19:55"),
    "CUSA90002": game("Spotify", "2026-10-14 20:32", ui="app", category="gdn"),
    "APOL00004": game("Apollo Save Tool", "2026-10-12 04:55", ui="app", category="gde"),
    "NPXS20111": game("Library", "2026-10-16 13:48", ui="app", category="gdi"),  # built in: not listed
}


class Coordinator:
    def __init__(self, data):
        self.data = data


class Entry:
    entry_id = "E1"
    data = {const.CONF_PS4_HOST: "192.0.2.1"}

    def __init__(self, options=None):
        self.options = options or {}


def make(coordinator_data, game_map=None, options=None):
    entry_data = {"host": "192.0.2.1", "ftp_port": 2121, "game_map": game_map or {}}
    hass = Hass({const.DOMAIN: {"E1": entry_data}})
    entity = mp_mod.PS4MediaPlayer(hass, Coordinator(coordinator_data), Entry(options))
    entity.hass = hass
    return entity, entry_data, hass


def run(coro):
    return asyncio.run(coro)


class StateTests(unittest.TestCase):
    def test_power_states(self):
        S = MediaPlayerState
        cases = [
            ({"power_state": "off"}, S.OFF),
            ({"power_state": "rest"}, S.OFF),  # HA deprecated STANDBY; power_state attribute tells rest apart
            ({"power_state": "rest", "title_id": "CUSA90003"}, S.OFF),  # suspended game isn't "playing"
            ({"power_state": "on"}, S.IDLE),
            ({"power_state": "on", "title_id": "CUSA90003"}, S.PLAYING),
            ({"title_id": "CUSA90003"}, S.PLAYING),  # game seen before the power heartbeat
            ({}, None),                              # unknown until klog reports
            ({"power_state": "on", "title_id": "CUSA90003", "not_responding": True}, None),  # frozen
            ({"power_state": "on", "not_responding": True}, None),
            ({"power_state": "rest", "not_responding": True}, S.OFF),
        ]
        for data, expected in cases:
            with self.subTest(data=data):
                self.assertEqual(make(data)[0].state, expected)

    def test_game_title_and_content(self):
        e, *_ = make({"power_state": "on", "title_id": "cusa90003", "game_name": " Game C "})
        self.assertEqual(e.media_title, "Game C")
        self.assertEqual(e.media_content_id, "CUSA90003")
        self.assertEqual(e.media_content_type, MediaType.GAME)
        e, *_ = make({"power_state": "on", "title_id": "CUSA90003"})
        self.assertEqual(e.media_title, "CUSA90003")  # name unknown → title ID
        e, *_ = make({"power_state": "on"})
        self.assertIsNone(e.media_title)
        self.assertIsNone(e.media_content_type)
        e, *_ = make({"power_state": "on", "title_id": "CUSA90003", "not_responding": True})
        self.assertIsNone(e.media_title)  # frozen: no game shown
        self.assertIsNone(e.media_content_id)

    def test_power_state_attribute(self):
        for power in ("on", "rest", "off", None):
            with self.subTest(power=power):
                self.assertEqual(make({"power_state": power})[0].extra_state_attributes,
                                 {"power_state": power, "not_responding": False})
        self.assertEqual(make({"power_state": "on", "not_responding": True})[0].extra_state_attributes,
                         {"power_state": "on", "not_responding": True})

    def test_features(self):
        F = MediaPlayerEntityFeature
        self.assertEqual(make({})[0].supported_features, F.TURN_OFF)  # rest payload needs no setup
        self.assertEqual(make({}, options={"regist_key": KEY})[0].supported_features, F.TURN_ON | F.TURN_OFF)
        self.assertEqual(
            make({}, options={"wake_entity": "button.wake_ps4"})[0].supported_features, F.TURN_ON | F.TURN_OFF
        )


class CoverTests(unittest.TestCase):
    def setUp(self):
        FTP_FETCHES.clear()

    def test_console_icon(self):  # the console's own icon0.png, never a URL (app.db's store link isn't read)
        e, *_ = make({"power_state": "on", "title_id": "CUSA90003"}, {"CUSA90003": {"cover": "/user/appmeta/CUSA90003/icon0.png"}})
        self.assertIsNone(e.media_image_url)
        self.assertEqual(e.media_image_hash, "CUSA90003")
        self.assertEqual(run(e.async_get_media_image()), (b"png:CUSA90003", "image/png"))
        self.assertEqual(FTP_FETCHES, [("192.0.2.1", 2121, "E1", "CUSA90003", "/user/appmeta/CUSA90003/icon0.png")])

    def test_cache_is_per_console(self):  # the same title can have a different icon on another console
        CACHED_COVERS[("OTHER", "CUSA90003")] = b"other console's icon"
        try:
            e, *_ = make({"power_state": "on", "title_id": "CUSA90003"})
            self.assertEqual(run(e.async_get_media_image()), (b"png:CUSA90003", "image/png"))
            self.assertEqual([f[2:4] for f in FTP_FETCHES], [("E1", "CUSA90003")], "fetched from this console")
        finally:
            CACHED_COVERS.clear()

    def test_no_game_no_picture(self):
        for data in ({"power_state": "on"}, {"power_state": "rest", "title_id": "CUSA90003"}, {"power_state": "off"}):
            with self.subTest(data=data):
                e, *_ = make(data)
                self.assertIsNone(e.media_image_hash)
                self.assertEqual(run(e.async_get_media_image()), (None, None))

    def test_fetch_failure_is_no_image(self):
        e, *_ = make({"power_state": "on", "title_id": "CUSA90003"})
        original = mp_mod._read_or_fetch_cover
        mp_mod._read_or_fetch_cover = lambda *a: (_ for _ in ()).throw(OSError("FTP off"))
        try:
            self.assertEqual(run(e.async_get_media_image()), (None, None))
        finally:
            mp_mod._read_or_fetch_cover = original

    def test_cover_fetch_holds_the_ftp_lock(self):
        e, entry_data, _ = make({"power_state": "on", "title_id": "CUSA90003"})
        held = []
        original = mp_mod._read_or_fetch_cover
        mp_mod._read_or_fetch_cover = lambda *a: held.append(entry_data["ftp_lock"].locked()) or b"png"
        try:
            self.assertEqual(run(e.async_get_media_image()), (b"png", "image/png"))
        finally:
            mp_mod._read_or_fetch_cover = original
        self.assertEqual(held, [True], "fetched under the console's FTP lock")
        self.assertFalse(entry_data["ftp_lock"].locked(), "released after")

    def test_cached_cover_no_ftp_and_none_to_a_sleeping_console(self):  # a cover grid asks for every title at once
        CACHED_COVERS.clear()
        CACHED_COVERS[("E1", "CUSA90001")] = b"disk:CUSA90001"
        try:
            for power in ("on", "rest", "off"):
                with self.subTest(power=power):
                    e, entry_data, _ = make({"power_state": power}, GAMES)
                    self.assertEqual(run(e.async_get_browse_image("game", "CUSA90001")), (b"disk:CUSA90001", "image/png"))
                    self.assertNotIn("ftp_lock", entry_data, "a cached cover doesn't wait for the FTP lock")
            for power in ("rest", "off"):
                with self.subTest(power=power):
                    e, *_ = make({"power_state": power}, GAMES)
                    self.assertEqual(run(e.async_get_browse_image("game", "CUSA90003")), (None, None))
            self.assertEqual(FTP_FETCHES, [], "no FTP to a console in rest or off")
            e, *_ = make({"power_state": "on"}, GAMES)
            self.assertEqual(run(e.async_get_browse_image("game", "CUSA90003")), (b"png:CUSA90003", "image/png"))
            self.assertEqual([f[3] for f in FTP_FETCHES], ["CUSA90003"], "on: a missing cover is fetched")
        finally:
            CACHED_COVERS.clear()


class TurnOnTests(unittest.TestCase):
    def setUp(self):
        WAKES.clear()

    def test_key_sends_ddp_wake(self):
        e, _, hass = make({"power_state": "rest"}, options={"regist_key": KEY})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(hass.services.calls, [])

    def test_key_and_wake_entity_in_rest_uses_ddp(self):
        e, _, hass = make({"power_state": "rest"}, options={"regist_key": KEY, "wake_entity": "button.wake_ps4"})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(hass.services.calls, [])

    def test_fully_off_uses_wake_entity(self):
        e, _, hass = make({"power_state": "off"}, options={"regist_key": KEY, "wake_entity": "button.wake_ps4"})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [])
        self.assertEqual(hass.services.calls, [("button", "press", {"entity_id": "button.wake_ps4"})])

    def test_unreachable_says_why(self):
        # a console with "Stay Connected to the Internet" off looks fully off and never wakes
        DDP_CODES.clear()
        LOGINS.clear()
        DDP_CODES.append(None)  # no DDP answer either
        e, _, hass = make({"power_state": "off"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        with self.assertRaises(HomeAssistantError) as raised:
            run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])  # the packet still goes out
        self.assertIn("Stay Connected to the Internet", str(raised.exception))
        self.assertEqual(LOGINS, [])  # no minute-long login wait for a console that isn't there

    def test_off_but_ddp_answers_wakes_as_usual(self):  # HA's "off" was stale: the console rests
        DDP_CODES.clear()
        DDP_CODES.extend([620, 620])
        e, *_ = make({"power_state": "off"}, options={"regist_key": KEY})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])

    def test_wake_entity_only(self):
        for entity_id, call in (
            ("button.wake_ps4", ("button", "press")),
            ("input_button.wake_ps4", ("input_button", "press")),
            ("script.wake_ps4", ("script", "turn_on")),
        ):
            with self.subTest(entity_id=entity_id):
                e, _, hass = make({"power_state": "rest"}, options={"wake_entity": entity_id})
                run(e.async_turn_on())
                self.assertEqual(hass.services.calls, [(*call, {"entity_id": entity_id})])

    def test_no_method_raises(self):
        e, *_ = make({"power_state": "rest"})
        with self.assertRaises(HomeAssistantError):
            run(e.async_turn_on())

    def test_send_failure_raises(self):
        async def broken(host, key):
            raise OSError("network unreachable")

        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY})
        mp_mod.async_wakeup = broken
        try:
            with self.assertRaises(HomeAssistantError):
                run(e.async_turn_on())
        finally:
            mp_mod.async_wakeup = fake_wakeup


class LoginAfterWakeTests(unittest.TestCase):
    """Turn on logs the paired user in after a wake (a short Remote Play session)."""

    def setUp(self):
        WAKES.clear()
        LOGINS.clear()
        DDP_CODES.clear()
        LOGIN_FAILURE.clear()

    def test_wake_from_rest_logs_in(self):
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(LOGINS, [("192.0.2.1", KEY, RP_KEY)])

    def test_no_rp_key_wakes_only(self):  # e.g. a regist key pasted from chiaki-ng
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY})
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(LOGINS, [])

    def test_already_on_no_login(self):
        DDP_CODES.append(200)
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})  # klog lagging
        run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(LOGINS, [])

    def test_on_but_choose_user_screen_logs_in(self):  # e.g. a login that failed earlier, or a wake HA didn't do
        DDP_CODES.append(200)
        e, *_ = make({"power_state": "on", "no_user_logged_in": True}, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_turn_on())
        self.assertEqual(LOGINS, [("192.0.2.1", KEY, RP_KEY)])

    def test_no_ddp_reply_still_tries(self):
        DDP_CODES.append(None)
        e, *_ = make({"power_state": None}, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_turn_on())
        self.assertEqual(len(LOGINS), 1)

    def test_wake_entity_press_no_login(self):
        e, _, hass = make(
            {"power_state": "off"}, options={"regist_key": KEY, "rp_key": RP_KEY, "wake_entity": "button.wake_ps4"}
        )
        run(e.async_turn_on())
        self.assertEqual(hass.services.calls, [("button", "press", {"entity_id": "button.wake_ps4"})])
        self.assertEqual(LOGINS, [])

    def test_login_failure_raises_after_wake(self):
        LOGIN_FAILURE.append("in_use")
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        with self.assertRaises(HomeAssistantError) as ctx:
            run(e.async_turn_on())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertIn("in use", str(ctx.exception))

    def test_second_turn_on_while_logging_in_is_skipped(self):
        e, entry_data, _ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})

        async def go():
            lock = entry_data.setdefault("rp_login_lock", asyncio.Lock())
            async with lock:
                await e.async_turn_on()

        run(go())
        self.assertEqual(WAKES, [("192.0.2.1", KEY)])
        self.assertEqual(LOGINS, [])


class ResumeAfterWakeTests(unittest.TestCase):
    """After turn on logged the user in, the suspended game comes back"""

    def setUp(self):
        for log in (WAKES, LOGINS, DDP_CODES, LOGIN_FAILURE, RESUMES, RESUME_FAILURE, LAUNCHES):
            log.clear()

    def test_wake_from_rest_logs_in_then_resumes(self):
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_turn_on())
        self.assertEqual((len(WAKES), len(LOGINS), RESUMES), (1, 1, ["E1"]))
        self.assertEqual(LAUNCHES, [])

    def test_choose_user_screen_login_resumes(self):  # the native pick-user path resumes the game too
        DDP_CODES.append(200)
        e, *_ = make({"power_state": "on", "no_user_logged_in": True}, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_turn_on())
        self.assertEqual(RESUMES, ["E1"])

    def test_no_login_no_resume(self):
        cases = {
            "no RP-Key": ({"power_state": "rest"}, {"regist_key": KEY}, []),
            "already on": ({"power_state": "rest"}, {"regist_key": KEY, "rp_key": RP_KEY}, [200]),
            "wake entity": ({"power_state": "off"}, {"regist_key": KEY, "rp_key": RP_KEY,
                                                     "wake_entity": "button.wake_ps4"}, []),
        }
        for name, (data, options, ddp) in cases.items():
            with self.subTest(name):
                DDP_CODES[:] = ddp
                e, *_ = make(data, options=options)
                run(e.async_turn_on())
                self.assertEqual(RESUMES, [])

    def test_login_failure_no_resume(self):
        LOGIN_FAILURE.append("in_use")
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        with self.assertRaises(HomeAssistantError):
            run(e.async_turn_on())
        self.assertEqual(RESUMES, [])

    def test_resume_failure_only_warns(self):  # the console is on and logged in: turn on did its job
        RESUME_FAILURE.append("Connection to PS4 BinLoader failed")
        e, *_ = make({"power_state": "rest"}, options={"regist_key": KEY, "rp_key": RP_KEY})
        with self.assertLogs(mp_mod._LOGGER, "WARNING") as logs:
            run(e.async_turn_on())
        self.assertEqual(RESUMES, ["E1"])
        self.assertIn("bringing back the suspended game failed", logs.output[0])


class LaunchFromRestTests(unittest.TestCase):
    """Picking a game in rest wakes + logs in first; the payload resumes it if it's the suspended one"""

    def setUp(self):
        for log in (WAKES, LOGINS, DDP_CODES, LOGIN_FAILURE, RESUMES, LAUNCHES, LAUNCHES_AFTER_WAKE,
                    LAUNCHES_CLOSE_FIRST):
            log.clear()

    def test_app_in_rest_never_closes_the_game(self):  # the suspended game stays for an app too
        e, *_ = make({"power_state": "rest"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_select_source("Apollo Save Tool"))
        self.assertEqual((LAUNCHES, LAUNCHES_AFTER_WAKE, LAUNCHES_CLOSE_FIRST), (["APOL00004"], [True], [False]))

    def test_select_source_in_rest_wakes_logs_in_launches(self):
        e, *_ = make({"power_state": "rest"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_select_source("Game B"))
        self.assertEqual((len(WAKES), len(LOGINS)), (1, 1))
        self.assertEqual((LAUNCHES, LAUNCHES_AFTER_WAKE), (["CUSA90001"], [True]))
        self.assertEqual(RESUMES, [])  # the launch payload resumes the game itself when it's the suspended one

    def test_play_media_in_rest(self):
        e, *_ = make({"power_state": "rest"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_play_media(MediaType.GAME, "CUSA90004"))
        self.assertEqual((LAUNCHES, LAUNCHES_AFTER_WAKE), (["CUSA90004"], [True]))

    def test_choose_user_screen_logs_in_first(self):
        DDP_CODES.append(200)
        e, *_ = make({"power_state": "on", "no_user_logged_in": True}, GAMES,
                     options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_select_source("Game B"))
        self.assertEqual((len(LOGINS), LAUNCHES_AFTER_WAKE), (1, [True]))

    def test_klog_stale_ddp_on_launches_without_login(self):  # klog still says rest after a wake HA didn't see
        DDP_CODES.append(200)
        e, *_ = make({"power_state": "rest"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_select_source("Game B"))
        self.assertEqual((len(WAKES), LOGINS, LAUNCHES_AFTER_WAKE), (1, [], [True]))

    def test_on_launches_directly(self):
        e, *_ = make({"power_state": "on"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        run(e.async_select_source("Game B"))
        self.assertEqual((WAKES, LOGINS, LAUNCHES_AFTER_WAKE), ([], [], [False]))

    def test_refusals_send_nothing(self):
        cases = {
            "fully off": ({"power_state": "off"}, {"regist_key": KEY, "rp_key": RP_KEY,
                                                   "wake_entity": "button.wake_ps4"}),
            "rest, no pairing": ({"power_state": "rest"}, {}),
            "rest, no RP-Key": ({"power_state": "rest"}, {"regist_key": KEY}),
        }
        for name, (data, options) in cases.items():
            with self.subTest(name):
                e, _, hass = make(data, GAMES, options=options)
                with self.assertRaises(HomeAssistantError):
                    run(e.async_select_source("Game B"))
                self.assertEqual((WAKES, LAUNCHES, hass.services.calls), ([], [], []))

    def test_login_failure_no_launch(self):
        LOGIN_FAILURE.append("not_on")
        e, *_ = make({"power_state": "rest"}, GAMES, options={"regist_key": KEY, "rp_key": RP_KEY})
        with self.assertRaises(HomeAssistantError):
            run(e.async_select_source("Game B"))
        self.assertEqual((len(WAKES), LAUNCHES), (1, []))


class TurnOffTests(unittest.TestCase):
    def setUp(self):
        RESTS.clear()
        WAKES.clear()

    def test_turn_off_enters_rest(self):
        e, _, hass = make({"power_state": "on"})
        run(e.async_turn_off())
        self.assertEqual(RESTS, ["E1"])
        self.assertEqual(WAKES, [])
        self.assertEqual(hass.services.calls, [])


class TitleListTests(unittest.TestCase):
    def setUp(self):
        LAUNCHES.clear()
        LAUNCHES_CLOSE_FIRST.clear()
        FTP_FETCHES.clear()

    def test_features_with_games(self):
        F = MediaPlayerEntityFeature
        e, *_ = make({}, GAMES)
        self.assertEqual(e.supported_features, F.TURN_OFF | F.SELECT_SOURCE | F.BROWSE_MEDIA | F.PLAY_MEDIA)

    def test_source_list_games_and_apps_last_played_first(self):
        e, *_ = make({"power_state": "on"}, GAMES)
        self.assertEqual(e.source_list, ["Game C", "Spotify", "Game A\u2122", "Apollo Save Tool", "Game B"])
        self.assertIsNone(e.source)
        self.assertIsNone(make({})[0].source_list)

    def test_source_is_running_title(self):
        e, *_ = make({"power_state": "on", "title_id": "CUSA90001"}, GAMES)
        self.assertEqual((e.source, e.media_content_type), ("Game B", MediaType.GAME))
        e, *_ = make({"power_state": "on", "title_id": "APOL00004"}, GAMES)
        self.assertEqual((e.source, e.media_content_type), ("Apollo Save Tool", MediaType.APP))
        e, *_ = make({"power_state": "rest", "title_id": "CUSA90001"}, GAMES)
        self.assertIsNone(e.source)

    def test_select_source_launches(self):
        e, *_ = make({"power_state": "on"}, GAMES)
        run(e.async_select_source("Game B"))
        run(e.async_select_source("Spotify"))
        self.assertEqual(LAUNCHES, ["CUSA90001", "CUSA90002"])
        self.assertEqual(LAUNCHES_CLOSE_FIRST, [True, False])  # a game closes the running game, an app never
        with self.assertRaises(HomeAssistantError):
            run(e.async_select_source("Library"))  # built-in apps aren't in the list
        self.assertEqual(LAUNCHES, ["CUSA90001", "CUSA90002"])

    def test_play_media_launches_games_and_apps(self):
        e, *_ = make({"power_state": "on"}, GAMES)
        run(e.async_play_media(MediaType.GAME, "CUSA90004"))
        run(e.async_play_media(MediaType.APP, "apol00004"))
        run(e.async_play_media(MediaType.GAME, "CUSA90002"))  # the app.db says app, whatever the type says
        self.assertEqual(LAUNCHES, ["CUSA90004", "apol00004", "CUSA90002"])
        self.assertEqual(LAUNCHES_CLOSE_FIRST, [True, False, False])
        with self.assertRaises(HomeAssistantError):
            run(e.async_play_media("music", "CUSA90004"))
        self.assertEqual(len(LAUNCHES), 3)

    def test_browse_root(self):
        e, *_ = make({}, GAMES)
        root = run(e.async_browse_media())
        self.assertEqual([c.media_content_id for c in root.children], ["recent", "all", "apps"])
        self.assertEqual([c.title for c in root.children], ["Recently played", "All games", "Apps"])
        self.assertTrue(all(c.can_expand and not c.can_play for c in root.children))

    def test_browse_recent_all_and_apps(self):
        e, *_ = make({}, GAMES)
        recent = run(e.async_browse_media("", "recent"))
        self.assertEqual([c.media_content_id for c in recent.children],
                         ["CUSA90003", "CUSA90002", "CUSA90004", "APOL00004", "CUSA90001"])
        all_games = run(e.async_browse_media("", "all"))
        self.assertEqual([c.title for c in all_games.children], ["Game A\u2122", "Game B", "Game C"])
        apps = run(e.async_browse_media("", "apps"))
        self.assertEqual([c.title for c in apps.children], ["Apollo Save Tool", "Spotify"])
        self.assertEqual(apps.children_media_class, MediaClass.APP)
        tile = recent.children[0]
        self.assertEqual((tile.media_class, tile.media_content_type, tile.can_play), (MediaClass.GAME, MediaType.GAME, True))
        self.assertEqual(tile.thumbnail, "/api/media_player_proxy/media_player.ps4/browse_media/game/CUSA90003?token=t")
        self.assertEqual(recent.children_media_class, MediaClass.GAME)
        app_tile = apps.children[0]
        self.assertEqual((app_tile.media_class, app_tile.media_content_type, app_tile.can_play),
                         (MediaClass.APP, MediaType.APP, True))
        self.assertEqual(app_tile.thumbnail, "/api/media_player_proxy/media_player.ps4/browse_media/app/APOL00004?token=t")

    def test_browse_unknown_folder(self):
        e, *_ = make({}, GAMES)
        with self.assertRaises(BrowseError):
            run(e.async_browse_media("", "nope"))

    def test_internet_browser_not_offered(self):  # built in: nowhere in the lists, and play_media refuses it
        browser = {"NPXS20102": game("Internet Browser", "2026-10-16 09:00", ui="app", category="gdi")}
        e, *_ = make({"power_state": "on"}, {**GAMES, **browser})
        apps = run(e.async_browse_media("", "apps"))
        self.assertEqual([c.title for c in apps.children], ["Apollo Save Tool", "Spotify"])
        recent = run(e.async_browse_media("", "recent"))
        self.assertNotIn("NPXS20102", [c.media_content_id for c in recent.children])
        self.assertNotIn("Internet Browser", e.source_list)
        with self.assertRaises(HomeAssistantError):
            run(e.async_play_media(MediaType.APP, "npxs20102"))
        with self.assertRaises(HomeAssistantError):
            run(e.async_select_source("Internet Browser"))
        self.assertEqual(LAUNCHES, [])

    def test_browse_root_without_folders(self):  # no cache, or the user has none: no Folders entry
        LAYOUT_CALLS.clear()
        e, *_ = make({}, GAMES)
        root = run(e.async_browse_media())
        self.assertNotIn("folders", [c.media_content_id for c in root.children])
        self.assertEqual(LAYOUT_CALLS, [("192.0.2.1", None)])  # nobody seen logging in yet

    def test_browse_image_is_console_icon(self):
        e, *_ = make({}, GAMES)
        self.assertEqual(run(e.async_get_browse_image("game", "CUSA90001")), (b"png:CUSA90001", "image/png"))
        self.assertEqual(FTP_FETCHES, [("192.0.2.1", 2121, "E1", "CUSA90001", "/user/appmeta/CUSA90001/icon0.png")])
        self.assertEqual(run(e.async_get_browse_image("game", "CUSA00000")), (None, None))  # not on this console
        self.assertEqual(len(FTP_FETCHES), 1)

    def test_panel_cover_urls(self):  # the Content tab: the media browser's tile URL, found through the entry data
        e, entry_data, _ = make({}, GAMES)
        self.assertNotIn("media_player_entity", entry_data)
        run(e.async_added_to_hass())
        self.assertIs(entry_data["media_player_entity"], e)
        self.assertEqual(e.cover_url("CUSA90001"), "/api/media_player_proxy/media_player.ps4/browse_media/game/CUSA90001?token=t")
        self.assertEqual(e.cover_url("APOL00004"), "/api/media_player_proxy/media_player.ps4/browse_media/app/APOL00004?token=t")
        self.assertEqual(FTP_FETCHES, [], "a URL, no fetch")
        run(e.async_will_remove_from_hass())
        self.assertNotIn("media_player_entity", entry_data)


FOLDERS = {
    "folders": [
        {"id": "000000002", "name": "Racing", "cover": "CUSA90001", "titles": ["CUSA90001", "CUSA90003"]},
        {"id": "000000001", "name": "Apps", "cover": "APOL00004", "titles": ["CUSA90002", "APOL00004"]},
    ],
    "loose": ["CUSA90004", "CUSA77777"],  # CUSA77777: not in this console's game map → no tile
}


class BrowseFolderTests(unittest.TestCase):
    """The logged-in user's home-screen folders (db.folder_layout) under "Folders"."""

    def setUp(self):
        LAYOUT.update(FOLDERS)
        LAYOUT_CALLS.clear()

    def tearDown(self):
        LAYOUT.clear()

    def test_root_has_folders_for_the_logged_in_user(self):
        e, *_ = make({"login_user_id": "1c99f420"}, GAMES)
        root = run(e.async_browse_media())
        self.assertEqual([c.media_content_id for c in root.children], ["recent", "all", "apps", "folders"])
        self.assertEqual(root.children[-1].title, "Folders")
        self.assertEqual(LAYOUT_CALLS, [("192.0.2.1", 0x1C99F420)])

    def test_folders_then_titles_outside_a_folder(self):
        e, *_ = make({}, GAMES)
        view = run(e.async_browse_media("", "folders"))
        self.assertEqual([c.media_content_id for c in view.children],
                         ["folder/000000002", "folder/000000001", "CUSA90004"])
        self.assertEqual([c.title for c in view.children], ["Racing", "Apps", "Game A\u2122"])
        racing, apps, game_a = view.children
        self.assertEqual((racing.media_class, racing.can_expand, racing.can_play), (MediaClass.DIRECTORY, True, False))
        url = "/api/media_player_proxy/media_player.ps4/browse_media/{}?token=t"
        self.assertEqual(racing.thumbnail, url.format("game/CUSA90001"))  # the cover of the title the folder names
        self.assertEqual(apps.thumbnail, url.format("app/APOL00004"))
        self.assertEqual((racing.children_media_class, apps.children_media_class), (MediaClass.GAME, MediaClass.APP))
        self.assertEqual((game_a.media_class, game_a.can_play), (MediaClass.GAME, True))

    def test_folder_in_the_consoles_order(self):
        e, *_ = make({}, GAMES)
        apps = run(e.async_browse_media("", "folder/000000001"))
        self.assertEqual(apps.title, "Apps")
        self.assertEqual([c.title for c in apps.children], ["Spotify", "Apollo Save Tool"])
        self.assertTrue(all(c.media_class == MediaClass.APP and c.can_play for c in apps.children))
        racing = run(e.async_browse_media("", "folder/000000002"))
        self.assertEqual([c.media_content_id for c in racing.children], ["CUSA90001", "CUSA90003"])

    def test_unknown_folder(self):  # e.g. another user logged in since the view was opened
        e, *_ = make({}, GAMES)
        with self.assertRaises(BrowseError):
            run(e.async_browse_media("", "folder/000000009"))


if __name__ == "__main__":
    unittest.main()
