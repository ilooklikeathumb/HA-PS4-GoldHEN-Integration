"""PS4 screenshots on the console and in HA: FTP walk, downloads, local copies. No HA imports.

Layout on the console (checked on a PS4 Pro, FW 12.00):
  /user/av_contents/photo/<app>/<title ID>/<bucket>/<YYYYMMDD_HHMMSS_nnnnnnnn>.jpg   4K JPEG, ~1 MB
  /user/av_contents/thumbnails/photo/<same path>.jpg                                 440x248 JPEG, ~11 KB
<app> is the app that took the shot (NPXS20001 = the system UI's Share button), <title ID> the game on screen
(NPXS20001 = home screen), <bucket> a short hex folder; each shot seen so far got its own. The file name is the
console's local time. A <name>.ext JSON sidecar ({"trophy": [...]}) was only seen on automatic trophy shots, so it
marks those. av_content_bg.db is no index (its table is empty), so the newest shots come from folder mtimes: a folder's
mtime moves when an entry lands in it, so a bucket older than the Nth newest shot can't hold a newer one.
"""
from __future__ import annotations

import contextlib
import ftplib
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Iterable

from .ftp_listing import list_directory

PHOTO_ROOT = "/user/av_contents/photo"
THUMB_ROOT = "/user/av_contents/thumbnails/photo"
HOME_SCREEN_TID = "NPXS20001"

FTP_TIMEOUT = 15
CONNECT_TRIES = 3     # a refused connect is retried: another FTP client may be using the console
CONNECT_RETRY_S = 2.0

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(jpg|jpeg|png)$", re.IGNORECASE)
_TAKEN_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})_")
_UNSAFE_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def valid_rel(rel: Any) -> bool:
    """<app>/<title ID>/<bucket>/<image>: the only shape the WS commands and the view accept (no '..', no '/x')."""
    if not isinstance(rel, str):
        return False
    parts = rel.split("/")
    return len(parts) == 4 and all(_SEGMENT_RE.match(p) for p in parts[:3]) and bool(_IMAGE_RE.match(parts[3]))


def taken_from_name(name: str) -> str | None:
    """Console local time from the file name, as YYYY-MM-DDTHH:MM:SS (no zone: the console's own clock)."""
    m = _TAKEN_RE.match(name)
    return "{}-{}-{}T{}:{}:{}".format(*m.groups()) if m else None


def safe_name(text: str | None, limit: int = 80) -> str | None:
    """A folder name that works on every filesystem HA's media folder may sit on (Samba included)."""
    if not text:
        return None
    clean = " ".join(_UNSAFE_RE.sub(" ", str(text)).split()).strip(". ")
    return clean[:limit].rstrip(". ") or None


def game_folder(title_id: str, game_map: dict[str, dict[str, Any]]) -> str:
    if title_id == HOME_SCREEN_TID:
        return "Home screen"
    return safe_name((game_map.get(title_id) or {}).get("name")) or title_id


def connect(host: str, port: int, *, tries: int = CONNECT_TRIES, retry_s: float = CONNECT_RETRY_S,
            factory: Callable[[], ftplib.FTP] = ftplib.FTP, sleep: Callable[[float], None] = time.sleep) -> ftplib.FTP:
    """Log in; retry a refused connect (an FTP client outside HA, on a PC, may be using the console)."""
    for attempt in range(tries):
        ftp = factory()
        try:
            ftp.connect(host, port, timeout=FTP_TIMEOUT)
            ftp.login()
            return ftp
        except ConnectionRefusedError:
            with contextlib.suppress(Exception):
                ftp.close()
            if attempt == tries - 1:
                raise
            sleep(retry_s)
    raise ConnectionRefusedError(host)  # tries < 1


def _dirs(ftp: ftplib.FTP, path: str) -> list[dict[str, Any]]:
    try:
        return [e for e in list_directory(ftp, path) if e["is_dir"]]
    except ftplib.error_perm:  # 550: no such folder (no screenshots yet)
        return []


def list_buckets(ftp: ftplib.FTP, title_id: str | None = None) -> list[dict[str, Any]]:
    """Every bucket folder with its mtime: 1 listing + 1 per app + 1 per game. With `title_id`, only that game's:
    1 listing + 1 per app (an app without the game answers 550)."""
    if title_id is not None:
        if not _SEGMENT_RE.match(title_id):
            return []
        return [b for app in _dirs(ftp, PHOTO_ROOT) for b in _dirs(ftp, f"{app['path']}/{title_id}")]
    return [b for app in _dirs(ftp, PHOTO_ROOT) for game in _dirs(ftp, app["path"]) for b in _dirs(ftp, game["path"])]


def shots_in_bucket(ftp: ftplib.FTP, bucket_path: str) -> list[dict[str, Any]]:
    rel_dir = bucket_path[len(PHOTO_ROOT) + 1:]
    parts = rel_dir.split("/")
    if not bucket_path.startswith(PHOTO_ROOT + "/") or len(parts) != 3:
        return []
    try:
        entries = [e for e in list_directory(ftp, bucket_path) if not e["is_dir"]]
    except ftplib.error_perm:
        return []
    names = {e["name"] for e in entries}
    shots = []
    for e in entries:
        rel = f"{rel_dir}/{e['name']}"
        if not valid_rel(rel):
            continue
        stem = e["name"].rsplit(".", 1)[0]
        shots.append({
            "rel": rel,
            "app": parts[0],
            "title_id": parts[1],
            "name": e["name"],
            "size": e["size"] or 0,
            "modified_utc": e["modified_utc"],
            "taken": taken_from_name(e["name"]),
            "trophy": f"{stem}.ext" in names,
        })
    return shots


def _newest_first(shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(shots, key=lambda s: (s["modified_utc"] or "", s["name"]), reverse=True)


def newest_shots(ftp: ftplib.FTP, count: int, title_id: str | None = None) -> list[dict[str, Any]]:
    """The newest `count` shots (of one game with `title_id`), listing only the buckets that can hold them (a bucket
    without an mtime is listed)."""
    buckets = sorted(list_buckets(ftp, title_id), key=lambda b: b["modified_utc"] or "~", reverse=True)
    shots: list[dict[str, Any]] = []
    for bucket in buckets:
        if len(shots) >= count:
            cutoff = shots[count - 1]["modified_utc"]
            if cutoff and bucket["modified_utc"] and bucket["modified_utc"] < cutoff:
                break
        shots = _newest_first(shots + shots_in_bucket(ftp, bucket["path"]))
    return shots[:count]


def all_shots(ftp: ftplib.FTP, title_id: str | None = None) -> list[dict[str, Any]]:
    return _newest_first([s for b in list_buckets(ftp, title_id) for s in shots_in_bucket(ftp, b["path"])])


def shot_by_rel(ftp: ftplib.FTP, rel: str) -> dict[str, Any] | None:
    if not valid_rel(rel):
        return None
    bucket = f"{PHOTO_ROOT}/{rel.rsplit('/', 1)[0]}"
    return next((s for s in shots_in_bucket(ftp, bucket) if s["rel"] == rel), None)


def photo_remote(rel: str) -> str:
    return f"{PHOTO_ROOT}/{rel}"


def thumb_remote(rel: str) -> str:
    return f"{THUMB_ROOT}/{rel}.jpg"  # the console names it <shot>.jpg.jpg


def thumb_local(cache_dir: Path, rel: str) -> Path:
    return cache_dir / (rel.replace("/", "__") + ".thumb.jpg")


def download(ftp: ftplib.FTP, remote: str, dest: Path) -> int:
    """Stream to <dest>.part in 64 KB blocks, then rename: never the whole file in RAM, never a half file in place."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        with open(part, "wb") as fh:
            ftp.retrbinary(f"RETR {remote}", fh.write, blocksize=65536)
        os.replace(part, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink()
        raise
    return dest.stat().st_size


def fetch_thumbs(ftp: ftplib.FTP, cache_dir: Path, shots: Iterable[dict[str, Any]]) -> None:
    """Fetch the console's thumbnails that aren't cached yet; a missing one (550) stays missing."""
    for shot in shots:
        local = thumb_local(cache_dir, shot["rel"])
        if local.exists():
            continue
        with contextlib.suppress(ftplib.error_perm):
            download(ftp, thumb_remote(shot["rel"]), local)


def touch(paths: Iterable[Path]) -> None:
    now = time.time()
    for path in paths:
        with contextlib.suppress(OSError):
            os.utime(path, (now, now))


def prune_thumbs(cache_dir: Path, cap: int, on_console: set[str] | None = None) -> int:
    """Drop thumbnails of shots gone from the console (when the full list is known), then the least recently used
    ones over `cap`. Returns how many were deleted."""
    if not cache_dir.is_dir():
        return 0
    files = [p for p in cache_dir.iterdir() if p.is_file()]
    gone = []
    if on_console is not None:
        keep_names = {thumb_local(cache_dir, rel).name for rel in on_console}
        gone = [p for p in files if p.name not in keep_names]
    rest = sorted((p for p in files if p not in gone), key=lambda p: p.stat().st_mtime, reverse=True)
    doomed = gone + rest[cap:]
    for path in doomed:
        with contextlib.suppress(OSError):
            path.unlink()
    return len(doomed)


def scan_copies(console_dir: Path) -> dict[str, int]:
    """File name -> size of every finished copy in this console's folder. The folder is the truth: a copy deleted in
    HA's Media browser counts as not in HA again."""
    copies: dict[str, int] = {}
    if console_dir.is_dir():
        for path in console_dir.rglob("*"):
            if path.is_file() and not path.name.endswith(".part"):
                copies[path.name] = path.stat().st_size
    return copies


def find_copy(console_dir: Path, name: str) -> Path | None:
    if not _IMAGE_RE.match(name) or not console_dir.is_dir():
        return None
    return next((p for p in console_dir.rglob(name) if p.is_file()), None)


def delete_shot(ftp: ftplib.FTP, rel: str) -> int:
    """Delete one shot on the console (only after the user ticked "PS4 original"): the image, its
    .dat / .ext sidecars and its thumbnail; folders stay. A missing piece (550) is skipped. Returns files deleted."""
    if not valid_rel(rel):
        raise ValueError(f"not a screenshot path: {rel!r}")
    bucket, name = rel.rsplit("/", 1)
    stem = name.rsplit(".", 1)[0]
    deleted = 0
    for path in (f"{PHOTO_ROOT}/{rel}", f"{PHOTO_ROOT}/{bucket}/{stem}.dat", f"{PHOTO_ROOT}/{bucket}/{stem}.ext",
                 thumb_remote(rel)):
        try:
            ftp.delete(path)
            deleted += 1
        except ftplib.error_perm as err:
            if not str(err).startswith("550"):
                raise
    return deleted


def delete_copy(console_dir: Path, name: str) -> bool:
    """Delete HA's copy of a shot; its game folder goes too once empty. Never touches the console."""
    path = find_copy(console_dir, name)
    if path is None:
        return False
    path.unlink()
    with contextlib.suppress(OSError):
        if path.parent != console_dir:
            path.parent.rmdir()  # only succeeds when empty
    return True


def newest_that_fit(shots: list[dict[str, Any]], room: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Newest first until the next one doesn't fit; that one and everything older are skipped."""
    fit: list[dict[str, Any]] = []
    used = 0
    ordered = _newest_first(shots)
    for i, shot in enumerate(ordered):
        if used + shot["size"] > room:
            return fit, ordered[i:]
        fit.append(shot)
        used += shot["size"]
    return fit, []
