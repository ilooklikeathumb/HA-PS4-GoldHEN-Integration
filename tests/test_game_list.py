"""The game list from app.db — games only, last played first, one cache file per console.

Runs the real db.py against a small app.db built here with the columns of a real one (an 11.00 Pro).
"""
import importlib
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_game_list_test_pkg"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
db = importlib.import_module(f"{PKG}.db")

COLS = ("titleId", "contentId", "titleName", "visible", "thumbnailUrl", "uiCategory", "category", "folderType",
        "lastAccessTime")

# (titleId, titleName, visible, uiCategory, category, folderType, lastAccessTime) per user table
USER_A = [
    ("CUSA90003", "Game C", 1, "game", "gd", 0, "2026-10-15 01:23:22.967"),
    ("CUSA90001", "Game B", 1, "game", "gd", 0, "2025-10-27 13:37:48.801"),
    ("CUSA90002", "Spotify", 1, "app", "gdn", 0, "2026-10-14 20:32:26.421"),        # app, not a game
    ("000000004", "Racing", 1, None, None, 1, "2026-10-12 03:33:35.381"),           # folder
    ("NPXS20109", "", 1, "app", "gdi", 0, "2026-10-15 14:54:32.024"),         # system entry
    ("CUSA99999", "Hidden Game", 0, "game", "gdi", 0, "2026-10-15 09:00:00.000"),   # not visible
    ("CUSA00001", "Same Name", 1, "game", "gd", 0, "2024-01-01 00:00:00.000"),
    ("CUSA00002", "Same Name", 1, "game", "gd", 0, "2024-01-02 00:00:00.000"),
]
USER_B = [
    ("CUSA90001", "Game B", 1, "game", "gd", 0, "2026-10-15 15:50:51.000"),          # user B played it last
    ("CUSA90006", "Game E", 1, "game", "gd", 0, "2026-10-15 15:57:18.000"),
]


def build_app_db() -> bytes:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.db"
        conn = sqlite3.connect(path)
        for user, rows in (("0503302569", USER_A), ("0503302571", USER_B)):
            conn.execute(f'CREATE TABLE "tbl_appbrowse_{user}" ({", ".join(COLS)})')
            for tid, name, visible, ui, cat, folder, last in rows:
                conn.execute(
                    f'INSERT INTO "tbl_appbrowse_{user}" VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    (tid, f"UP0000-{tid}_00", name, visible, None, ui, cat, folder, last),
                )
        conn.execute("CREATE TABLE tbl_appinfo (titleId, key, val)")
        conn.commit()
        conn.close()
        return path.read_bytes()


class GameListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game_map = db._extract_game_map(build_app_db())
        cls.games = db.game_list(cls.game_map)

    def test_games_only_last_played_first(self):
        self.assertEqual(
            [tid for tid, _ in self.games],
            ["CUSA90006", "CUSA90001", "CUSA90003", "CUSA00002", "CUSA00001"],
        )

    def test_newest_play_across_users(self):
        self.assertEqual(self.game_map["CUSA90001"]["last_access"], "2026-10-15 15:50:51.000")

    def test_apps_folders_hidden_left_out(self):
        tids = {tid for tid, _ in self.games}
        for tid in ("CUSA90002", "000000004", "NPXS20109", "CUSA99999"):
            self.assertNotIn(tid, tids)
        self.assertEqual(self.game_map["CUSA90002"]["ui_category"], "app")  # still known for names + covers

    def test_duplicate_names_get_title_id(self):
        names = dict(self.games)
        self.assertEqual(names["CUSA00001"], "Same Name (CUSA00001)")
        self.assertEqual(names["CUSA00002"], "Same Name (CUSA00002)")
        self.assertEqual(names["CUSA90001"], "Game B")

    def test_existing_fields_kept(self):
        self.assertEqual(self.game_map["CUSA90003"]["cover"], "/user/appmeta/CUSA90003/icon0.png")
        self.assertIsNone(self.game_map["CUSA90003"]["cdn_cover"])

    def test_empty_map(self):
        self.assertEqual(db.game_list({}), [])


class CachePathTests(unittest.TestCase):
    def test_one_cache_per_console(self):
        self.assertEqual(str(db._cache_path("192.0.2.189")), "/config/ps4/db/192.0.2.189/app.db")
        self.assertNotEqual(db._cache_path("192.0.2.189"), db._cache_path("192.0.2.121"))

    def test_cache_round_trip_per_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = db.APP_DB_LOCAL
            db.APP_DB_LOCAL = f"{tmp}/{{host}}/app.db"
            try:
                db._ensure_db_dir("192.0.2.1")
                db._cache_path("192.0.2.1").write_bytes(build_app_db())
                self.assertIn("CUSA90006", db.load_cached("192.0.2.1"))
                self.assertIsNone(db.load_cached("192.0.2.2"))  # never the other console's games
            finally:
                db.APP_DB_LOCAL = original


if __name__ == "__main__":
    unittest.main()
