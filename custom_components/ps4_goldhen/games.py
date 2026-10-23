"""The panel's Content tab: a cover grid of the console's games and apps, and one game's page.

Read only, from what HA already holds: the cached app.db (db.py), play time (play_time.py), HA's save copies
(save_backup.py) and the save / screenshot settings. No PS4 call here: the game page's screenshots come from
ps4_goldhen/screenshots/list with a title ID, and its buttons use what the other tabs use (media_player.play_media,
ps4_goldhen.close_title, saves/backup with a title ID, screenshots/sync kind game). Covers are the media browser's
(the media player's proxy, served from the console's cached icon0.png).
"""
from __future__ import annotations

from typing import Any, Callable

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from . import db
from . import game_session
from . import play_time
from . import save_backup
from . import screenshots
from .const import DOMAIN


def _entry_data(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    data = hass.data.get(DOMAIN, {}).get(entry_id)
    if not isinstance(data, dict) or "coordinator" not in data:
        raise HomeAssistantError("Unknown PS4")
    return data


def _cover(data: dict[str, Any], tid: str) -> str | None:
    player = data.get("media_player_entity")  # set once the media player is added (media_player.py)
    return player.cover_url(tid) if player is not None else None


def _kind(game_map: dict[str, dict[str, Any]], tid: str) -> str:
    return "app" if db.is_app(game_map, tid) else "game"


async def async_list(hass: HomeAssistant, entry_id: str) -> dict[str, Any]:
    """Every game and installed app `db.title_list` lists (no built-in titles), last played on the console first,
    with HA's play time."""
    data = _entry_data(hass, entry_id)
    game_map = data.get("game_map", {})
    rt = play_time.runtime(hass, entry_id)
    played: dict[str, dict[str, Any]] = {}
    if rt is not None:
        now = dt_util.utcnow()
        rt.book.accrue(now)
        played = {g["title_id"]: g for g in rt.book.summary(now)["games"]}
    titles = []
    for tid, name in db.title_list(game_map):
        p = played.get(tid)
        titles.append({
            "title_id": tid, "name": name, "kind": _kind(game_map, tid), "cover": _cover(data, tid),
            "play": {"seconds": p["seconds"], "sessions": p["sessions"], "last": p["last"]} if p else None,
        })
    return {"titles": titles}


def _saves(listing: dict[str, Any], tid: str) -> list[dict[str, Any]]:
    """HA's copies of one game per user: each slot's newest copy and how many older versions HA keeps."""
    out = []
    for user in listing["users"]:
        for game in user["games"]:
            if game["title_id"] != tid:
                continue
            out.append({
                "uid": user["uid"], "user": user["name"], "last_saved": game["last_saved"], "bytes": game["bytes"],
                "slots": [{"slot": s["slot"], "name": s["name"], "saved": s["saved"], "bytes": s["bytes"],
                           "versions": len(s["versions"]), "on_ps4": s["on_ps4"]} for s in game["slots"]],
            })
    return out


async def async_detail(hass: HomeAssistant, entry_id: str, title_id: str) -> dict[str, Any]:
    """One game's page: its app.db details, play time, HA's save copies and the save / screenshot settings (what the
    page's buttons need to know)."""
    data = _entry_data(hass, entry_id)
    game_map = data.get("game_map", {})
    tid = title_id.strip().upper()
    names = dict(db.title_list(game_map))
    if tid not in names:
        raise HomeAssistantError("That game isn't in this PS4's game list")
    user = ((data["coordinator"].data or {}).get("login_user_id"))
    info = await hass.async_add_executor_job(db.title_info, data["host"], tid, int(user, 16) if user else None)
    pt = play_time.runtime(hass, entry_id)
    play = None
    if pt is not None:
        now = dt_util.utcnow()
        pt.book.accrue(now)
        play = pt.book.title_summary(tid, now)
    sv = save_backup.runtime(hass, entry_id)
    saves_on = sv is not None and sv.settings["mode"] != save_backup.MODE_OFF
    ss = screenshots.runtime(hass, entry_id)
    # the game left running behind the home screen: Launch of another game closes it, Back up skips it
    behind = game_session.suspended(hass, entry_id)
    return {
        "title_id": tid, "name": names[tid], "kind": _kind(game_map, tid), "cover": _cover(data, tid), "info": info, "play": play,
        "suspended": {"title_id": behind, "name": names.get(behind, behind)} if behind else None,
        "saves": _saves(await save_backup.async_list(hass, entry_id), tid) if saves_on else None,
        "saves_settings": save_backup.settings_view(hass, sv) if sv is not None else None,
        "screenshot_settings": screenshots.settings_view(hass, ss) if ss is not None else None,
    }


# ── Panel API (admin only, like the panel) ─────────────────────────────────────

def _ws_handler(fn: Callable) -> Callable:
    """HomeAssistantError → a WS error the panel shows."""
    async def _wrapped(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
        try:
            connection.send_result(msg["id"], await fn(hass, msg))
        except HomeAssistantError as err:
            connection.send_error(msg["id"], "games_error", str(err))
    return _wrapped


def _cmd(schema: dict, fn: Callable) -> Callable:
    return websocket_api.websocket_command(schema)(websocket_api.require_admin(websocket_api.async_response(
        _ws_handler(fn))))


async def _ws_list(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_list(hass, msg["entry_id"])


async def _ws_detail(hass: HomeAssistant, msg: dict) -> dict[str, Any]:
    return await async_detail(hass, msg["entry_id"], msg["title_id"])


_ENTRY = {vol.Required("entry_id"): str}
_COMMANDS = (
    _cmd({vol.Required("type"): "ps4_goldhen/games/list", **_ENTRY}, _ws_list),
    _cmd({vol.Required("type"): "ps4_goldhen/games/detail", **_ENTRY, vol.Required("title_id"): str}, _ws_detail),
)


def register_once(hass: HomeAssistant) -> None:
    flag = f"{DOMAIN}_games_registered"
    if hass.data.get(flag):
        return
    for command in _COMMANDS:
        websocket_api.async_register_command(hass, command)
    hass.data[flag] = True
