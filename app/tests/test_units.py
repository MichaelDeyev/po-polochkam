"""Обрывы, защита папок и совпадение отпечатков с библиотекой, на которой настраивались пороги."""
import datetime as dt
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from make_fixture import noise_photo  # noqa: E402
from razbor import about, dates, images, moves, paths  # noqa: E402
from razbor.analysis import Cancelled, kind_of  # noqa: E402
from razbor.planner import dated_name, original_rel  # noqa: E402


class Ctx:
    def __init__(self, stop_after=None):
        self.n, self.stop_after = 0, stop_after

    def cancelled(self):
        self.n += 1
        return self.stop_after is not None and self.n > self.stop_after

    def progress(self, *a):
        pass


def row(rel, dst, size=None):
    return {"rel": rel, "dst": dst, "action": "убрать", "date": "", "size": size}


class Moves(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="razbor-unit-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        os.makedirs(os.path.join(self.root, paths.SERVICE_DIR))
        os.makedirs(os.path.join(self.root, "а"))
        for i in range(5):
            with open(os.path.join(self.root, "а", f"{i}.jpg"), "w") as f:
                f.write(str(i))
        self.rows = [row(f"а\\{i}.jpg", f"б\\{i}.jpg", 1) for i in range(5)]

    def test_resume_after_cancel(self):
        j = moves.new_journal(self.root, "layout", "т", 5)
        with self.assertRaises(Cancelled):
            moves.run_moves(self.root, self.rows, j, Ctx(stop_after=2), False, lambda *a: None)
        left = len(os.listdir(os.path.join(self.root, "а")))
        self.assertEqual(left, 3)
        moved, failed = moves.run_moves(self.root, self.rows, j, Ctx(), False, lambda *a: None)
        self.assertEqual((moved, failed), (3, []))
        self.assertEqual(len(os.listdir(os.path.join(self.root, "б"))), 5)
        self.assertFalse(os.path.exists(os.path.join(self.root, "а")), "опустевшая папка осталась")
        res = moves.rollback(self.root, j, Ctx(), lambda *a: None)
        self.assertEqual(res["back"], 5)
        self.assertEqual(len(os.listdir(os.path.join(self.root, "а"))), 5)
        self.assertFalse(os.path.exists(os.path.join(self.root, "б")))

    def test_never_overwrites(self):
        os.makedirs(os.path.join(self.root, "б"))
        with open(os.path.join(self.root, "б", "0.jpg"), "w") as f:
            f.write("чужой файл")
        self.assertTrue(moves.preflight(self.root, self.rows, set()))
        j = moves.new_journal(self.root, "layout", "т", 5)
        moved, failed = moves.run_moves(self.root, self.rows, j, Ctx(), False, lambda *a: None)
        self.assertEqual((moved, len(failed)), (4, 1))
        with open(os.path.join(self.root, "б", "0.jpg")) as f:
            self.assertEqual(f.read(), "чужой файл")
        self.assertTrue(os.path.exists(os.path.join(self.root, "а", "0.jpg")))

    def test_changed_file_refused(self):
        self.assertTrue(any("изменился" in p for p in moves.preflight(self.root, [row("а\\0.jpg", "б\\0.jpg", 999)], set())))

    def test_disk_gone(self):
        j = moves.new_journal(self.root, "layout", "т", 5)
        real_rename = os.rename
        with mock.patch.object(moves.os, "rename", side_effect=OSError("диск пропал")), \
                mock.patch.object(moves.os.path, "isdir", return_value=False):
            with self.assertRaises(moves.DiskGone):
                moves.run_moves(self.root, self.rows, j, Ctx(), False, lambda *a: None)
        self.assertIs(os.rename, real_rename)
        # После возвращения диска работа продолжается и незавершённое намерение не мешает откату.
        moved, failed = moves.run_moves(self.root, self.rows, j, Ctx(), False, lambda *a: None)
        self.assertEqual((moved, failed), (5, []))
        self.assertEqual(moves.rollback(self.root, j, Ctx(), lambda *a: None)["back"], 5)

    def test_rollback_after_user_deleted(self):
        j = moves.new_journal(self.root, "review", "т", 5)
        moves.run_moves(self.root, self.rows, j, Ctx(), False, lambda *a: None)
        shutil.rmtree(os.path.join(self.root, "б"))
        res = moves.rollback(self.root, j, Ctx(), lambda *a: None)
        self.assertEqual((res["back"], res["already_deleted"], res["problems"]), (0, 5, []))

    def test_only_last_can_rollback(self):
        a = moves.new_journal(self.root, "layout", "1", 1)
        moves.run_moves(self.root, self.rows[:1], a, Ctx(), False, lambda *x: None)
        b = moves.new_journal(self.root, "review", "2", 1)
        moves.run_moves(self.root, self.rows[1:2], b, Ctx(), False, lambda *x: None)
        js = moves.list_journals(self.root)
        self.assertEqual([x["can_rollback"] for x in js], [True, False])
        moves.rollback(self.root, b, Ctx(), lambda *x: None)
        self.assertEqual([x["can_rollback"] for x in moves.list_journals(self.root)], [False, True])


class Roots(unittest.TestCase):
    def test_refused(self):
        for path in (os.environ["WINDIR"], os.environ["WINDIR"] + "\\System32", os.environ["PROGRAMFILES"], os.path.expanduser("~"),
                     os.environ["LOCALAPPDATA"], "C:\\", "Z:\\нет такой", "\\\\server\\share"):
            self.assertTrue(paths.check_root(path)[0], path)

    def test_cloud_refused(self):
        tmp = tempfile.mkdtemp(prefix="razbor-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        for name in ("OneDrive", "Dropbox", "Google Drive"):
            cloud = os.path.join(tmp, name, "Фото")
            os.makedirs(cloud)
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": "C:\\нет"}):
                self.assertTrue(paths.check_root(cloud)[0], name)


class Pure(unittest.TestCase):
    def test_hashes_match_imagehash(self):
        try:
            import imagehash
        except ImportError:
            self.skipTest("нет imagehash")
        for seed in range(30):
            im = noise_photo(seed)
            self.assertEqual(images.phash(im), str(imagehash.phash(im)), seed)
            self.assertEqual(images.dhash(im), str(imagehash.dhash(im)), seed)

    def test_dates(self):
        self.assertEqual(dates.date_from_name("IMG_20180512_143307.jpg"), dt.datetime(2018, 5, 12, 14, 33, 7))
        self.assertEqual(dates.date_from_name("Снимок экрана 2018-05-12 в 14.33.07.png"), dt.datetime(2018, 5, 12, 14, 33, 7))
        self.assertIsNone(dates.date_from_name("IMG_1234.jpg"))
        self.assertIsNone(dates.parse_dt("2001:01:01 00:00:00"))
        self.assertIsNone(dates.parse_dt("2999:01:01 00:00:00"))
        when, src, conflict = dates.pick_date({"ExifIFD:DateTimeOriginal": "2021:05:05 10:00:00"}, "IMG_20180512_143307.jpg", set())
        self.assertEqual(when.year, 2018)
        self.assertTrue(conflict)
        self.assertEqual(dates.pick_date({}, "a.jpg", set()), (None, None, None))
        self.assertTrue(dates.pick_date({"QuickTime:CreateDate": "2020:01:01 10:00:00"}, "a.mov", {dt.date(2020, 1, 1)})[1].startswith(dates.WEAK))

    def test_report_hides_paths(self):
        root = tempfile.mkdtemp(prefix="razbor-unit-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(paths.service_dir(root), "журналы"))
        app = mock.Mock(root=root, store=None, job=None, disk="ok")
        with mock.patch.object(about, "app_data_dir", return_value=root):
            with open(os.path.join(root, "журнал ошибок.txt"), "w", encoding="utf-8") as f:
                f.write(f"ERROR нет файла {root}\\Отпуск\\IMG_1.jpg\n")
            text = about.build_report(app, "не открывается папка")
            path = about.save_report(app, "", folder=root)
        self.assertNotIn(root, text)
        self.assertIn("<выбранная папка>\\Отпуск\\IMG_1.jpg", text)
        self.assertIn("не открывается папка", text)
        self.assertIn("версия", text)
        self.assertTrue(os.path.isfile(path))

    def test_iphone_edits(self):
        from razbor.review import iphone_edits
        group = ["_Дубли\\похожие\\мы\\IMG_0059.JPG", "Фото по годам\\2018\\2018-04\\2018-04-14_155359_IMG_E0059.jpg",
                 "Фото по годам\\2018\\2018-04\\2018-04-14_155400_IMG_E0060 (2).jpg", "IMG_E0061.jpg", "IMG_0061_2.jpg",
                 "DSC_E0059.jpg", "IMG_E00591.jpg"]
        self.assertEqual(iphone_edits(group), {group[1]: "IMG_0059.JPG", group[3]: "IMG_0061_2.jpg"},
                         "правка без оригинала в группе или чужое имя не должны помечаться")

    def test_review_file(self):
        from razbor.review import import_rel, parse_marks
        pairs, bad = parse_marks("\ufeff# комментарий\r\nубрать\tФото по годам\\a.jpg\r\n\r\nОставить\t\"b.jpg\"\r\nудалить\tc.jpg\r\nубрать\r\n")
        self.assertEqual((pairs, bad), ([(False, "Фото по годам\\a.jpg"), (True, "b.jpg")], 2))
        self.assertEqual(import_rel("E:\\Фото по годам\\a.jpg", "E:\\"), "Фото по годам\\a.jpg")
        self.assertEqual(import_rel("d:/Архив/Фото по годам/a.jpg", "D:\\Архив"), "Фото по годам\\a.jpg", "та же папка, другой регистр и слеши")
        self.assertEqual(import_rel("F:\\Фото по годам\\a.jpg", "E:\\"), "Фото по годам\\a.jpg", "диск подключили под другой буквой")
        self.assertEqual(import_rel(".\\Фото по годам\\a.jpg", "E:\\"), "Фото по годам\\a.jpg")

    def test_window(self):
        from razbor import window
        cmd = window.edge_command("msedge.exe", "http://127.0.0.1:5000/?t=x")
        self.assertIn("--app=http://127.0.0.1:5000/?t=x", cmd)
        self.assertIn(f"--user-data-dir={os.path.join(paths.app_data_dir(), 'окно')}", cmd,
                      "у окна свой профиль: вкладки и история человека не смешиваются с программой")
        self.assertTrue(any("msImplicitSignin" in a and "msEdgeStartupBoost" in a for a in cmd))
        with mock.patch.object(window, "find_edge", return_value=None), mock.patch.object(window.webbrowser, "open") as op:
            self.assertEqual(window.open_window("http://127.0.0.1:1/"), "browser", "без Edge — обычный браузер")
            op.assert_called_once()

    def test_names(self):
        self.assertEqual(kind_of("._a.jpg"), "junk")
        self.assertEqual(kind_of("A.HEIC"), "photo")
        self.assertEqual(kind_of("a.txt"), "other")
        self.assertEqual(original_rel("_Дубли\\похожие\\я\\a.jpg"), "я\\a.jpg")
        self.assertEqual(original_rel("я\\a.jpg"), "я\\a.jpg")
        name = dated_name(dt.datetime(2020, 1, 2, 3, 4, 5), "ю" * 300 + ".jpg")
        self.assertLessEqual(len(name), 240)
        self.assertTrue(name.endswith(".jpg"))


if __name__ == "__main__":
    unittest.main(verbosity=1)
