"""PS4 app.db downloader, cache manager, and game map builder.

One cache file per console (`/config/ps4/db/<host>/app.db`), so a failed pull falls back to that console's own list,
and `game_list` = the games for the media player's source list + game browser.
"""
from __future__ import annotations

import contextlib
import ftplib
import io
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
            "cover":       str | None,   # FTP path to icon0.png on the PS4
            "cdn_cover":   str | None,   # Sony CDN https:// URL (may be None/empty)
            "ui_category": str | None,   # "game" / "app"
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

                tid_col       = cols["titleid"]
                name_col      = cols["titlename"]
                cdn_cover_col = cols.get("thumbnailurl")
                vis_col       = cols.get("visible")

                # game vs app + last played, for the game list
                extra_cols = [cols.get("thumbnailurl"), cols.get("uicategory"), cols.get("lastaccesstime")]
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
                    cdn   = str(row[2]).strip() if cdn_cover_col and row[2] else None
                    ui_category = str(row[3]).strip().lower() if row[3] else None
                    last_access = str(row[4]).strip() if row[4] else ""

                    if not tid or not tname:
                        continue

                    if cdn and not cdn.startswith("http"):
                        cdn = None

                    ftp_cover = f"/user/appmeta/{tid}/icon0.png"

                    if tid not in game_map:
                        game_map[tid] = {
                            "name":        tname,
                            "cover":       ftp_cover,
                            "cdn_cover":   cdn,
                            "ui_category": ui_category,
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


def game_list(game_map: dict[str, dict[str, Any]]) -> list[tuple[str, str]]:
    """Games only (no apps, folders or system entries), last played first: [(title ID, display name)].

    A name shared by two titles (e.g. two regions) gets its title ID appended, so every name maps back
    to exactly one title.
    """
    games = sorted(
        ((tid, info) for tid, info in game_map.items() if info.get("ui_category") == "game"),
        key=lambda item: (item[1].get("last_access") or "", item[0]),
        reverse=True,
    )
    counts: dict[str, int] = {}
    for _, info in games:
        counts[info["name"]] = counts.get(info["name"], 0) + 1
    return [(tid, info["name"] if counts[info["name"]] == 1 else f"{info['name']} ({tid})") for tid, info in games]


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
