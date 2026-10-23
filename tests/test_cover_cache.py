"""Cover cache: one folder per console, and the app.db pull's check that fetches a changed icon again.

Runs the real cover helpers from __init__.py (extracted, like test_db_refresh) against a fake console FTP.
"""
import ast
import ftplib
import importlib.util
import io
import logging
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components/ps4_goldhen/__init__.py"
LISTING = ROOT / "custom_components/ps4_goldhen/ftp_listing.py"

spec = importlib.util.spec_from_file_location("cover_test_listing", LISTING)
listing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = listing
spec.loader.exec_module(listing)

_HELPERS = {
    "_ICON_PATH", "_ICON_GONE", "_cover_cache_path", "_read_cached_cover", "_write_cached_cover", "_fetch_cover_ftp",
    "_read_or_fetch_cover", "_console_icon_size", "_refresh_cached_covers", "_drop_shared_cover_cache",
}


def load_helpers(cover_dir: Path) -> dict[str, Any]:
    ns: dict[str, Any] = {"_LOGGER": logging.getLogger("cover-cache-test"), "Path": Path, "io": io, "os": os,
                          "Any": Any, "COVER_CACHE_DIR": str(cover_dir), "list_directory": listing.list_directory}
    keep = [node for node in ast.parse(INIT.read_text()).body
            if (isinstance(node, ast.FunctionDef) and node.name in _HELPERS)
            or (isinstance(node, ast.Assign) and any(getattr(t, "id", "") in _HELPERS for t in node.targets))]
    exec(compile(ast.Module(body=keep, type_ignores=[]), str(INIT), "exec"), ns)
    return ns


class FakeConsoleFTP:
    """A console's FTP: `files` = path -> bytes. size_reply: "ok", "unsupported" (502) or "refused" (550 for
    every file, as a server whose SIZE only works in some modes might answer)."""

    files: dict[str, bytes] = {}
    size_reply = "ok"
    sessions: list[list[str]] = []

    def __init__(self):
        self.commands: list[str] = []
        self.cwd_path = "/"
        FakeConsoleFTP.sessions.append(self.commands)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, host, port, timeout=None):
        self.commands.append(f"CONNECT {host}:{port}")

    def login(self):
        self.commands.append("LOGIN")

    def voidcmd(self, cmd):
        self.commands.append(cmd)

    def size(self, path):
        self.commands.append(f"SIZE {path}")
        if self.size_reply == "unsupported":
            raise ftplib.error_perm("502 Command not implemented")
        if self.size_reply == "refused" or path not in self.files:
            raise ftplib.error_perm("550 No such file")
        return len(self.files[path])

    def cwd(self, path):
        self.commands.append(f"CWD {path}")
        if not any(f.startswith(path.rstrip("/") + "/") for f in self.files):
            raise ftplib.error_perm("550 No such directory")
        self.cwd_path = path

    def mlsd(self):
        self.commands.append("MLSD")
        folder = self.cwd_path.rstrip("/") + "/"
        for path, content in self.files.items():
            if path.startswith(folder) and "/" not in path[len(folder):]:
                yield path[len(folder):], {"type": "file", "size": str(len(content))}

    def retrbinary(self, cmd, callback):
        self.commands.append(cmd)
        path = cmd.removeprefix("RETR ")
        if path not in self.files:
            raise ftplib.error_perm("550 No such file")
        callback(self.files[path])


ICON = "/user/appmeta/{}/icon0.png".format


class CoverCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.covers = Path(self.tmp.name) / "covers"
        self.ns = load_helpers(self.covers)
        FakeConsoleFTP.files = {}
        FakeConsoleFTP.size_reply = "ok"
        FakeConsoleFTP.sessions = []
        patcher = patch("ftplib.FTP", FakeConsoleFTP)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def cache(self, console, tid, content):
        path = self.covers / console / f"{tid}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def refresh(self, console="E1"):
        return self.ns["_refresh_cached_covers"]("192.0.2.1", 2121, console)

    def test_each_console_caches_its_own_icon(self):  # same title, a restyled icon on one console only
        fetch = self.ns["_read_or_fetch_cover"]
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"masked icon"}
        self.assertEqual(fetch("192.0.2.1", 2121, "E1", "CUSA00001", ICON("CUSA00001")), b"masked icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"plain icon"}
        self.assertEqual(fetch("192.0.2.2", 2121, "E2", "CUSA00001", ICON("CUSA00001")), b"plain icon")
        self.assertEqual((self.covers / "E1/CUSA00001.png").read_bytes(), b"masked icon")
        self.assertEqual((self.covers / "E2/CUSA00001.png").read_bytes(), b"plain icon")
        self.assertEqual(self.ns["_read_cached_cover"]("E1", "CUSA00001"), b"masked icon")
        self.assertIsNone(self.ns["_read_cached_cover"]("E3", "CUSA00001"))
        sessions = len(FakeConsoleFTP.sessions)
        self.assertEqual(fetch("192.0.2.1", 2121, "E1", "CUSA00001", ICON("CUSA00001")), b"masked icon")
        self.assertEqual(len(FakeConsoleFTP.sessions), sessions, "a cached cover opens no FTP")

    def test_changed_icon_is_fetched_again(self):
        stale = self.cache("E1", "CUSA00001", b"old")
        kept = self.cache("E1", "CUSA00002", b"same icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"restyled icon", ICON("CUSA00002"): b"same icon"}
        self.assertEqual(self.refresh(), 1)
        self.assertEqual(stale.read_bytes(), b"restyled icon")
        self.assertEqual(kept.read_bytes(), b"same icon")
        self.assertEqual(sorted(p.name for p in stale.parent.iterdir()), ["CUSA00001.png", "CUSA00002.png"],
                         "no temp file left behind")
        [commands] = FakeConsoleFTP.sessions
        self.assertIn("TYPE I", commands)
        self.assertEqual([c for c in commands if c.startswith("RETR")], [f"RETR {ICON('CUSA00001')}"])
        self.assertNotIn("MLSD", commands, "SIZE answered: no listings")

    def test_uninstalled_title_is_dropped(self):
        gone = self.cache("E1", "CUSA00009", b"icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"another title"}
        self.assertEqual(self.refresh(), 1)
        self.assertFalse(gone.exists())

    def test_no_size_command_uses_listings(self):
        FakeConsoleFTP.size_reply = "unsupported"
        stale = self.cache("E1", "CUSA00001", b"old")
        kept = self.cache("E1", "CUSA00002", b"same icon")
        gone = self.cache("E1", "CUSA00009", b"icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"restyled icon", ICON("CUSA00002"): b"same icon"}
        self.assertEqual(self.refresh(), 2)
        self.assertEqual(stale.read_bytes(), b"restyled icon")
        self.assertEqual(kept.read_bytes(), b"same icon")
        self.assertFalse(gone.exists())
        [commands] = FakeConsoleFTP.sessions
        self.assertEqual(len([c for c in commands if c.startswith("SIZE")]), 1, "SIZE tried once, then listings")

    def test_size_refused_is_confirmed_by_the_listing(self):  # a 550 from SIZE alone never deletes a cover
        FakeConsoleFTP.size_reply = "refused"
        kept = self.cache("E1", "CUSA00001", b"same icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"same icon"}
        self.assertEqual(self.refresh(), 0)
        self.assertTrue(kept.exists())

    def test_unknown_size_keeps_the_cover(self):
        kept = self.cache("E1", "CUSA00001", b"icon")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"restyled icon"}
        with patch.object(FakeConsoleFTP, "size", lambda self, path: None):
            self.ns["list_directory"] = lambda ftp, folder: [{"name": "icon0.png", "is_dir": False, "size": None}]
            self.assertEqual(self.refresh(), 0)
        self.assertEqual(kept.read_bytes(), b"icon")

    def test_other_consoles_untouched(self):
        other = self.cache("E2", "CUSA00001", b"other console's icon")
        self.cache("E1", "CUSA00001", b"old")
        FakeConsoleFTP.files = {ICON("CUSA00001"): b"this console's icon"}
        self.refresh("E1")
        self.assertEqual(other.read_bytes(), b"other console's icon")

    def test_nothing_cached_opens_no_ftp(self):
        self.assertEqual(self.refresh(), 0)
        self.assertEqual(FakeConsoleFTP.sessions, [])

    def test_shared_cache_from_before_is_dropped(self):
        self.covers.mkdir(parents=True)
        (self.covers / "CUSA00001.png").write_bytes(b"one console's icon, shown on all")
        mine = self.cache("E1", "CUSA00001", b"icon")
        self.ns["_drop_shared_cover_cache"]()
        self.assertEqual(sorted(p.name for p in self.covers.iterdir()), ["E1"])
        self.assertTrue(mine.exists())

    def test_drop_with_no_cover_folder(self):
        self.ns["_drop_shared_cover_cache"]()  # a fresh install: nothing there, no error
        self.assertFalse(self.covers.exists())


if __name__ == "__main__":
    unittest.main()
