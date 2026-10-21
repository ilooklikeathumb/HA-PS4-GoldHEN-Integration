"""The PS4 GoldHEN Integration."""
from __future__ import annotations

import asyncio
import contextlib
import errno
import io
import json
import logging
import os
import re
import shutil
import socket
from collections import deque
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web
import voluptuous as vol

from homeassistant.components import panel_custom, websocket_api
from homeassistant.components.frontend import StaticPathConfig
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .payload_target import payload_target
from .const import (
    DOMAIN,
    PLATFORMS,
    CONF_PS4_HOST,
    CONF_BINLOADER_PORT,
    CONF_FTP_PORT,
    CONF_RPI_PORT,
    CONF_KLOG_PORT,
    DEFAULT_BINLOADER_PORT,
    DEFAULT_FTP_PORT,
    DEFAULT_RPI_PORT,
    DEFAULT_KLOG_PORT,
    PAYLOAD_DIR,
    COVER_CACHE_DIR,
    TCP_PROBE_TIMEOUT,
    SENSOR_CURRENT_GAME,
    SENSOR_CPU_TEMP,
    SENSOR_SOC_TEMP,
    SENSOR_TITLE_ID,
    SENSOR_GAME_NAME,
    SENSOR_GAME_COVER,
    SENSOR_KLOG_LAST_LINE,
    SENSOR_SOC_POWER,
    SENSOR_CPU_POWER,
    SENSOR_GPU_POWER,
    SENSOR_TOTAL_POWER,
    SENSOR_FAN_DUTY,
    SENSOR_FW_VERSION,
    SENSOR_HW_MODEL,
    SENSOR_CONSOLE_ID,
    signal_klog_line,
    HOME_SCREEN,
    POWER_ON,
    POWER_REST,
    POWER_OFF,
    APP_DB_REMOTE,
    APP_DB_LOCAL,
    DB_REFRESH_INTERVAL,
)
from . import console_info
from . import fan
from . import live_sensors
from . import screenshots
from . import screenshot_entities  # noqa: F401  loaded here, the platforms import it in setup
from . import game_session
from . import play_time
from . import save_backup
from . import save_backup_entities  # noqa: F401  loaded here, the platforms import it in setup
from .entity import device_name
from .ddp import async_search
from . import db as ps4_db
from . import ftp_session
from .ftp_telemetry import async_fetch_telemetry

_LOGGER = logging.getLogger(__name__)

# long on purpose: every poll is an FTP session, and HA keeps to one at a time per console
_FTP_POLL_INTERVAL  = timedelta(minutes=10)
# telemetry poll off — ps4_state.json needs the unfinished PS4State plugin (FTP answers 550: the file isn't there),
# and every poll would be one more FTP session (about 17,000 a day at 5 s) for nothing.
_FTP_TELEMETRY_ENABLED = False
_PS4STATE_JSON_PATH = "/data/GoldHEN/ps4_state.json"
_FORCE_DB_COOLDOWN  = 300  # s between on-demand app.db pulls for the same unknown title
_DB_POWER_WAIT      = 30   # s the first app.db pull waits for the power state
_SVC_SEND_PAYLOAD   = "send_payload"

_PANEL_URL_PATH       = "ps4_goldhen"
_PANEL_SIDEBAR_TITLE  = "PS4 GoldHEN"
_PANEL_SIDEBAR_ICON   = "mdi:sony-playstation"
_PANEL_WEBCOMPONENT   = "ps4-goldhen-panel"

_JS_STATIC_URL            = "/api/ps4_goldhen/frontend/ps4-goldhen-panel.js"
_JS_MODULE_URL            = f"{_JS_STATIC_URL}?v=1.0.0"  # cache bust: change it with every panel change
_LOGO_STATIC_URL          = "/api/ps4_goldhen/frontend/goldhen_logo.png"
_PAYLOAD_ICONS_STATIC_URL = "/api/ps4_goldhen/frontend/payload_icons"

_BUNDLED_PAYLOADS_DIRNAME = "bundled_payloads"

_HOME_SCREEN_STATE  = HOME_SCREEN
_IDLE_STATE         = "Idle"
_HOME_SCREEN_APP_ID = "NPXS20001"

_TITLE_ID_RE = re.compile(r"[A-Z]{4}\d{5}")

_KLOG_SL_FOCUS_PATTERN = re.compile(
    r"\[SL\]\s+AppFocusChanged\s+\[([A-Z0-9]+)\]\s*->\s*\[([A-Z0-9]+)\]",
    re.IGNORECASE,
)
_KLOG_LNC_LAUNCH_PATTERN = re.compile(
    r"\[SceLncService\]\s+launchApp\(([A-Z]{4}\d{5})\)",
    re.IGNORECASE,
)
_KLOG_BGFT_GAME_START = re.compile(
    r"\[BGFT\].*GameWillStart\(([A-Z]{4}\d{5}),",
    re.IGNORECASE,
)
_KLOG_GAME_CLOSE_PATTERN  = re.compile(r"Game Close detected", re.IGNORECASE)
_KLOG_BGFT_GAME_STOPPED   = re.compile(
    r"\[BGFT\].*GameStopped\(([A-Z]{4}\d{5}),",
    re.IGNORECASE,
)
_KLOG_EXIT_TO_HOME_PATTERN = re.compile(
    r"OnFocusActiveSceneChanged\s+\[ApplicationExitScene\s*:\s*ApplicationExitScene\]\s*->\s*\[ContentAreaScene\s*:\s*ContentAreaScene\]",
    re.IGNORECASE,
)
# reasons the game was closed (not left running behind the home screen)
_KLOG_CLOSE_REASONS = frozenset({"game_close_detected", "bgft_game_stopped", "exit_scene_to_home"})

# console power state from SceSystemStateMgr. Without it rest is invisible — in rest ShellUI
# relaunches and logs a home-screen focus line. Names = SystemState enum (OpenOrbis toolchain sys_service.h).
_KLOG_STATE_CHANGE_PATTERN    = re.compile(r"\[SceSystemStateMgr\]\s+State Change:\s+([A-Z_]+)")
_KLOG_KEEPING_STANDBY_PATTERN = re.compile(r"\[SceSystemStateMgr\]\s+Keeping MAIN_ON_STANDBY")  # every 60 s in rest
_KLOG_POWER_MODE_PATTERN      = re.compile(r"\[SceSystemStateMgr\]\s+Power Mode Change:")  # only while WORKING
_KLOG_TRIGGER_CODE_PATTERN    = re.compile(r"\[SceSystemStateMgr\]\s+Trigger Code:\s+([A-Z_]+)")

_SYSTEM_STATE_POWER = {
    "WORKING":           POWER_ON,
    "MAIN_ON_STANDBY":   POWER_REST,  # rest, main CPU kept awake by a background task — klog keeps streaming
    "SUSPEND_ON_GOING":  POWER_REST,  # rest, main CPU suspending — klog goes silent, socket gets reset
    "POWER_SAVING":      POWER_REST,
    "SHUTDOWN_ON_GOING": POWER_OFF,
}
# the choose-user screen. Rest logs every user out, so a wake lands there until someone logs in; the
# Remote Play login (media_player turn on) also runs when the console is already on but sitting on this screen.
_KLOG_USER_SELECT_PATTERN     = re.compile(r"\[UserSelectScene\]\s+CurrentSceneState")
_KLOG_LOGGED_IN_COUNT_PATTERN = re.compile(r"\[SceLncService\]\s+Num\. of logged-in users is (\d+)")
_KLOG_ON_LOGIN_PATTERN        = re.compile(r"\[Theme/I\]\s*:\s*OnLogin\s*:\s*([0-9a-fA-F]{8})")
# klog connected (main CPU up) but no "Keeping MAIN_ON_STANDBY" within this many seconds → the console is WORKING
# (catches a wake whose "State Change: WORKING" line was emitted before klog reconnected)
_REST_HEARTBEAT_GRACE = 90.0
# klog silent this long while the state says rest / off → ask DDP whether the console woke up.
# Also the gap between two such checks (one SRCH a minute at most).
_STALE_KLOG_CHECK = 60.0

_KLOG_NOISE_PATTERNS = (
    re.compile(r"\bD88391\b",                    re.IGNORECASE),
    re.compile(r"\bfrom tbl_appbrowse_",         re.IGNORECASE),
    re.compile(r"\bfrom tblappbrowse",           re.IGNORECASE),
    re.compile(r"^\s*<\d+>\s*=+ bindValue",      re.IGNORECASE),
    re.compile(r"^\s*<\d+>\s*=+ sql\s*=",        re.IGNORECASE),
    re.compile(r"^\s*======== sql\s*=",          re.IGNORECASE),
    re.compile(r"^\s*======== bindValue",        re.IGNORECASE),
    re.compile(r"^\s*======== limit\s*=",        re.IGNORECASE),
    re.compile(r"uhub\d+: giving up port",       re.IGNORECASE),
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _ensure_domain_root(hass: HomeAssistant) -> dict[str, Any]:
    hass.data.setdefault(DOMAIN, {})
    root: dict[str, Any] = hass.data[DOMAIN]
    root.setdefault("_global", {})
    g: dict[str, Any] = root["_global"]
    g.setdefault("panel_registered",           False)
    g.setdefault("frontend_registered",        False)
    g.setdefault("ws_registered",              False)
    g.setdefault("bundled_payloads_installed", False)
    g.setdefault("http_views_registered",      False)
    return root


def _global(hass: HomeAssistant) -> dict[str, Any]:
    return _ensure_domain_root(hass)["_global"]


def _copy_bundled_payloads_to_config() -> int:
    src_dir = Path(__file__).parent / _BUNDLED_PAYLOADS_DIRNAME
    dst_dir = Path(PAYLOAD_DIR)
    if not src_dir.exists() or not src_dir.is_dir():
        return 0
    dst_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for p in sorted(src_dir.iterdir()):
        if not p.is_file() or p.suffix.lower() not in (".bin", ".elf"):
            continue
        dst = dst_dir / p.name
        if dst.exists():
            continue
        shutil.copy2(str(p), str(dst))
        copied += 1
    return copied


def _list_payloads_blocking(payload_dir: str) -> list[str]:
    p = Path(payload_dir)
    p.mkdir(parents=True, exist_ok=True)
    hidden = {"linux.bin"}
    return [
        e.name for e in sorted(p.iterdir(), key=lambda e: e.name)
        if e.is_file()
        and e.name.lower() not in hidden
        and e.suffix.lower() in (".bin", ".elf")
    ]


async def _register_frontend_and_panel_once(hass: HomeAssistant) -> None:
    g = _global(hass)
    if not g["frontend_registered"]:
        payload_icons_dir = hass.config.path(
            f"custom_components/{DOMAIN}/frontend/payload_icons"
        )
        await hass.async_add_executor_job(
            partial(os.makedirs, payload_icons_dir, exist_ok=True)
        )
        await hass.http.async_register_static_paths(
            [
                StaticPathConfig(
                    _JS_STATIC_URL,
                    hass.config.path(
                        f"custom_components/{DOMAIN}/frontend/ps4-goldhen-panel.js"
                    ),
                    False,
                ),
                StaticPathConfig(
                    _LOGO_STATIC_URL,
                    hass.config.path(
                        f"custom_components/{DOMAIN}/frontend/goldhen_logo.png"
                    ),
                    False,
                ),
                StaticPathConfig(
                    _PAYLOAD_ICONS_STATIC_URL,
                    payload_icons_dir,
                    False,
                ),
            ]
        )
        g["frontend_registered"] = True

    if not g["panel_registered"]:
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=_PANEL_URL_PATH,
            webcomponent_name=_PANEL_WEBCOMPONENT,
            module_url=_JS_MODULE_URL,
            sidebar_title=_PANEL_SIDEBAR_TITLE,
            sidebar_icon=_PANEL_SIDEBAR_ICON,
            config={},
            require_admin=True,  # admin-only panel
        )
        g["panel_registered"] = True


async def _send_bin_tcp(
    host: str, port: int, filepath: str, timeout: float = 30.0
) -> None:
    loop = asyncio.get_running_loop()
    try:
        data = await loop.run_in_executor(None, lambda: open(filepath, "rb").read())
    except Exception as err:
        raise HomeAssistantError(f"Cannot read payload file {filepath}: {err}") from err
    await _send_bin_bytes(host, port, data, os.path.basename(filepath), timeout)


_PAYLOAD_GAP = 3.0  # s from the end of one payload send to the start of the next one to the same console
_payload_queues: dict[str, asyncio.Lock] = {}  # per console host: one send at a time
_payload_last: dict[str, float] = {}  # per console host: loop time the last send ended


async def _send_bin_bytes(host: str, port: int, data: bytes, name: str, timeout: float = 30.0) -> None:
    """Send payload bytes built in memory (the launch payload carries the title ID).

    Every payload HA sends comes through here: one send at a time per console, at least _PAYLOAD_GAP apart. BinLoader
    takes one payload per connection, and the next connection waits until the previous payload has had time to load.
    """
    async with _payload_queues.setdefault(host, asyncio.Lock()):
        loop = asyncio.get_running_loop()
        wait = _payload_last.get(host, float("-inf")) + _PAYLOAD_GAP - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _LOGGER.info("Sending payload %s to %s:%d", name, host, port)
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.write(data)
            await asyncio.wait_for(writer.drain(), timeout=timeout)
            writer.close()
            await writer.wait_closed()
            _LOGGER.info("Payload sent successfully.")
        except Exception as err:
            raise HomeAssistantError(
                f"Connection to PS4 BinLoader failed: {err}"
            ) from err
        finally:
            _payload_last[host] = loop.time()


def _is_real_game_title_id(value: str | None) -> bool:
    if not value:
        return False
    value = value.strip().upper()
    return bool(_TITLE_ID_RE.fullmatch(value)) and not value.startswith("NPXS")


# ── Cover image disk cache ─────────────────────────────────────────────────────

def _cover_cache_path(tid: str) -> Path:
    return Path(COVER_CACHE_DIR) / f"{tid.upper()}.png"


def _fetch_cover_ftp(host: str, ftp_port: int, cover_path: str) -> bytes:
    """Blocking FTP fetch of a cover image from the PS4."""
    import ftplib
    buf = io.BytesIO()
    with ftplib.FTP() as ftp:
        ftp.connect(host, ftp_port, timeout=15)
        ftp.login()
        ftp.retrbinary(f"RETR {cover_path}", buf.write)
    return buf.getvalue()


def _read_or_fetch_cover(host: str, ftp_port: int, tid: str, cover_path: str) -> bytes:
    """
    Blocking helper — returns cover PNG bytes.
    1. Check /config/ps4/covers/{TID}.png — return cached bytes if present.
    2. FTP-fetch from PS4, write to cache, return bytes.
    """
    cache_file = _cover_cache_path(tid)

    if cache_file.exists():
        _LOGGER.debug("[ps4_goldhen] Cover cache hit: %s", cache_file)
        return cache_file.read_bytes()

    _LOGGER.debug("[ps4_goldhen] Cover cache miss for %s — fetching via FTP", tid)
    img_bytes = _fetch_cover_ftp(host, ftp_port, cover_path)

    # Persist to cache
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(img_bytes)
        _LOGGER.debug("[ps4_goldhen] Cover cached: %s", cache_file)
    except Exception as err:
        _LOGGER.warning(
            "[ps4_goldhen] Could not write cover cache for %s: %s", tid, err
        )

    return img_bytes


# ── Klog state machine ─────────────────────────────────────────────────────────

class KlogStateMachine:
    def __init__(self) -> None:
        self.current_title_id: str | None = None
        self.last_reason       = "init"
        self.last_signal_line  = ""
        self.recent_lines: deque[str] = deque(maxlen=250)
        self.klog_connected: bool = False
        self._pending_launch: str | None = None
        self.power_state: str | None = None    # POWER_ON / POWER_REST / POWER_OFF, None = unknown
        self.power_trigger: str | None = None  # last SceSystemStateMgr Trigger Code (REMOTE_PLAY, HDMI_CEC, MISC…)
        self._connected_at: float | None = None
        self._rest_or_off_seen_since_connect = False
        self.no_user_logged_in: bool = False  # on the choose-user screen
        # the game left running behind the home screen (PS button) or in rest; None once it's closed,
        # another game takes focus or the console goes off. Save backup backs it up when it's left.
        self.suspended_title_id: str | None = None

    def snapshot(self) -> dict[str, Any]:
        state = self.current_title_id if self.current_title_id else _HOME_SCREEN_STATE
        return {
            SENSOR_CURRENT_GAME:  state,
            SENSOR_TITLE_ID:      self.current_title_id,
            "state_reason":       self.last_reason,
            "state_signal_line":  self.last_signal_line,
            "pending_title_id":   self._pending_launch,
            "klog_connected":     self.klog_connected,
            "power_state":        self.power_state,
            "power_trigger":      self.power_trigger,
            "no_user_logged_in":  self.no_user_logged_in,
            "suspended_title_id": self.suspended_title_id,
        }

    def ingest(self, line: str) -> bool:
        self.recent_lines.append(line[-300:])
        self.klog_connected = True

        for pattern in _KLOG_NOISE_PATTERNS:
            if pattern.search(line):
                return False

        m = _KLOG_STATE_CHANGE_PATTERN.search(line)
        if m:
            power = _SYSTEM_STATE_POWER.get(m.group(1))
            return self._set_power(power, "state_change_" + m.group(1).lower(), line) if power else False

        if _KLOG_KEEPING_STANDBY_PATTERN.search(line):
            return self._set_power(POWER_REST, "keeping_main_on_standby", line)

        if _KLOG_POWER_MODE_PATTERN.search(line):
            return self._set_power(POWER_ON, "power_mode_change", line)

        # choose-user screen (rest logs everyone out) ↓
        if _KLOG_USER_SELECT_PATTERN.search(line):
            return self._set_no_user(True)
        m = _KLOG_LOGGED_IN_COUNT_PATTERN.search(line)
        if m:
            return self._set_no_user(m.group(1) == "0")
        m = _KLOG_ON_LOGIN_PATTERN.search(line)
        if m:  # OnLogin : ffffffff = nobody (the Welcome screen logs it)
            return self._set_no_user(False) if m.group(1).lower() != "ffffffff" else False

        m = _KLOG_TRIGGER_CODE_PATTERN.search(line)
        if m:
            changed = self.power_trigger != m.group(1)
            self.power_trigger = m.group(1)
            return changed

        m = _KLOG_SL_FOCUS_PATTERN.search(line)
        if m:
            new_app = m.group(2).strip().upper()
            if _is_real_game_title_id(new_app):
                # a game taking focus means someone is playing, even if the WORKING line was missed
                power_changed = self._set_power(POWER_ON, "sl_focus_game", line)
                return self._set(new_app, "sl_focus_game", line) or power_changed
            elif new_app == _HOME_SCREEN_APP_ID:
                if self._pending_launch:
                    _LOGGER.debug(
                        "Ignoring sl_focus_home — launch pending for %s",
                        self._pending_launch,
                    )
                    return False
                if self.current_title_id is not None:
                    return self._set(None, "sl_focus_home", line)
            return False

        m = _KLOG_BGFT_GAME_START.search(line)
        if m:
            tid = m.group(1).strip().upper()
            if _is_real_game_title_id(tid):
                self._pending_launch = tid
                return self._set(tid, "bgft_game_will_start", line)

        m = _KLOG_LNC_LAUNCH_PATTERN.search(line)
        if m:
            tid = m.group(1).strip().upper()
            if _is_real_game_title_id(tid):
                self._pending_launch = tid
                self.last_reason      = "lnc_launch_pending"
                self.last_signal_line = line[-300:]
                return False

        if _KLOG_GAME_CLOSE_PATTERN.search(line):
            if self.current_title_id is not None:
                return self._set(None, "game_close_detected", line)
            return self._clear_suspended(None, "game_close_detected", line)

        m = _KLOG_BGFT_GAME_STOPPED.search(line)
        if m:
            tid = m.group(1).strip().upper()
            if self.current_title_id == tid:
                return self._set(None, "bgft_game_stopped", line)
            return self._clear_suspended(tid, "bgft_game_stopped", line)

        if _KLOG_EXIT_TO_HOME_PATTERN.search(line):
            if self._pending_launch:
                _LOGGER.debug(
                    "Ignoring exit_scene_to_home — launch pending for %s",
                    self._pending_launch,
                )
                return False
            if self.current_title_id is not None:
                return self._set(None, "exit_scene_to_home", line)
            return self._clear_suspended(None, "exit_scene_to_home", line)

        return False

    def _set(self, title_id: str | None, reason: str, line: str) -> bool:
        changed                = self.current_title_id != title_id
        # a game losing focus without a close line keeps running behind the home screen / in rest
        if title_id is not None:
            self.suspended_title_id = None
        elif self.current_title_id is not None and reason not in _KLOG_CLOSE_REASONS:
            self.suspended_title_id = self.current_title_id
        self.current_title_id  = title_id
        self._pending_launch   = None
        self.last_reason       = reason
        self.last_signal_line  = line[-300:]
        return changed

    def _clear_suspended(self, title_id: str | None, reason: str, line: str) -> bool:
        """The background game closed (title_id None = whichever one it is)."""
        if self.suspended_title_id is None or title_id not in (None, self.suspended_title_id):
            return False
        self.suspended_title_id = None
        self.last_reason        = reason
        self.last_signal_line   = line[-300:]
        return True

    def _set_no_user(self, value: bool) -> bool:
        changed = self.no_user_logged_in != value
        self.no_user_logged_in = value
        return changed

    # power state ↓
    def _set_power(self, power: str, reason: str, line: str) -> bool:
        if power != POWER_ON:
            # seen live on this connection → the heartbeat rule in tick() must not override it
            self._rest_or_off_seen_since_connect = True
            self.no_user_logged_in = False  # only means something while on; the wake logs it again
        if power == self.power_state:
            return False
        self.power_state = power
        if power != POWER_ON:
            # the game is suspended / gone — don't keep showing its name and cover
            self._set(None, reason, line)
            if power == POWER_OFF:
                self.suspended_title_id = None  # nothing survives a shutdown
        else:
            self.last_reason      = reason
            self.last_signal_line = line[-300:]
        return True

    def on_klog_connected(self, now: float) -> None:
        self._connected_at = now
        self._rest_or_off_seen_since_connect = False

    def on_klog_unreachable(self) -> bool:
        """Connect failed with EHOSTUNREACH: no ARP reply, so the console is fully off (rest still answers and
        refuses with 111). Also covers a shutdown whose klog lines HA never saw, e.g. HA was restarting."""
        return self._set_power(POWER_OFF, "klog_host_unreachable", "")

    def on_ddp_on(self) -> bool:
        """DDP answered 200 (on) while klog still said rest / off, so the klog socket went stale."""
        return self._set_power(POWER_ON, "ddp_on_klog_stale", "")

    def on_ddp_status(self, code: int, title_id: str | None) -> bool:
        """DDP's answer at entry setup and each klog connect. Klog only learns the game from the next
        AppFocusChanged, so without this an HA restart / reload shows the home screen while a game runs.
        `200` = on; `running-app-titleid` = the running game, also while it's in the background (still reported
        after the PS button), as HA core's ps4 shows it. Never overrides what klog has seen."""
        if code != 200 or self._rest_or_off_seen_since_connect:
            return False
        changed = False
        if self.power_state != POWER_ON:
            self.power_state      = POWER_ON
            self.last_reason      = "ddp_on_at_connect"
            self.last_signal_line = ""
            changed = True
        if self.current_title_id is None and _is_real_game_title_id(title_id):
            changed = self._set(title_id.strip().upper(), "ddp_running_app", "") or changed
        return changed

    def tick(self, now: float) -> bool:
        """Klog only connects while the main CPU runs (WORKING or MAIN_ON_STANDBY), and MAIN_ON_STANDBY logs a
        'Keeping' line every 60 s. Connected for the grace period without one → the console is on."""
        if (
            self.power_state != POWER_ON
            and self.klog_connected
            and self._connected_at is not None
            and not self._rest_or_off_seen_since_connect
            and now - self._connected_at >= _REST_HEARTBEAT_GRACE
        ):
            self.power_state      = POWER_ON
            self.last_reason      = "klog_up_no_rest_heartbeat"
            self.last_signal_line = ""
            return True
        return False

    def restore_power_state(self, power: str | None) -> None:
        """Seed from the sensor's pre-restart state, so an HA restart during deep rest (klog refused) still says rest."""
        if self.power_state is None and power in (POWER_REST, POWER_OFF):
            self.power_state = power
            self.last_reason = "restored"


# ── Klog line parser ───────────────────────────────────────────────────────────

async def _force_db_refresh(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: DataUpdateCoordinator,
) -> None:
    entry_data = hass.data[DOMAIN].get(entry_id)
    if not entry_data:
        return
    try:
        game_map = await ftp_session.run(
            hass, entry_data, ps4_db.download_and_parse, entry_data["host"], entry_data["ftp_port"]
        )
        entry_data["game_map"] = game_map
        klog_data = entry_data["klog_data"]
        tid = klog_data.get(SENSOR_TITLE_ID)
        if tid and tid in game_map:
            klog_data[SENSOR_GAME_NAME]  = game_map[tid].get("name")
            klog_data[SENSOR_GAME_COVER] = game_map[tid].get("cover")
            coordinator.async_set_updated_data(
                {**(coordinator.data or {}), **klog_data}
            )
        _LOGGER.info(
            "[ps4_goldhen] force db refresh done — %d titles", len(game_map)
        )
    except Exception as err:
        _LOGGER.warning("[ps4_goldhen] force db refresh failed: %s", err)
    finally:
        entry_data["force_db_running"] = False


def _parse_klog_line(
    hass: HomeAssistant,
    line: str,
    entry_data: dict[str, Any],
    entry_id: str,
) -> bool:
    state_machine: KlogStateMachine = entry_data["klog_state_machine"]

    for pattern in _KLOG_NOISE_PATTERNS:
        if pattern.search(line):
            return False

    state_changed = state_machine.ingest(line)

    klog_data = entry_data["klog_data"]
    klog_data.update(state_machine.snapshot())
    klog_data[SENSOR_KLOG_LAST_LINE] = line[:300]

    tid = klog_data.get(SENSOR_TITLE_ID)
    if tid:
        game_info = entry_data.get("game_map", {}).get(tid, {})
        klog_data[SENSOR_GAME_NAME]  = game_info.get("name")
        klog_data[SENSOR_GAME_COVER] = game_info.get("cover")
        # game_map empty or title missing — refresh app.db immediately
        # one pull in flight, max one per title per _FORCE_DB_COOLDOWN: klog sends many lines a second,
        # and HA keeps to one FTP session per console
        if not game_info.get("name"):
            coordinator = entry_data.get("coordinator")
            now = hass.loop.time()
            last = entry_data.setdefault("force_db_last", {}).get(tid, 0.0)
            if coordinator and not entry_data.get("force_db_running") and now - last >= _FORCE_DB_COOLDOWN:
                entry_data["force_db_last"][tid] = now
                entry_data["force_db_running"] = True
                asyncio.ensure_future(
                    _force_db_refresh(hass, entry_id, coordinator)
                )
    else:
        klog_data[SENSOR_GAME_NAME]  = None
        klog_data[SENSOR_GAME_COVER] = None

    # no bus event per klog line: the recorder would store every one
    return state_changed


# ── Background tasks ───────────────────────────────────────────────────────────

def _klog_power_tick(hass: HomeAssistant, entry_id: str, coordinator: DataUpdateCoordinator) -> None:
    """Run the rest-heartbeat check while klog is quiet (a WORKING console can be silent)."""
    entry_data = hass.data[DOMAIN].get(entry_id)
    if entry_data and entry_data["klog_state_machine"].tick(hass.loop.time()):
        entry_data["klog_data"].update(entry_data["klog_state_machine"].snapshot())
        coordinator.async_set_updated_data({**(coordinator.data or {}), **entry_data["klog_data"]})


def async_apply_ddp_status(hass: HomeAssistant, entry_id: str, reply) -> None:
    """Power + running game from a DDP reply when klog doesn't know them yet (`on_ddp_status`)."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not entry_data or reply is None:
        return
    state_machine = entry_data["klog_state_machine"]
    if not state_machine.on_ddp_status(reply.code, reply.headers.get("running-app-titleid")):
        return
    klog_data = entry_data["klog_data"]
    klog_data.update(state_machine.snapshot())
    tid = klog_data.get(SENSOR_TITLE_ID)
    game_info = entry_data.get("game_map", {}).get(tid, {}) if tid else {}
    klog_data[SENSOR_GAME_NAME]  = game_info.get("name") or (reply.headers.get("running-app-name") if tid else None)
    klog_data[SENSOR_GAME_COVER] = game_info.get("cover")
    _LOGGER.debug("DDP at connect: power %s, game %s", state_machine.power_state, tid)
    coordinator = entry_data["coordinator"]
    coordinator.async_set_updated_data({**(coordinator.data or {}), **klog_data})


async def _klog_stale_check(
    hass: HomeAssistant, entry_id: str, host: str, coordinator: DataUpdateCoordinator
) -> bool:
    """True if klog went stale (the state says rest / off, DDP says on); the caller then reconnects.

    A wake from deep rest (SUSPEND_ON_GOING) that didn't come from HA can leave HA's pre-rest klog socket open
    (keepalives answered) but silent: HA shows rest while the console is in use and refuses every call until a reload.
    DDP `SRCH` goes to Sony's own discovery service (UDP 987), not a GoldHEN port: `200` = on, `620` = rest.
    """
    entry_data = hass.data[DOMAIN].get(entry_id)
    if not entry_data:
        return False
    state_machine = entry_data["klog_state_machine"]
    power = state_machine.power_state
    if power not in (POWER_REST, POWER_OFF):
        return False
    try:
        reply = await async_search(host)
    except OSError as err:
        _LOGGER.debug("Stale klog check: DDP to %s failed: %s", host, err)
        return False
    if reply is None or reply.code != 200:
        return False
    _LOGGER.warning("Klog at %s silent while HA showed %s, but DDP says the PS4 is on: reopening klog", host, power)
    if state_machine.on_ddp_on():
        entry_data["klog_data"].update(state_machine.snapshot())
        coordinator.async_set_updated_data({**(coordinator.data or {}), **entry_data["klog_data"]})
    return True


async def _klog_listener_task(
    hass: HomeAssistant,
    entry_id: str,
    host: str,
    port: int,
    coordinator: DataUpdateCoordinator,
) -> None:
    _LOGGER.info("Starting klog listener for %s:%d", host, port)
    # one WARNING per outage: a console on another subnet that's off times out every 20 s until the router
    # answers "unreachable" (otherwise ~30 lines in 10 min per console)
    failing = False

    while True:
        writer = None
        reconnect_delay = 10
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
            _LOGGER.info("Connected to klog at %s:%d", host, port)
            failing = False
            # TCP keepalive so a PS4 that vanishes (rest/reboot/GoldHEN reload, no FIN) is detected
            sock = writer.get_extra_info("socket")
            if sock is not None:
                with contextlib.suppress(Exception):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)

            entry_data = hass.data[DOMAIN].get(entry_id)
            if entry_data:
                entry_data["klog_state_machine"].klog_connected = True
                entry_data["klog_state_machine"].on_klog_connected(hass.loop.time())
                entry_data["klog_data"]["klog_connected"] = True
                coordinator.async_set_updated_data(
                    {**(coordinator.data or {}), **entry_data["klog_data"]}
                )
                console_info.async_on_klog_connected(hass, entry_id)
                live_sensors.async_on_klog_connected(hass, entry_id)

            text_buffer = ""
            next_stale_check = hass.loop.time() + _STALE_KLOG_CHECK

            while True:
                try:
                    chunk = await asyncio.wait_for(reader.read(4096), timeout=30.0)
                except asyncio.TimeoutError as err:
                    if err.errno is not None:
                        # ETIMEDOUT = keepalive gave up (console lost power / froze). Same class as
                        # wait_for's timeout, but the dead reader re-raises without yielding: looping here would
                        # spin the event loop and run HA out of memory. Leave the socket like any other error.
                        raise
                    # A quiet klog connection isn't dead (an idle console logs little); HA keeps it open.
                    # The keepalive above catches a dead one.
                    _klog_power_tick(hass, entry_id, coordinator)
                    if hass.loop.time() >= next_stale_check:
                        next_stale_check = hass.loop.time() + _STALE_KLOG_CHECK
                        if await _klog_stale_check(hass, entry_id, host, coordinator):
                            reconnect_delay = 1
                            break
                    continue

                if not chunk:
                    _LOGGER.warning("Klog connection closed by PS4")
                    break
                next_stale_check = hass.loop.time() + _STALE_KLOG_CHECK

                text_buffer += chunk.decode("utf-8", errors="replace")
                lines        = text_buffer.split("\n")
                text_buffer  = lines[-1][-65536:]

                entry_data = hass.data[DOMAIN].get(entry_id)
                if not entry_data:
                    break

                changed = False
                for line in lines[:-1]:
                    line = line.rstrip("\r")
                    # the console-info payload's line (model, serial, firmware) is kept out of the Klog tab
                    # (and the recorder), and so are the live sensors payload's lines (one every 30 s)
                    if line and (console_info.async_handle_line(hass, entry_id, line)
                                 or live_sensors.async_handle_line(hass, entry_id, line)):
                        continue
                    if line:
                        # dispatcher, not the event bus: the recorder would keep every line as an event
                        async_dispatcher_send(hass, signal_klog_line(entry_id), line[:8192])
                    if line and _parse_klog_line(hass, line, entry_data, entry_id):
                        changed = True

                state_machine = entry_data["klog_state_machine"]
                if state_machine.tick(hass.loop.time()):
                    entry_data["klog_data"].update(state_machine.snapshot())
                    changed = True

                if changed:
                    coordinator.async_set_updated_data(
                        {**(coordinator.data or {}), **entry_data["klog_data"]}
                    )

        except asyncio.CancelledError:
            _LOGGER.info("Klog listener task cancelled")
            raise
        except Exception as err:
            err_str = str(err)
            if getattr(err, "errno", None) in (errno.EHOSTUNREACH, errno.EHOSTDOWN):
                _LOGGER.debug("Klog host unreachable (PS4 off) %s:%d: %s", host, port, err)
                entry_data = hass.data[DOMAIN].get(entry_id)
                if entry_data and entry_data["klog_state_machine"].on_klog_unreachable():
                    entry_data["klog_data"].update(entry_data["klog_state_machine"].snapshot())
            elif "111" in err_str or "Connect call failed" in err_str:
                _LOGGER.debug(
                    "Klog unavailable (PS4 off/rest) %s:%d: %s", host, port, err
                )
            elif failing:
                _LOGGER.debug("Klog connection error for %s:%d: %s", host, port, err_str or type(err).__name__)
            else:
                # a connect timeout has no message ("...:3232: " in the log)
                _LOGGER.warning(
                    "Klog connection error for %s:%d: %s (until it connects again, repeats go to the debug log)",
                    host, port, err_str or type(err).__name__,
                )
                failing = True

        finally:
            if writer is not None:
                writer.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)

        entry_data = hass.data[DOMAIN].get(entry_id)
        if entry_data:
            entry_data["klog_state_machine"].klog_connected = False
            entry_data["klog_data"]["klog_connected"] = False
            coordinator.async_set_updated_data(
                {**(coordinator.data or {}), **entry_data["klog_data"]}
            )

        _LOGGER.info("Reconnecting to klog in %ds...", reconnect_delay)
        await asyncio.sleep(reconnect_delay)


async def _db_refresh_task(
    hass: HomeAssistant,
    entry_id: str,
    coordinator: DataUpdateCoordinator,
) -> None:
    # at HA start the power state comes a few seconds later (restore, klog, DDP); a resting console's
    # first pull would still log the FTP WARNING. Wait for it, in memory only, up to _DB_POWER_WAIT.
    for _ in range(_DB_POWER_WAIT // 2):
        entry_data = hass.data[DOMAIN].get(entry_id)
        if not entry_data:
            return
        if entry_data["klog_state_machine"].power_state is not None:
            break
        await asyncio.sleep(2)

    while True:
        entry_data = hass.data[DOMAIN].get(entry_id)
        if not entry_data:
            return

        host     = entry_data["host"]
        ftp_port = entry_data["ftp_port"]

        # no pull while the console is in rest / off. FTP refuses then, and each try would log a WARNING
        # (2 an hour with two consoles). A game starting later still pulls at once (_force_db_refresh).
        if entry_data["klog_state_machine"].power_state in (POWER_REST, POWER_OFF):
            _LOGGER.debug("app.db refresh skipped for %s: the console is in rest or off", host)
            if not entry_data.get("game_map"):
                cached = await hass.async_add_executor_job(ps4_db.load_cached, host)
                if cached:
                    entry_data["game_map"] = cached
            await asyncio.sleep(DB_REFRESH_INTERVAL)
            continue

        try:
            game_map = await ftp_session.run(hass, entry_data, ps4_db.download_and_parse, host, ftp_port)
            entry_data["game_map"] = game_map
            _LOGGER.info(
                "app.db refreshed for %s — %d titles loaded", host, len(game_map)
            )

            klog_data = entry_data["klog_data"]
            tid = klog_data.get(SENSOR_TITLE_ID)
            if tid and tid in game_map:
                klog_data[SENSOR_GAME_NAME]  = game_map[tid].get("name")
                klog_data[SENSOR_GAME_COVER] = game_map[tid].get("cover")
                coordinator.async_set_updated_data(
                    {**(coordinator.data or {}), **klog_data}
                )

        except asyncio.CancelledError:
            _LOGGER.info("DB refresh task cancelled")
            raise
        except Exception as err:
            _LOGGER.warning("app.db refresh failed for %s: %s", host, err)

        await asyncio.sleep(DB_REFRESH_INTERVAL)


# ── WebSocket commands ─────────────────────────────────────────────────────────

def _cached_firmware(hass: HomeAssistant, entry_id: str) -> str | None:
    """The console's firmware from the console info; None for an unloaded entry or before the first read."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    return ((entry_data.get("console_info") or {}).get("info") or {}).get("firmware")


@websocket_api.websocket_command({vol.Required("type"): "ps4_goldhen/list_entries"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_list_entries(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    entries = hass.config_entries.async_entries(DOMAIN)
    out = [
        {
            "entry_id":       entry.entry_id,
            "title":          entry.title,
            "name":           device_name(hass, entry),  # the picker shows the console's HA name
            "ps4_host":       entry.data.get(CONF_PS4_HOST),
            "ftp_port":       entry.data.get(CONF_FTP_PORT,       DEFAULT_FTP_PORT),
            "binloader_port": entry.data.get(CONF_BINLOADER_PORT, DEFAULT_BINLOADER_PORT),
            "klog_port":      entry.data.get(CONF_KLOG_PORT,      DEFAULT_KLOG_PORT),
            "rpi_port":       entry.data.get(CONF_RPI_PORT,       DEFAULT_RPI_PORT),
            # the panel names it before a payload send; None until the console info has a firmware
            "firmware":       _cached_firmware(hass, entry.entry_id),
        }
        for entry in entries
    ]
    connection.send_result(msg["id"], {"entries": out})


@websocket_api.websocket_command({vol.Required("type"): "ps4_goldhen/list_payloads"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_list_payloads(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: dict,
) -> None:
    try:
        items = await hass.async_add_executor_job(
            _list_payloads_blocking, PAYLOAD_DIR
        )
        connection.send_result(
            msg["id"], {"payloads": items, "payload_dir": PAYLOAD_DIR}
        )
    except Exception as err:
        connection.send_error(msg["id"], "list_error", str(err))


# ── FTP JSON poll ──────────────────────────────────────────────────────────────

async def _poll_ftp_json(
    host: str,
    ftp_port: int,
    entry_id: str,
    hass: HomeAssistant,
    coordinator: DataUpdateCoordinator,
) -> dict[str, Any]:
    entry_data = _ensure_domain_root(hass).get(entry_id, {})
    if not _FTP_TELEMETRY_ENABLED:  # no FTP session at all
        existing = dict(entry_data.get("klog_data", {}))
        existing["ftp_reachable"] = None
        existing["telemetry_status"] = "disabled"
        entry_data.setdefault("klog_data", {}).update(ftp_reachable=None, telemetry_status="disabled")
        return existing
    async with ftp_session.lock(entry_data):
        result = await async_fetch_telemetry(host, ftp_port, _PS4STATE_JSON_PATH)
    # Re-read after awaiting so concurrent klog updates are not overwritten.
    existing = dict(entry_data.get("klog_data", {}))
    dynamic_keys = (
        SENSOR_CPU_TEMP, SENSOR_SOC_TEMP, SENSOR_SOC_POWER,
        SENSOR_CPU_POWER, SENSOR_GPU_POWER, SENSOR_TOTAL_POWER,
        SENSOR_FAN_DUTY,
    )
    for key in dynamic_keys:
        existing[key] = result.values.get(key) if result.values is not None else None
    if result.values is not None:
        for key in (SENSOR_FW_VERSION, SENSOR_HW_MODEL, SENSOR_CONSOLE_ID):
            existing[key] = result.values.get(key)
    existing["ftp_reachable"] = result.ftp_reachable
    existing["telemetry_status"] = result.status
    if result.status != "ok":
        _LOGGER.debug("PS4 telemetry unavailable (%s): %s", host, result.status)
    root = _ensure_domain_root(hass)
    if root.get(entry_id) is entry_data:
        entry_data.setdefault("klog_data", {}).update(existing)
    return existing



# ── Config entry setup ─────────────────────────────────────────────────────────

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    host           = entry.data[CONF_PS4_HOST]
    binloader_port = entry.data.get(CONF_BINLOADER_PORT, DEFAULT_BINLOADER_PORT)
    ftp_port       = entry.data.get(CONF_FTP_PORT,       DEFAULT_FTP_PORT)
    rpi_port       = entry.data.get(CONF_RPI_PORT,       DEFAULT_RPI_PORT)
    klog_port      = entry.data.get(CONF_KLOG_PORT,      DEFAULT_KLOG_PORT)

    root          = _ensure_domain_root(hass)
    state_machine = KlogStateMachine()

    root[entry.entry_id] = {
        "host":               host,
        "binloader_port":     binloader_port,
        "ftp_port":           ftp_port,
        "rpi_port":           rpi_port,
        "klog_port":          klog_port,
        "klog_state_machine": state_machine,
        "klog_data": {
            **state_machine.snapshot(),
            SENSOR_CPU_TEMP:       None,
            SENSOR_SOC_TEMP:       None,
            SENSOR_GAME_NAME:      None,
            SENSOR_GAME_COVER:     None,
            SENSOR_KLOG_LAST_LINE: None,
            SENSOR_SOC_POWER:      None,
            SENSOR_CPU_POWER:      None,
            SENSOR_GPU_POWER:      None,
            SENSOR_TOTAL_POWER:    None,
            SENSOR_FAN_DUTY:       None,
            SENSOR_FW_VERSION:     None,
            SENSOR_HW_MODEL:       None,
            SENSOR_CONSOLE_ID:     None,
            "ftp_reachable":       False,
        },
        "game_map": {},
    }

    async def _poll_update() -> dict[str, Any]:
        return await _poll_ftp_json(host, ftp_port, entry.entry_id, hass, coordinator)

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{host}",
        update_method=_poll_update,
        update_interval=_FTP_POLL_INTERVAL if _FTP_TELEMETRY_ENABLED else None,
    )

    await coordinator.async_config_entry_first_refresh()
    root[entry.entry_id]["coordinator"] = coordinator
    await console_info.async_setup(hass, entry)  # cached model / serial / firmware
    live_sensors.async_setup(hass, entry)  # live temperatures + fan duty
    await fan.async_load(hass, entry)  # what Fan control off goes back to
    await screenshots.async_setup(hass, entry, coordinator)  # off until turned on in the panel
    await play_time.async_setup(hass, entry)  # before game_session, which it listens to
    game_session.async_setup(hass, entry, coordinator)  # game started / left signals
    await save_backup.async_setup(hass, entry, coordinator)  # off until turned on in the panel

    klog_task = entry.async_create_background_task(
        hass,
        _klog_listener_task(hass, entry.entry_id, host, klog_port, coordinator),
        name=f"{DOMAIN}_klog_{entry.entry_id}",
    )
    root[entry.entry_id]["klog_task"] = klog_task

    db_task = entry.async_create_background_task(
        hass,
        _db_refresh_task(hass, entry.entry_id, coordinator),
        name=f"{DOMAIN}_db_{entry.entry_id}",
    )
    root[entry.entry_id]["db_task"] = db_task

    g = _global(hass)

    if not g["ws_registered"]:
        websocket_api.async_register_command(hass, ws_list_entries)
        websocket_api.async_register_command(hass, ws_list_payloads)
        from .websocket import async_setup as async_setup_websocket
        async_setup_websocket(hass)
        g["ws_registered"] = True

    await _register_frontend_and_panel_once(hass)

    if not g.get("bundled_payloads_installed"):
        await hass.async_add_executor_job(_copy_bundled_payloads_to_config)
        g["bundled_payloads_installed"] = True

    if not g.get("http_views_registered"):
        hass.http.register_view(PS4FTPDownloadView())
        hass.http.register_view(PS4FTPUploadView())
        g["http_views_registered"] = True

    _SEND_PAYLOAD_SCHEMA = vol.Schema(
        {
            vol.Required("payload_file"): str,
            vol.Optional("ps4_host"): str,
            # still accepted so older automations don't fail; ignored (the console's own port is used)
            vol.Optional("binloader_port"): vol.All(
                vol.Coerce(int), vol.Range(min=1024, max=65535)
            ),
            vol.Optional("timeout", default=30): vol.All(
                vol.Coerce(float), vol.Range(min=1)
            ),
        }
    )

    async def handle_send_payload(call: ServiceCall) -> None:
        p_file   = call.data["payload_file"]
        # one of THIS integration's consoles, on its own BinLoader port. The service is registered once
        # (first entry), so it must not close over that entry's host (or every payload would go to the first console).
        try:
            t_host, t_port = payload_target(
                (e.data for e in hass.config_entries.async_entries(DOMAIN)),
                call.data.get("ps4_host"),
                host_key=CONF_PS4_HOST,
                port_key=CONF_BINLOADER_PORT,
                default_port=DEFAULT_BINLOADER_PORT,
            )
        except ValueError as err:
            raise HomeAssistantError(str(err)) from err
        # payloads only from PAYLOAD_DIR (never any other HA path)
        filepath = os.path.realpath(os.path.join(PAYLOAD_DIR, os.path.basename(p_file)))
        if os.path.dirname(filepath) != os.path.realpath(PAYLOAD_DIR):
            raise ValueError("payload_file must be a file in " + PAYLOAD_DIR)
        await _send_bin_tcp(t_host, t_port, filepath, call.data.get("timeout", 30))

    if not hass.services.has_service(DOMAIN, _SVC_SEND_PAYLOAD):
        hass.services.async_register(
            DOMAIN, _SVC_SEND_PAYLOAD, handle_send_payload,
            schema=_SEND_PAYLOAD_SCHEMA,
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


# ── HTTP views ─────────────────────────────────────────────────────────────────

class PS4FTPDownloadView(HomeAssistantView):
    url          = "/api/ps4_goldhen/ftp/download"
    name         = "api:ps4_goldhen:ftp_download"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        import ftplib
        if not request["hass_user"].is_admin:
            return web.Response(text="Admin only", status=403)
        entry_id = request.query.get("entry_id")
        path     = request.query.get("path")
        if not entry_id or not path:
            return web.Response(text="Missing entry_id or path", status=400)
        data = _ensure_domain_root(request.app["hass"]).get(entry_id)
        if not data:
            return web.Response(text="Entry not found", status=404)
        def _get_file():
            buf = io.BytesIO()
            with ftplib.FTP() as ftp:
                ftp.connect(data["host"], int(data["ftp_port"]), timeout=15)
                ftp.login()
                ftp.retrbinary(f"RETR {path}", buf.write)
            return buf.getvalue()
        try:
            content = await ftp_session.run(request.app["hass"], data, _get_file, wait=ftp_session.FTP_BUSY_WAIT)
            return web.Response(
                body=content,
                content_type="application/octet-stream",
                headers={
                    "Content-Disposition":
                        f'attachment; filename="{os.path.basename(path)}"'
                },
            )
        except ftp_session.FTPBusy as err:
            return web.Response(text=str(err), status=503)
        except Exception as err:
            return web.Response(text=f"FTP Error: {err}", status=500)


class PS4FTPUploadView(HomeAssistantView):
    url          = "/api/ps4_goldhen/ftp/upload"
    name         = "api:ps4_goldhen:ftp_upload"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        import ftplib
        if not request["hass_user"].is_admin:
            return web.Response(text="Admin only", status=403)
        reader   = await request.multipart()
        entry_id = path = file_field = None
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "entry_id":
                entry_id = (await part.read(decode=True)).decode()
            elif part.name == "path":
                path = (await part.read(decode=True)).decode()
            elif part.name == "file":
                file_field = part
                break
        if not all([entry_id, path, file_field]):
            return web.Response(text="Missing data", status=400)
        data = _ensure_domain_root(request.app["hass"]).get(entry_id)
        if not data:
            return web.Response(text="Entry not found", status=404)
        full_dest = (path.rstrip("/") + "/" + file_field.filename).replace("//", "/")
        def _upload(content):
            with ftplib.FTP() as ftp:
                ftp.connect(data["host"], int(data["ftp_port"]), timeout=15)
                ftp.login()
                ftp.storbinary(f"STOR {full_dest}", io.BytesIO(content))
        try:
            content = await file_field.read(decode=True)  # before the lock: the browser's upload isn't FTP time
            await ftp_session.run(request.app["hass"], data, _upload, content, wait=ftp_session.FTP_BUSY_WAIT)
            return web.json_response({"success": True, "path": full_dest})
        except ftp_session.FTPBusy as err:
            return web.Response(text=str(err), status=503)
        except Exception as err:
            return web.Response(text=f"FTP Upload Error: {err}", status=500)


# ── Teardown ───────────────────────────────────────────────────────────────────

async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = _ensure_domain_root(hass).get(entry.entry_id)
    if entry_data:
        await play_time.async_unload(hass, entry)  # written before a reload reads it
        for task_key in ("klog_task", "db_task"):
            t = entry_data.get(task_key)
            if t and not t.done():
                t.cancel()
                with contextlib.suppress(Exception):
                    await asyncio.gather(t, return_exceptions=True)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _ensure_domain_root(hass).pop(entry.entry_id, None)
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """A deleted console takes its cached console info with it."""
    await console_info.async_remove(hass, entry)
    await fan.async_remove(hass, entry)
    await screenshots.async_remove(hass, entry)  # settings + thumbnail cache (copies stay)
    await play_time.async_remove(hass, entry)
    await save_backup.async_remove(hass, entry)  # settings + download zips (backups stay)
