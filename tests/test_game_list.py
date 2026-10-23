"""The title list from app.db — games and installed apps, last played first, one cache file per console — and one
user's home-screen folders.

Runs the real db.py against a small app.db built here with the columns and categories of a real one (an 11.00 Pro).
"""
import importlib
import json
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
        "lastAccessTime", "canRemove")

# (titleId, titleName, visible, uiCategory, category, folderType, lastAccessTime, canRemove) per user table
USER_A = [
    ("CUSA90003", "Game C", 1, "game", "gd", 0, "2026-10-15 01:23:22.967", 1),
    ("CUSA90001", "Game B", 1, "game", "gd", 0, "2025-10-27 13:37:48.801", 1),
    ("CUSA90002", "Spotify", 1, "app", "gdn", 0, "2026-10-14 20:32:26.421", 1),          # installed app
    ("APOL00004", "Apollo Save Tool", 1, "app", "gde", 0, "2026-10-13 04:55:15.537", 1),  # homebrew app
    ("NPXS39041", "Store", 1, "app", "gde", 0, "2026-10-14 06:20:47.226", 1),            # homebrew, system-style ID
    ("NPXS20102", "Internet Browser", 1, "app", "gdi", 0, "2025-10-11 13:48:56.733", 0),  # built in: not listed
    ("NPXS20111", "Library", 1, "app", "gdi", 0, "1970-01-01 00:00:27.443", 0),          # built-in app
    ("CUSA90008", "THE PLAYROOM", 1, "game", "gd", 0, "1970-01-01 00:00:33.676", 0),     # built-in, reads as a game
    ("CUSA90007", "Spotify", 0, "app", "gdi", 0, "1970-01-01 00:00:20.507", 1),          # preloaded stub, not installed
    ("000000004", "Racing", 1, None, None, 1, "2026-10-12 03:33:35.381", 1),             # folder
    ("NPXS20109", "\uf896", 1, "app", "gdi", 0, "2026-10-15 14:54:32.024", 0),           # system entry
    ("CUSA99999", "Hidden Game", 0, "game", "gdi", 0, "2026-10-15 09:00:00.000", 0),     # not visible
    ("CUSA00001", "Same Name", 1, "game", "gd", 0, "2024-01-01 00:00:00.000", 1),
    ("CUSA00002", "Same Name", 1, "game", "gd", 0, "2024-01-02 00:00:00.000", 1),
]
USER_B = [
    ("CUSA90001", "Game B", 1, "game", "gd", 0, "2026-10-15 15:50:51.000", 1),            # user B played it last
    ("CUSA90006", "Game E", 1, "game", "gd", 0, "2026-10-15 15:57:18.000", 1),
]


def build_app_db(without: tuple[str, ...] = ()) -> bytes:
    """The app.db above; `without` = columns an older firmware's table may not have."""
    keep = [i for i, col in enumerate(COLS) if col not in without]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.db"
        conn = sqlite3.connect(path)
        for user, rows in (("0503302569", USER_A), ("0503302571", USER_B)):
            conn.execute(f'CREATE TABLE "tbl_appbrowse_{user}" ({", ".join(COLS[i] for i in keep)})')
            for tid, name, visible, ui, cat, folder, last, can_remove in rows:
                full = (tid, f"UP0000-{tid}_00", name, visible, f"https://cdn.example/{tid}.png", ui, cat, folder, last,
                        can_remove)
                values = [full[i] for i in keep]
                conn.execute(
                    f'INSERT INTO "tbl_appbrowse_{user}" VALUES ({", ".join("?" * len(values))})', values,
                )
        conn.execute("CREATE TABLE tbl_appinfo (titleId, key, val)")
        conn.commit()
        conn.close()
        return path.read_bytes()


class TitleListTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.game_map = db._extract_game_map(build_app_db())
        cls.titles = db.title_list(cls.game_map)

    def test_games_and_apps_last_played_first(self):
        self.assertEqual(
            [tid for tid, _ in self.titles],
            ["CUSA90006", "CUSA90001", "CUSA90003", "CUSA90002", "NPXS39041", "APOL00004", "CUSA00002",
             "CUSA00001"],
        )

    def test_newest_play_across_users(self):
        self.assertEqual(self.game_map["CUSA90001"]["last_access"], "2026-10-15 15:50:51.000")

    def test_built_in_titles_folders_hidden_left_out(self):
        tids = {tid for tid, _ in self.titles}
        for tid in ("000000004", "NPXS20109", "NPXS20111", "CUSA90008", "CUSA90007", "CUSA99999"):
            self.assertNotIn(tid, tids)
        self.assertEqual(self.game_map["NPXS20111"]["category"], "gdi")  # still known for names + covers
        self.assertIs(self.game_map["CUSA90008"]["can_remove"], False)
        self.assertIs(self.game_map["CUSA90003"]["can_remove"], True)

    def test_is_app(self):
        self.assertTrue(db.is_app(self.game_map, "APOL00004"))
        self.assertTrue(db.is_app(self.game_map, "CUSA90002"))
        self.assertFalse(db.is_app(self.game_map, "CUSA90001"))
        self.assertFalse(db.is_app(self.game_map, "CUSA00000"))  # not on this console

    def test_is_built_in(self):
        self.assertTrue(db.is_built_in(self.game_map, "NPXS20102"))   # the Internet Browser
        self.assertFalse(db.is_built_in(self.game_map, "APOL00004"))
        self.assertFalse(db.is_built_in(self.game_map, "NPXS39041"))  # homebrew with a system-style ID
        self.assertFalse(db.is_built_in(self.game_map, "CUSA00000"))  # not on this console

    def test_no_can_remove_column(self):  # the category still tells the built-in apps
        game_map = db._extract_game_map(build_app_db(without=("canRemove",)))
        self.assertIsNone(game_map["CUSA90002"]["can_remove"])
        tids = [tid for tid, _ in db.title_list(game_map)]
        self.assertIn("NPXS39041", tids)
        self.assertNotIn("NPXS20111", tids)

    def test_no_category_or_can_remove_column(self):  # the console's own apps are the NPXS ones
        game_map = db._extract_game_map(build_app_db(without=("category", "canRemove")))
        self.assertIsNone(game_map["CUSA90002"]["category"])
        tids = [tid for tid, _ in db.title_list(game_map)]
        self.assertIn("CUSA90002", tids)
        self.assertIn("APOL00004", tids)
        self.assertNotIn("NPXS20111", tids)
        self.assertNotIn("NPXS39041", tids)  # can't be told from a built-in one without either column

    def test_internet_browser_not_listed(self):  # built in: HA doesn't offer to open it, with or without the columns
        self.assertTrue(db.is_app(self.game_map, "NPXS20102"))
        self.assertNotIn("NPXS20102", dict(self.titles))
        game_map = db._extract_game_map(build_app_db(without=("category", "canRemove")))
        self.assertNotIn("NPXS20102", dict(db.title_list(game_map)))

    def test_duplicate_names_get_title_id(self):
        names = dict(self.titles)
        self.assertEqual(names["CUSA00001"], "Same Name (CUSA00001)")
        self.assertEqual(names["CUSA00002"], "Same Name (CUSA00002)")
        self.assertEqual(names["CUSA90001"], "Game B")
        self.assertEqual(names["CUSA90002"], "Spotify")  # the hidden stub with the same name isn't listed

    def test_existing_fields_kept(self):
        self.assertEqual(self.game_map["CUSA90003"]["cover"], "/user/appmeta/CUSA90003/icon0.png")

    def test_cover_is_the_console_icon_never_a_store_url(self):  # app.db's thumbnailUrl is ignored
        for tid, info in self.game_map.items():
            with self.subTest(tid=tid):
                self.assertNotIn("cdn_cover", info)
                self.assertFalse(any("cdn.example" in str(value) for value in info.values()))

    def test_empty_map(self):
        self.assertEqual(db.title_list({}), [])


FOLDER_COLS = ("titleId", "titleName", "visible", "uiCategory", "category", "canRemove", "folderType", "parentFolderId",
               "positionInFolder", "folderInfo", "lastAccessIndex")
USER_A_ID, USER_B_ID = 0x1DFFC9A9, 0x1DFFC9AB  # klog's OnLogin hex; the tables carry them in decimal


def folder(fid, name, index, cursor=None):
    info = json.dumps({"titleId": cursor, "index": 0}) if cursor else None
    return (fid, name, 1, None, None, 1, 1, None, 0, info, index)


def title(tid, name, index, parent=None, position=0, ui="game", category="gd", visible=1, can_remove=1):
    return (tid, name, visible, ui, category, can_remove, 0, parent, position, None, index)


# lastAccessIndex = the home screen's order (bigger = newer)
FOLDER_USER_A = [
    folder("000000004", "Ps", 70),                                   # only a built-in app inside → dropped
    folder("000000002", "Racing", 60, cursor="CUSA90009"),           # names a hidden title → first title
    folder("000000001", "Apps", 50, cursor="APOL00004"),
    folder("000000003", "", 10),                                     # no name typed
    title("NPXS20109", "\uf896", 90, ui="app", category="gdi", can_remove=0),  # system entry
    title("CUSA90006", "Game E", 80),
    title("CUSA90003", "Game C", 45, "000000002", 3),
    title("APOL00004", "Apollo Save Tool", 40, "000000001", 2, ui="app", category="gde"),
    title("ITEM00001", "Itemzflow", 35, ui="app", category="gde"),
    title("CUSA90002", "Spotify", 30, "000000001", 1, ui="app", category="gdn"),
    title("CUSA90001", "Game B", 20, "000000002", 1),
    title("CUSA90005", "Game F", 8, "000000003", 1),
    title("CUSA90004", "Game A", 5, "000000002", 5),                 # positions can have gaps
    title("CUSA90009", "Hidden Game", 4, "000000002", 2, visible=0),
    title("NPXS20111", "Library", 1, "000000004", 1, ui="app", category="gdi", can_remove=0),
]
FOLDER_USER_B = [  # no folders; played last
    title("CUSA90001", "Game B", 3),
    title("CUSA90006", "Game E", 2),
]


def build_folder_db(without: tuple[str, ...] = ()) -> bytes:
    keep = [i for i, col in enumerate(FOLDER_COLS) if col not in without]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "app.db"
        conn = sqlite3.connect(path)
        for user, rows, day in ((USER_A_ID, FOLDER_USER_A, "2026-01-01"), (USER_B_ID, FOLDER_USER_B, "2026-10-20")):
            table = f"tbl_appbrowse_{user:010d}"
            conn.execute(f'CREATE TABLE "{table}" ({", ".join(FOLDER_COLS[i] for i in keep)}, lastAccessTime)')
            for row in rows:
                values = [row[i] for i in keep] + [f"{day} 00:{row[-1]:02d}:00.000"]  # same order as the index
                conn.execute(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(values))})', values)
        conn.commit()
        conn.close()
        return path.read_bytes()


def folder_layout(user_id, without: tuple[str, ...] = ()):
    """db.folder_layout on a cached copy of the app.db above."""
    with tempfile.TemporaryDirectory() as tmp:
        original = db.APP_DB_LOCAL
        db.APP_DB_LOCAL = f"{tmp}/{{host}}/app.db"
        try:
            db._ensure_db_dir("192.0.2.1")
            db._cache_path("192.0.2.1").write_bytes(build_folder_db(without))
            return db.folder_layout("192.0.2.1", user_id)
        finally:
            db.APP_DB_LOCAL = original


class FolderLayoutTests(unittest.TestCase):
    def test_folders_in_console_order(self):
        layout = folder_layout(USER_A_ID)
        self.assertEqual(
            [(f["id"], f["name"], f["cover"], f["titles"]) for f in layout["folders"]],
            [
                ("000000002", "Racing", "CUSA90001", ["CUSA90001", "CUSA90003", "CUSA90004"]),
                ("000000001", "Apps", "APOL00004", ["CUSA90002", "APOL00004"]),
                ("000000003", "Folder", "CUSA90005", ["CUSA90005"]),
            ],
        )

    def test_titles_outside_a_folder(self):  # listed titles only, home-screen order
        self.assertEqual(folder_layout(USER_A_ID)["loose"], ["CUSA90006", "ITEM00001"])

    def test_unknown_user_is_whoever_played_last(self):
        for user_id in (None, 0x12345678):
            with self.subTest(user_id=user_id):
                self.assertEqual(folder_layout(user_id), {"folders": [], "loose": ["CUSA90001", "CUSA90006"]})
        self.assertEqual(folder_layout(USER_B_ID)["folders"], [])

    def test_no_index_column_orders_by_last_access(self):
        layout = folder_layout(USER_A_ID, without=("lastAccessIndex",))
        self.assertEqual([f["id"] for f in layout["folders"]], ["000000002", "000000001", "000000003"])
        self.assertEqual(layout["loose"], ["CUSA90006", "ITEM00001"])

    def test_no_folder_columns(self):  # a firmware without folders
        layout = folder_layout(USER_A_ID, without=("folderType", "parentFolderId", "positionInFolder", "folderInfo"))
        self.assertEqual(layout, {"folders": [], "loose": []})

    def test_no_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = db.APP_DB_LOCAL
            db.APP_DB_LOCAL = f"{tmp}/{{host}}/app.db"
            try:
                self.assertIsNone(db.folder_layout("192.0.2.1", USER_A_ID))
            finally:
                db.APP_DB_LOCAL = original


INFO_COLS = ("titleId", "contentId", "titleName", "contentSize", "installDate", "folderType", "parentFolderId",
             "lastAccessTime")
# (titleId, contentId, titleName, contentSize, installDate, folderType, parentFolderId), values as a live 11.00 Pro has them
INFO_USER_A = [
    ("000000004", None, "Racing", None, None, 1, None),
    ("000000005", None, "", None, None, 1, None),
    ("CUSA90003", "EP0177-CUSA90003_00-GAMECRACING00001", "Game C", 16318615552, "2025-10-05 17:14:05.813", 0, "000000004"),
    ("CUSA90008", "IP9100-CUSA90008_00-PLAYROOM00000000", "THE PLAYROOM", 576622592, "1970-01-01 00:00:33.676", 0, "000000005"),
    ("HBRW90001", "ED1234-HBRW90001_00-0000000000000000", "Homebrew", 53030912, "2025-10-05 16:34:46.402", 0, None),
]
INFO_USER_B = [  # played last; no folders
    ("CUSA90003", "EP0177-CUSA90003_00-GAMECRACING00001", "Game C", 16318615552, "2025-10-05 17:14:05.813", 0, None),
]
APPINFO = [("CUSA90003", "APP_VER", "01.02"), ("CUSA90003", "VERSION", "01.00"), ("CUSA90008", "VERSION", "01.00"),
           ("CUSA90003", "SYSTEM_VER", 67141632)]


def title_info(tid, user_id, without: tuple[str, ...] = (), appinfo=True):
    """db.title_info on a cached copy of a small app.db (`without` = columns an older firmware may not have)."""
    keep = [i for i, col in enumerate(INFO_COLS) if col not in without]
    with tempfile.TemporaryDirectory() as tmp:
        original = db.APP_DB_LOCAL
        db.APP_DB_LOCAL = f"{tmp}/{{host}}/app.db"
        try:
            db._ensure_db_dir("192.0.2.1")
            conn = sqlite3.connect(db._cache_path("192.0.2.1"))
            for user, rows, day in ((USER_A_ID, INFO_USER_A, "2026-01-01"), (USER_B_ID, INFO_USER_B, "2026-10-20")):
                table = f"tbl_appbrowse_{user:010d}"
                conn.execute(f'CREATE TABLE "{table}" ({", ".join(INFO_COLS[i] for i in keep)})')
                for row in rows:
                    values = [(*row, f"{day} 00:00:00.000")[i] for i in keep]
                    conn.execute(f'INSERT INTO "{table}" VALUES ({", ".join("?" * len(values))})', values)
            if appinfo:
                conn.execute("CREATE TABLE tbl_appinfo (titleId, key, val)")
                conn.executemany("INSERT INTO tbl_appinfo VALUES (?, ?, ?)", APPINFO)
            conn.commit()
            conn.close()
            return db.title_info("192.0.2.1", tid, user_id)
        finally:
            db.APP_DB_LOCAL = original


class TitleInfoTests(unittest.TestCase):
    def test_a_game_in_the_users_folder(self):
        self.assertEqual(title_info("cusa90003", USER_A_ID), {
            "content_id": "EP0177-CUSA90003_00-GAMECRACING00001", "region": "Europe", "size": 16318615552,
            "installed": "2025-10-05 17:14:05", "folder": "Racing", "version": "1.02",
        })

    def test_another_user_or_unknown_reads_whoever_played_last(self):
        self.assertIsNone(title_info("CUSA90003", USER_B_ID)["folder"])
        self.assertIsNone(title_info("CUSA90003", None)["folder"])  # user B played last: top level
        self.assertEqual(title_info("HBRW90001", USER_B_ID)["size"], 53030912)  # only in user A's table: still found

    def test_preinstalled_and_homebrew(self):
        info = title_info("CUSA90008", USER_A_ID)
        self.assertIsNone(info["installed"])        # 1970 = not recorded
        self.assertIsNone(info["region"])           # IP… = Sony's own build, no store region
        self.assertEqual(info["folder"], "Folder")  # a folder without a typed name
        self.assertEqual(info["version"], "1.00")   # no APP_VER: VERSION
        info = title_info("HBRW90001", USER_A_ID)
        self.assertIsNone(info["region"])
        self.assertIsNone(info["version"])

    def test_older_firmware_columns_and_no_appinfo(self):
        info = title_info("CUSA90003", USER_A_ID, without=("contentSize", "installDate", "parentFolderId"), appinfo=False)
        self.assertEqual(info, {"content_id": "EP0177-CUSA90003_00-GAMECRACING00001", "region": "Europe", "size": None,
                                "installed": None, "folder": None, "version": None})

    def test_not_on_this_console_or_no_cache(self):
        self.assertIsNone(title_info("CUSA00000", USER_A_ID))
        with tempfile.TemporaryDirectory() as tmp:
            original = db.APP_DB_LOCAL
            db.APP_DB_LOCAL = f"{tmp}/{{host}}/app.db"
            try:
                self.assertIsNone(db.title_info("192.0.2.1", "CUSA90003", None))
            finally:
                db.APP_DB_LOCAL = original


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
