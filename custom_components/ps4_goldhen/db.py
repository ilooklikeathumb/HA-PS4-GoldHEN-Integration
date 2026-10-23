"""PS4 app.db downloader, cache manager, and game map builder.

One cache file per console (`/config/ps4/db/<host>/app.db`), so a failed pull falls back to that console's own list,
and `title_list` = the games and installed apps for the media player's source list + media browser.
`folder_layout` = one user's home-screen folders for the media browser, read from the cached copy.
`title_info` = one title's details (size, version, region, install date, folder) for the panel's game page.
"""
from __future__ import annotations

import contextlib
import ftplib
import io
import json
import logging
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from .const import APP_DB_REMOTE, APP_DB_LOCAL, DEFAULT_FTP_PORT

_LOGGER = logging.getLogger(__name__)


def _cache_path(host: str) -> Path:
    return Path(APP_DB_LOCAL.format(host=host))


def _ensure_db_dir(host: str) -> None:
    """Create /config/ps4/db/<host>/ if it doesn't exist."""
    _cache_path(host).parent.mkdir(parents=True, exist_ok=True)


def _list_appbrowse_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table'
          AND (
            lower(name) LIKE 'tblappbrowse%'
            OR lower(name) LIKE 'tbl_appbrowse%'
          )
        ORDER BY name
        """
    ).fetchall()
    return [str(r[0]) for r in rows if r and r[0]]


def _table_columns(conn: sqlite3.Connection, table: str) -> dict[str, str]:
    """Return {lower_col_name: actual_col_name} for every column in table."""
    escaped = table.replace('"', '""')
    rows = conn.execute(f'PRAGMA table_info("{escaped}")').fetchall()
    return {str(r[1]).lower(): str(r[1]) for r in rows}


def _extract_game_map(db_bytes: bytes) -> dict[str, dict[str, Any]]:
    """
    Parse raw app.db bytes.
    Returns {
        titleId: {
            "name":        str,
            "cover":       str | None,   # FTP path to icon0.png on the PS4 (the only cover source)
            "ui_category": str | None,   # "game" / "app"
            "category":    str | None,   # e.g. "gd" game, "gde" homebrew, "gdn" store app, "gdi" / "gda" built-in
            "can_remove":  bool | None,  # False = part of the firmware (built-in apps, THE PLAYROOM)
            "last_access": str,          # lastAccessTime, newest of the console's users ("" if unknown)
        }
    }
    """
    game_map: dict[str, dict[str, Any]] = {}
    tmp_path = ""

    try:
        with tempfile.NamedTemporaryFile(
            prefix="ps4gh_appdb_", suffix=".db", delete=False
        ) as tmp:
            tmp.write(db_bytes)
            tmp_path = tmp.name

        conn = sqlite3.connect(tmp_path)
        conn.row_factory = sqlite3.Row

        try:
            all_tables = [
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
            _LOGGER.debug("app.db tables: %s", all_tables)

            tables = _list_appbrowse_tables(conn)
            _LOGGER.debug("app.db appbrowse tables found: %s", tables)

            for table in tables:
                cols = _table_columns(conn, table)
                _LOGGER.debug("Table %s columns: %s", table, list(cols.keys()))

                if "titleid" not in cols or "titlename" not in cols:
                    _LOGGER.warning(
                        "Skipping table %s — missing titleid or titlename", table
                    )
                    continue

                tid_col  = cols["titleid"]
                name_col = cols["titlename"]
                vis_col  = cols.get("visible")

                # game vs app, built-in or installed, last played: for the title list
                extra_cols = [
                    cols.get("uicategory"), cols.get("lastaccesstime"), cols.get("category"), cols.get("canremove"),
                ]
                select_cols = f'"{tid_col}", "{name_col}"' + "".join(
                    f', "{c}"' if c else ", NULL" for c in extra_cols
                )

                where = f'WHERE "{vis_col}" = 1' if vis_col else ""
                query = f'SELECT {select_cols} FROM "{table}" {where}'
                _LOGGER.debug("Executing: %s", query)

                try:
                    rows = conn.execute(query).fetchall()
                except Exception as err:
                    _LOGGER.warning("Query failed on %s: %s", table, err)
                    continue

                _LOGGER.debug("Table %s returned %d rows", table, len(rows))

                for row in rows:
                    tid   = str(row[0]).strip().upper() if row[0] else None
                    tname = str(row[1]).strip()         if row[1] else None
                    ui_category = str(row[2]).strip().lower() if row[2] else None
                    last_access = str(row[3]).strip() if row[3] else ""
                    category = str(row[4]).strip().lower() if row[4] else None
                    can_remove = None if row[5] is None else str(row[5]).strip() != "0"

                    if not tid or not tname:
                        continue

                    ftp_cover = f"/user/appmeta/{tid}/icon0.png"

                    if tid not in game_map:
                        game_map[tid] = {
                            "name":        tname,
                            "cover":       ftp_cover,
                            "ui_category": ui_category,
                            "category":    category,
                            "can_remove":  can_remove,
                            "last_access": last_access,
                        }
                    elif last_access > game_map[tid]["last_access"]:  # one table per user: keep the newest play
                        game_map[tid]["last_access"] = last_access

        finally:
            conn.close()

    finally:
        if tmp_path:
            with contextlib.suppress(Exception):
                os.unlink(tmp_path)

    _LOGGER.info("app.db total resolved: %d titles", len(game_map))
    return game_map


# app.db categories of the console's built-in apps (Internet Browser, Library, PlayStation Store, Capture Gallery…)
# and of preloaded stubs that aren't installed; an installed app reads e.g. "gde" (homebrew) or "gdn"
_BUILT_IN_CATEGORIES = frozenset({"gdi", "gda"})


def _built_in(tid: str, info: dict[str, Any]) -> bool:
    """Part of the firmware: canRemove 0 (the built-in apps and THE PLAYROOM, which reads as a game), or a built-in
    category. Without either column, the console's own apps are the NPXS ones."""
    can_remove, category = info.get("can_remove"), info.get("category")
    if can_remove is None and category is None:
        return tid.startswith("NPXS")
    return can_remove is False or category in _BUILT_IN_CATEGORIES


def _listed(tid: str, info: dict[str, Any]) -> bool:
    """Every installed game and app: not built in (the Internet Browser included: it runs inside the home screen and
    HA can't close it), not a folder or a system entry."""
    return info.get("ui_category") in ("game", "app") and not _built_in(tid, info)


def is_app(game_map: dict[str, dict[str, Any]], tid: str) -> bool:
    """The title is an app (Apollo, Spotify…), not a game."""
    return (game_map.get(tid) or {}).get("ui_category") == "app"


def is_built_in(game_map: dict[str, dict[str, Any]], tid: str) -> bool:
    """The title is part of the firmware (Internet Browser, Library, THE PLAYROOM…): HA doesn't open it."""
    info = game_map.get(tid)
    return info is not None and _built_in(tid, info)


def title_list(game_map: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Games and installed apps (no built-in titles, folders or system entries), last played first:
    [(title ID, display name)].

    A name shared by two titles (e.g. two regions) gets its title ID appended, so every name maps back
    to exactly one title.
    """
    titles = sorted(
        ((tid, info) for tid, info in game_map.items() if _listed(tid, info)),
        key=lambda item: (item[1].get("last_access") or "", item[0]),
        reverse=True,
    )
    counts: dict[str, int] = {}
    for _, info in titles:
        counts[info["name"]] = counts.get(info["name"], 0) + 1
    return [(tid, info["name"] if counts[info["name"]] == 1 else f"{info['name']} ({tid})") for tid, info in titles]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _console_order(row: dict[str, Any]) -> tuple[int, str]:
    """The home screen's order, newest first when sorted in reverse: lastAccessIndex counts up per user."""
    index = _int(row["lastaccessindex"])
    return (-1 if index is None else index, str(row["lastaccesstime"] or ""))


def _pick_user_table(conn: sqlite3.Connection, user_id: int | None) -> str | None:
    """The user's own table (its name ends in the user ID in decimal); else the table of whoever played last."""
    tables = {}
    for table in _list_appbrowse_tables(conn):
        suffix = table.rsplit("_", 1)[-1]
        if suffix.isdigit():
            tables[int(suffix)] = table
    if user_id in tables:
        return tables[user_id]
    newest, picked = "", None
    for table in tables.values():
        cols = _table_columns(conn, table)
        if "lastaccesstime" not in cols:
            continue
        last = conn.execute(f'SELECT max("{cols["lastaccesstime"]}") FROM "{table}"').fetchone()[0]
        if last and str(last) > newest:
            newest, picked = str(last), table
    return picked or next(iter(tables.values()), None)


def _folder_layout(conn: sqlite3.Connection, user_id: int | None) -> dict[str, list] | None:
    table = _pick_user_table(conn, user_id)
    if table is None:
        return None
    cols = _table_columns(conn, table)
    if not {"titleid", "titlename", "foldertype", "parentfolderid"} <= cols.keys():
        return {"folders": [], "loose": []}  # older firmware: no folders
    wanted = ("titleid", "titlename", "visible", "uicategory", "category", "canremove", "foldertype", "parentfolderid",
              "positioninfolder", "folderinfo", "lastaccessindex", "lastaccesstime")
    select = ", ".join(f'"{cols[c]}"' if c in cols else "NULL" for c in wanted)
    rows = sorted(
        (dict(zip(wanted, r)) for r in conn.execute(f'SELECT {select} FROM "{table}"')),
        key=_console_order, reverse=True,
    )
    rows = [r for r in rows if r["titleid"] and _int(r["visible"]) != 0]
    folders = {str(r["titleid"]).strip().upper(): r for r in rows if _int(r["foldertype"]) == 1}

    members: dict[str, list[tuple[int, str]]] = {}
    loose: list[str] = []
    for row in rows:
        tid = str(row["titleid"]).strip().upper()
        info = {
            "ui_category": str(row["uicategory"]).strip().lower() if row["uicategory"] else None,
            "category":    str(row["category"]).strip().lower() if row["category"] else None,
            "can_remove":  None if row["canremove"] is None else str(row["canremove"]).strip() != "0",
        }
        if tid in folders or not row["titlename"] or not _listed(tid, info):
            continue
        parent = str(row["parentfolderid"] or "").strip().upper()
        if parent in folders:
            members.setdefault(parent, []).append((_int(row["positioninfolder"]) or 0, tid))
        else:
            loose.append(tid)

    layout = []
    for fid, row in folders.items():
        titles = [tid for _, tid in sorted(members.get(fid, []), key=lambda member: member[0])]
        if not titles:
            continue
        try:  # {"titleId": a title in the folder, "index": its place, 0-based}: the folder tile shows its cover
            cursor = str(json.loads(row["folderinfo"]).get("titleId") or "").strip().upper()
        except (TypeError, ValueError, AttributeError):
            cursor = ""
        layout.append({
            "id":     fid,
            "name":   str(row["titlename"] or "").strip() or "Folder",  # the console's default name
            "cover":  cursor if cursor in titles else titles[0],
            "titles": titles,
        })
    return {"folders": layout, "loose": loose}


def folder_layout(host: str, user_id: int | None) -> dict[str, list] | None:
    """One user's home-screen folders, from this console's cached app.db (never the PS4).

    `user_id` = the console user (klog logs it in hex at login); None or unknown → whoever played last.
    Returns {"folders": [{"id", "name", "cover", "titles"}], "loose": [title ID]} in the console's order: folders and
    titles outside a folder newest first, as on the home screen; inside a folder, the folder's own order. Only titles
    `title_list` lists; a folder left empty is dropped. None when there's no cached app.db.
    """
    cache_path = _cache_path(host)
    if not cache_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{cache_path.as_uri()}?mode=ro", uri=True)
        try:
            return _folder_layout(conn, user_id)
        finally:
            conn.close()
    except sqlite3.Error as err:
        _LOGGER.warning("[ps4_goldhen] Could not read folders from %s: %s", cache_path, err)
        return None


# contentId's first two letters: the store region the title was bought from (homebrew and Sony's own builds use others)
_REGIONS = {"UP": "US", "EP": "Europe", "JP": "Japan", "HP": "Asia", "KP": "Korea"}


def _install_date(value: Any) -> str | None:
    """installDate is the console's local time, "YYYY-MM-DD HH:MM:SS.mmm"; 1970 = not recorded (preinstalled titles)."""
    text = str(value or "").strip()
    return text[:19] if len(text) >= 19 and text[:4].isdigit() and int(text[:4]) >= 2000 else None


def _app_version(value: Any) -> str | None:
    """APP_VER "01.07" → "1.07" (the installed patch level)."""
    text = str(value or "").strip()
    return text[1:] if len(text) > 1 and text[0] == "0" and text[1].isdigit() else text or None


def _title_info(conn: sqlite3.Connection, tid: str, user_id: int | None) -> dict[str, Any] | None:
    picked = _pick_user_table(conn, user_id)
    tables = sorted(_list_appbrowse_tables(conn), key=lambda t: t != picked)  # the user's own table first
    wanted = ("titleid", "contentid", "contentsize", "installdate", "parentfolderid")
    for table in tables:
        cols = _table_columns(conn, table)
        if "titleid" not in cols:
            continue
        select = ", ".join(f'"{cols[c]}"' if c in cols else "NULL" for c in wanted)
        row = conn.execute(f'SELECT {select} FROM "{table}" WHERE upper("{cols["titleid"]}") = ?', (tid,)).fetchone()
        if row is None:
            continue
        row = dict(zip(wanted, row))
        folder = None
        parent = str(row["parentfolderid"] or "").strip()
        if parent and "titlename" in cols:
            hit = conn.execute(f'SELECT "{cols["titlename"]}" FROM "{table}" WHERE "{cols["titleid"]}" = ?',
                               (parent,)).fetchone()
            folder = (str(hit[0] or "").strip() or "Folder") if hit else None  # the console's default name
        content_id = str(row["contentid"] or "").strip()
        info: dict[str, Any] = {
            "content_id": content_id or None,
            "region": _REGIONS.get(content_id[:2].upper()),
            "size": _int(row["contentsize"]),
            "installed": _install_date(row["installdate"]),
            "folder": folder,
            "version": None,
        }
        with contextlib.suppress(sqlite3.Error):  # tbl_appinfo: the title's param.sfo keys
            keys = dict(conn.execute(
                "SELECT key, val FROM tbl_appinfo WHERE upper(titleId) = ? AND key IN ('APP_VER', 'VERSION')", (tid,)))
            info["version"] = _app_version(keys.get("APP_VER") or keys.get("VERSION"))
        return info
    return None


def title_info(host: str, tid: str, user_id: int | None) -> dict[str, Any] | None:
    """One title's details for the panel's game page, from this console's cached app.db (never the PS4): content ID,
    store region, installed size (bytes), install date (console time), version, the home-screen folder it sits in
    (of `user_id`, else whoever played last). None when there's no cached app.db or the title isn't in it."""
    cache_path = _cache_path(host)
    if not cache_path.exists():
        return None
    try:
        conn = sqlite3.connect(f"{cache_path.as_uri()}?mode=ro", uri=True)
        try:
            return _title_info(conn, tid.strip().upper(), user_id)
        finally:
            conn.close()
    except sqlite3.Error as err:
        _LOGGER.warning("[ps4_goldhen] Could not read %s from %s: %s", tid, cache_path, err)
        return None


def load_cached(host: str) -> dict[str, dict[str, Any]] | None:
    """
    Load this console's cached app.db (/config/ps4/db/<host>/app.db) if it exists.
    Returns None if no cache is available.
    """
    cache_path = _cache_path(host)
    if not cache_path.exists():
        _LOGGER.debug("No cached app.db found at %s", cache_path)
        return None
    try:
        db_bytes = cache_path.read_bytes()
        _LOGGER.info(
            "[ps4_goldhen] Loading app.db from cache: %s (%d bytes)",
            cache_path, len(db_bytes),
        )
        return _extract_game_map(db_bytes)
    except Exception as err:
        _LOGGER.warning("[ps4_goldhen] Failed to load cached app.db: %s", err)
        return None


def download_and_parse(host: str, port: int) -> dict[str, dict[str, Any]]:
    """
    Blocking — FTP-download app.db from the PS4, cache it to
    /config/ps4/db/<host>/app.db, and return the game map.
    Call via hass.async_add_executor_job().

    Falls back to the cached copy if the PS4 is unreachable.
    """
    _ensure_db_dir(host)
    _LOGGER.info(
        "[ps4_goldhen] Attempting app.db download from %s:%d via FTP", host, port
    )

    buffer = io.BytesIO()
    db_bytes: bytes | None = None

    try:
        with ftplib.FTP() as ftp:
            ftp.connect(host, port, timeout=20)
            ftp.login()

            for candidate in (APP_DB_REMOTE, APP_DB_REMOTE + ".bak"):
                buffer.seek(0)
                buffer.truncate()
                try:
                    ftp.retrbinary(f"RETR {candidate}", buffer.write)
                    db_bytes = buffer.getvalue()
                    if db_bytes:
                        _LOGGER.info(
                            "[ps4_goldhen] Downloaded %s from PS4 (%d bytes)",
                            candidate, len(db_bytes),
                        )
                        break
                except ftplib.error_perm as err:
                    _LOGGER.debug("FTP RETR %s failed: %s", candidate, err)
                    continue

    except ftplib.all_errors as err:
        _LOGGER.warning(
            "[ps4_goldhen] FTP connection to %s:%d failed: %s — "
            "will attempt to use cached app.db",
            host, port, err,
        )
    except Exception as err:
        _LOGGER.error(
            "[ps4_goldhen] Unexpected error downloading app.db from %s:%d: %s — "
            "will attempt to use cached app.db",
            host, port, err,
        )

    if db_bytes:
        # Save fresh copy to /config/ps4/db/<host>/app.db
        cache_path = _cache_path(host)
        try:
            cache_path.write_bytes(db_bytes)
            _LOGGER.info(
                "[ps4_goldhen] app.db cached to %s", cache_path
            )
        except Exception as err:
            _LOGGER.warning(
                "[ps4_goldhen] Could not write app.db cache to %s: %s",
                cache_path, err,
            )
        return _extract_game_map(db_bytes)

    # FTP failed — fall back to cache
    cached = load_cached(host)
    if cached is not None:
        _LOGGER.info(
            "[ps4_goldhen] Using cached app.db — %d titles loaded", len(cached)
        )
        return cached

    raise FileNotFoundError(
        f"Could not download app.db from PS4 FTP ({host}:{port}) "
        f"and no cached copy exists at {_cache_path(host)}. "
        f"Ensure GoldHEN FTP is enabled on your PS4 and it is reachable."
    )
