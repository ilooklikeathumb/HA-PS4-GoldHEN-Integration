"""PS4 screenshots in HA: Latest screenshot thumbnail, Sync latest, Sync to HA's media folder.

Off by default, per console, set in the panel's Screenshots tab. Settings live in .storage, not the entry options:
saving options reloads the entry, which drops klog. Copies go to <media>/PS4 Screenshots/<console>/<game>/<file>;
the console folder is fixed when screenshots are first turned on, so renaming the device doesn't split the copies.
Copies are one-way and add-only: HA never writes to the PS4 and never deletes a copy to make room (the storage limit
and a 1 GB free-disk floor stop a copy instead). The only deletes are the panel's "Delete N…", which asks every
time: HA copy and/or PS4 original. The console's own thumbnails (~11 KB) are cached in
/config/ps4/screenshots/<entry>/thumbs, at most 200 per console; a full walk drops those of shots gone from the PS4.
HA keeps to one FTP session per console: every session here takes the entry's FTP lock (shared with the cover fetch),
a sync reconnects every 25 files so the cover fetch and app.db pull get a turn, and a refused connect is retried.
A console in rest mode or off gets no FTP: the panel's list shows HA's last read and the actions that need the
console refuse. Console-side layout and the newest-shot walk: capture_files.py.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
import ftplib
from functools import partial
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import capture_files as cf
from .const import DOMAIN, POWER_OFF, POWER_ON, POWER_REST, PS4_CONFIG_DIR, SENSOR_TITLE_ID
from .dynamic_entities import DynamicEntities
from .entity import device_name
from .ftp_session import session as _session  # shared with save backup

_LOGGER = logging.getLogger(__name__)

MODE_ON_REQUEST = "on_request"  # copies only when asked: Sync all / Sync N / the viewer's Sync / the device button
MODES = (MODE_ON_REQUEST,)
VIEW_MAX_BYTES = 32 << 20     # a full-size shot read from the PS4 for the viewer (a 4K PNG is ~10–15 MB)

LIMIT_MIN_GB, LIMIT_MAX_GB, DEFAULT_LIMIT_GB = 0.5, 50.0, 2.0
MEDIA_FOLDER = "PS4 Screenshots"
CACHE_ROOT = Path(PS4_CONFIG_DIR) / "screenshots"
THUMB_CAP = 200
PAGE_MAX = 96
MIN_FREE_BYTES = 1 << 30
FILES_PER_SESSION = 25
_AUTO_DELAY_ON = 30.0         # s after the console comes on (klog settles, the cover fetch goes first)
_AUTO_DELAY_LEFT_GAME = 10.0  # s after the game on screen changes or closes
_PLAN_TTL = 120.0             # s a full walk is reused by the Sync that follows its dialog

# last_sync["state"]: the panel's result line after a copy
STATUS_SYNCED = "synced"
STATUS_LIMIT = "limit_reached"
STATUS_FAILED = "failed"
# a sensor this version doesn't have: its registry entry is removed at setup
_OLD_SENSOR_UNIQUE_ID = "{domain}_{entry_id}_screenshot_sync_status"

_STORE_VERSION = 1
_DEFAULTS: dict[str, Any] = {
    "enabled": False, "mode": MODE_ON_REQUEST, "limit_gb": DEFAULT_LIMIT_GB,
    "folder": None, "latest": None, "last_sync": None,
}
# Which device-page entities exist for the current settings
_WANTED: dict[str, Callable[[dict[str, Any]], bool]] = {
    "image": lambda s: s["enabled"],
    "button": lambda s: s["enabled"],
}


@dataclass
class _Runtime:
    entry: ConfigEntry
    store: Store
    settings: dict[str, Any]
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one plan / copy at a time per console
    syncing: bool = False
    progress: dict[str, int] = field(default_factory=lambda: {"done": 0, "total": 0})
    usage: dict[str, int] = field(default_factory=lambda: {"count": 0, "bytes": 0})
    names: set[str] = field(default_factory=set)  # file names of the copies in HA (the folder is the truth)
    plan: tuple[float, list[dict[str, Any]]] | None = None
    shown: dict[str, Any] | None = None  # the panel's last list read: {read_at, shots}. Memory only.
    prev: tuple[Any, Any] = (None, None)
    auto_cancel: Callable[[], None] | None = None
    dynamic: DynamicEntities = field(init=False)

    def __post_init__(self) -> None:
        self.dynamic = DynamicEntities(lambda platform: _WANTED[platform](self.settings))


def signal(entry_id: str) -> str:
    return f"{DOMAIN}_screenshots_{entry_id}"


def runtime(hass: HomeAssistant, entry_id: str) -> _Runtime | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data.get("screenshots") if isinstance(data, dict) else None


def _all_runtimes(hass: HomeAssistant) -> list[_Runtime]:
    return [d["screenshots"] for d in hass.data.get(DOMAIN, {}).values() if isinstance(d, dict) and "screenshots" in d]


def _require(hass: HomeAssistant, entry_id: str) -> _Runtime:
    rt = runtime(hass, entry_id)
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    if not rt.settings["enabled"]:
        raise HomeAssistantError("Screenshots are off for this PS4 (panel → Screenshots)")
    return rt


def _media_root(hass: HomeAssistant) -> Path:
    dirs = hass.config.media_dirs or {}
    return Path(dirs.get("local") or next(iter(dirs.values()), None) or hass.config.path("media"))


def console_dir(hass: HomeAssistant, rt: _Runtime) -> Path | None:
    folder = rt.settings.get("folder")
    return _media_root(hass) / MEDIA_FOLDER / folder if folder else None


def _cache_dir(entry_id: str) -> Path:
    return CACHE_ROOT / entry_id / "thumbs"


def _limit_bytes(rt: _Runtime) -> int:
    return int(float(rt.settings["limit_gb"]) * (1 << 30))


def _disk_room(path: Path) -> int:
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free - MIN_FREE_BYTES


def _game_map(hass: HomeAssistant, entry_id: str) -> dict[str, dict[str, Any]]:
    return hass.data[DOMAIN][entry_id].get("game_map", {})


def _ftp_error(err: BaseException) -> HomeAssistantError:
    return HomeAssistantError(f"PS4 not reachable over FTP (is it on, with GoldHEN's FTP enabled?): {err}")


# ── Setup ──────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.screenshots.{entry.entry_id}")
    rt = _Runtime(entry, store, {**_DEFAULTS, **(await store.async_load() or {})})
    if rt.settings["mode"] not in MODES:  # any other saved mode reads as On request
        rt.settings["mode"] = MODE_ON_REQUEST
    hass.data[DOMAIN][entry.entry_id]["screenshots"] = rt
    registry = er.async_get(hass)
    old_id = _OLD_SENSOR_UNIQUE_ID.format(domain=DOMAIN, entry_id=entry.entry_id)
    if old_sensor := registry.async_get_entity_id("sensor", DOMAIN, old_id):
        registry.async_remove(old_sensor)
    if rt.settings["enabled"]:
        await _async_rescan(hass, rt)
    entry.async_on_unload(coordinator.async_add_listener(partial(_on_coordinator, hass, entry.entry_id, coordinator)))
    entry.async_on_unload(partial(_cancel_auto, rt))
    _register_once(hass)


async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted: its settings and thumbnail cache go. The copies in the media folder stay (they're the user's)."""
    await Store(hass, _STORE_VERSION, f"{DOMAIN}.screenshots.{entry.entry_id}").async_remove()
    await hass.async_add_executor_job(partial(shutil.rmtree, CACHE_ROOT / entry.entry_id, ignore_errors=True))


@callback
def register_platform(hass: HomeAssistant, entry: ConfigEntry, platform: str, add_entities: Callable,
                      factory: Callable[[], list]) -> None:
    """Called from a platform's setup: its screenshot entities come and go with the settings, without a reload."""
    rt = runtime(hass, entry.entry_id)
    if rt is not None:
        rt.dynamic.register(hass, platform, add_entities, factory)


@callback
def _on_coordinator(hass: HomeAssistant, entry_id: str, coordinator) -> None:
    """Refresh the Latest screenshot thumbnail when the console comes on or a game is left (copies stay manual)."""
    rt = runtime(hass, entry_id)
    if rt is None:
        return
    data = coordinator.data or {}
    power, tid = data.get("power_state"), data.get(SENSOR_TITLE_ID)
    prev_power, prev_tid = rt.prev
    rt.prev = (power, tid)
    if not rt.settings["enabled"]:
        return
    if power == POWER_ON and prev_power != POWER_ON:
        _schedule_auto(hass, rt, _AUTO_DELAY_ON)
    elif prev_tid and tid != prev_tid:
        _schedule_auto(hass, rt, _AUTO_DELAY_LEFT_GAME)


def _cancel_auto(rt: _Runtime) -> None:
    if rt.auto_cancel is not None:
        rt.auto_cancel()
        rt.auto_cancel = None


@callback
def _schedule_auto(hass: HomeAssistant, rt: _Runtime, delay: float) -> None:
    _cancel_auto(rt)

    @callback
    def _fire(_now) -> None:
        rt.auto_cancel = None
        rt.entry.async_create_background_task(hass, _async_auto_pass(hass, rt), f"{DOMAIN}_screenshots_auto")

    rt.auto_cancel = async_call_later(hass, delay, _fire)


async def _async_auto_pass(hass: HomeAssistant, rt: _Runtime) -> None:
    entry_id = rt.entry.entry_id
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if data is None or not rt.settings["enabled"] or rt.op_lock.locked():
        return
    if (data["coordinator"].data or {}).get("power_state") != POWER_ON:
        return  # rest / off: no FTP to a sleeping console; the next power-on catches up
    try:
        async with rt.op_lock:
            await _async_newest(hass, rt, 1)
    except ftplib.all_errors as err:
        _LOGGER.debug("Screenshot check for %s skipped: %s", data.get("host"), err)


# ── Shared steps ───────────────────────────────────────────────────────────────

async def _async_rescan(hass: HomeAssistant, rt: _Runtime) -> dict[str, int]:
    folder = console_dir(hass, rt)
    copies = await hass.async_add_executor_job(cf.scan_copies, folder) if folder else {}
    rt.usage = {"count": len(copies), "bytes": sum(copies.values())}
    rt.names = set(copies)
    return copies


async def _async_save(hass: HomeAssistant, rt: _Runtime) -> None:
    await rt.store.async_save(rt.settings)
    async_dispatcher_send(hass, signal(rt.entry.entry_id))


async def _async_newest(hass: HomeAssistant, rt: _Runtime, count: int, offset: int = 0,
                        title_id: str | None = None) -> list[dict[str, Any]]:
    """The newest offset+count shots (+1 to know if there are more), thumbnails for the page and the latest shot.
    With `title_id`: one game's shots (the game page), and the console's latest shot stays as it was."""
    cache = _cache_dir(rt.entry.entry_id)

    def _work(ftp: ftplib.FTP) -> list[dict[str, Any]]:
        shots = cf.newest_shots(ftp, offset + count + 1, title_id)
        cf.fetch_thumbs(ftp, cache, shots[:1] + shots[offset:offset + count])
        return shots

    shots = await _session(hass, rt.entry.entry_id, _work)
    used = [cf.thumb_local(cache, s["rel"]) for s in shots[:1] + shots[offset:offset + count]]
    await hass.async_add_executor_job(cf.touch, used)
    await hass.async_add_executor_job(cf.prune_thumbs, cache, THUMB_CAP)
    if title_id is not None:
        return shots
    latest = shots[0] if shots else None
    if latest != rt.settings["latest"]:
        rt.settings["latest"] = latest
        await _async_save(hass, rt)
    return shots


async def _async_copy(hass: HomeAssistant, rt: _Runtime, shots: list[dict[str, Any]]) -> dict[str, int]:
    """Copy the given shots that aren't in HA yet, newest first, within the limit and the free-disk floor.

    Caller holds rt.op_lock. Raises the FTP / disk error after recording it on the sync status.
    """
    entry_id = rt.entry.entry_id
    if console_dir(hass, rt) is None:
        raise HomeAssistantError("No folder for this PS4 yet: turn screenshots off and on again")
    folder: Path = console_dir(hass, rt)  # type: ignore[assignment]
    copies = await _async_rescan(hass, rt)
    todo = [s for s in shots if s["name"] not in copies]
    room = min(_limit_bytes(rt) - rt.usage["bytes"], await hass.async_add_executor_job(_disk_room, folder))
    fit, skipped = cf.newest_that_fit(todo, max(room, 0))
    game_map = _game_map(hass, entry_id)
    rt.progress = {"done": 0, "total": len(fit)}
    rt.syncing = True
    async_dispatcher_send(hass, signal(entry_id))
    error: str | None = None
    try:
        for start in range(0, len(fit), FILES_PER_SESSION):
            chunk = fit[start:start + FILES_PER_SESSION]

            def _work(ftp: ftplib.FTP, chunk: list[dict[str, Any]] = chunk) -> None:
                for shot in chunk:
                    dest = folder / cf.game_folder(shot["title_id"], game_map) / shot["name"]
                    cf.download(ftp, cf.photo_remote(shot["rel"]), dest)
                    rt.progress["done"] += 1  # read by the panel's status poll; one writer

            await _session(hass, entry_id, _work)
    except (*ftplib.all_errors, HomeAssistantError) as err:
        error = str(err) or type(err).__name__
        raise
    finally:
        rt.syncing = False
        state = STATUS_FAILED if error else STATUS_LIMIT if skipped else STATUS_SYNCED
        rt.settings["last_sync"] = {
            "state": state, "finished": dt_util.utcnow().isoformat(), "copied": rt.progress["done"],
            "skipped": len(skipped), "error": error,
        }
        await _async_rescan(hass, rt)
        await _async_save(hass, rt)
    return {"copied": rt.progress["done"], "skipped": len(skipped), "already": len(shots) - len(todo)}


def _check_free(rt: _Runtime) -> None:
    if rt.op_lock.locked():
        raise HomeAssistantError("Busy: a screenshot sync is running for this PS4")


def _asleep(hass: HomeAssistant, entry_id: str) -> bool:
    return _power(hass, entry_id) in (POWER_REST, POWER_OFF)


def _check_awake(hass: HomeAssistant, entry_id: str) -> None:
    if _asleep(hass, entry_id):
        raise HomeAssistantError("The PS4 is in rest mode or off: turn it on first (HA doesn't use FTP on a sleeping PS4)")


# ── Actions (button + panel) ───────────────────────────────────────────────────

async def async_import_latest(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Device-page button: copy the newest shot. No confirmation (one file, add-only)."""
    rt = _require(hass, entry.entry_id)
    _check_awake(hass, entry.entry_id)
    _check_free(rt)
    try:
        async with rt.op_lock:
            shots = await _async_newest(hass, rt, 1)
            if not shots:
                raise HomeAssistantError("No screenshots on this PS4")
            result = await _async_copy(hass, rt, shots[:1])
    except ftplib.all_errors as err:
        raise _ftp_error(err) from err
    if result["skipped"]:
        raise HomeAssistantError(
            f"Not copied: it doesn't fit under this PS4's {rt.settings['limit_gb']} GB screenshot limit "
            "(or HA would have less than 1 GB free). Raise the limit in the panel's Screenshots tab."
        )


async def async_set_settings(hass: HomeAssistant, entry_id: str, changes: dict[str, Any]) -> None:
    rt = runtime(hass, entry_id)
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    turned_on = changes.get("enabled") and not rt.settings["enabled"]
    rt.settings.update(changes)
    if not rt.settings["enabled"]:
        rt.shown = None
    if rt.settings["enabled"] and not rt.settings["folder"]:
        base = cf.safe_name(device_name(hass, rt.entry)) or "PS4"
        taken = {o.settings.get("folder") for o in _all_runtimes(hass) if o is not rt}
        name, n = base, 2
        while name in taken:
            name, n = f"{base} ({n})", n + 1
        rt.settings["folder"] = name
    await _async_rescan(hass, rt)
    await _async_save(hass, rt)
    rt.dynamic.reconcile(hass)
    if turned_on:
        _schedule_auto(hass, rt, 1.0)  # fill the Latest screenshot thumbnail


def settings_view(hass: HomeAssistant, rt: _Runtime) -> dict[str, Any]:
    folder = console_dir(hass, rt)
    return {
        "enabled": rt.settings["enabled"], "mode": rt.settings["mode"], "limit_gb": rt.settings["limit_gb"],
        "limit_min_gb": LIMIT_MIN_GB, "limit_max_gb": LIMIT_MAX_GB,
        "folder": str(folder) if folder else None,
        "usage": rt.usage, "syncing": rt.syncing, "progress": rt.progress, "last_sync": rt.settings["last_sync"],
        "latest": rt.settings["latest"], "power_state": _power(hass, rt.entry.entry_id),
    }


def _power(hass: HomeAssistant, entry_id: str) -> str | None:
    coordinator = (hass.data.get(DOMAIN, {}).get(entry_id) or {}).get("coordinator")
    return ((coordinator.data if coordinator else None) or {}).get("power_state")


def _thumb_data(cache: Path, rels: list[str]) -> dict[str, str]:
    out = {}
    for rel in rels:
        path = cf.thumb_local(cache, rel)
        if path.is_file():
            out[rel] = "data:image/jpeg;base64," + base64.b64encode(path.read_bytes()).decode()
    return out


async def async_list(hass: HomeAssistant, entry_id: str, offset: int, count: int,
                     title_id: str | None = None) -> dict[str, Any]:
    """One page of the newest shots for the panel, thumbnails inline (~15 KB each as base64). A console in rest mode or
    off gets no FTP: the page comes from HA's last read (memory only, so none after a restart). With `title_id`: one
    game's shots for the game page, none while the console sleeps (HA's last read is of the newest shots only)."""
    rt = _require(hass, entry_id)
    power = _power(hass, entry_id)
    asleep = power in (POWER_REST, POWER_OFF)
    if asleep:
        shots = rt.shown["shots"] if rt.shown and title_id is None else []
    else:
        try:
            shots = await _async_newest(hass, rt, count, offset, title_id)
        except ftplib.all_errors as err:
            raise _ftp_error(err) from err
        if title_id is None:
            rt.shown = {"read_at": dt_util.utcnow().isoformat(), "shots": shots[:offset + count]}
    page = shots[offset:offset + count]
    copies = await _async_rescan(hass, rt)
    thumbs = await hass.async_add_executor_job(_thumb_data, _cache_dir(entry_id), [s["rel"] for s in page])
    game_map = _game_map(hass, entry_id)
    return {
        "shots": [
            {**s, "game": cf.game_folder(s["title_id"], game_map), "in_ha": s["name"] in copies,
             "thumb": thumbs.get(s["rel"])}
            for s in page
        ],
        "more": len(shots) > offset + count,
        "asleep": asleep, "power_state": power,
        "read_at": rt.shown["read_at"] if rt.shown and title_id is None else None,
    }


async def async_plan(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Full walk for the Sync dialog: what's new, per game, and how much room is left."""
    rt = _require(hass, entry_id)
    _check_awake(hass, entry_id)
    _check_free(rt)
    async with rt.op_lock:
        try:
            shots = await _session(hass, entry_id, cf.all_shots)
        except ftplib.all_errors as err:
            raise _ftp_error(err) from err
        rt.plan = (time.monotonic(), shots)
        await hass.async_add_executor_job(
            partial(cf.prune_thumbs, _cache_dir(entry_id), THUMB_CAP, on_console={s["rel"] for s in shots})
        )
        copies = await _async_rescan(hass, rt)
        folder = console_dir(hass, rt)
        room_disk = await hass.async_add_executor_job(_disk_room, folder) if folder else 0
    game_map = _game_map(hass, entry_id)
    new = [s for s in shots if s["name"] not in copies]
    return {
        "total": len(shots), "in_ha": len(shots) - len(new),
        "new": [
            {"rel": s["rel"], "title_id": s["title_id"], "game": cf.game_folder(s["title_id"], game_map),
             "size": s["size"], "modified_utc": s["modified_utc"]}
            for s in new
        ],
        "used_bytes": rt.usage["bytes"], "limit_bytes": _limit_bytes(rt),
        "room_bytes": max(0, min(_limit_bytes(rt) - rt.usage["bytes"], room_disk)),
    }


def _pick(shots: list[dict[str, Any]], scope: dict[str, Any]) -> list[dict[str, Any]]:
    kind = scope["kind"]
    if kind == "game":
        return [s for s in shots if s["title_id"] == scope.get("title_id")]
    if kind == "newest":
        return shots[: max(0, int(scope.get("count") or 0))]  # shots are newest first
    if kind == "selected":
        wanted = set(scope.get("rels") or [])
        return [s for s in shots if s["rel"] in wanted]
    return list(shots)


async def async_start_sync(hass: HomeAssistant, entry_id: str, scope: dict[str, Any]) -> int:
    """Start a copy in the background (it outlives the panel); returns how many new shots it will try."""
    rt = _require(hass, entry_id)
    _check_awake(hass, entry_id)
    _check_free(rt)
    await rt.op_lock.acquire()
    try:
        fresh = rt.plan and time.monotonic() - rt.plan[0] < _PLAN_TTL
        if scope["kind"] == "selected" and not fresh:
            rels = [r for r in scope.get("rels") or [] if cf.valid_rel(r)]
            shots = await _session(hass, entry_id, lambda ftp: [s for r in rels if (s := cf.shot_by_rel(ftp, r))])
        elif scope["kind"] == "game" and not fresh:  # the game page: that game's folders only, no full walk
            shots = await _session(hass, entry_id, partial(cf.all_shots, title_id=str(scope.get("title_id") or "")))
        else:
            shots = rt.plan[1] if fresh else await _session(hass, entry_id, cf.all_shots)  # type: ignore[index]
        copies = await _async_rescan(hass, rt)
        if scope["kind"] == "newest":  # "the newest N" means N of the new ones
            shots = [s for s in shots if s["name"] not in copies]
        todo = [s for s in _pick(shots, scope) if s["name"] not in copies]
    except ftplib.all_errors as err:
        rt.op_lock.release()
        raise _ftp_error(err) from err
    except BaseException:
        rt.op_lock.release()
        raise
    rt.plan = None

    async def _run() -> None:
        try:
            await _async_copy(hass, rt, todo)
        except (*ftplib.all_errors, HomeAssistantError) as err:
            _LOGGER.warning("Screenshot sync for %s stopped: %s", hass.data[DOMAIN][entry_id].get("host"), err)
        finally:
            rt.op_lock.release()

    rt.entry.async_create_background_task(hass, _run(), f"{DOMAIN}_screenshots_sync")
    return len(todo)


async def async_import_one(hass: HomeAssistant, entry_id: str, rel: str) -> dict[str, Any]:
    """A card click on a shot that's only on the PS4: copy it now (awaited, one file)."""
    rt = _require(hass, entry_id)
    if not cf.valid_rel(rel):
        raise HomeAssistantError("Invalid screenshot path")
    _check_awake(hass, entry_id)
    _check_free(rt)
    try:
        async with rt.op_lock:
            shot = await _session(hass, entry_id, partial(cf.shot_by_rel, rel=rel))
            if shot is None:
                raise HomeAssistantError("That screenshot is no longer on the PS4")
            result = await _async_copy(hass, rt, [shot])
    except ftplib.all_errors as err:
        raise _ftp_error(err) from err
    return {**result, "in_ha": not result["skipped"]}


async def async_delete(hass: HomeAssistant, entry_id: str, rels: list[str], *, ha: bool, ps4: bool) -> dict[str, int]:
    """Panel "Delete N…". The user picks per delete: HA's copies (`ha`) and/or the PS4 originals
    (`ps4`, permanent). The PS4 side is the only place this integration ever deletes a capture on the console."""
    rt = _require(hass, entry_id)
    if not ha and not ps4:
        raise HomeAssistantError("Nothing to delete: tick HA copy and/or PS4 original")
    if not rels or not all(cf.valid_rel(r) for r in rels):
        raise HomeAssistantError("Invalid screenshot path")
    if ps4:
        _check_awake(hass, entry_id)  # before HA's copies go: a refused delete deletes nothing
    _check_free(rt)
    result = {"ha_deleted": 0, "ps4_deleted": 0}
    async with rt.op_lock:
        try:
            folder = console_dir(hass, rt)
            if ha and folder is not None:
                names = [r.rsplit("/", 1)[1] for r in rels]
                result["ha_deleted"] = await hass.async_add_executor_job(
                    lambda: sum(cf.delete_copy(folder, n) for n in names)
                )
            if ps4:
                try:
                    files = await _session(hass, entry_id, lambda ftp: [cf.delete_shot(ftp, r) for r in rels])
                except ftplib.all_errors as err:
                    raise _ftp_error(err) from err
                result["ps4_deleted"] = sum(1 for n in files if n)
                cache = _cache_dir(entry_id)
                await hass.async_add_executor_job(
                    lambda: [cf.thumb_local(cache, r).unlink(missing_ok=True) for r in rels]
                )
                rt.plan = None
                if rt.shown:
                    rt.shown["shots"] = [s for s in rt.shown["shots"] if s["rel"] not in rels]
                if (rt.settings.get("latest") or {}).get("rel") in rels:
                    try:
                        await _async_newest(hass, rt, 1)  # the Latest screenshot moves to the next newest
                    except ftplib.all_errors as err:
                        _LOGGER.debug("Latest screenshot refresh after a delete failed: %s", err)
        finally:
            await _async_rescan(hass, rt)
            await _async_save(hass, rt)
    return result


async def async_latest_thumb(hass: HomeAssistant, entry_id: str) -> bytes | None:
    rt = runtime(hass, entry_id)
    latest = rt.settings.get("latest") if rt else None
    if not latest:
        return None
    path = cf.thumb_local(_cache_dir(entry_id), latest["rel"])
    return await hass.async_add_executor_job(lambda: path.read_bytes() if path.is_file() else None)


# ── Panel API (admin only, like the panel) ─────────────────────────────────────

def _ws_handler(fn: Callable) -> Callable:
    """HomeAssistantError → a WS error the panel shows."""
    async def _wrapped(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
        try:
            connection.send_result(msg["id"], await fn(hass, msg))
        except HomeAssistantError as err:
            connection.send_error(msg["id"], "screenshots_error", str(err))
    return _wrapped


def _cmd(schema: dict, fn: Callable) -> Callable:
    return websocket_api.websocket_command(schema)(websocket_api.require_admin(websocket_api.async_response(
        _ws_handler(fn))))


async def _ws_get(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    rt = runtime(hass, msg["entry_id"])
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    return settings_view(hass, rt)


async def _ws_set(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    changes = {k: msg[k] for k in ("enabled", "mode", "limit_gb") if k in msg}
    await async_set_settings(hass, msg["entry_id"], changes)
    return settings_view(hass, runtime(hass, msg["entry_id"]))  # type: ignore[arg-type]


async def _ws_list(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_list(hass, msg["entry_id"], msg["offset"], msg["count"], msg.get("title_id"))


async def _ws_plan(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_plan(hass, msg["entry_id"])


async def _ws_sync(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    scope = {k: msg[k] for k in ("kind", "title_id", "count", "rels") if k in msg}
    return {"started": True, "total": await async_start_sync(hass, msg["entry_id"], scope)}


async def _ws_import(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_import_one(hass, msg["entry_id"], msg["rel"])


async def _ws_delete(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_delete(hass, msg["entry_id"], msg["rels"], ha=msg["ha"], ps4=msg["ps4"])


_ENTRY = {vol.Required("entry_id"): str}
_COMMANDS = (
    _cmd({vol.Required("type"): "ps4_goldhen/screenshots/get", **_ENTRY}, _ws_get),
    _cmd({
        vol.Required("type"): "ps4_goldhen/screenshots/set", **_ENTRY,
        vol.Optional("enabled"): bool,
        vol.Optional("mode"): vol.In(MODES),
        vol.Optional("limit_gb"): vol.All(vol.Coerce(float), vol.Range(min=LIMIT_MIN_GB, max=LIMIT_MAX_GB)),
    }, _ws_set),
    _cmd({
        vol.Required("type"): "ps4_goldhen/screenshots/list", **_ENTRY,
        vol.Optional("offset", default=0): vol.All(int, vol.Range(min=0, max=100000)),
        vol.Optional("count", default=24): vol.All(int, vol.Range(min=1, max=PAGE_MAX)),
        vol.Optional("title_id"): str,
    }, _ws_list),
    _cmd({vol.Required("type"): "ps4_goldhen/screenshots/plan", **_ENTRY}, _ws_plan),
    _cmd({
        vol.Required("type"): "ps4_goldhen/screenshots/sync", **_ENTRY,
        vol.Required("kind"): vol.In(("all", "game", "newest", "selected")),
        vol.Optional("title_id"): str,
        vol.Optional("count"): vol.All(int, vol.Range(min=1, max=100000)),
        vol.Optional("rels"): [str],
    }, _ws_sync),
    _cmd({vol.Required("type"): "ps4_goldhen/screenshots/import", **_ENTRY, vol.Required("rel"): str}, _ws_import),
    _cmd({
        vol.Required("type"): "ps4_goldhen/screenshots/delete", **_ENTRY,
        vol.Required("rels"): [str], vol.Required("ha"): bool, vol.Required("ps4"): bool,
    }, _ws_delete),
)


def _read_capped(ftp: ftplib.FTP, remote: str, cap: int) -> bytes:
    """One shot into memory for the viewer (a few MB), refused past `cap`."""
    buf = bytearray()

    def _put(chunk: bytes) -> None:
        buf.extend(chunk)
        if len(buf) > cap:
            raise HomeAssistantError("Too big to show")

    ftp.retrbinary(f"RETR {remote}", _put, blocksize=65536)
    return bytes(buf)


class PS4ScreenshotView(HomeAssistantView):
    """A shot full size for the panel's viewer (signed path; admin only like the panel). HA's copy when there is one;
    otherwise read from the PS4 for this view only and not kept: viewing never syncs."""
    url = "/api/ps4_goldhen/screenshots/{entry_id}/full"
    name = "api:ps4_goldhen:screenshot_full"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        if not request["hass_user"].is_admin:
            return web.Response(text="Admin only", status=403)
        rel = request.query.get("rel", "")
        rt = runtime(hass, entry_id)
        if rt is None or not rt.settings["enabled"] or not cf.valid_rel(rel):
            return web.Response(text="Not found", status=404)
        folder = console_dir(hass, rt)
        path = await hass.async_add_executor_job(cf.find_copy, folder, rel.rsplit("/", 1)[1]) if folder else None
        if path is not None:
            return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
        if _power(hass, entry_id) != POWER_ON:
            return web.Response(text="The PS4 is in rest mode or off", status=409)  # no FTP then
        try:
            body = await _session(hass, entry_id, partial(_read_capped, remote=cf.photo_remote(rel), cap=VIEW_MAX_BYTES))
        except (*ftplib.all_errors, HomeAssistantError) as err:
            return web.Response(text=str(err), status=502)
        kind = "image/png" if rel.lower().endswith(".png") else "image/jpeg"
        return web.Response(body=body, content_type=kind, headers={"Cache-Control": "private, max-age=600"})


def _register_once(hass: HomeAssistant) -> None:
    flag = f"{DOMAIN}_screenshots_registered"
    if hass.data.get(flag):
        return
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)
    hass.http.register_view(PS4ScreenshotView())
    hass.data[flag] = True
