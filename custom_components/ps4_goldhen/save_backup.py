"""PS4 save backup to HA's /share: Off / On (manual, the default) per console, panel → Saves.

The console's encrypted save files (image + sealed key per slot) are copied as they are, in Apollo Save Tool's USB layout,
to /share/PS4 Saves/<console>/<user>/. Latest holds each slot's newest copy and History its older ones (save_files.py).
Pruning only touches HA's copies: History is thinned per slot (newest 5, then 1/hour for a day, 1/day for 30 days,
1/week) and, over the storage limit, the oldest unkept versions go first. Latest is never pruned; a slot that still
doesn't fit is skipped (limit_reached).

A game that is open (in focus, or suspended behind the home screen) is never copied: it may have its save mounted and
half written, and a torn copy looks fine until the restore fails. It is skipped (game_open) until it is closed.
Torn-copy guard for the rest: the title folder is listed again right after its slots are downloaded, and a slot
rewritten meanwhile is dropped and waits.

A pass runs only when asked: Back up now (panel, device page button, or the user's own automation). There is no
automatic mode: anyone who wants one writes an automation. The panel's Stop ends a pass after the save it's copying:
what it copied stays, and the next pass copies the rest. The open-game check reads
game_session.py. Every FTP session takes the entry's FTP lock (ftp_session.py) and fetches at most 12 slots
(24 files), so the cover fetch and the panel's FTP actions get a turn.

Restore: Send to PS4 uploads a copy (a game's Latest, all games, or one History version) into Apollo's Fake USB folder
on this or another console in HA (/data/fakeusb/PS4/SAVEDATA/0000000000000000, replacing what HA sent there before),
then Apollo → USB Saves → Copy save game to HDD. That folder is the only place HA writes on a PS4. A zip of the same
tree is the fallback for a real USB stick.
Settings live in .storage, not the entry options: saving options reloads the entry, which drops klog.

The panel's two panes (On PS4 | In HA): a console read (the same walk as a pass, no save downloads) shows each save on
the PS4 against HA's newest copy, with its name from the console's savedata.db. It is kept in memory for
CONSOLE_FRESH_S and every pass refreshes it. Back up / Send / Download / Delete take single saves: `items` of
{title_id, slot?, stamp?} for one user (a game = every newest copy, a slot = its newest copy, + stamp = that older
version). Delete only ever touches HA's copies.
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import ftplib
from functools import partial
import logging
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

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
from . import game_session
from . import save_files as sf
from .const import DOMAIN, POWER_OFF, POWER_REST
from .dynamic_entities import DynamicEntities
from .entity import device_name
from .ftp_session import session

_LOGGER = logging.getLogger(__name__)

MODE_OFF, MODE_MANUAL = "off", "manual"  # the panel calls Manual "On"; a mode this version doesn't have loads as Manual
MODES = (MODE_OFF, MODE_MANUAL)

LIMIT_MIN_GB, LIMIT_MAX_GB, DEFAULT_LIMIT_GB = 0.5, 50.0, 5.0
SHARE_ROOT = Path("/share")  # HA OS / Supervised; a Core install falls back to <config>/share
FOLDER = "PS4 Saves"
MIN_FREE_BYTES = 1 << 30
SLOTS_PER_SESSION = 12        # 24 files: the cover fetch and app.db pull get a turn between sessions
ZIP_MAX_AGE_S = 3600.0
CONSOLE_FRESH_S = 300.0       # the panel's PS4 pane reads the console again after this, or on Refresh
MAX_ITEMS = 5000
_OLD_SWITCH_UNIQUE_ID = "{domain}_{entry_id}_automatic_save_backup"  # a switch this version doesn't have

# Status sensor keys; the words people see are in translations (entity.sensor.save_backup_status.state)
STATUS_NEVER = "never"
STATUS_BACKING_UP = "backing_up"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_LIMIT = "limit_reached"
STATUS_GAME_OPEN = "game_open"  # the last pass skipped a game that was open then (it may be closed by now)
STATUS_FAILED = "failed"
STATUS_STOPPED = "stopped"  # Stop ended the last pass before it copied everything
STATUS_OPTIONS = [STATUS_NEVER, STATUS_BACKING_UP, STATUS_UP_TO_DATE, STATUS_LIMIT, STATUS_GAME_OPEN, STATUS_FAILED,
                  STATUS_STOPPED]

_STORE_VERSION = 1
_DEFAULTS: dict[str, Any] = {
    "mode": MODE_MANUAL, "limit_gb": DEFAULT_LIMIT_GB, "folder": None,
    "users": {},       # user ID → folder name, fixed when first seen (renaming the user doesn't split the copies)
    "last": None, "last_success": None,
    "sent": None,      # what HA last put in this console's Fake USB folder: {when, from, games, bytes}
    # The files HA uploaded there and hasn't deleted, recorded before the first upload; the only files Remove from PS4
    # (or the next send) deletes. None = saved before this list was kept: `sent` names them.
    "fake_usb_files": None,
}
_ZIP_RE = re.compile(r'^[^/\\\x00-\x1f"]{1,200}\.zip$')


@dataclass
class _Runtime:
    entry: ConfigEntry
    store: Store
    settings: dict[str, Any]
    op_lock: asyncio.Lock = field(default_factory=asyncio.Lock)  # one pass at a time per console
    running: bool = False
    stop: threading.Event = field(default_factory=threading.Event)  # Stop pressed: the pass ends after this save
    progress: dict[str, int] = field(default_factory=lambda: {"done": 0, "total": 0})
    usage: dict[str, int] = field(default_factory=lambda: {"bytes": 0, "versions": 0})
    # The last console read: {mono, read_at, users: {uid: {title: {slot: info}}}, meta: {uid: save
    # names}, fake_usb: {title: [slots]}}. Memory only.
    console: dict[str, Any] | None = None
    dynamic: DynamicEntities = field(init=False)

    def __post_init__(self) -> None:
        self.dynamic = DynamicEntities(lambda platform: self.settings["mode"] != MODE_OFF)


def signal(entry_id: str) -> str:
    return f"{DOMAIN}_saves_{entry_id}"


def runtime(hass: HomeAssistant, entry_id: str) -> _Runtime | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    return data.get("saves") if isinstance(data, dict) else None


def _all_runtimes(hass: HomeAssistant) -> list[_Runtime]:
    return [d["saves"] for d in hass.data.get(DOMAIN, {}).values() if isinstance(d, dict) and "saves" in d]


def _require(hass: HomeAssistant, entry_id: str) -> _Runtime:
    rt = runtime(hass, entry_id)
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    if rt.settings["mode"] == MODE_OFF:
        raise HomeAssistantError("Save backup is off for this PS4 (panel → Saves)")
    return rt


def share_root(hass: HomeAssistant) -> Path:
    return SHARE_ROOT if SHARE_ROOT.is_dir() else Path(hass.config.path("share"))


def console_dir(hass: HomeAssistant, rt: _Runtime) -> Path | None:
    folder = rt.settings.get("folder")
    return share_root(hass) / FOLDER / folder if folder else None


def downloads_dir(hass: HomeAssistant, entry_id: str) -> Path:
    return share_root(hass) / FOLDER / sf.DOWNLOADS / entry_id


def _limit_bytes(rt: _Runtime) -> int:
    return int(float(rt.settings["limit_gb"]) * (1 << 30))


def _power(hass: HomeAssistant, entry_id: str) -> str | None:
    data = hass.data.get(DOMAIN, {}).get(entry_id) or {}
    coordinator = data.get("coordinator")
    return ((coordinator.data if coordinator else None) or {}).get("power_state")


def status(rt: _Runtime) -> str:
    last = rt.settings["last"]
    if rt.running:
        return STATUS_BACKING_UP
    return last["state"] if last else STATUS_NEVER


# ── Setup ──────────────────────────────────────────────────────────────────────

async def async_setup(hass: HomeAssistant, entry: ConfigEntry, coordinator) -> None:
    store: Store = Store(hass, _STORE_VERSION, f"{DOMAIN}.saves.{entry.entry_id}")
    rt = _Runtime(entry, store, {**copy.deepcopy(_DEFAULTS), **(await store.async_load() or {})})
    if rt.settings["mode"] not in MODES:  # a mode this version doesn't have
        rt.settings["mode"] = MODE_MANUAL
    rt.settings.pop("pending", None)  # a key this version doesn't have
    hass.data[DOMAIN][entry.entry_id]["saves"] = rt
    registry = er.async_get(hass)
    old_id = _OLD_SWITCH_UNIQUE_ID.format(domain=DOMAIN, entry_id=entry.entry_id)
    if old_switch := registry.async_get_entity_id("switch", DOMAIN, old_id):
        registry.async_remove(old_switch)
    if rt.settings["mode"] != MODE_OFF and not rt.settings["folder"]:
        _pin_folder(hass, rt)  # Manual by default: the folder is fixed on the first start
        await rt.store.async_save(rt.settings)
    if rt.settings["folder"]:
        await _async_repair(hass, rt)  # an HA stop or crash mid-install is followed by this start
        await _async_rescan(hass, rt)
    await hass.async_add_executor_job(sf.prune_downloads, downloads_dir(hass, entry.entry_id), ZIP_MAX_AGE_S, time.time())
    _register_once(hass)


async def async_remove(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Entry deleted: its settings and download zips go. The backups stay (they're the user's)."""
    await Store(hass, _STORE_VERSION, f"{DOMAIN}.saves.{entry.entry_id}").async_remove()
    await hass.async_add_executor_job(partial(shutil.rmtree, downloads_dir(hass, entry.entry_id), ignore_errors=True))


@callback
def register_platform(hass: HomeAssistant, entry: ConfigEntry, platform: str, add_entities: Callable,
                      factory: Callable[[], list]) -> None:
    """Called from a platform's setup: the save backup entities exist only while backup isn't Off."""
    rt = runtime(hass, entry.entry_id)
    if rt is not None:
        rt.dynamic.register(hass, platform, add_entities, factory)


def _open_titles(hass: HomeAssistant, entry_id: str) -> set[str]:
    """Games running or suspended on the console: their saves may be mounted and half written."""
    playing = game_session.current(hass, entry_id)
    return {t for t in ((playing or {}).get("title_id"), game_session.suspended(hass, entry_id)) if t}


# ── The backup pass ────────────────────────────────────────────────────────────

def _walk(ftp: ftplib.FTP, titles: set[str] | None, known: set[str]) -> dict[str, Any]:
    """Users (names of new ones), each title's slots (every title, or only `titles`), each user's save names (the
    console's savedata.db) and what sits in Apollo's Fake USB folder. Listings and small reads only."""
    users = sf.list_users(ftp)
    walk: dict[str, Any] = {
        "users": users, "names": {uid: sf.read_user_name(ftp, uid) for uid in users if uid not in known},
        "meta": {uid: sf.read_save_db(ftp, uid) for uid in users}, "fake_usb": sf.list_fake_usb(ftp), "found": [],
    }
    for uid in users:
        for tid in sf.list_titles(ftp, uid) if titles is None else sorted(t for t in titles if sf.valid_title(t)):
            slots = sf.list_slots(ftp, uid, tid)
            if slots:
                walk["found"].append((uid, tid, slots))
    return walk


def _remember(rt: _Runtime, walk: dict[str, Any], titles: set[str] | None) -> None:
    """Keep what a walk saw for the panel's PS4 pane. A pass over some games updates only those games, and only once a
    full read exists."""
    users: dict[str, dict[str, Any]] = {uid: {} for uid in walk["users"]}
    for uid, tid, slots in walk["found"]:
        users[uid][tid] = slots
    if titles is None:
        rt.console = {"mono": time.monotonic(), "read_at": dt_util.utcnow().isoformat(), "users": users,
                      "meta": walk["meta"], "fake_usb": walk["fake_usb"]}
    elif rt.console is not None:
        for uid, games in users.items():
            mine = rt.console["users"].setdefault(uid, {})
            for tid in titles:
                mine.pop(tid, None)
            mine.update(games)
        rt.console["meta"].update(walk["meta"])
        rt.console["fake_usb"] = walk["fake_usb"]


def _name_copies(console: Path, users: dict[str, str], walk: dict[str, Any]) -> None:
    """Name HA's newest copies that match the console's saves (the names describe the console's save)."""
    names: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for uid, tid, slots in walk["found"]:
        for slot, info in slots.items():
            meta = walk["meta"].get(uid, {}).get(tid, {}).get(slot)
            if meta and uid in users and not sf.is_changed(info, sf.local_slot(console / users[uid], tid, slot)):
                names.setdefault(uid, {}).setdefault(tid, {})[slot] = meta
    for uid, games in names.items():
        sf.merge_latest_meta(console / users[uid], games)


def _game(tid: str, game_map: dict[str, dict[str, Any]], names: dict[str, str]) -> str:
    """The game's name: HA's game list, else its saves' main_title (games no longer installed), else the title ID."""
    return cf.safe_name((game_map.get(tid) or {}).get("name")) or cf.safe_name(names.get(tid)) or tid


def _changed(console: Path, users: dict[str, str], found: list) -> list[dict[str, Any]]:
    """Slots that are new or rewritten on the console, the most recently saved first."""
    todo = []
    for uid, tid, slots in found:
        for slot, info in slots.items():
            if sf.is_changed(info, sf.local_slot(console / users[uid], tid, slot)):
                todo.append({"uid": uid, "title": tid, "slot": slot, "info": info})
    return sorted(todo, key=lambda t: t["info"]["mtime"] or 0, reverse=True)


def _room(console: Path, limit: int) -> int:
    return min(limit - sf.usage(console)["bytes"], sf.disk_room(console, MIN_FREE_BYTES))


def _fetch(ftp: ftplib.FTP, uid: str, tid: str, chunk: list[dict[str, Any]], staging: Path,
           stop: threading.Event) -> tuple[list, dict]:
    """Download the chunk's slots, each after the History versions picked to make room for it are deleted. Once Stop
    is pressed no new slot starts: a stopped pass never deletes a version for a save it didn't copy."""
    staged = []
    for t in chunk:
        if stop.is_set():
            break
        for version in t.get("make_room", ()):
            sf.delete_version(version)
        staged.append((t["slot"], t["info"], sf.fetch_slot(ftp, uid, tid, t["slot"], t["info"], staging)))
    return staged, sf.list_slots(ftp, uid, tid)  # listed again: did the game write while we copied?


def _install(user_dir: Path, tid: str, staged: list, after: dict, label: str, tz, now: datetime) -> tuple[int, int]:
    made = again = 0
    for slot, info, files in staged:
        if after.get(slot) != info:
            for path in files:
                path.unlink(missing_ok=True)
            again += 1
            continue
        sf.install(user_dir, tid, slot, files, label, tz)
        made += 1
        for version in sf.thin([v for v in sf.versions(user_dir) if v["title"] == tid and v["slot"] == slot], now):
            sf.delete_version(version)
    return made, again


def _assign_user_folders(rt: _Runtime, names: dict[str, str | None]) -> None:
    users: dict[str, str] = rt.settings["users"]
    for uid, name in names.items():
        if uid not in users:
            base = cf.safe_name(name) or uid
            users[uid] = base if base not in users.values() else f"{base} ({uid})"


def _in_scope(picks: set[tuple[str, str, str | None]] | None, uid: str, tid: str, slot: str) -> bool:
    return picks is None or (uid, tid, None) in picks or (uid, tid, slot) in picks


async def _async_backup(hass: HomeAssistant, rt: _Runtime, titles: set[str] | None,
                        picks: set[tuple[str, str, str | None]] | None = None) -> dict[str, Any]:
    """One pass: every title (None) or some, optionally only the ticked saves (`picks`: (uid, title, slot or None =
    the whole game)). Caller holds rt.op_lock. Raises the FTP / disk error after recording it."""
    entry_id = rt.entry.entry_id
    console = console_dir(hass, rt)
    if console is None:
        raise HomeAssistantError("No folder for this PS4 yet: turn save backup off and on again")
    game_map = hass.data[DOMAIN][entry_id].get("game_map", {})
    tz = dt_util.get_default_time_zone()
    rt.running, rt.progress = True, {"done": 0, "total": 0}
    rt.stop.clear()
    async_dispatcher_send(hass, signal(entry_id))
    result: dict[str, Any] = {"checked": 0, "copied": 0, "skipped": 0, "retried": 0, "already": 0, "open": [],
                              "left": 0}
    opened: set[str] = set()  # changed but open on the console: skipped, never copied
    error: str | None = None
    left = 0  # saves that fit but weren't reached yet
    try:
        walk = await session(hass, entry_id, partial(_walk, titles=titles, known=set(rt.settings["users"])))
        _assign_user_folders(rt, walk["names"])
        _remember(rt, walk, titles)
        users = rt.settings["users"]
        found = walk["found"]
        await _async_repair(hass, rt)
        todo = await hass.async_add_executor_job(_changed, console, users, found)
        todo = [t for t in todo if _in_scope(picks, t["uid"], t["title"], t["slot"])]
        result["checked"] = sum(1 for uid, tid, slots in found for slot in slots if _in_scope(picks, uid, tid, slot))
        result["already"] = result["checked"] - len(todo)
        busy = _open_titles(hass, entry_id)
        opened |= {t["title"] for t in todo if t["title"] in busy}
        todo = [t for t in todo if t["title"] not in busy]
        rt.progress["total"] = len(todo)
        async_dispatcher_send(hass, signal(entry_id))

        room = await hass.async_add_executor_job(_room, console, _limit_bytes(rt))
        pool: list[dict[str, Any]] | None = None  # History versions that may go to make room, read on first need
        fits = []
        for item in todo:
            need = item["info"]["size"] + item["info"]["key_size"]
            if need > room:
                if pool is None:
                    pool = await hass.async_add_executor_job(
                        lambda: [v for folder in users.values() for v in sf.versions(console / folder)])
                picked = sf.pick_for_room(pool, need - room)
                if picked is None:  # this one can't fit; a smaller one after it still may
                    result["skipped"] += 1
                    continue
                item["make_room"] = picked  # deleted right before this save is copied (_fetch)
                pool = [v for v in pool if v not in picked]
                room += sum(v["bytes"] for v in picked)
            room -= need
            fits.append(item)

        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in fits:
            groups.setdefault((item["uid"], item["title"]), []).append(item)
        left = len(fits)
        for (uid, tid), items in groups.items():
            if rt.stop.is_set():
                break
            if tid in _open_titles(hass, entry_id):  # started during the pass
                opened.add(tid)
                rt.progress["done"] += len(items)
                left -= len(items)
                continue
            label = _game(tid, game_map, sf.game_names(walk["meta"].get(uid, {})))
            for start in range(0, len(items), SLOTS_PER_SESSION):
                if rt.stop.is_set():
                    break
                chunk = items[start:start + SLOTS_PER_SESSION]
                staged, after = await session(hass, entry_id, partial(
                    _fetch, uid=uid, tid=tid, chunk=chunk, staging=console / sf.STAGING, stop=rt.stop))
                now = dt_util.now().replace(tzinfo=None)
                made, again = await hass.async_add_executor_job(
                    _install, console / users[uid], tid, staged, after, label, tz, now)
                result["copied"] += made
                result["retried"] += again
                rt.progress["done"] += len(staged)
                left -= len(staged)
                await _async_rescan(hass, rt)  # the panel's "X of N GB" grows as copies land
                async_dispatcher_send(hass, signal(entry_id))
        await hass.async_add_executor_job(_name_copies, console, dict(users), walk)
    except (*ftplib.all_errors, OSError, HomeAssistantError) as err:
        error = str(err) or type(err).__name__
        raise
    finally:
        await hass.async_add_executor_job(partial(shutil.rmtree, console / sf.STAGING, ignore_errors=True))
        rt.running = False
        stopped = rt.stop.is_set() and left > 0
        rt.stop.clear()
        result["open"] = sorted(cf.game_folder(t, game_map) for t in opened)
        result["left"] = left if stopped else 0
        state = (STATUS_FAILED if error else STATUS_STOPPED if stopped else STATUS_LIMIT if result["skipped"]
                 else STATUS_GAME_OPEN if opened else STATUS_UP_TO_DATE)
        finished = dt_util.utcnow().isoformat()
        rt.settings["last"] = {"state": state, "finished": finished, "scope": sorted(titles) if titles else "all",
                               "error": error, **result}
        if state == STATUS_UP_TO_DATE and titles is None:  # only a pass over everything
            rt.settings["last_success"] = finished
        await _async_rescan(hass, rt)
        await _async_save(hass, rt)
    return result


# ── Shared steps ───────────────────────────────────────────────────────────────

async def _async_repair(hass: HomeAssistant, rt: _Runtime) -> None:
    """Whole pairs only: a copy cut short by an HA stop or crash between install()'s renames gets finished or dropped."""
    console = console_dir(hass, rt)
    users = list(rt.settings["users"].values())
    fixed = await hass.async_add_executor_job(
        lambda: sum(sf.repair_half_pairs(console / folder) for folder in users)) if console else 0
    if fixed:
        _LOGGER.warning("Save backup for %s: repaired %d file(s) an interrupted backup left half copied",
                        hass.data[DOMAIN][rt.entry.entry_id].get("host"), fixed)


async def _async_rescan(hass: HomeAssistant, rt: _Runtime) -> None:
    folder = console_dir(hass, rt)
    rt.usage = await hass.async_add_executor_job(sf.usage, folder) if folder else {"bytes": 0, "versions": 0}


async def _async_save(hass: HomeAssistant, rt: _Runtime) -> None:
    await rt.store.async_save(rt.settings)
    async_dispatcher_send(hass, signal(rt.entry.entry_id))


def _check_free(rt: _Runtime) -> None:
    if rt.op_lock.locked():
        raise HomeAssistantError("Busy: a save backup or a send is running for this PS4")


def _pin_folder(hass: HomeAssistant, rt: _Runtime) -> None:
    """The console's folder under /share/PS4 Saves, named once (renaming the console later doesn't move it)."""
    base = cf.safe_name(device_name(hass, rt.entry)) or "PS4"
    taken = {o.settings.get("folder") for o in _all_runtimes(hass) if o is not rt}
    name, n = base, 2
    while name in taken:
        name, n = f"{base} ({n})", n + 1
    rt.settings["folder"] = name


# ── Actions (button, panel) ────────────────────────────────────────────────────

async def async_set_settings(hass: HomeAssistant, entry_id: str, changes: dict[str, Any]) -> None:
    rt = runtime(hass, entry_id)
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    rt.settings.update(changes)
    if rt.settings["mode"] != MODE_OFF and not rt.settings["folder"]:
        _pin_folder(hass, rt)
    await _async_rescan(hass, rt)
    await _async_save(hass, rt)
    rt.dynamic.reconcile(hass)


def _item(item: dict[str, Any]) -> tuple[str, str | None, str | None]:
    tid, slot, stamp = item.get("title_id"), item.get("slot"), item.get("stamp")
    if not sf.valid_title(tid) or (slot is not None and not sf.valid_slot(slot)) or (
            stamp is not None and (slot is None or not sf.valid_stamp(stamp))):
        raise HomeAssistantError("Invalid save")
    return tid, slot, stamp  # type: ignore[return-value]


def _backup_scope(uid: str | None, items: list[dict[str, Any]] | None, title_id: str | None = None
                  ) -> tuple[set[str] | None, set[tuple[str, str, str | None]] | None]:
    """The ticked saves on the PS4 pane (one user) → the titles to walk + (uid, title, slot or None) picks. `title_id`
    (the game page): that game's saves of every user. Neither: everything."""
    if title_id is not None:
        if not sf.valid_title(title_id):
            raise HomeAssistantError("Unknown game")
        return {title_id}, None
    if not items:
        return None, None
    if not sf.valid_user(uid):
        raise HomeAssistantError("Unknown PS4 user")
    picks = set()
    for item in items:
        tid, slot, stamp = _item(item)
        if stamp is not None:
            raise HomeAssistantError("Back up copies what's on the PS4 now, not an older version")
        picks.add((uid, tid, slot))
    return {tid for _, tid, _ in picks}, picks  # type: ignore[misc]


async def async_start_backup(hass: HomeAssistant, entry_id: str, uid: str | None = None,
                             items: list[dict[str, Any]] | None = None, title_id: str | None = None) -> None:
    """Button / panel: a pass in the background (it outlives the panel). Everything, the saves ticked on the PS4
    pane, or one game (the game page)."""
    rt = _require(hass, entry_id)
    titles, picks = _backup_scope(uid, items, title_id)
    if _power(hass, entry_id) in (POWER_REST, POWER_OFF):
        raise HomeAssistantError("The PS4 is in rest mode or off: turn it on first (HA doesn't use FTP on a sleeping PS4)")
    _check_free(rt)
    await rt.op_lock.acquire()

    async def _run() -> None:
        try:
            await _async_backup(hass, rt, titles, picks)
        except (*ftplib.all_errors, OSError, HomeAssistantError) as err:
            _LOGGER.warning("Save backup for %s stopped: %s", hass.data[DOMAIN][entry_id].get("host"), err)
        finally:
            rt.op_lock.release()

    rt.entry.async_create_background_task(hass, _run(), f"{DOMAIN}_saves_backup")


async def async_stop_backup(hass: HomeAssistant, entry_id: str) -> None:
    """Panel Stop: the pass ends after the save it's copying (no half file on either side); what it copied stays."""
    rt = _require(hass, entry_id)
    if not rt.running:
        raise HomeAssistantError("No save backup is running for this PS4")
    rt.stop.set()


def _user_dir(hass: HomeAssistant, rt: _Runtime, uid: str) -> Path:
    folder = rt.settings["users"].get(uid) if sf.valid_user(uid) else None
    console = console_dir(hass, rt)
    if folder is None or console is None:
        raise HomeAssistantError("Unknown PS4 user")
    return console / folder


def _version_dir(user_dir: Path, title: str, slot: str, stamp: str) -> Path:
    if not (sf.valid_title(title) and sf.valid_slot(slot) and sf.valid_stamp(stamp)):
        raise HomeAssistantError("Invalid save version")
    path = sf.history_title_dir(user_dir, title, None) / slot / stamp
    if not path.is_dir():
        raise HomeAssistantError("That version is no longer in HA")
    return path


async def async_keep(hass: HomeAssistant, entry_id: str, uid: str, title: str, slot: str, stamp: str,
                     kept: bool) -> None:
    rt = _require(hass, entry_id)
    _check_free(rt)  # a pass may be thinning this slot
    user_dir = _user_dir(hass, rt, uid)
    path = await hass.async_add_executor_job(_version_dir, user_dir, title, slot, stamp)
    await hass.async_add_executor_job(sf.set_kept, path, kept)
    async_dispatcher_send(hass, signal(entry_id))


def _resolve(user_dir: Path, items: list[dict[str, Any]] | None, one_per_save: bool
             ) -> list[tuple[str, str, str | None]]:
    """Ticked items on the HA pane → (title, slot, version stamp or None = HA's newest copy). A game = each of its
    newest copies; no items = every game's. `one_per_save`: Apollo's USB layout holds one copy of each save, so Send
    and Download take one version per slot."""
    newest = {(s["title"], s["slot"]) for s in sf.latest_slots(user_dir)}
    picks: list[tuple[str, str, str | None]] = []
    for item in items if items else [{"title_id": t} for t in sorted({t for t, _ in newest})]:
        tid, slot, stamp = _item(item)
        if stamp is not None:
            _version_dir(user_dir, tid, slot, stamp)  # type: ignore[arg-type]  # raises when it's gone
            picks.append((tid, slot, stamp))  # type: ignore[arg-type]
        elif slot is not None:
            if (tid, slot) not in newest:
                raise HomeAssistantError("HA has no newest copy of that save any more: refresh the list")
            picks.append((tid, slot, None))
        else:
            picks += [(tid, s, None) for t, s in sorted(newest) if t == tid]
    picks = list(dict.fromkeys(picks))
    if one_per_save and len({(t, s) for t, s, _ in picks}) < len(picks):
        raise HomeAssistantError("Pick one version per save: Apollo's USB folder holds one copy of each save")
    if not picks:
        raise HomeAssistantError("Nothing backed up yet")
    return picks


async def _members(hass: HomeAssistant, rt: _Runtime, uid: str, items: list[dict[str, Any]] | None
                   ) -> tuple[list[tuple[str, str, str | None]], list[tuple[Path, str]]]:
    """The ticked saves and HA's files for them, named PS4/SAVEDATA/… like on a stick."""
    user_dir = _user_dir(hass, rt, uid)
    picks = await hass.async_add_executor_job(_resolve, user_dir, items, True)
    members = await hass.async_add_executor_job(sf.members, user_dir, picks)
    if not members:
        raise HomeAssistantError("Nothing backed up yet")
    return picks, members


def _game_names(user_dir: Path | None, console_meta: dict[str, Any]) -> dict[str, str]:
    """Game names from HA's named copies and the console's save list (its last read)."""
    names = sf.game_names(sf.latest_meta(user_dir)) if user_dir else {}
    for meta in console_meta.values():
        names = {**sf.game_names(meta), **names}
    return names


def _console_meta(rt: _Runtime) -> dict[str, Any]:
    return dict((rt.console or {}).get("meta", {}))


async def async_zip(hass: HomeAssistant, entry_id: str, uid: str, items: list[dict[str, Any]] | None = None
                    ) -> dict[str, Any]:
    """A zip for a USB stick: the ticked saves (newest copies or older versions, one per save), or every game's newest.

    One zip per console on HA's disk. Making one deletes the console's previous zip (a download
    already running keeps reading the deleted file), and a zip that would leave HA under 1 GB free is refused. The zip
    also goes after an hour."""
    rt = _require(hass, entry_id)
    user_dir = _user_dir(hass, rt, uid)
    picks, members = await _members(hass, rt, uid, items)
    game_map = hass.data[DOMAIN][entry_id].get("game_map", {})
    names = await hass.async_add_executor_job(_game_names, user_dir, _console_meta(rt))
    titles = sorted({t for t, _, _ in picks})
    game = _game(titles[0], game_map, names) if len(titles) == 1 and items else (
        f"{len(titles)} games" if items else "all games")
    stamps = {s for _, _, s in picks}
    tag = picks[0][2] if len(picks) == 1 and picks[0][2] else "latest" if stamps == {None} else "saves"
    name = (cf.safe_name(f"{rt.settings['folder']} {user_dir.name} {game} {tag}", 180) or "saves") + ".zip"
    folder = downloads_dir(hass, entry_id)
    await hass.async_add_executor_job(sf.prune_downloads, folder, 0.0, time.time())  # the previous zip goes
    need = sum(path.stat().st_size for path, _ in members)
    room = await hass.async_add_executor_job(sf.disk_room, folder, MIN_FREE_BYTES)
    if need > room:
        raise HomeAssistantError(f"Not enough free space on HA's disk for a {need / 1048576:.0f} MB zip "
                                 "(HA keeps 1 GB free)")
    size = await hass.async_add_executor_job(sf.write_zip, folder / name, members)
    async_call_later(hass, ZIP_MAX_AGE_S + 60, partial(_prune_later, folder))
    return {"name": name, "bytes": size, "path": f"/api/ps4_goldhen/saves/{entry_id}/download?file={quote(name)}"}


def _awake_target(hass: HomeAssistant, target: str) -> _Runtime:
    rt = runtime(hass, target)
    if rt is None:
        raise HomeAssistantError("Unknown PS4")
    if _power(hass, target) in (POWER_REST, POWER_OFF):
        raise HomeAssistantError("That PS4 is in rest mode or off: turn it on first (HA doesn't use FTP on a sleeping PS4)")
    _check_free(rt)
    return rt


def _save_names(user_dir: Path, picks: list[tuple[str, str, str | None]]) -> list[dict[str, Any]]:
    """What a send carried, named (the Fake USB section of the PS4 pane shows these names)."""
    latest = sf.latest_meta(user_dir)
    out = []
    for tid, slot, stamp in picks:
        meta = (sf.version_meta(sf.history_title_dir(user_dir, tid, None) / slot / stamp) if stamp
                else latest.get(tid, {}).get(slot, {}))
        out.append({"title_id": tid, "slot": slot, "stamp": stamp, "name": meta.get("name")})
    return out


def _fake_usb_files(rt: _Runtime) -> list[str]:
    files = rt.settings.get("fake_usb_files")
    return list(files) if files is not None else sf.sent_files(rt.settings.get("sent"))


def _clear_and_list(ftp: ftplib.FTP, files: list[str], check: list[str]) -> tuple[int, dict[str, list[str]], list[str]]:
    """Delete HA's earlier files, then read what's left in the folder and which of `check` it already holds."""
    return sf.clear_fake_usb(ftp, files), sf.list_fake_usb(ftp), sf.fake_usb_taken(ftp, check)


async def async_send(hass: HomeAssistant, entry_id: str, uid: str, items: list[dict[str, Any]] | None = None,
                     target: str | None = None) -> dict[str, Any]:
    """Restore, step 1: put HA's copies of the ticked saves (or every game's newest) into Apollo's Fake USB folder on a
    PS4 (this one or another in HA), replacing what HA sent there before. Files HA didn't send stay; a send that would
    write over one is refused. Step 2 is on the console: Apollo → USB Saves → Copy save game to HDD."""
    rt = _require(hass, entry_id)
    target = target or entry_id
    trt = _awake_target(hass, target)
    user_dir = _user_dir(hass, rt, uid)
    picks, members = await _members(hass, rt, uid, items)
    by_title: dict[str, list[tuple[Path, str]]] = {}
    for path, rel in members:
        by_title.setdefault(rel.split("/")[3], []).append((path, rel))
    game_map = hass.data[DOMAIN][entry_id].get("game_map", {})
    names = await hass.async_add_executor_job(_game_names, user_dir, _console_meta(rt))
    saves = await hass.async_add_executor_job(_save_names, user_dir, picks)
    remotes = sf.fake_usb_paths(members)
    sent = 0
    async with trt.op_lock:
        try:
            trt.settings["fake_usb_files"] = _fake_usb_files(trt)  # before `sent` goes: a record from before the list
            trt.settings["sent"] = None
            _, there, taken = await session(hass, target, partial(
                _clear_and_list, files=trt.settings["fake_usb_files"], check=remotes))
            trt.settings["fake_usb_files"] = []
            if taken:  # checked before the record: a refused send never deletes these
                names = ", ".join(r.removeprefix(sf.FAKE_USB_SAVES + "/") for r in taken[:3])
                more = f" and {len(taken) - 3} more" if len(taken) > 3 else ""
                raise FileExistsError(f"The PS4's Fake USB folder already holds {names}{more}, not sent by HA: move or"
                                      f" delete {'it' if len(taken) == 1 else 'them'} on the console first")
            trt.settings["fake_usb_files"] = remotes  # before the first upload: a crash mid-send still knows them
            await _async_save(hass, trt)
            for group in by_title.values():  # one FTP session per game: the cover fetch gets a turn
                sent += await session(hass, target, partial(sf.send_to_fake_usb, members=group))
        except (*ftplib.all_errors, OSError, ValueError) as err:
            with contextlib.suppress(*ftplib.all_errors, OSError):  # never leave half a save behind for Apollo
                await session(hass, target, partial(sf.clear_fake_usb, files=_fake_usb_files(trt)))
                trt.settings["fake_usb_files"] = []
            await _async_save(hass, trt)
            raise HomeAssistantError(f"Send to PS4 stopped: {err or type(err).__name__}") from err
        trt.settings["sent"] = {
            "when": dt_util.utcnow().isoformat(), "from": rt.settings["folder"], "user": user_dir.name,
            "games": sorted(_game(t, game_map, names) for t in by_title),
            "version": picks[0][2] if len(picks) == 1 else None, "files": len(members), "bytes": sent,
            "saves": saves,
        }
        if trt.console is not None:  # what's there now: what HA didn't send, plus this send
            trt.console["fake_usb"] = {**there, **{
                t: sorted({*there.get(t, ()), *(s for tt, s, _ in picks if tt == t)}) for t in by_title}}
        await _async_save(hass, trt)
    return {"target": device_name(hass, trt.entry), **trt.settings["sent"]}


async def async_clear_sent(hass: HomeAssistant, target: str) -> int:
    """Remove from PS4: delete what HA put in that console's Fake USB folder (only that)."""
    trt = _awake_target(hass, target)
    async with trt.op_lock:
        try:
            deleted, there, _ = await session(hass, target, partial(
                _clear_and_list, files=_fake_usb_files(trt), check=[]))
        except (*ftplib.all_errors, OSError) as err:
            raise HomeAssistantError(f"Couldn't clear the PS4's Fake USB folder: {err or type(err).__name__}") from err
        trt.settings["sent"] = None
        trt.settings["fake_usb_files"] = []
        if trt.console is not None:
            trt.console["fake_usb"] = there
        await _async_save(hass, trt)
    return deleted


async def async_delete(hass: HomeAssistant, entry_id: str, uid: str, items: list[dict[str, Any]]) -> dict[str, int]:
    """Panel → Delete from HA: HA's copies of the ticked saves (newest copies and / or older versions,
    Keep or not). Never the PS4's saves: HA writes only to Apollo's Fake USB folder there."""
    rt = _require(hass, entry_id)
    if not items:
        raise HomeAssistantError("Nothing ticked")
    _check_free(rt)  # a pass may be thinning these slots
    user_dir = _user_dir(hass, rt, uid)

    def _work() -> int:
        deleted = 0
        for tid, slot, stamp in _resolve(user_dir, items, False):
            if stamp:
                sf.delete_version({"path": _version_dir(user_dir, tid, slot, stamp)})
                deleted += 1
            else:
                deleted += sf.delete_latest(user_dir, tid, slot)
        return deleted

    async with rt.op_lock:
        try:
            deleted = await hass.async_add_executor_job(_work)
        finally:
            await _async_rescan(hass, rt)
            async_dispatcher_send(hass, signal(entry_id))
    return {"deleted": deleted}


def _prune_later(folder: Path, _now: datetime) -> None:
    """Runs in the executor (a plain function, not a callback)."""
    sf.prune_downloads(folder, ZIP_MAX_AGE_S, time.time())


def _iso(mtime: int) -> str:
    return datetime.fromtimestamp(mtime, timezone.utc).isoformat().replace("+00:00", "Z")


def _inventory(console: Path, users: dict[str, str], game_map: dict[str, dict[str, Any]],
               on_ps4: dict[str, dict[str, Any]] | None, console_meta: dict[str, Any]) -> list[dict[str, Any]]:
    """What HA holds, per user → game → slot: the newest copy (when the game wrote it, its name) and the History
    versions. `on_ps4` (the last console read) marks saves no longer on the console."""
    out = []
    for uid, folder in sorted(users.items(), key=lambda kv: kv[1].lower()):
        user_dir = console / folder
        meta = sf.latest_meta(user_dir)
        names = _game_names(user_dir, console_meta)
        there = None if on_ps4 is None else on_ps4.get(uid, {})
        games: dict[str, dict[str, dict[str, Any]]] = {}

        def _slot(tid: str, slot: str) -> dict[str, Any]:
            return games.setdefault(tid, {}).setdefault(slot, {
                "slot": slot, "name": None, "saved": None, "bytes": 0, "versions": [],
                "on_ps4": None if there is None else slot in there.get(tid, {})})

        for s in sf.latest_slots(user_dir):
            row = _slot(s["title"], s["slot"])
            row.update(saved=_iso(s["mtime"]), bytes=s["bytes"], name=meta.get(s["title"], {}).get(s["slot"], {}).get("name"))
        for v in sorted(sf.versions(user_dir), key=lambda v: v["when"], reverse=True):
            vname = sf.version_meta(v["path"]).get("name")
            row = _slot(v["title"], v["slot"])
            row["versions"].append({"stamp": v["stamp"], "bytes": v["bytes"], "kept": v["kept"], "name": vname})
            if row["saved"] is None and row["name"] is None:  # only older versions: the newest one's name
                row["name"] = vname
        rows = []
        for tid, slots in games.items():
            saved = [s["saved"] for s in slots.values() if s["saved"]]
            rows.append({
                "title_id": tid, "game": _game(tid, game_map, names), "last_saved": max(saved) if saved else None,
                "bytes": sum(s["bytes"] + sum(v["bytes"] for v in s["versions"]) for s in slots.values()),
                "slots": sorted(slots.values(), key=lambda s: s["saved"] or "", reverse=True),
                "on_ps4": None if there is None else tid in there,
            })
        rows.sort(key=lambda g: g["last_saved"] or "", reverse=True)
        out.append({"uid": uid, "name": folder, "games": rows})
    return out


async def async_list(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """The panel's HA pane: HA's copies only (no FTP), so it works while the PS4 sleeps."""
    rt = _require(hass, entry_id)
    console = console_dir(hass, rt)
    on_ps4 = copy.deepcopy(rt.console["users"]) if rt.console else None
    users = await hass.async_add_executor_job(
        _inventory, console, dict(rt.settings["users"]), hass.data[DOMAIN][entry_id].get("game_map", {}), on_ps4,
        _console_meta(rt)) if console else []
    return {"users": users}


def _console_view(console: Path | None, rt_console: dict[str, Any] | None, users: dict[str, str],
                  game_map: dict[str, dict[str, Any]], open_titles: set[str], sent: dict[str, Any] | None
                  ) -> dict[str, Any]:
    """The PS4 pane: per user → game → save, each checked against HA's newest copy (in_ha / changed / not_in_ha), and
    what sits in the Fake USB folder."""
    out: dict[str, Any] = {"read_at": rt_console["read_at"] if rt_console else None, "users": [], "fake_usb": []}
    if not rt_console:
        return out
    all_names: dict[str, str] = {}
    for uid, games in sorted(rt_console["users"].items(), key=lambda kv: users.get(kv[0], kv[0]).lower()):
        meta = rt_console["meta"].get(uid, {})
        names = sf.game_names(meta)
        all_names.update(names)
        user_dir = console / users[uid] if console and uid in users else None
        rows = []
        for tid, slots in games.items():
            srows = []
            for slot, info in slots.items():
                local = sf.local_slot(user_dir, tid, slot) if user_dir else None
                srows.append({
                    "slot": slot, "name": meta.get(tid, {}).get(slot, {}).get("name"),
                    "saved": _iso(info["mtime"]) if info["mtime"] is not None else None,
                    "bytes": info["size"] + info["key_size"],
                    "status": "not_in_ha" if local is None else "changed" if sf.is_changed(info, local) else "in_ha",
                })
            srows.sort(key=lambda s: s["saved"] or "", reverse=True)
            rows.append({"title_id": tid, "game": _game(tid, game_map, names), "open": tid in open_titles,
                         "last_saved": srows[0]["saved"] if srows else None, "slots": srows})
        rows.sort(key=lambda g: g["last_saved"] or "", reverse=True)
        out["users"].append({"uid": uid, "name": users.get(uid, uid), "games": rows})
    sent_names = {(s["title_id"], s["slot"]): s.get("name") for s in (sent or {}).get("saves", [])}
    out["fake_usb"] = [
        {"title_id": tid, "game": _game(tid, game_map, all_names),
         "slots": [{"slot": s, "name": sent_names.get((tid, s))} for s in slots]}
        for tid, slots in sorted(rt_console["fake_usb"].items())
    ]
    return out


async def async_console(hass: HomeAssistant, entry_id: str, refresh: bool = False) -> dict[str, Any]:
    """The panel's PS4 pane. A read walks the console like a pass (listings + savedata.db, no save
    downloads) in one FTP session; it's kept CONSOLE_FRESH_S and every pass refreshes it. A sleeping console, or one
    busy with a pass or a send, gets the last read."""
    rt = _require(hass, entry_id)
    power = _power(hass, entry_id)
    asleep = power in (POWER_REST, POWER_OFF)
    stale = rt.console is None or time.monotonic() - rt.console["mono"] > CONSOLE_FRESH_S
    if not asleep and (refresh or stale) and not rt.op_lock.locked():
        async with rt.op_lock:
            try:
                walk = await session(hass, entry_id, partial(_walk, titles=None, known=set(rt.settings["users"])))
            except ftplib.all_errors as err:
                raise HomeAssistantError(f"Couldn't read the PS4's saves: {err or type(err).__name__}") from err
            _assign_user_folders(rt, walk["names"])
            _remember(rt, walk, None)
            if console := console_dir(hass, rt):
                await hass.async_add_executor_job(_name_copies, console, dict(rt.settings["users"]), walk)
            if walk["names"]:
                await _async_save(hass, rt)
    view = await hass.async_add_executor_job(
        _console_view, console_dir(hass, rt), copy.deepcopy(rt.console), dict(rt.settings["users"]),
        hass.data[DOMAIN][entry_id].get("game_map", {}), _open_titles(hass, entry_id), rt.settings["sent"])
    return {**view, "power_state": power, "asleep": asleep, "running": rt.running, "sent": rt.settings["sent"]}


def settings_view(hass: HomeAssistant, rt: _Runtime) -> dict[str, Any]:
    folder = console_dir(hass, rt)
    return {
        "mode": rt.settings["mode"], "limit_gb": rt.settings["limit_gb"],
        "limit_min_gb": LIMIT_MIN_GB, "limit_max_gb": LIMIT_MAX_GB,
        "folder": str(folder) if folder else None, "usage": rt.usage, "running": rt.running,
        "stopping": rt.running and rt.stop.is_set(), "progress": rt.progress, "last": rt.settings["last"],
        "last_success": rt.settings["last_success"],
        "status": status(rt), "power_state": _power(hass, rt.entry.entry_id),
        "sent": rt.settings["sent"],
    }


# ── Panel API (admin only, like the panel) ─────────────────────────────────────

def _ws_handler(fn: Callable) -> Callable:
    """HomeAssistantError → a WS error the panel shows."""
    async def _wrapped(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
        try:
            connection.send_result(msg["id"], await fn(hass, msg))
        except HomeAssistantError as err:
            connection.send_error(msg["id"], "saves_error", str(err))
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
    await async_set_settings(hass, msg["entry_id"], {k: msg[k] for k in ("mode", "limit_gb") if k in msg})
    return settings_view(hass, runtime(hass, msg["entry_id"]))  # type: ignore[arg-type]


async def _ws_list(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_list(hass, msg["entry_id"])


async def _ws_console(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_console(hass, msg["entry_id"], msg.get("refresh", False))


async def _ws_backup(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    await async_start_backup(hass, msg["entry_id"], msg.get("uid"), msg.get("items"), msg.get("title_id"))
    return {"started": True}


async def _ws_stop(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    await async_stop_backup(hass, msg["entry_id"])
    return {"stopping": True}


async def _ws_keep(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    await async_keep(hass, msg["entry_id"], msg["uid"], msg["title_id"], msg["slot"], msg["stamp"], msg["kept"])
    return {"kept": msg["kept"]}


async def _ws_zip(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_zip(hass, msg["entry_id"], msg["uid"], msg.get("items"))


async def _ws_send(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_send(hass, msg["entry_id"], msg["uid"], msg.get("items"), msg.get("target_entry_id"))


async def _ws_delete(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_delete(hass, msg["entry_id"], msg["uid"], msg["items"])


async def _ws_clear_sent(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return {"deleted": await async_clear_sent(hass, msg["entry_id"])}


_ENTRY = {vol.Required("entry_id"): str}
# the ticked saves of one user, {title_id, slot?, stamp?} each (a game, a newest copy, an older version)
_ITEMS = vol.All([{vol.Required("title_id"): str, vol.Optional("slot"): str, vol.Optional("stamp"): str}],
                 vol.Length(max=MAX_ITEMS))
_COMMANDS = (
    _cmd({vol.Required("type"): "ps4_goldhen/saves/get", **_ENTRY}, _ws_get),
    _cmd({
        vol.Required("type"): "ps4_goldhen/saves/set", **_ENTRY,
        vol.Optional("mode"): vol.In(MODES),
        vol.Optional("limit_gb"): vol.All(vol.Coerce(float), vol.Range(min=LIMIT_MIN_GB, max=LIMIT_MAX_GB)),
    }, _ws_set),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/list", **_ENTRY}, _ws_list),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/console", **_ENTRY, vol.Optional("refresh"): bool}, _ws_console),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/backup", **_ENTRY, vol.Optional("uid"): str,
          vol.Optional("items"): _ITEMS, vol.Optional("title_id"): str}, _ws_backup),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/stop", **_ENTRY}, _ws_stop),
    _cmd({
        vol.Required("type"): "ps4_goldhen/saves/keep", **_ENTRY, vol.Required("uid"): str,
        vol.Required("title_id"): str, vol.Required("slot"): str, vol.Required("stamp"): str,
        vol.Required("kept"): bool,
    }, _ws_keep),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/zip", **_ENTRY, vol.Required("uid"): str,
          vol.Optional("items"): _ITEMS}, _ws_zip),
    _cmd({
        vol.Required("type"): "ps4_goldhen/saves/send", **_ENTRY, vol.Required("uid"): str,
        vol.Optional("items"): _ITEMS, vol.Optional("target_entry_id"): str,
    }, _ws_send),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/delete", **_ENTRY, vol.Required("uid"): str,
          vol.Required("items"): _ITEMS}, _ws_delete),
    _cmd({vol.Required("type"): "ps4_goldhen/saves/clear_sent", **_ENTRY}, _ws_clear_sent),
)


class PS4SaveDownloadView(HomeAssistantView):
    """A zip made by ps4_goldhen/saves/zip (signed path; admin only like the panel)."""
    url = "/api/ps4_goldhen/saves/{entry_id}/download"
    name = "api:ps4_goldhen:save_download"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        if not request["hass_user"].is_admin:
            return web.Response(text="Admin only", status=403)
        name = request.query.get("file", "")
        if runtime(hass, entry_id) is None or not _ZIP_RE.match(name) or Path(name).name != name:
            return web.Response(text="Not found", status=404)
        path = downloads_dir(hass, entry_id) / name
        if not await hass.async_add_executor_job(path.is_file):
            return web.Response(text="Expired: make the zip again", status=404)
        return web.FileResponse(path, headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}", "Cache-Control": "no-store"})


def _register_once(hass: HomeAssistant) -> None:
    flag = f"{DOMAIN}_saves_registered"
    if hass.data.get(flag):
        return
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)
    hass.http.register_view(PS4SaveDownloadView())
    hass.data[flag] = True
