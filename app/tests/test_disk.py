"""Отключение диска на ходу: программа не зависает, ждёт диск и продолжает; пауза отпускает диск.

Диск настоящий, только виртуальный: папка подключается буквой через subst, а «выдёргивается» командой subst /D.

    python app\\tests\\test_disk.py                     из исходников
    python app\\tests\\test_disk.py --exe "dist\\....exe"  собранная программа
"""
import json
import os
import shutil
import string
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from make_fixture import build  # noqa: E402
from test_flow import Client  # noqa: E402

EXE = None


def free_letter():
    used = {d[0].upper() for d in os.listdrives()} if hasattr(os, "listdrives") else set()
    for x in reversed(string.ascii_uppercase[3:]):
        if x not in used and not os.path.exists(x + ":\\"):
            return x
    raise RuntimeError("нет свободной буквы диска")


def subst(letter, target=None):
    args = ["subst", letter + ":"] + ([target] if target else ["/D"])
    subprocess.run(args, check=True, capture_output=True)


def status(c, path, data=None):
    """(код ответа, тело) — 503 здесь ожидаемый ответ, а не сбой."""
    try:
        return 200, (c.post(path, data) if data is not None else c.get(path))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class Disk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="razbor-disk-")
        cls.media = os.path.join(cls.tmp, "носитель")
        build(os.path.join(cls.media, "Тест фото"))
        cls.letter = free_letter()
        subst(cls.letter, cls.media)
        cls.root = cls.letter + ":\\Тест фото"
        env = {**os.environ, "LOCALAPPDATA": os.path.join(cls.tmp, "appdata"), "RAZBOR_REPORT_DIR": os.path.join(cls.tmp, "отчёты")}
        if EXE:
            for name in ("PYTHONPATH", "PYTHONHOME", "APPDATA"):
                env.pop(name, None)
            env["PATH"] = os.environ["WINDIR"] + "\\System32"
        os.makedirs(env["LOCALAPPDATA"])
        cmd = [EXE, "--no-browser"] if EXE else [sys.executable, os.path.join(os.path.dirname(HERE), "run.py"), "--no-browser"]
        cls.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL)
        info = os.path.join(env["LOCALAPPDATA"], "РазборФото", "запущена.json")
        for _ in range(600):
            if os.path.exists(info):
                break
            time.sleep(0.1)
        time.sleep(0.3)
        cls.c = Client(json.load(open(info, encoding="utf-8"))["url"])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.c.post("/api/quit")
            cls.proc.wait(15)
        except Exception:
            cls.proc.kill()
        cls.c.op.close()
        if os.path.exists(cls.letter + ":\\"):
            subst(cls.letter)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def wait_disk(self, want, limit=15):
        end = time.time() + limit
        while time.time() < end:
            if self.c.get("/api/disk")["disk"] == want:
                return
            time.sleep(0.3)
        self.fail(f"диск не перешёл в состояние {want}")

    def test_disk(self):
        c = self.c
        self.assertEqual(c.post("/api/open", {"path": self.root}).get("root"), self.root)
        c.post("/api/analyze")
        self.assertEqual(c.wait()["status"], "done")
        s = c.get("/api/review/summary?mode=all")
        self.assertGreater(s["all"], 0)
        g = c.get("/api/review/group?pos=0&mode=all")["group"]
        c.post("/api/review/mark", {"gid": g["id"], "marks": [[g["photos"][0]["id"], False]]})
        c.raw(f"/thumb?id={g['photos'][0]['id']}")       # миниатюра теперь лежит на компьютере

        # Диск выдернули: программа сама это замечает и отвечает, а не зависает
        subst(self.letter)
        self.wait_disk("gone")
        st = c.get("/api/state")
        self.assertEqual(st["disk"], "gone")
        self.assertNotIn("analysis", st)
        code, body = status(c, "/api/review/summary?mode=all")
        self.assertEqual((code, body.get("disk")), (503, "gone"))
        code, body = status(c, "/api/review/mark", {"gid": g["id"], "marks": [[g["photos"][1]["id"], False]]})
        self.assertEqual((code, body.get("disk")), (503, "gone"))
        self.assertGreater(len(c.raw(f"/thumb?id={g['photos'][0]['id']}")), 100)    # готовую миниатюру видно и без диска
        code, _ = status(c, "/api/analyze", {})
        self.assertEqual(code, 503)
        report = c.post("/api/report_problem", {"note": "диск отключился", "reveal": False})["path"]     # отчёт сохраняется и без диска
        self.assertIn("Диск: отключён", open(report, encoding="utf-8").read())

        # Диск вернули: та же база, пометка на месте
        subst(self.letter, self.media)
        self.wait_disk("ok")
        g2 = c.get("/api/review/group?pos=0&mode=all")["group"]
        self.assertFalse(g2["photos"][0]["keep"])
        self.assertTrue(g2["seen"])
        self.assertEqual(c.get("/api/state")["disk"], "ok")

        # Пауза отпускает базу: папку с ней можно переименовать (пока база открыта, Windows не даёт)
        real = os.path.join(self.media, "Тест фото")
        renamed = real + " на паузе"
        try:
            os.rename(real, renamed)
            os.rename(renamed, real)
            held = False
        except OSError:
            held = True
        self.assertTrue(held, "пока программа работает, база должна быть открыта")
        self.assertEqual(c.post("/api/pause"), {"disk": "paused"})
        code, body = status(c, "/api/review/summary?mode=all")
        self.assertEqual((code, body.get("disk")), (503, "paused"))
        os.rename(real, renamed)
        time.sleep(2.5)
        self.assertEqual(c.get("/api/disk")["disk"], "paused")      # пауза сама не снимается
        self.assertIn("error", c.post("/api/resume"))               # папки нет — продолжить нельзя
        os.rename(renamed, real)
        self.assertEqual(c.post("/api/resume"), {"disk": "ok"})
        self.assertEqual(c.get("/api/review/summary?mode=all")["seen"], s["seen"] + 1)

        # Пауза во время работы: работа останавливается, потом её можно запустить снова
        c.post("/api/analyze")
        self.assertEqual(c.post("/api/pause"), {"disk": "paused"})
        self.assertIn(c.get("/api/state")["job"]["status"], ("cancelled", "done"))
        self.assertEqual(c.post("/api/resume"), {"disk": "ok"})
        c.post("/api/analyze")
        self.assertEqual(c.wait()["status"], "done")

        # Если диск вернулся под другой буквой, можно открыть папку заново
        subst(self.letter)
        self.wait_disk("gone")
        other = free_letter()
        subst(other, self.media)
        try:
            self.assertEqual(c.post("/api/open", {"path": other + ":\\Тест фото"}).get("root"), other + ":\\Тест фото")
            self.assertEqual(c.get("/api/disk")["disk"], "ok")
            self.assertTrue(c.get("/api/review/group?pos=0&mode=all")["group"]["seen"])
            c.post("/api/pause")        # отпустить базу, чтобы букву можно было убрать
        finally:
            subst(other)
        subst(self.letter, self.media)


if __name__ == "__main__":
    if "--exe" in sys.argv:
        i = sys.argv.index("--exe")
        EXE = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    unittest.main(verbosity=2)
