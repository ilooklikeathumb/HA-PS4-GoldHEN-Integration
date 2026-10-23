"""PS4 screenshots on the console (FTP walk, newest shots, downloads) and HA's copies.

The fake console mirrors what a 12.00 Pro showed: photo/NPXS20001/<title>/<bucket>/<name>.jpg with .dat
and (trophy shots) .ext sidecars, thumbnails at thumbnails/photo/<same path>.jpg, MLSD with UTC modify facts.
"""
import ftplib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components/ps4_goldhen"
PKG = "ps4_capture_files_test_pkg"

pkg = types.ModuleType(PKG)
pkg.__path__ = [str(COMP)]
sys.modules[PKG] = pkg
cf = importlib.import_module(f"{PKG}.capture_files")

P = cf.PHOTO_ROOT
T = cf.THUMB_ROOT


class FakeConsole:
    """Directory tree + file bytes, served through a fake ftplib.FTP that counts listings."""

    def __init__(self):
        self.dirs: dict[str, dict[str, dict]] = {}
        self.files: dict[str, bytes] = {}

    def add_dir(self, path, modify):
        parent, name = path.rsplit("/", 1)
        self.dirs.setdefault(path, {})
        if parent:
            self.dirs.setdefault(parent, {})[name] = {"type": "dir", "modify": modify}

    def add_file(self, path, data, modify):
        parent, name = path.rsplit("/", 1)
        self.dirs.setdefault(parent, {})[name] = {"type": "file", "size": str(len(data)), "modify": modify}
        self.files[path] = data

    def add_shot(self, title, bucket, name, modify, *, size=1000, trophy=False, thumb=True, app="NPXS20001"):
        for path, m in ((f"{P}/{app}", "20260101000000"), (f"{P}/{app}/{title}", modify), (f"{P}/{app}/{title}/{bucket}", modify)):
            if path not in self.dirs:
                self.add_dir(path, m)
            else:
                parent, leaf = path.rsplit("/", 1)
                self.dirs[parent][leaf]["modify"] = max(self.dirs[parent][leaf]["modify"], m)
        stem = name.rsplit(".", 1)[0]
        self.add_file(f"{P}/{app}/{title}/{bucket}/{name}", b"J" * size, modify)
        self.add_file(f"{P}/{app}/{title}/{bucket}/{stem}.dat", b"d" * 592, modify)
        if trophy:
            self.add_file(f"{P}/{app}/{title}/{bucket}/{stem}.ext", b'{"version":2,"trophy":[]}', modify)
        if thumb:
            self.add_file(f"{T}/{app}/{title}/{bucket}/{name}.jpg", b"t" * 11, modify)


class FakeFTP:
    def __init__(self, console: FakeConsole):
        self.console = console
        self.cwd_path = "/"
        self.listings: list[str] = []
        self.retrieved: list[str] = []
        self.fail_retr: set[str] = set()
        self.deleted: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cwd(self, path):
        if path not in self.console.dirs:
            raise ftplib.error_perm("550 No such directory")
        self.cwd_path = path

    def mlsd(self):
        self.listings.append(self.cwd_path)
        for name, facts in sorted(self.console.dirs[self.cwd_path].items()):
            yield name, dict(facts)

    def delete(self, path):
        if path not in self.console.files:
            raise ftplib.error_perm("550 No such file")
        del self.console.files[path]
        parent, name = path.rsplit("/", 1)
        self.console.dirs[parent].pop(name, None)
        self.deleted.append(path)

    def retrbinary(self, cmd, callback, blocksize=8192):
        path = cmd.split(" ", 1)[1]
        if path in self.fail_retr:
            callback(b"half")
            raise ConnectionResetError("dropped")
        if path not in self.console.files:
            raise ftplib.error_perm("550 No such file")
        self.retrieved.append(path)
        data = self.console.files[path]
        for i in range(0, len(data), blocksize):
            callback(data[i:i + blocksize])


def live_like_console():
    """The three shots on a 12.00 Pro: two Game D trophy shots, one home screen shot."""
    c = FakeConsole()
    c.add_shot("CUSA90005", "375", "20261015_110310_00680394.jpg", "20261015150310", size=1028262, trophy=True)
    c.add_shot("CUSA90005", "3e9", "20261015_111203_00205776.jpg", "20261015151203", size=885853, trophy=True)
    c.add_shot("NPXS20001", "147", "20261015_214637_00434169.jpg", "20261016014637", size=383363)
    return c


class PathRules(unittest.TestCase):
    def test_valid_rel_accepts_console_shapes_only(self):
        self.assertTrue(cf.valid_rel("NPXS20001/CUSA90005/375/20261015_110310_00680394.jpg"))
        self.assertTrue(cf.valid_rel("NPXS20001/CUSA90005/3e9/shot.PNG"))
        for bad in ("../x/y/z.jpg", "a/b/c.jpg", "a/b/c/d/e.jpg", "a/b/../x.jpg", "/a/b/c/d.jpg", "a/b/c/d.dat",
                    "a/b c/d/e.jpg", None, 5):
            self.assertFalse(cf.valid_rel(bad), bad)

    def test_taken_is_the_console_clock_from_the_name(self):
        self.assertEqual(cf.taken_from_name("20261015_110310_00680394.jpg"), "2026-10-15T11:03:10")
        self.assertIsNone(cf.taken_from_name("photo.jpg"))

    def test_folder_names(self):
        game_map = {"CUSA90005": {"name": "GAME: D"}, "CUSA1": {"name": "a/b\\c?.."}}
        self.assertEqual(cf.game_folder("CUSA90005", game_map), "GAME D")
        self.assertEqual(cf.game_folder("NPXS20001", game_map), "Home screen")
        self.assertEqual(cf.game_folder("CUSA99999", game_map), "CUSA99999")  # unknown game: its title ID
        self.assertEqual(cf.game_folder("CUSA1", game_map), "a b c")
        self.assertEqual(cf.safe_name("PS4 PRO A"), "PS4 PRO A")
        self.assertIsNone(cf.safe_name("..."))
        self.assertIsNone(cf.safe_name(None))


class ConnectRetry(unittest.TestCase):
    def test_refused_connect_is_retried_then_works(self):
        attempts, sleeps = [], []

        class Flaky:
            def connect(self, host, port, timeout):
                attempts.append((host, port, timeout))
                if len(attempts) < 3:
                    raise ConnectionRefusedError(111, "Connection refused")

            def login(self):
                pass

            def close(self):
                pass

        ftp = cf.connect("192.0.2.121", 2121, factory=Flaky, sleep=sleeps.append)
        self.assertIsInstance(ftp, Flaky)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [cf.CONNECT_RETRY_S] * 2)

    def test_gives_up_after_the_tries(self):
        class Refused:
            def connect(self, *a, **k):
                raise ConnectionRefusedError(111, "Connection refused")

            def close(self):
                pass

        with self.assertRaises(ConnectionRefusedError):
            cf.connect("192.0.2.121", 2121, factory=Refused, sleep=lambda s: None)

    def test_other_errors_are_not_retried(self):
        calls = []

        class Down:
            def connect(self, *a, **k):
                calls.append(1)
                raise TimeoutError("timed out")

        with self.assertRaises(TimeoutError):
            cf.connect("192.0.2.121", 2121, factory=Down, sleep=lambda s: None)
        self.assertEqual(len(calls), 1)


class Walk(unittest.TestCase):
    def test_live_layout_newest_first_with_trophy_flag(self):
        shots = cf.all_shots(FakeFTP(live_like_console()))
        self.assertEqual([s["name"] for s in shots], [
            "20261015_214637_00434169.jpg", "20261015_111203_00205776.jpg", "20261015_110310_00680394.jpg"])
        home, game_d_late, game_d_early = shots
        self.assertEqual(home["rel"], "NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg")
        self.assertEqual((home["title_id"], home["size"], home["trophy"]), ("NPXS20001", 383363, False))
        self.assertEqual((game_d_early["title_id"], game_d_early["trophy"]), ("CUSA90005", True))
        self.assertEqual(game_d_early["modified_utc"], "2026-10-15T15:03:10Z")
        self.assertEqual(game_d_early["taken"], "2026-10-15T11:03:10")

    def test_newest_lists_only_buckets_that_can_hold_newer_shots(self):
        c = FakeConsole()
        for i in range(40):  # 40 shots of one game, one bucket each (as seen live), one per minute
            c.add_shot("CUSA00001", f"{i:x}", f"20261001_10{i:02d}00_000000{i:02d}.jpg", f"202610011{i // 60}{i % 60:02d}00")
        ftp = FakeFTP(c)
        newest = cf.newest_shots(ftp, 3)
        self.assertEqual([s["name"][:15] for s in newest], ["20261001_103900", "20261001_103800", "20261001_103700"])
        bucket_lists = [p for p in ftp.listings if p.count("/") == 6]
        self.assertLessEqual(len(bucket_lists), 4)  # 3 + the one that proves the rest are older; not all 40

    def test_newest_handles_a_reused_bucket(self):
        c = live_like_console()
        c.add_shot("CUSA90005", "375", "20261016_090000_00000001.jpg", "20261016130000")  # lands in an old bucket
        self.assertEqual(cf.newest_shots(FakeFTP(c), 1)[0]["name"], "20261016_090000_00000001.jpg")

    def test_no_screenshots_folder_is_empty_not_an_error(self):
        ftp = FakeFTP(FakeConsole())
        self.assertEqual(cf.all_shots(ftp), [])
        self.assertEqual(cf.newest_shots(ftp, 24), [])
        self.assertEqual(cf.newest_shots(ftp, 6, "CUSA90005"), [])

    def test_one_game_lists_only_its_folders(self):  # the game page: no walk of every game
        c = live_like_console()
        c.add_shot("CUSA90006", "a01", "20261016_100000_00000001.jpg", "20261016140000")
        c.add_shot("CUSA90005", "b02", "20261016_110000_00000002.jpg", "20261016150000", app="CUSA90005")  # in-game share
        ftp = FakeFTP(c)
        shots = cf.all_shots(ftp, "CUSA90005")
        self.assertEqual([s["name"] for s in shots], [
            "20261016_110000_00000002.jpg", "20261015_111203_00205776.jpg", "20261015_110310_00680394.jpg"])
        self.assertEqual({s["title_id"] for s in shots}, {"CUSA90005"})
        self.assertNotIn(f"{P}/NPXS20001", ftp.listings)  # the apps' game folders aren't listed
        self.assertFalse(any("CUSA90006" in p or p.endswith("/NPXS20001/NPXS20001") for p in ftp.listings))
        self.assertEqual([s["name"] for s in cf.newest_shots(FakeFTP(c), 1, "CUSA90005")], ["20261016_110000_00000002.jpg"])
        self.assertEqual(cf.list_buckets(FakeFTP(c), "../x"), [])  # only a title ID's shape is listed

    def test_shot_by_rel(self):
        ftp = FakeFTP(live_like_console())
        rel = "NPXS20001/CUSA90005/3e9/20261015_111203_00205776.jpg"
        self.assertEqual(cf.shot_by_rel(ftp, rel)["size"], 885853)
        self.assertIsNone(cf.shot_by_rel(ftp, "NPXS20001/CUSA90005/3e9/20991231_000000_00000000.jpg"))
        self.assertIsNone(cf.shot_by_rel(ftp, "../../etc/passwd.jpg"))


class Files(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_download_streams_to_part_then_renames(self):
        ftp = FakeFTP(live_like_console())
        dest = self.dir / "PS4 PRO A" / "Home screen" / "20261015_214637_00434169.jpg"
        size = cf.download(ftp, f"{P}/NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg", dest)
        self.assertEqual(size, 383363)
        self.assertEqual(dest.stat().st_size, 383363)
        self.assertEqual(list(dest.parent.iterdir()), [dest])

    def test_failed_download_leaves_nothing(self):
        ftp = FakeFTP(live_like_console())
        remote = f"{P}/NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg"
        ftp.fail_retr.add(remote)
        dest = self.dir / "x" / "shot.jpg"
        with self.assertRaises(ConnectionResetError):
            cf.download(ftp, remote, dest)
        self.assertEqual(list(dest.parent.iterdir()), [])

    def test_thumbs_fetched_once_and_missing_ones_skipped(self):
        c = live_like_console()
        c.add_shot("CUSA00002", "a1", "20261016_100000_00000001.jpg", "20261016140000", thumb=False)
        ftp = FakeFTP(c)
        shots = cf.all_shots(ftp)
        cf.fetch_thumbs(ftp, self.dir, shots)
        self.assertEqual(len(list(self.dir.iterdir())), 3)  # the one without a console thumbnail is skipped
        self.assertTrue(all(p.endswith(".jpg.jpg") for p in ftp.retrieved))
        ftp.retrieved.clear()
        cf.fetch_thumbs(ftp, self.dir, shots)
        self.assertEqual(ftp.retrieved, [], "cached thumbnails are not fetched again")

    def test_prune_drops_gone_shots_then_least_recently_used(self):
        rels = [f"NPXS20001/CUSA00001/{i:x}/20261001_1000{i:02d}_00000000.jpg" for i in range(6)]
        for i, rel in enumerate(rels):
            path = cf.thumb_local(self.dir, rel)
            path.write_bytes(b"t")
            os.utime(path, (1000 + i, 1000 + i))
        self.assertEqual(cf.prune_thumbs(self.dir, cap=10, on_console=set(rels[1:])), 1)  # rels[0] left the PS4
        self.assertEqual(cf.prune_thumbs(self.dir, cap=3), 2)  # oldest-used two over the cap
        left = sorted(p.name for p in self.dir.iterdir())
        self.assertEqual(left, sorted(cf.thumb_local(self.dir, r).name for r in rels[3:]))

    def test_scan_and_find_copies(self):
        console = self.dir / "PS4 PRO A"
        (console / "Home screen").mkdir(parents=True)
        (console / "Home screen" / "a.jpg").write_bytes(b"x" * 10)
        (console / "Home screen" / "b.jpg.part").write_bytes(b"x" * 99)
        (console / "Game D").mkdir()
        (console / "Game D" / "c.jpg").write_bytes(b"x" * 5)
        self.assertEqual(cf.scan_copies(console), {"a.jpg": 10, "c.jpg": 5})
        self.assertEqual(cf.scan_copies(self.dir / "missing"), {})
        self.assertEqual(cf.find_copy(console, "c.jpg"), console / "Game D" / "c.jpg")
        self.assertIsNone(cf.find_copy(console, "../x.jpg"))
        self.assertIsNone(cf.find_copy(console, "zzz.jpg"))

    def test_delete_shot_removes_image_sidecars_and_thumbnail(self):
        c = live_like_console()
        ftp = FakeFTP(c)
        rel = "NPXS20001/CUSA90005/375/20261015_110310_00680394.jpg"  # trophy shot: .dat + .ext
        self.assertEqual(cf.delete_shot(ftp, rel), 4)
        self.assertEqual(sorted(p.rsplit("/", 1)[1] for p in ftp.deleted), [
            "20261015_110310_00680394.dat", "20261015_110310_00680394.ext", "20261015_110310_00680394.jpg",
            "20261015_110310_00680394.jpg.jpg"])
        self.assertEqual([s["rel"] for s in cf.all_shots(ftp)], [
            "NPXS20001/NPXS20001/147/20261015_214637_00434169.jpg",
            "NPXS20001/CUSA90005/3e9/20261015_111203_00205776.jpg"], "the other shots are untouched")
        self.assertEqual(cf.delete_shot(ftp, rel), 0, "already gone: 550s are skipped")
        with self.assertRaises(ValueError):
            cf.delete_shot(ftp, "../../system/x.jpg")

    def test_delete_copy_only_touches_that_copy(self):
        console = self.dir / "PS4 PRO A"
        (console / "Game D").mkdir(parents=True)
        (console / "Game D" / "a.jpg").write_bytes(b"x")
        (console / "Game D" / "b.jpg").write_bytes(b"x")
        (console / "Home screen").mkdir()
        (console / "Home screen" / "c.jpg").write_bytes(b"x")
        self.assertTrue(cf.delete_copy(console, "a.jpg"))
        self.assertTrue((console / "Game D").is_dir(), "folder kept while it has shots")
        self.assertTrue(cf.delete_copy(console, "c.jpg"))
        self.assertFalse((console / "Home screen").exists(), "empty game folder removed")
        self.assertFalse(cf.delete_copy(console, "a.jpg"))
        self.assertEqual(cf.scan_copies(console), {"b.jpg": 1})

    def test_newest_that_fit(self):
        shots = [
            {"name": "a", "size": 400, "modified_utc": "2026-10-01T00:00:01Z"},
            {"name": "c", "size": 400, "modified_utc": "2026-10-01T00:00:03Z"},
            {"name": "b", "size": 400, "modified_utc": "2026-10-01T00:00:02Z"},
        ]
        fit, skipped = cf.newest_that_fit(shots, 900)
        self.assertEqual([s["name"] for s in fit], ["c", "b"])
        self.assertEqual([s["name"] for s in skipped], ["a"])
        self.assertEqual(cf.newest_that_fit(shots, 0), ([], cf._newest_first(shots)))


if __name__ == "__main__":
    unittest.main()
