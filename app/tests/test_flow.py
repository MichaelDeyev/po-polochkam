"""Весь путь через HTTP, как в браузере: анализ, раскладка, разбор, откат. Работает на временной папке.

    python app\\tests\\test_flow.py                     запустить программу из исходников
    python app\\tests\\test_flow.py --exe "dist\\....exe"  проверить собранную программу
"""
import hashlib
import http.client
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from make_fixture import build  # noqa: E402
from razbor.paths import SERVICE_DIR, long_path  # noqa: E402

EXE = None


def tree(root):
    """Путь → (хеш, дата изменения) без служебной папки."""
    out = {}
    for d, dirs, files in os.walk(long_path(root)):
        dirs[:] = [x for x in dirs if x != SERVICE_DIR]
        for f in files:
            p = os.path.join(d, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, long_path(root))] = (hashlib.sha1(fh.read()).hexdigest(), os.stat(p).st_mtime_ns)
    return out


class Client:
    def __init__(self, url):
        self.base = url.split("/?")[0]
        self.token = url.split("t=")[1]
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.op.open(url).read()

    def get(self, path):
        return json.load(self.op.open(self.base + path))

    def post(self, path, data=None):
        req = urllib.request.Request(self.base + path, json.dumps(data or {}).encode(), {"X-Token": self.token, "Content-Type": "application/json"})
        return json.load(self.op.open(req))

    def raw(self, path):
        return self.op.open(self.base + path).read()

    def wait(self, limit=300):
        end = time.time() + limit
        while time.time() < end:
            job = self.get("/api/state")["job"]
            if job["status"] != "running":
                return job
            time.sleep(0.2)
        raise TimeoutError


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="razbor-test-")
        cls.root = os.path.join(cls.tmp, "Тест фото")
        build(cls.root)
        cls.before = tree(cls.root)
        cls.reports = os.path.join(cls.tmp, "отчёты")
        env = {**os.environ, "LOCALAPPDATA": os.path.join(cls.tmp, "appdata"), "RAZBOR_REPORT_DIR": cls.reports}
        if EXE:     # собранная программа не должна видеть Python этого компьютера
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
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_flow(self):
        c, root = self.c, self.root
        # Чужие запросы
        req = urllib.request.Request(c.base + "/api/analyze", b"{}", {"Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        self.assertEqual(e.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(c.base + "/api/state")
        # Старая вкладка с прежним ключом не портит следующий запрос на том же соединении
        host, port = c.base.split("//")[1].split(":")
        conn = http.client.HTTPConnection(host, int(port))
        conn.request("POST", "/api/ping", b"{}", {"X-Token": "старый".encode().hex(), "Content-Length": "2"})
        self.assertEqual(conn.getresponse().read() and 403, 403)
        conn.request("GET", "/")
        self.assertEqual(conn.getresponse().status, 403)    # без ключа, но это настоящий ответ программы, а не 501
        conn.close()
        # Обучение показывается один раз и запоминается на этом компьютере
        self.assertFalse(c.get("/api/state")["tutorial_done"])
        self.assertEqual(c.post("/api/tutorial/done"), {"ok": True})
        self.assertTrue(c.get("/api/state")["tutorial_done"])
        # Запретные папки
        self.assertIn("error", c.post("/api/open", {"path": os.environ.get("WINDIR", "C:\\Windows")}))
        self.assertIn("error", c.post("/api/open", {"path": "C:\\"}))
        self.assertIn("error", c.post("/api/open", {"path": os.path.expanduser("~")}))
        self.assertIn("error", c.post("/api/open", {"path": root + "\\OneDrive\\нет"}))

        # Анализ не меняет файлы
        self.assertEqual(c.post("/api/open", {"path": root}).get("root"), root)
        c.post("/api/settings", {"keep_folders": ["Свадьба"], "separate_screens": True, "separate_live": True, "separate_scans": True, "fix_file_dates": True})
        c.post("/api/analyze")
        job = c.wait()
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(tree(root), self.before, "анализ изменил файлы")
        info = job["result"]
        self.assertEqual(info["exact"], 1)
        self.assertEqual(info["similar"], 2)     # уменьшенная копия и правка айфона
        rep = c.get("/api/report")
        acts = {a["action"]: a["files"] for a in rep["actions"]}
        self.assertEqual(acts["дубль"], 1)
        self.assertEqual(acts["похожий дубль"], 2)
        self.assertEqual(acts["скриншот"], 1)
        self.assertEqual(acts["скан"], 1)
        self.assertEqual(acts["мусор"], 1)
        self.assertGreaterEqual(acts["проверить"], 2)    # снимок без даты и видео-заглушка не теряются
        self.assertEqual(rep["unreadable"], 1)
        self.assertEqual(c.get("/api/state")["layout_moves"], rep["moves"], "страница не узнает, что раскладка не сделана")

        # Отметка времени: повторный анализ берёт готовое и не считает заново
        c.post("/api/analyze")
        self.assertEqual(c.wait()["status"], "done")

        # Раскладка
        c.post("/api/apply/layout")
        job = c.wait()
        self.assertEqual(job["status"], "done", job)
        self.assertFalse(job["result"]["failed"])
        after = tree(root)
        self.assertEqual(sorted(v[0] for v in after.values()), sorted(v[0] for v in self.before.values()), "содержимое файлов изменилось или файл пропал")
        self.assertIn("Фото по годам\\2019\\2019-05\\2019-05-06_101112_IMG_0001.jpg", after)
        self.assertIn("Фото по годам\\2018\\2018-05\\2018-05-12_143307_IMG_20180512_143307.jpg", after)
        self.assertIn("Скриншоты\\2021\\2021-02\\2021-02-03_102030_Screenshot_2021-02-03-10-20-30.png", after)
        self.assertIn("Фото по годам\\2024\\2024-08\\2024-08-09_183000_IMG_5000.heic", after)
        self.assertIn("Свадьба\\кадр1.jpg", after)
        self.assertIn("куча\\заметка.txt", after)
        self.assertTrue(any(k.startswith("_Дубли\\похожие\\") for k in after))
        self.assertTrue(any(k.startswith("_Мусор\\") for k in after))
        self.assertTrue(any("ю" * 100 in k for k in after))
        # Дата файла исправлена
        st = os.stat(long_path(root, "Фото по годам\\2019\\2019-05\\2019-05-06_101112_IMG_0001.jpg"))
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(st.st_mtime)), "2019-05-06")

        # Повторный анализ на разложенном: ничего не переезжает
        c.post("/api/analyze")
        self.assertEqual(c.wait()["status"], "done")
        self.assertEqual(c.get("/api/report")["moves"], 0)
        self.assertEqual(c.get("/api/state")["layout_moves"], 0)

        # Разбор: пометки переживают раскладку, непросмотренные группы не трогаются
        s = c.get("/api/review/summary?mode=all")
        self.assertGreaterEqual(s["all"], 2)
        groups = [c.get(f"/api/review/group?pos={i}&mode=all")["group"] for i in range(s["all"])]
        burst = next(g for g in groups if any("DSC1000" in p["path"] for p in g["photos"]))
        edit = next(p for g in groups for p in g["photos"] if "IMG_E0100" in p["path"])
        self.assertTrue((edit["edit_of"] or "").endswith("IMG_0100.jpg"), "правка айфона не помечена")
        self.assertFalse(any(p["edit_of"] for p in burst["photos"]), "лишняя пометка правки")
        self.assertEqual(sum("DSC" in p["path"] for p in burst["photos"]), 4, "серия разбита на разные группы")
        self.assertTrue(c.raw(f"/thumb?id={burst['photos'][0]['id']}").startswith(b"\xff\xd8"))
        self.assertEqual(rep["unreadable"], 1, "снимок HEIC не открылся")
        dup_group = next(g for g in groups if any(p["path"].startswith("_Дубли") for p in g["photos"]))
        dup = next(p for p in dup_group["photos"] if p["path"].startswith("_Дубли"))
        self.assertFalse(dup["keep"])
        self.assertEqual(c.post("/api/review/plan")["remove"], 0)   # ничего не смотрели — ничего не трогаем
        st = c.get("/api/state")
        self.assertEqual((st["review_unseen"], st["review_pending"]), (s["all"], 0), "подсказка «Следующий шаг» не знает о разборе")

        # Разбор со старой страницы: снимок вне групп программы (полным путём), несуществующий снимок, пояснения и мусор
        in_groups = {p["path"] for g in groups for p in g["photos"]}
        loose = next(r for r in sorted(tree(root)) if r.startswith("Фото по годам") and r.lower().endswith(".jpg") and r not in in_groups)
        text = ("﻿# Разбор похожих: весь прогресс\r\n# группы: abc\r\nубрать\t" + os.path.join(root, loose) +
                "\r\nубрать\tФото по годам\\нет такого.jpg\r\nчто-то непонятное\r\n")
        r = c.post("/api/review/import", {"text": text})
        self.assertEqual((r["found"], r["drop"], r["missing"], r["bad"], r["outside_groups"], r["written"]), (1, 1, 1, 1, 1, False))
        self.assertEqual(c.post("/api/review/plan")["remove"], 0, "проверка файла уже записала пометки")
        self.assertTrue(c.post("/api/review/import", {"text": text, "write": True})["written"])
        self.assertEqual(c.post("/api/review/plan")["remove"], 1, "решение из файла не выполнится: снимок вне групп программы")

        drop = burst["photos"][1]
        c.post("/api/review/mark", {"gid": burst["id"], "marks": [[drop["id"], False]]})
        c.post("/api/review/mark", {"gid": dup_group["id"], "marks": [[dup["id"], True]]})
        plan = c.post("/api/review/plan")
        self.assertEqual((plan["remove"], plan["restore"]), (2, 1))
        st = c.get("/api/state")
        self.assertEqual((st["review_unseen"], st["review_pending"]), (s["all"] - 2, 3))
        self.assertEqual(c.get("/api/review/empty")["groups"], [])
        c.post("/api/review/apply")
        job = c.wait()
        self.assertEqual(job["status"], "done", job)
        after = tree(root)
        self.assertTrue(os.path.exists(long_path(root, "_Удалить\\" + drop["path"])))
        self.assertTrue(os.path.exists(long_path(root, "_Удалить\\" + loose)), "снимок из файла разбора не переехал")
        r = c.post("/api/review/import", {"text": text})
        self.assertEqual((r["found"], r["already_removed"]), (0, 1), "повторная загрузка не видит, что снимок уже в «_Удалить»")
        self.assertFalse(os.path.exists(long_path(root, dup["path"])))
        self.assertEqual(c.get("/api/state")["review_pending"], 0, "после применения пометки всё ещё ждут")
        self.assertEqual(sorted(v[0] for v in after.values()), sorted(v[0] for v in self.before.values()), "файл пропал")

        # Без даты: подсказки, проверка ввода, перенос по указанной дате
        page = c.get("/api/check/list?offset=0")
        self.assertEqual(page["total"], c.get("/api/state")["undated"])
        items = {x["name"]: x for x in page["items"]}
        self.assertIn("2020-07", [h["value"] for h in items["IMG_0003.jpg"]["hints"]], "нет подсказки по соседнему номеру кадра")
        self.assertIn("2016", [h["value"] for h in items["море.jpg"]["hints"]], "нет подсказки по названию папки")
        self.assertIn("error", c.post("/api/check/set", {"values": [[items["море.jpg"]["id"], "2016-13"]]}))
        self.assertIn("error", c.post("/api/check/set", {"values": [[items["море.jpg"]["id"], "../x"]]}))
        c.post("/api/check/set", {"values": [[items["IMG_0003.jpg"]["id"], "2020-07"], [items["море.jpg"]["id"], "2016"]]})
        self.assertEqual(c.get("/api/check/list?offset=0")["decided"], 2)
        self.assertEqual(c.post("/api/check/plan")["moves"], 2)
        c.post("/api/check/apply")
        job = c.wait()
        self.assertEqual(job["status"], "done", job)
        moved = os.stat(long_path(root, "Фото по годам\\2020\\2020-07\\IMG_0003.jpg"))
        self.assertTrue(os.path.exists(long_path(root, "Фото по годам\\2016\\море.jpg")))
        self.assertEqual(moved.st_mtime_ns, self.before["куча\\IMG_0003.jpg"][1], "дата файла изменилась, хотя точного дня никто не знает")
        self.assertEqual(c.get("/api/check/list?offset=0")["total"], page["total"] - 2)

        # Журналы хранят относительные пути
        js = c.get("/api/journals")["journals"]
        self.assertEqual(len(js), 3)
        self.assertEqual([j["can_rollback"] for j in js], [True, False, False])
        self.assertEqual(c.get("/api/finish")["drop"]["files"], 2, "в «_Удалить» снимок из разбора и снимок из файла")

        # Версия и отчёт для автора: ошибки страницы попадают в отчёт, путь к папке скрыт
        version = c.get("/api/state")["version"]
        self.assertRegex(version, r"^версия от \d\d\.\d\d\.\d{4}, сборка [0-9a-f]{7}" if EXE else "^версия из исходников$")
        c.post("/api/client_error", {"message": "проверка: ошибка страницы"})
        report = c.post("/api/report_problem", {"note": "проверка отчёта", "reveal": False})["path"]
        self.assertEqual(os.path.dirname(report), self.reports)
        text = open(report, encoding="utf-8-sig").read()
        for part in ("проверка отчёта", "страница: проверка: ошибка страницы", "Раскладка по папкам", "Windows:"):
            self.assertIn(part, text)
        self.assertNotIn(root, text, "в отчёте путь к папке человека")

        # Откат всего: дерево и даты как до начала
        for j in js:
            self.assertEqual(c.post("/api/rollback", {"name": j["name"]}).get("started"), "rollback")
            job = c.wait()
            self.assertEqual(job["status"], "done", job)
            self.assertFalse(job["result"]["problems"], job)
        self.assertEqual(tree(root), self.before, "откат не вернул всё как было")
        self.assertFalse(os.path.exists(long_path(root, "Фото по годам")), "после отката остались пустые папки")

    def test_window_close(self):
        """Идёт после test_flow (по алфавиту): программа завершается, когда закрыто последнее окно."""
        c = self.c
        for path, start in (("/favicon.ico", b"\x00\x00\x01\x00"), ("/icon.png", b"\x89PNG")):
            self.assertTrue(urllib.request.urlopen(c.base + path).read().startswith(start), path)   # значок без ключа
        c.post("/api/ping", {"page": "окно-1"})
        c.post("/api/ping", {"page": "окно-2"})
        c.post("/api/bye", {"page": "окно-1"})
        time.sleep(7)
        self.assertIsNone(self.proc.poll(), "закрыли одно окно из двух, а программа завершилась")
        c.post("/api/bye", {"page": "окно-2"})
        c.post("/api/ping", {"page": "окно-3"})     # обновили страницу: новое окно отозвалось сразу
        c.post("/api/bye", {"page": "окно-3"})
        self.proc.wait(15)                           # закрыли последнее окно — программа завершилась сама
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "appdata", "РазборФото", "запущена.json")))


if __name__ == "__main__":
    if "--exe" in sys.argv:
        i = sys.argv.index("--exe")
        EXE = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    unittest.main(verbosity=2)
