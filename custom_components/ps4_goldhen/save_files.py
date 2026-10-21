"""PS4 save data on the console and in HA: FTP walk, change check, Apollo's USB layout, history.
No HA imports.

Layout on the console (read-only listings on a PS4 Pro, FW 12.00):
  /user/home/<user ID, 8 hex>/username.dat                     the user's name (17 B)
  /user/home/<user ID>/savedata/<title ID>/sdimg_<slot>        encrypted save image, fixed size (e.g. 3 MB / 10 MB)
  /user/home/<user ID>/savedata/<title ID>/<slot>.bin          its sealed key (96 B)
A title folder's mtime does NOT move when a slot is rewritten in place (one title: folder 19:35Z, its image 00:55Z), so
a pass lists every title folder it checks and compares each slot's size + mtime with HA's copy.

Layout in HA, per console and user. Apollo Save Tool reads PS4/SAVEDATA/<16 hex>/<title ID>/<slot> + <slot>.bin from a
USB stick root, any 16 hex, and re-signs on "Copy to HDD" (Apollo docs/usage.md, source exec_cmd.c _copy_save_pfs):
  Latest/PS4/SAVEDATA/0000000000000000/<title ID>/<slot>, <slot>.bin
  History/<game> (<title ID>)/<slot>/<YYYY-MM-DD HHMMSS>/PS4/SAVEDATA/0000000000000000/<title ID>/<slot>, <slot>.bin
A copy gets the console's mtime, so Latest is also the "already backed up" record (the folder is the truth), and a
version folder is named after when the game wrote it (HA's time zone). A `.keep` file in a version folder exempts it
from pruning. History is thinned like Time Machine / Syncthing's staggered versioning (see thin()).

Restore: HA uploads a copy into Apollo's Fake USB folder on a console, /data/fakeusb/PS4/SAVEDATA/0000000000000000/
<title ID>/ (the only console folder HA writes to), then Apollo → USB Saves → Copy save game to HDD. The same tree
also unpacks from a zip onto a real stick.

Save names: the console's own save list, /system_data/savedata/<user ID>/db/user/savedata.db (SQLite,
Apollo saves.h SAVES_DB_PATH; 16 KB on a PS4 Pro, journal_mode delete), table savedata: title_id, dir_name (= the slot),
main_title (the game, or "Saved Data"), sub_title (e.g. "Chapter 1 (100.0%)"), detail. HA keeps a copy's name beside
it: Latest/saves.json ({title: {slot: meta}}) and <version>/save.json. Both sit outside PS4/, so they never go into a
zip or to a console.
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, tzinfo
import ftplib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
from typing import Any, Iterable
import zipfile

from .capture_files import download
from .ftp_listing import list_directory

HOME_ROOT = "/user/home"
ACCOUNT_DIR = "0000000000000000"  # Apollo: any 16 hex works for USB saves, it re-signs to the active user
# Apollo ≥ 1.3.0 lists /data/fakeusb in USB Saves like a stick (README "External saves (HDD)", source saves.h
# FAKE_USB_PATH). Its default USB Saves Source (Auto-detect) uses it when no stick is plugged in.
FAKE_USB_ROOT = "/data/fakeusb"
FAKE_USB_SAVES = f"{FAKE_USB_ROOT}/PS4/SAVEDATA/{ACCOUNT_DIR}"  # the only console folder HA writes to or clears
LATEST, HISTORY, STAGING, DOWNLOADS = "Latest", "History", ".staging", ".downloads"
KEEP_MARK = ".keep"
LATEST_META, VERSION_META = "saves.json", "save.json"  # save names, beside PS4/
STAMP_FMT = "%Y-%m-%d %H%M%S"
USERNAME_MAX = 256
SAVE_DB_REMOTE = "/system_data/savedata/{uid}/db/user/savedata.db"  # Apollo saves.h SAVES_DB_PATH
SAVE_DB_MAX = 8 << 20
TEXT_MAX = 200
_GENERIC_GAME = "saved data"  # main_title some games leave as is

KEEP_NEWEST = 5                 # always kept, whatever their age
HOURLY_FOR = timedelta(days=1)  # then the newest per hour for a day,
DAILY_FOR = timedelta(days=30)  # per day for 30 days, per ISO week after that

_USER_RE = re.compile(r"^[0-9a-f]{8}$")
_TITLE_RE = re.compile(r"^[A-Z]{4}\d{5}$")
_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{6})(?: \(\d+\))?$")
_GAME_DIR_RE = re.compile(r"\(([A-Z]{4}\d{5})\)$")


def valid_user(value: Any) -> bool:
    return isinstance(value, str) and bool(_USER_RE.match(value))


def valid_title(value: Any) -> bool:
    return isinstance(value, str) and bool(_TITLE_RE.match(value))


def valid_slot(value: Any) -> bool:
    return isinstance(value, str) and bool(_SLOT_RE.match(value))


def valid_stamp(value: Any) -> bool:
    return isinstance(value, str) and bool(_STAMP_RE.match(value))


# ── Console (FTP, blocking) ────────────────────────────────────────────────────

def title_remote(uid: str, title: str) -> str:
    return f"{HOME_ROOT}/{uid}/savedata/{title}"


def image_remote(uid: str, title: str, slot: str) -> str:
    return f"{title_remote(uid, title)}/sdimg_{slot}"


def key_remote(uid: str, title: str, slot: str) -> str:
    return f"{title_remote(uid, title)}/{slot}.bin"


def _entries(ftp: ftplib.FTP, path: str) -> list[dict[str, Any]]:
    try:
        return list_directory(ftp, path)
    except ftplib.error_perm:  # 550: no such folder
        return []


def list_users(ftp: ftplib.FTP) -> list[str]:
    return [e["name"] for e in _entries(ftp, HOME_ROOT) if e["is_dir"] and valid_user(e["name"])]


def read_user_name(ftp: ftplib.FTP, uid: str) -> str | None:
    """username.dat: the name, NUL-padded. At most USERNAME_MAX bytes are kept."""
    buf = bytearray()

    def _take(chunk: bytes) -> None:
        buf.extend(chunk[: max(0, USERNAME_MAX - len(buf))])

    try:
        ftp.retrbinary(f"RETR {HOME_ROOT}/{uid}/username.dat", _take)
    except ftplib.error_perm:
        return None
    return bytes(buf).split(b"\0", 1)[0].decode("utf-8", "replace").strip() or None


def list_titles(ftp: ftplib.FTP, uid: str) -> list[str]:
    return sorted(e["name"] for e in _entries(ftp, f"{HOME_ROOT}/{uid}/savedata") if e["is_dir"] and valid_title(e["name"]))


def _epoch(modified_utc: str | None) -> int | None:
    if not modified_utc:
        return None
    try:
        return int(datetime.fromisoformat(modified_utc.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def list_slots(ftp: ftplib.FTP, uid: str, title: str) -> dict[str, dict[str, Any]]:
    """slot → size + mtime of its image and key. A slot needs both files (the console writes them together)."""
    files = {e["name"]: e for e in _entries(ftp, title_remote(uid, title)) if not e["is_dir"]}
    slots: dict[str, dict[str, Any]] = {}
    for name, image in files.items():
        slot = name[len("sdimg_"):] if name.startswith("sdimg_") else None
        key = files.get(f"{slot}.bin") if slot else None
        if key is None or not valid_slot(slot):
            continue
        slots[slot] = {  # type: ignore[index]
            "size": image["size"] or 0, "mtime": _epoch(image["modified_utc"]),
            "key_size": key["size"] or 0, "key_mtime": _epoch(key["modified_utc"]),
        }
    return slots


def fetch_slot(ftp: ftplib.FTP, uid: str, title: str, slot: str, info: dict[str, Any],
               staging: Path) -> tuple[Path, Path]:
    """Download image + key into staging (streamed, .part then rename), stamped with the console's mtimes."""
    image = staging / f"{uid}_{title}_{slot}"
    key = staging / f"{uid}_{title}_{slot}.bin"
    download(ftp, image_remote(uid, title, slot), image)
    download(ftp, key_remote(uid, title, slot), key)
    for path, mtime in ((image, info["mtime"]), (key, info["key_mtime"])):
        if mtime is not None:
            os.utime(path, (mtime, mtime))
    return image, key


def _text(value: Any) -> str:
    return value.strip()[:TEXT_MAX] if isinstance(value, str) else ""


def parse_save_db(data: bytes) -> dict[str, dict[str, dict[str, str]]]:
    """savedata.db → title → slot → {name, game, detail} (only the non-empty ones). {} when it isn't one."""
    out: dict[str, dict[str, dict[str, str]]] = {}
    fd, tmp = tempfile.mkstemp(prefix="ps4gh_savedata_", suffix=".db")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            cols = {row[1].lower() for row in conn.execute("PRAGMA table_info(savedata)")}
            if not {"title_id", "dir_name"} <= cols:
                return {}
            picked = ", ".join(c if c in cols else "NULL" for c in ("sub_title", "main_title", "detail"))
            for title, slot, *texts in conn.execute(f"SELECT title_id, dir_name, {picked} FROM savedata"):
                if valid_title(title) and valid_slot(slot):
                    out.setdefault(title, {})[slot] = {
                        k: _text(v) for k, v in zip(("name", "game", "detail"), texts) if _text(v)}
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return out


def read_save_db(ftp: ftplib.FTP, uid: str) -> dict[str, dict[str, dict[str, str]]]:
    """The console's save list for one user (what Settings → Saved Data shows). {} when it's missing or too big: the
    panel then shows slot IDs."""
    folder, name = SAVE_DB_REMOTE.format(uid=uid).rsplit("/", 1)
    entry = next((e for e in _entries(ftp, folder) if e["name"] == name and not e["is_dir"]), None)
    if entry is None or not entry["size"] or entry["size"] > SAVE_DB_MAX:
        return {}
    buf = bytearray()
    try:
        ftp.retrbinary(f"RETR {folder}/{name}", buf.extend)
    except ftplib.error_perm:
        return {}
    return parse_save_db(bytes(buf))


def game_names(meta: dict[str, dict[str, dict[str, str]]]) -> dict[str, str]:
    """title → the game's name from its saves: the shortest main_title that isn't "Saved Data" (a game's backup slots
    can say "<game> (Backup)")."""
    out = {}
    for title, slots in meta.items():
        names = [m["game"] for m in slots.values() if m.get("game") and m["game"].lower() != _GENERIC_GAME]
        if names:
            out[title] = min(names, key=len)
    return out


# ── Console: Apollo's Fake USB folder (restore, FTP, blocking) ─────────────────

def _sent_name(name: str) -> bool:
    return valid_slot(name[:-4] if name.endswith(".bin") else name)


def fake_usb_remote(rel: str) -> str:
    """A zip_members name (PS4/SAVEDATA/<account>/<title>/<file>) → its path in the console's Fake USB folder."""
    parts = rel.split("/")
    if (len(parts) != 5 or parts[:3] != ["PS4", "SAVEDATA", ACCOUNT_DIR] or not valid_title(parts[3])
            or not _sent_name(parts[4])):
        raise ValueError(f"not a save file: {rel!r}")
    return f"{FAKE_USB_SAVES}/{parts[3]}/{parts[4]}"


def _ensure_dir(ftp: ftplib.FTP, path: str) -> None:
    current = ""
    for part in path.strip("/").split("/"):
        current += "/" + part
        try:
            ftp.cwd(current)
        except ftplib.error_perm:
            ftp.mkd(current)


def clear_fake_usb(ftp: ftplib.FTP) -> int:
    """Delete what HA sent before: save files in FAKE_USB_SAVES/<title>/, then the emptied title folders. Anything
    else under /data/fakeusb (another account folder, Apollo's own exports) stays. Returns files deleted."""
    deleted = 0
    for title in _entries(ftp, FAKE_USB_SAVES):
        if not title["is_dir"] or not valid_title(title["name"]):
            continue
        folder = f"{FAKE_USB_SAVES}/{title['name']}"
        for item in _entries(ftp, folder):
            if not item["is_dir"] and _sent_name(item["name"]):
                ftp.delete(f"{folder}/{item['name']}")
                deleted += 1
        ftp.cwd(FAKE_USB_SAVES)
        with contextlib.suppress(ftplib.error_perm):
            ftp.rmd(folder)  # only succeeds when empty
    return deleted


def list_fake_usb(ftp: ftplib.FTP) -> dict[str, list[str]]:
    """What sits in the Fake USB folder's account folder: title → slots (image and key both there)."""
    out = {}
    for title in _entries(ftp, FAKE_USB_SAVES):
        if title["is_dir"] and valid_title(title["name"]):
            names = {e["name"] for e in _entries(ftp, f"{FAKE_USB_SAVES}/{title['name']}") if not e["is_dir"]}
            slots = sorted(n for n in names if valid_slot(n) and f"{n}.bin" in names)
            if slots:
                out[title["name"]] = slots
    return out


def send_to_fake_usb(ftp: ftplib.FTP, members: list[tuple[Path, str]]) -> int:
    """Upload HA's copies (zip_members of Latest or a version) into the Fake USB folder, streamed in 64 KB blocks,
    then compare each file's size on the console. A failed upload deletes its partial file. Returns bytes sent."""
    remotes = [(path, fake_usb_remote(rel)) for path, rel in members]  # every name checked before the first write
    folders: set[str] = set()
    sent = 0
    for path, remote in remotes:
        folder = remote.rsplit("/", 1)[0]
        if folder not in folders:
            _ensure_dir(ftp, folder)
            folders.add(folder)
        try:
            with open(path, "rb") as fh:
                ftp.storbinary(f"STOR {remote}", fh, blocksize=65536)
        except BaseException:
            with contextlib.suppress(Exception):
                ftp.delete(remote)
            raise
        sent += path.stat().st_size
    on_console = {f"{folder}/{e['name']}": e["size"] for folder in folders for e in _entries(ftp, folder)}
    for path, remote in remotes:
        if on_console.get(remote) != path.stat().st_size:
            raise OSError(f"{remote}: {on_console.get(remote)} bytes on the PS4, {path.stat().st_size} in HA")
    return sent


# ── HA side (blocking) ─────────────────────────────────────────────────────────

def _account_dir(root: Path) -> Path:
    return root / "PS4" / "SAVEDATA" / ACCOUNT_DIR


def usb_tree(root: Path, title: str) -> Path:
    """<root>/PS4/SAVEDATA/<account>/<title>: copy <root>/PS4 to a USB stick and Apollo lists the save."""
    return _account_dir(root) / title


def latest_dir(user_dir: Path, title: str) -> Path:
    return usb_tree(user_dir / LATEST, title)


def local_slot(user_dir: Path, title: str, slot: str) -> dict[str, Any] | None:
    """HA's newest copy of a slot, in list_slots' shape; None if there is none (or half of one)."""
    folder = latest_dir(user_dir, title)
    image, key = folder / slot, folder / f"{slot}.bin"
    if not (image.is_file() and key.is_file()):
        return None
    si, sk = image.stat(), key.stat()
    return {"size": si.st_size, "mtime": int(si.st_mtime), "key_size": sk.st_size, "key_mtime": int(sk.st_mtime)}


def is_changed(console: dict[str, Any], local: dict[str, Any] | None) -> bool:
    """New or rewritten on the console. A console file without an mtime always counts as changed."""
    if local is None or console["mtime"] is None or console["key_mtime"] is None:
        return True
    return any(console[k] != local[k] for k in ("size", "mtime", "key_size", "key_mtime"))


def stamp(mtime: float, tz: tzinfo) -> str:
    return datetime.fromtimestamp(mtime, tz).strftime(STAMP_FMT)


def parse_stamp(name: str) -> datetime | None:
    m = _STAMP_RE.match(name)
    return datetime.strptime(m.group(1), STAMP_FMT) if m else None


def history_title_dir(user_dir: Path, title: str, label: str | None) -> Path:
    """The game's History folder: an existing one for this title (the game's name may have arrived later), else new."""
    root = user_dir / HISTORY
    if root.is_dir():
        for path in sorted(root.iterdir()):
            if path.is_dir() and (path.name == title or path.name.endswith(f"({title})")):
                return path
    return root / (f"{label} ({title})" if label and label != title else title)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    part = path.with_name(path.name + ".part")
    part.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    os.replace(part, path)


def latest_meta(user_dir: Path) -> dict[str, dict[str, dict[str, str]]]:
    """Names of HA's newest copies: title → slot → {name, game, detail}."""
    return _read_json(user_dir / LATEST / LATEST_META)


def version_meta(version_dir: Path) -> dict[str, str]:
    return _read_json(version_dir / VERSION_META)


def _set_latest_meta(user_dir: Path, meta: dict[str, dict[str, dict[str, str]]]) -> None:
    path = user_dir / LATEST / LATEST_META
    meta = {title: slots for title, slots in meta.items() if slots}
    if meta:
        _write_json(path, meta)
    else:
        path.unlink(missing_ok=True)


def merge_latest_meta(user_dir: Path, names: dict[str, dict[str, dict[str, str]]]) -> None:
    """Name HA's newest copies (only those that match the console's save the names came from). Writes only on a
    change."""
    meta = latest_meta(user_dir)
    merged = {title: dict(slots) for title, slots in meta.items()}
    for title, slots in names.items():
        merged.setdefault(title, {}).update(slots)
    if merged != meta:
        _set_latest_meta(user_dir, merged)


def install(user_dir: Path, title: str, slot: str, staged: tuple[Path, Path], label: str | None,
            tz: tzinfo) -> Path | None:
    """Move HA's current copy of the slot into History (named after when the game wrote it, its save name along), then
    the new one into Latest, unnamed until the pass names it. Renames only (staging sits on the same disk). Returns the
    History version folder made, if any."""
    latest = latest_dir(user_dir, title)
    image, key = latest / slot, latest / f"{slot}.bin"
    meta = latest_meta(user_dir)
    old_name = meta.get(title, {}).pop(slot, None)
    made = None
    if image.is_file() and key.is_file():
        base = history_title_dir(user_dir, title, label) / slot
        name = first = stamp(image.stat().st_mtime, tz)
        n = 2
        while (base / name).exists():
            name, n = f"{first} ({n})", n + 1
        dest = usb_tree(base / name, title)
        dest.mkdir(parents=True)
        os.replace(image, dest / slot)
        os.replace(key, dest / f"{slot}.bin")
        made = base / name
        if old_name:
            _write_json(made / VERSION_META, old_name)
    latest.mkdir(parents=True, exist_ok=True)
    os.replace(staged[0], image)
    os.replace(staged[1], key)
    if old_name is not None:
        _set_latest_meta(user_dir, meta)
    return made


def delete_latest(user_dir: Path, title: str, slot: str) -> bool:
    """Delete HA's newest copy of a slot (panel → Delete from HA). Its older versions stay. The next backup copies the
    slot again if it's still on the console."""
    folder = latest_dir(user_dir, title)
    found = False
    for path in (folder / slot, folder / f"{slot}.bin"):
        if path.is_file():
            path.unlink()
            found = True
    meta = latest_meta(user_dir)
    if meta.get(title, {}).pop(slot, None) is not None:
        _set_latest_meta(user_dir, meta)
    with contextlib.suppress(OSError):
        folder.rmdir()
    return found


def dir_bytes(path: Path) -> int:
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file()) if path.is_dir() else 0


def _title_of(game_dir: str) -> str | None:
    if valid_title(game_dir):
        return game_dir
    m = _GAME_DIR_RE.search(game_dir)
    return m.group(1) if m else None


def versions(user_dir: Path) -> list[dict[str, Any]]:
    """Every History version of this user: title, slot, stamp, when (naive, HA's zone), kept, bytes, path."""
    out: list[dict[str, Any]] = []
    root = user_dir / HISTORY
    if not root.is_dir():
        return out
    for game_dir in root.iterdir():
        title = _title_of(game_dir.name) if game_dir.is_dir() else None
        if title is None:
            continue
        for slot_dir in game_dir.iterdir():
            if not (slot_dir.is_dir() and valid_slot(slot_dir.name)):
                continue
            for version in slot_dir.iterdir():
                when = parse_stamp(version.name) if version.is_dir() else None
                if when is None:
                    continue
                out.append({
                    "title": title, "slot": slot_dir.name, "stamp": version.name, "when": when,
                    "kept": (version / KEEP_MARK).exists(), "bytes": dir_bytes(version), "path": version,
                })
    return out


def latest_slots(user_dir: Path) -> list[dict[str, Any]]:
    """Every slot in Latest: title, slot, mtime (the console's), bytes."""
    out: list[dict[str, Any]] = []
    root = _account_dir(user_dir / LATEST)
    if not root.is_dir():
        return out
    for title_dir in root.iterdir():
        if not (title_dir.is_dir() and valid_title(title_dir.name)):
            continue
        for image in title_dir.iterdir():
            key = image.with_name(image.name + ".bin")
            if image.is_file() and valid_slot(image.name) and key.is_file():
                out.append({"title": title_dir.name, "slot": image.name, "mtime": int(image.stat().st_mtime),
                            "bytes": image.stat().st_size + key.stat().st_size})
    return out


def _bucket(when: datetime, now: datetime) -> tuple:
    age = now - when
    if age < HOURLY_FOR:
        return ("hour", when.strftime("%Y%m%d%H"))
    if age < DAILY_FOR:
        return ("day", when.date())
    return ("week", tuple(when.isocalendar()[:2]))


def thin(slot_versions: Iterable[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """The History versions of one slot to delete: keep the newest KEEP_NEWEST, then the newest per hour for a day, per
    day for 30 days, per week after that (Time Machine / Syncthing "staggered"). Kept (.keep) versions always stay."""
    doomed, seen = [], set()
    for i, version in enumerate(sorted(slot_versions, key=lambda v: v["when"], reverse=True)):
        if version["kept"]:
            continue
        bucket = _bucket(version["when"], now)
        if i < KEEP_NEWEST or bucket not in seen:
            seen.add(bucket)
            continue
        doomed.append(version)
    return doomed


def pick_for_room(all_versions: Iterable[dict[str, Any]], deficit: int) -> list[dict[str, Any]] | None:
    """Oldest unkept History versions that free `deficit` bytes; None if even all of them don't. Latest (each slot's
    newest copy) is never a candidate."""
    if deficit <= 0:
        return []
    picked, freed = [], 0
    for version in sorted((v for v in all_versions if not v["kept"]), key=lambda v: v["when"]):
        picked.append(version)
        freed += version["bytes"]
        if freed >= deficit:
            return picked
    return None


def delete_version(version: dict[str, Any]) -> None:
    shutil.rmtree(version["path"], ignore_errors=True)
    for parent in (version["path"].parent, version["path"].parent.parent):  # empty slot / game folders go too
        try:
            parent.rmdir()
        except OSError:
            break


def set_kept(version_dir: Path, kept: bool) -> None:
    mark = version_dir / KEEP_MARK
    if kept:
        mark.touch()
    elif mark.exists():
        mark.unlink()


def usage(console_dir: Path) -> dict[str, int]:
    """Bytes and History versions under one console's folder (staging and downloads not counted)."""
    total = versions_count = 0
    if console_dir.is_dir():
        for user_dir in console_dir.iterdir():
            if user_dir.is_dir() and not user_dir.name.startswith("."):
                total += dir_bytes(user_dir)
                versions_count += len(versions(user_dir))
    return {"bytes": total, "versions": versions_count}


def disk_room(path: Path, floor: int) -> int:
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free - floor


def members(user_dir: Path, picks: Iterable[tuple[str, str, str | None]]) -> list[tuple[Path, str]]:
    """HA's files for each (title, slot, version stamp or None = the newest copy), as (file, name in the zip). Names
    start with PS4/ so a zip unpacks straight onto a USB stick. Names and Keep marks never go along."""
    out = []
    for title, slot, version in picks:
        root = history_title_dir(user_dir, title, None) / slot / version if version else user_dir / LATEST
        folder = usb_tree(root, title)
        image, key = folder / slot, folder / f"{slot}.bin"
        if image.is_file() and key.is_file():
            out += [(image, f"PS4/SAVEDATA/{ACCOUNT_DIR}/{title}/{slot}"),
                    (key, f"PS4/SAVEDATA/{ACCOUNT_DIR}/{title}/{slot}.bin")]
    return out


def write_zip(dest: Path, members: list[tuple[Path, str]]) -> int:
    """Stored (the images are encrypted, so nothing compresses), streamed from disk to <dest>.part, then renamed."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with zipfile.ZipFile(part, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for path, name in members:
                zf.write(path, name)
        os.replace(part, dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    return dest.stat().st_size


def prune_downloads(folder: Path, max_age_s: float, now: float) -> None:
    if folder.is_dir():
        for path in folder.iterdir():
            if path.is_file() and now - path.stat().st_mtime > max_age_s:
                path.unlink(missing_ok=True)
