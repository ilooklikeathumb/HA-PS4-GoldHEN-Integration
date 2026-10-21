"""PS4 save data on the console (FTP walk, slots) and HA's copies (Apollo's USB layout, history thinning).

The fake console mirrors a 12.00 Pro's listings: /user/home/10000001/{username.dat,savedata/<title>/
sdimg_<slot> + <slot>.bin}, an empty sce_backup2 folder, MLSD with UTC modify facts.
Save names from the console's savedata.db (columns as on a 12.00 Pro), names
kept beside HA's copies, per-save members, Delete from HA, the Fake USB listing.
"""
from datetime import datetime, timedelta, timezone
import ftplib
import importlib
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_save_files_test_pkg"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_capture_files import FakeConsole, FakeFTP  # noqa: E402

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
sf = importlib.import_module(f"{PKG}.save_files")

UID = "10000001"
GAME_C, OTHER = "CUSA90003", "CUSA90006"
H = sf.HOME_ROOT


def write_save(c: FakeConsole, uid: str, title: str, slot: str, data: bytes, modify: str, key_modify: str | None = None):
    folder = f"{H}/{uid}/savedata/{title}"
    if folder not in c.dirs:
        c.add_dir(folder, "20260422005259")
    c.add_file(f"{folder}/sdimg_{slot}", data, modify)
    c.add_file(f"{folder}/{slot}.bin", b"k" * 96, key_modify or modify)


# a 12.00 Pro's savedata.db columns
SAVE_DB_COLUMNS = ("id", "title_id", "dir_name", "main_title", "sub_title", "detail", "tmp_dir_name", "is_broken",
                   "user_param", "blocks", "free_blocks", "size_kib", "mtime", "fake_broken", "account_id", "user_id",
                   "faked_owner", "cloud_icon_url", "cloud_revision", "game_title_id", "account_id_in_usb_path")


def save_db(rows, columns=SAVE_DB_COLUMNS) -> bytes:
    """A savedata.db: rows of (title_id, dir_name, main_title, sub_title), in whichever of those columns exist."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "savedata.db"
        conn = sqlite3.connect(path)
        conn.execute(f"CREATE TABLE savedata ({', '.join(columns)})")
        keys = [c for c in ("title_id", "dir_name", "main_title", "sub_title") if c in columns]
        for row in rows:
            conn.execute(f"INSERT INTO savedata ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})",
                         [dict(zip(("title_id", "dir_name", "main_title", "sub_title"), row))[k] for k in keys])
        conn.commit()
        conn.close()
        return path.read_bytes()


GAME_C_NAMES = [
    (GAME_C, "SAVEDATAPlayer", "Game C", "Player"),
    (GAME_C, "sce_sdmemory", "Game C (Backup)", "(Autosave) Chapter 1 (100.0%)"),
    (OTHER, "SAVEDATA01", "Saved Data", ""),
    ("../etc", "x", "nope", "nope"),
]


def add_save_db(c: FakeConsole, uid: str, rows=GAME_C_NAMES, data: bytes | None = None):
    folder = sf.SAVE_DB_REMOTE.format(uid=uid).rsplit("/", 1)[0]
    parts = folder.strip("/").split("/")
    for i in range(1, len(parts) + 1):
        path = "/" + "/".join(parts[:i])
        if path not in c.dirs:
            c.add_dir(path, "20260421213050")
    c.add_file(f"{folder}/savedata.db", save_db(rows) if data is None else data, "20261016234147")


def save_console() -> FakeConsole:
    """A 12.00 Pro's user, images shrunk to a few hundred bytes."""
    c = FakeConsole()
    c.add_dir(H, "20260421213050")
    c.add_dir(f"{H}/{UID}", "20260421213050")
    c.add_dir(f"{H}/system", "20260421213050")  # not a user ID: skipped
    c.add_file(f"{H}/{UID}/username.dat", b"player1" + b"\0" * 11, "20261016051107")
    c.add_dir(f"{H}/{UID}/savedata", "20261016014304")
    c.add_dir(f"{H}/{UID}/savedata/sce_backup2", "20261016014304")
    write_save(c, UID, GAME_C, "SAVEDATAPlayer", b"P" * 300, "20261016014221", "20261016011910")
    write_save(c, UID, GAME_C, "sce_sdmemory", b"M" * 1000, "20261016014304", "20260422005259")
    write_save(c, UID, OTHER, "SAVEDATA01", b"O" * 640, "20261016005531", "20261015193539")
    c.add_file(f"{H}/{UID}/savedata/{GAME_C}/SAVEDATAOrphan.bin", b"k" * 96, "20261016011910")  # key without image
    c.add_file(f"{H}/{UID}/savedata/{GAME_C}/sdimg_NoKey", b"x" * 10, "20261016011910")  # image without key
    return c


class WritableFTP(FakeFTP):
    """FakeFTP plus the writes Send to PS4 uses (MKD, STOR, RMD). console.fail_stor: paths whose upload drops after
    the first block; console.short_stor: paths that land one byte short."""

    def __init__(self, console: FakeConsole):
        super().__init__(console)
        self.stored: list[str] = []
        self.made: list[str] = []

    def mkd(self, path):
        parent = path.rsplit("/", 1)[0]
        if path in self.console.dirs or (parent and parent not in self.console.dirs):
            raise ftplib.error_perm("550 Requested action not taken")
        self.console.add_dir(path, "20261016120000")
        self.made.append(path)
        return path

    def storbinary(self, cmd, fp, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        if path.rsplit("/", 1)[0] not in self.console.dirs:
            raise ftplib.error_perm("550 No such directory")
        data = b""
        while chunk := fp.read(blocksize):
            data += chunk
            if path in getattr(self.console, "fail_stor", ()):
                self.console.add_file(path, data, "20261016120000")
                raise ConnectionResetError("dropped")
        if path in getattr(self.console, "short_stor", ()):
            data = data[:-1]
        self.console.add_file(path, data, "20261016120000")
        self.stored.append(path)

    def rmd(self, path):
        if path not in self.console.dirs or self.console.dirs[path]:
            raise ftplib.error_perm("550 Directory not empty")
        del self.console.dirs[path]
        parent, name = path.rsplit("/", 1)
        self.console.dirs[parent].pop(name, None)


def epoch(modify: str) -> int:
    return int(datetime.strptime(modify, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp())


class PathRules(unittest.TestCase):
    def test_title_slot_user(self):
        for good in ("CUSA90003", "APOL00004", "LAPY20006"):
            self.assertTrue(sf.valid_title(good))
        for bad in ("sce_backup2", "cusa90003", "CUSA0304", "../CUSA90003", None):
            self.assertFalse(sf.valid_title(bad))
        for good in ("SAVEDATAPlayer", "sce_sdmemory", "sce_bu_sce_sdmemory", "SAVEDATA00"):
            self.assertTrue(sf.valid_slot(good))
        for bad in ("", "a/b", "x" * 33, "..", None):
            self.assertFalse(sf.valid_slot(bad))
        self.assertTrue(sf.valid_user(UID))
        self.assertFalse(sf.valid_user("system"))

    def test_stamps(self):
        self.assertEqual(sf.stamp(epoch("20261016014221"), timezone.utc), "2026-10-16 014221")
        self.assertEqual(sf.parse_stamp("2026-10-16 014221 (2)"), datetime(2026, 10, 16, 1, 42, 21))
        self.assertIsNone(sf.parse_stamp("2026-10-16"))


class ConsoleWalk(unittest.TestCase):
    def setUp(self):
        self.ftp = FakeFTP(save_console())

    def test_users_and_name(self):
        self.assertEqual(sf.list_users(self.ftp), [UID])
        self.assertEqual(sf.read_user_name(self.ftp, UID), "player1")
        self.assertIsNone(sf.read_user_name(self.ftp, "deadbeef"))

    def test_titles_skip_system_folders(self):
        self.assertEqual(sf.list_titles(self.ftp, UID), [GAME_C, OTHER])

    def test_slots_need_image_and_key(self):
        slots = sf.list_slots(self.ftp, UID, GAME_C)
        self.assertEqual(sorted(slots), ["SAVEDATAPlayer", "sce_sdmemory"])
        self.assertEqual(slots["SAVEDATAPlayer"], {
            "size": 300, "mtime": epoch("20261016014221"), "key_size": 96, "key_mtime": epoch("20261016011910"),
        })

    def test_missing_folders_are_empty(self):
        self.assertEqual(sf.list_titles(self.ftp, "deadbeef"), [])
        self.assertEqual(sf.list_slots(self.ftp, UID, "CUSA99999"), {})


class HaCopies(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.user = self.tmp / "PS4 PRO A" / "player1"
        self.staging = self.tmp / "PS4 PRO A" / sf.STAGING
        self.console = save_console()
        self.ftp = FakeFTP(self.console)

    def tearDown(self):
        self._tmp.cleanup()

    def back_up(self, title, slot, label=None):
        info = sf.list_slots(self.ftp, UID, title)[slot]
        staged = sf.fetch_slot(self.ftp, UID, title, slot, info, self.staging)
        return info, sf.install(self.user, title, slot, staged, label, timezone.utc)

    def test_first_copy_lands_in_latest_with_the_console_mtime(self):
        info, made = self.back_up(GAME_C, "SAVEDATAPlayer")
        self.assertIsNone(made)
        latest = self.user / "Latest/PS4/SAVEDATA/0000000000000000" / GAME_C
        self.assertEqual((latest / "SAVEDATAPlayer").read_bytes(), b"P" * 300)
        self.assertEqual((latest / "SAVEDATAPlayer.bin").read_bytes(), b"k" * 96)
        self.assertEqual(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"), info)
        self.assertFalse(sf.is_changed(info, sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer")))
        self.assertEqual(list(self.staging.iterdir()), [], "staging is empty after the install")

    def test_rewrite_moves_the_old_copy_into_history(self):
        self.back_up(GAME_C, "SAVEDATAPlayer", "Game C")
        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016020000", "20261016011910")
        info = sf.list_slots(self.ftp, UID, GAME_C)["SAVEDATAPlayer"]
        self.assertTrue(sf.is_changed(info, sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer")))
        _, made = self.back_up(GAME_C, "SAVEDATAPlayer", "Game C")
        self.assertEqual(made, self.user / "History/Game C (CUSA90003)/SAVEDATAPlayer/2026-10-16 014221")
        old = made / "PS4/SAVEDATA/0000000000000000" / GAME_C
        self.assertEqual((old / "SAVEDATAPlayer").read_bytes(), b"P" * 300)
        self.assertTrue((old / "SAVEDATAPlayer.bin").is_file())
        latest = self.user / "Latest/PS4/SAVEDATA/0000000000000000" / GAME_C
        self.assertEqual((latest / "SAVEDATAPlayer").read_bytes(), b"Q" * 300)

    def test_history_folder_is_reused_when_the_name_arrives_later_and_stamps_dont_collide(self):
        self.back_up(OTHER, "SAVEDATA01")
        self.back_up(OTHER, "SAVEDATA01")  # same console mtime twice: the 2nd version gets " (2)"
        self.back_up(OTHER, "SAVEDATA01", "Some Game")
        slot_dir = self.user / "History" / OTHER / "SAVEDATA01"
        self.assertEqual(sorted(p.name for p in slot_dir.iterdir()), ["2026-10-16 005531", "2026-10-16 005531 (2)"])
        self.assertEqual([p.name for p in (self.user / "History").iterdir()], [OTHER])
        found = sf.versions(self.user)
        self.assertEqual({(v["title"], v["slot"]) for v in found}, {(OTHER, "SAVEDATA01")})
        self.assertEqual(found[0]["bytes"], 640 + 96)

    def test_failed_download_leaves_latest_alone(self):
        self.back_up(GAME_C, "SAVEDATAPlayer")
        self.ftp.fail_retr.add(sf.image_remote(UID, GAME_C, "SAVEDATAPlayer"))
        info = sf.list_slots(self.ftp, UID, GAME_C)["SAVEDATAPlayer"]
        with self.assertRaises(ConnectionResetError):
            sf.fetch_slot(self.ftp, UID, GAME_C, "SAVEDATAPlayer", info, self.staging)
        self.assertEqual([p.name for p in self.staging.iterdir()], [])
        self.assertEqual(sf.local_slot(self.user, GAME_C, "SAVEDATAPlayer"), info)

    def test_latest_slots_usage_zip(self):
        self.back_up(GAME_C, "SAVEDATAPlayer", "Game C")
        self.back_up(GAME_C, "sce_sdmemory", "Game C")
        self.back_up(OTHER, "SAVEDATA01")
        write_save(self.console, UID, OTHER, "SAVEDATA01", b"N" * 640, "20261016030000", "20261015193539")
        _, made = self.back_up(OTHER, "SAVEDATA01")
        sf.set_kept(made, True)
        self.assertEqual(len(sf.latest_slots(self.user)), 3)
        self.assertEqual(sf.usage(self.tmp / "PS4 PRO A"), {"bytes": 396 + 1096 + 2 * 736, "versions": 1})

        game_c_only = sf.members(self.user, [(GAME_C, "SAVEDATAPlayer", None), (GAME_C, "sce_sdmemory", None)])
        self.assertEqual([n for _, n in game_c_only], [
            "PS4/SAVEDATA/0000000000000000/CUSA90003/SAVEDATAPlayer",
            "PS4/SAVEDATA/0000000000000000/CUSA90003/SAVEDATAPlayer.bin",
            "PS4/SAVEDATA/0000000000000000/CUSA90003/sce_sdmemory",
            "PS4/SAVEDATA/0000000000000000/CUSA90003/sce_sdmemory.bin",
        ])
        version = sf.members(self.user, [(OTHER, "SAVEDATA01", made.name)])
        self.assertEqual([n for _, n in version], ["PS4/SAVEDATA/0000000000000000/CUSA90006/SAVEDATA01",
                                                   "PS4/SAVEDATA/0000000000000000/CUSA90006/SAVEDATA01.bin"],
                         "the .keep mark stays out of the zip")
        dest = self.tmp / "out" / "x.zip"
        sf.write_zip(dest, version)
        with zipfile.ZipFile(dest) as zf:
            self.assertEqual(zf.read("PS4/SAVEDATA/0000000000000000/CUSA90006/SAVEDATA01"), b"O" * 640)
            self.assertEqual(zf.infolist()[0].compress_type, zipfile.ZIP_STORED)
        self.assertFalse((self.tmp / "out" / "x.zip.part").exists())

    def test_delete_version_drops_empty_parents(self):
        self.back_up(OTHER, "SAVEDATA01")
        _, made = self.back_up(OTHER, "SAVEDATA01")
        sf.delete_version({"path": made})
        self.assertFalse((self.user / "History" / OTHER).exists())
        self.assertTrue((self.user / "History").is_dir())

    def test_prune_downloads(self):
        folder = self.tmp / sf.DOWNLOADS
        folder.mkdir()
        old, new = folder / "old.zip", folder / "new.zip"
        old.write_bytes(b"x")
        new.write_bytes(b"x")
        os.utime(old, (1000, 1000))
        sf.prune_downloads(folder, 3600, now=1000 + 3601)
        self.assertEqual([p.name for p in folder.iterdir()], ["new.zip"])


def v(when: datetime, kept=False, size=10, stamp=None):
    return {"when": when, "kept": kept, "bytes": size, "stamp": stamp or when.strftime(sf.STAMP_FMT)}


class Retention(unittest.TestCase):
    NOW = datetime(2026, 10, 16, 12, 0, 0)

    def test_the_newest_five_always_stay(self):
        versions = [v(self.NOW - timedelta(minutes=m)) for m in (1, 2, 3, 4, 5, 6, 7)]
        doomed = sf.thin(versions, self.NOW)
        self.assertEqual([d["when"] for d in doomed], [self.NOW - timedelta(minutes=6), self.NOW - timedelta(minutes=7)],
                         "6th and 7th share the hour the newest five already cover")

    def test_one_per_hour_day_week(self):
        recent = [v(self.NOW - timedelta(minutes=m)) for m in (1, 2, 3, 4, 5)]
        hourly = [v(self.NOW - timedelta(hours=3, minutes=m)) for m in (5, 20, 40)]   # 08:55-08:20: 1 stays
        daily = [v(self.NOW - timedelta(days=3, hours=h)) for h in (0, 2)]            # same day: 1 stays
        weekly = [v(datetime(2026, 7, 7, 10)), v(datetime(2026, 7, 9, 10)), v(datetime(2026, 7, 20, 10))]
        doomed = sf.thin(recent + hourly + daily + weekly, self.NOW)
        self.assertEqual(sorted(d["when"] for d in doomed), sorted([
            hourly[1]["when"], hourly[2]["when"], daily[1]["when"], datetime(2026, 7, 7, 10),
        ]))

    def test_kept_versions_are_never_thinned_and_dont_take_a_bucket(self):
        recent = [v(self.NOW - timedelta(minutes=m)) for m in (1, 2, 3, 4, 5)]
        kept = v(self.NOW - timedelta(days=3, hours=0), kept=True)
        other = v(self.NOW - timedelta(days=3, hours=1))
        self.assertEqual(sf.thin(recent + [kept, other], self.NOW), [], "the unkept one still gets that day")

    def test_room_takes_the_oldest_unkept(self):
        old, mid, new = (v(datetime(2026, 1, d), size=100) for d in (1, 2, 3))
        kept = v(datetime(2025, 1, 1), kept=True, size=1000)
        self.assertEqual(sf.pick_for_room([new, kept, old, mid], 150), [old, mid])
        self.assertEqual(sf.pick_for_room([new, old], 0), [])
        self.assertIsNone(sf.pick_for_room([new, kept, old], 500), "kept versions are never freed")


class FakeUsb(unittest.TestCase):
    """Restore: HA's copies go into Apollo's Fake USB folder on the console, and only there."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.c = save_console()
        self.c.add_dir("/data", "20260101000000")
        self.ftp = WritableFTP(self.c)
        src = sf.latest_dir(Path(self.tmp.name), GAME_C)
        src.mkdir(parents=True)
        (src / "SAVEDATAPlayer").write_bytes(b"P" * 300)
        (src / "SAVEDATAPlayer.bin").write_bytes(b"k" * 96)
        self.members = sf.members(Path(self.tmp.name), [(GAME_C, "SAVEDATAPlayer", None)])
        self.fake = f"{sf.FAKE_USB_SAVES}/{GAME_C}"

    def test_remote_names(self):
        self.assertEqual(sf.fake_usb_remote(f"PS4/SAVEDATA/{sf.ACCOUNT_DIR}/{GAME_C}/SAVEDATAPlayer.bin"),
                         f"/data/fakeusb/PS4/SAVEDATA/0000000000000000/{GAME_C}/SAVEDATAPlayer.bin")
        for bad in (f"PS4/SAVEDATA/{sf.ACCOUNT_DIR}/{GAME_C}/../x", f"PS4/SAVEDATA/1234567890abcdef/{GAME_C}/s",
                    f"PS4/SAVEDATA/{sf.ACCOUNT_DIR}/cusa1/s", f"PS4/APOLLO/{sf.ACCOUNT_DIR}/{GAME_C}/s", "PS4/x"):
            with self.assertRaises(ValueError, msg=bad):
                sf.fake_usb_remote(bad)

    def test_send_makes_the_folders_uploads_and_checks_sizes(self):
        self.assertEqual(sf.send_to_fake_usb(self.ftp, self.members), 396)
        self.assertEqual(self.c.files[f"{self.fake}/SAVEDATAPlayer"], b"P" * 300)
        self.assertEqual(self.c.files[f"{self.fake}/SAVEDATAPlayer.bin"], b"k" * 96)
        self.assertEqual(self.ftp.made, ["/data/fakeusb", "/data/fakeusb/PS4", "/data/fakeusb/PS4/SAVEDATA",
                                         sf.FAKE_USB_SAVES, self.fake])
        self.assertTrue(all(p.startswith(sf.FAKE_USB_SAVES + "/") for p in self.ftp.stored))
        again = WritableFTP(self.c)
        sf.send_to_fake_usb(again, self.members)
        self.assertEqual(again.made, [], "existing folders are reused")

    def test_a_short_upload_fails_the_check(self):
        self.c.short_stor = {f"{self.fake}/SAVEDATAPlayer"}
        with self.assertRaisesRegex(OSError, "299 bytes on the PS4, 300 in HA"):
            sf.send_to_fake_usb(self.ftp, self.members)

    def test_a_dropped_upload_deletes_its_partial_file(self):
        self.c.fail_stor = {f"{self.fake}/SAVEDATAPlayer"}
        with self.assertRaises(ConnectionResetError):
            sf.send_to_fake_usb(self.ftp, self.members)
        self.assertNotIn(f"{self.fake}/SAVEDATAPlayer", self.c.files)

    def test_clear_takes_only_what_ha_sent(self):
        sf.send_to_fake_usb(self.ftp, self.members)
        other_account = "/data/fakeusb/PS4/SAVEDATA/1234567890abcdef/CUSA00001"
        self.c.add_dir("/data/fakeusb/PS4/SAVEDATA/1234567890abcdef", "20261016120000")
        self.c.add_dir(other_account, "20261016120000")
        self.c.add_file(f"{other_account}/SAVEDATA00", b"x", "20261016120000")
        self.c.add_dir("/data/fakeusb/PS4/APOLLO", "20261016120000")
        self.c.add_file(f"{sf.FAKE_USB_SAVES}/notes.txt", b"mine", "20261016120000")
        self.c.add_file(f"{self.fake}/readme.txt", b"mine", "20261016120000")
        self.assertEqual(sf.clear_fake_usb(self.ftp), 2)
        self.assertNotIn(f"{self.fake}/SAVEDATAPlayer", self.c.files)
        self.assertIn(f"{self.fake}/readme.txt", self.c.files, "not a save file name: stays, so its folder stays")
        for kept in (f"{other_account}/SAVEDATA00", f"{sf.FAKE_USB_SAVES}/notes.txt"):
            self.assertIn(kept, self.c.files)
        self.assertIn("/data/fakeusb/PS4/APOLLO", self.c.dirs)
        self.c.files.pop(f"{self.fake}/readme.txt")
        self.c.dirs[self.fake].pop("readme.txt")
        sf.send_to_fake_usb(self.ftp, self.members)
        self.assertEqual(sf.clear_fake_usb(self.ftp), 2)
        self.assertNotIn(self.fake, self.c.dirs, "the emptied title folder goes")
        self.assertEqual(sf.clear_fake_usb(WritableFTP(save_console())), 0, "no Fake USB folder yet: nothing to do")

    def test_listing_what_sits_there(self):
        """The PS4 pane shows the Fake USB folder as it is."""
        self.assertEqual(sf.list_fake_usb(self.ftp), {}, "no folder yet")
        sf.send_to_fake_usb(self.ftp, self.members)
        self.c.add_file(f"{self.fake}/SAVEDATAHalf", b"x", "20261016120000")  # no key: not a save
        self.c.add_file(f"{self.fake}/readme.txt", b"x", "20261016120000")
        self.assertEqual(sf.list_fake_usb(self.ftp), {GAME_C: ["SAVEDATAPlayer"]})


class SaveNames(unittest.TestCase):
    """The console's own save list names each save; HA keeps the name beside its copy."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.user = self.tmp / "player1"
        self.console = save_console()
        self.ftp = FakeFTP(self.console)

    def test_parse_the_consoles_save_list(self):
        self.assertEqual(sf.parse_save_db(save_db(GAME_C_NAMES)), {
            GAME_C: {"SAVEDATAPlayer": {"name": "Player", "game": "Game C"},
                   "sce_sdmemory": {"name": "(Autosave) Chapter 1 (100.0%)", "game": "Game C (Backup)"}},
            OTHER: {"SAVEDATA01": {"game": "Saved Data"}},
        }, "empty texts left out, a bad title ID skipped")

    def test_other_columns_or_not_a_db(self):
        older = save_db(GAME_C_NAMES[:1], columns=("title_id", "dir_name", "main_title"))
        self.assertEqual(sf.parse_save_db(older), {GAME_C: {"SAVEDATAPlayer": {"game": "Game C"}}})
        self.assertEqual(sf.parse_save_db(save_db([], columns=("title_id", "main_title"))), {})
        self.assertEqual(sf.parse_save_db(b"not a database" * 100), {})
        self.assertEqual(sf.parse_save_db(b""), {})
        long = sf.parse_save_db(save_db([(GAME_C, "SAVEDATAPlayer", "x", "y" * 500)]))
        self.assertEqual(len(long[GAME_C]["SAVEDATAPlayer"]["name"]), sf.TEXT_MAX)

    def test_read_over_ftp(self):
        self.assertEqual(sf.read_save_db(self.ftp, UID), {}, "no save list: no read")
        self.assertEqual(self.ftp.retrieved, [])
        add_save_db(self.console, UID)
        self.assertEqual(sf.read_save_db(self.ftp, UID)[GAME_C]["SAVEDATAPlayer"]["name"], "Player")
        self.assertEqual(self.ftp.retrieved, [sf.SAVE_DB_REMOTE.format(uid=UID)])
        orig, sf.SAVE_DB_MAX = sf.SAVE_DB_MAX, 100
        try:
            self.assertEqual(sf.read_save_db(FakeFTP(self.console), UID), {}, "too big: not read")
        finally:
            sf.SAVE_DB_MAX = orig

    def test_game_names(self):
        meta = sf.parse_save_db(save_db(GAME_C_NAMES))
        self.assertEqual(sf.game_names(meta), {GAME_C: "Game C"}, "not 'Saved Data', not the (Backup) one")

    def back_up(self, title, slot):
        info = sf.list_slots(self.ftp, UID, title)[slot]
        staged = sf.fetch_slot(self.ftp, UID, title, slot, info, self.tmp / sf.STAGING)
        return sf.install(self.user, title, slot, staged, None, timezone.utc)

    def test_the_name_moves_into_history_with_its_copy(self):
        self.back_up(GAME_C, "SAVEDATAPlayer")
        self.back_up(GAME_C, "sce_sdmemory")
        self.assertEqual(sf.latest_meta(self.user), {}, "a new copy is unnamed until the pass names it")
        sf.merge_latest_meta(self.user, {GAME_C: {"SAVEDATAPlayer": {"name": "Chapter 2"},
                                                "sce_sdmemory": {"name": "Autosave"}}})
        meta_file = self.user / sf.LATEST / sf.LATEST_META
        written = meta_file.stat().st_mtime_ns
        os.utime(meta_file, ns=(1, 1))
        sf.merge_latest_meta(self.user, {GAME_C: {"SAVEDATAPlayer": {"name": "Chapter 2"}}})
        self.assertEqual(meta_file.stat().st_mtime_ns, 1, "nothing new: not written again")
        self.assertGreater(written, 1)

        write_save(self.console, UID, GAME_C, "SAVEDATAPlayer", b"Q" * 300, "20261016020000", "20261016011910")
        made = self.back_up(GAME_C, "SAVEDATAPlayer")
        self.assertEqual(sf.version_meta(made), {"name": "Chapter 2"})
        self.assertEqual(sf.latest_meta(self.user), {GAME_C: {"sce_sdmemory": {"name": "Autosave"}}},
                         "the new copy waits for its own name")
        names = [n for _, n in sf.members(self.user, [(GAME_C, "SAVEDATAPlayer", made.name), (GAME_C, "sce_sdmemory", None)])]
        self.assertFalse(any(n.endswith(".json") for n in names), "names never go to a zip or a console")
        self.assertEqual(len(names), 4)

    def test_delete_the_newest_copy(self):
        self.back_up(OTHER, "SAVEDATA01")
        write_save(self.console, UID, OTHER, "SAVEDATA01", b"N" * 640, "20261016030000", "20261015193539")
        made = self.back_up(OTHER, "SAVEDATA01")
        sf.merge_latest_meta(self.user, {OTHER: {"SAVEDATA01": {"name": "Slot 1"}}})
        self.assertTrue(sf.delete_latest(self.user, OTHER, "SAVEDATA01"))
        self.assertIsNone(sf.local_slot(self.user, OTHER, "SAVEDATA01"))
        self.assertFalse(sf.latest_dir(self.user, OTHER).exists(), "the empty title folder goes")
        self.assertFalse((self.user / sf.LATEST / sf.LATEST_META).exists(), "its name goes, no names left: no file")
        self.assertTrue(made.is_dir(), "older versions stay")
        self.assertFalse(sf.delete_latest(self.user, OTHER, "SAVEDATA01"), "already gone")


if __name__ == "__main__":
    unittest.main()
