"""Локальный сервер для интерфейса. Слушает только этот компьютер, без интернета; чужие страницы отсекает токен."""
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import about, images, moves, paths
from .app import App, DiskUnavailable
from .folder_dialog import Picker
from .review import Review
from .undated import Undated
from .window import open_window

INSTANCE_FILE = "запущена.json"
IDLE_LIMIT_SECONDS = 180        # окно давно не отзывается и работы нет: программа завершается
CLOSE_GRACE_SECONDS = 5         # последнее окно закрыли: ждём столько на случай, если его просто обновили
MUTEX_NAME = "PoPolochkam.Running"     # пока он есть, установщик и удаление просят закрыть программу (AppMutex)
ICONS = {"/favicon.ico": ("icon.ico", "image/x-icon"), "/icon.png": ("icon.png", "image/png")}
THUMB_LIMIT_BYTES = 2 * 2**30
NO_THUMB = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 240"><rect width="320" height="240" fill="#8884"/>'
            '<text x="160" y="125" font-family="sans-serif" font-size="16" text-anchor="middle" fill="#888">Не удалось показать</text></svg>')


class Handler(BaseHTTPRequestHandler):
    server_version = "Razbor"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    # ---------- ответы ----------

    def send_bytes(self, body, ctype, code=200, extra=()):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        if self.close_connection:
            self.send_header("Connection", "close")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, data, code=200):
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code,
                        [("Cache-Control", "no-store")])

    # ---------- доступ ----------

    def host_ok(self):
        return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}")

    def cookie_ok(self):
        return f"razbor={self.server.token}" in (self.headers.get("Cookie") or "")

    def do_GET(self):
        self.server.touch()
        if not self.host_ok():
            return self.send_bytes(b"", "text/plain", 403)
        url = urlparse(self.path)
        q = parse_qs(url.query)
        if url.path == "/":
            given = q.get("t", [""])[0]
            if not secrets.compare_digest(given, self.server.token) and not self.cookie_ok():
                return self.send_bytes("Запустите программу заново.".encode("utf-8"), "text/plain; charset=utf-8", 403)
            html = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", "index.html"), encoding="utf-8").read()
            return self.send_bytes(html.replace("{{TOKEN}}", self.server.token).encode("utf-8"), "text/html; charset=utf-8", 200, [
                ("Set-Cookie", f"razbor={self.server.token}; HttpOnly; SameSite=Strict; Path=/"),
                ("Cache-Control", "no-store"),
                ("Content-Security-Policy", "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")])
        if url.path in ICONS:       # значок окна: в нём нет ничего личного, отдаём без ключа
            with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui", ICONS[url.path][0]), "rb") as f:
                return self.send_bytes(f.read(), ICONS[url.path][1], 200, [("Cache-Control", "max-age=86400")])
        if not self.cookie_ok():
            return self.send_json({"error": "нет доступа"}, 403)
        try:
            if url.path == "/thumb":
                return self.thumb(int(q["id"][0]))
            return self.send_json(self.get_api(url.path, q))
        except Exception as e:
            return self.failed(e)

    def do_POST(self):
        self.server.touch()
        # Отказ без чтения тела: соединение закрываем, иначе тело будет принято за начало следующего запроса.
        if not self.host_ok() or not secrets.compare_digest(self.headers.get("X-Token") or "", self.server.token):
            self.close_connection = True
            return self.send_json({"error": "нет доступа"}, 403)
        length = int(self.headers.get("Content-Length") or 0)
        if length > 32 * 2**20:      # файл разбора со старой страницы бывает в несколько мегабайт
            self.close_connection = True
            return self.send_json({"error": "слишком большой запрос"}, 413)
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return self.send_json(self.post_api(urlparse(self.path).path, data))
        except Exception as e:
            return self.failed(e)

    def failed(self, e):
        """Отключённый диск — не сбой программы: странице отвечаем 503 с пометкой disk, и она показывает окно ожидания."""
        app: App = self.server.app
        if isinstance(e, DiskUnavailable) or (app.root and (app.disk != "ok" or not app.root_present())):
            app.disk_lost()
            return self.send_json({"error": "Диск отключён.", "disk": app.disk}, 503)
        logging.exception("запрос %s", self.path)
        return self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    # ---------- данные ----------

    def get_api(self, path, q):
        app: App = self.server.app
        one = lambda k, d="": q.get(k, [d])[0]
        if path == "/api/state":
            return {**app.state(), "version": about.version_text()}
        if path == "/api/pick_folder":
            return self.server.picker.poll()
        if path == "/api/disk":
            return {"disk": app.check_disk(), "root": app.root}
        if not app.store:
            return {"error": "Папка не выбрана."}
        app.need_disk()
        if path == "/api/folders":
            sub = one("sub")
            base = os.path.join(app.root, sub) if sub else app.root
            names = [n for n in paths.browse(paths.long_path(app.root, sub) if sub else paths.long_path(app.root))
                     if n not in paths.MANAGED_DIRS]
            return {"sub": sub, "folders": names, "base": base}
        if path == "/api/report":
            return app.report()
        if path == "/api/plan":
            return {"rows": app.plan_rows(one("op", "layout"), one("action"), int(one("offset", "0")))}
        rv = Review(app.store)
        if path == "/api/review/summary":
            return rv.summary(one("mode", "all"))
        if path == "/api/review/group":
            return {"group": rv.group(int(one("pos", "0")), one("mode", "all"))}
        if path == "/api/review/empty":
            return {"groups": rv.all_removed()}
        if path == "/api/check/list":
            return Undated(app.store).page(max(0, int(one("offset", "0"))))
        if path == "/api/journals":
            return {"journals": moves.list_journals(app.root)}
        if path == "/api/finish":
            return app.finish_info()
        return {"error": "неизвестный запрос"}

    def post_api(self, path, d):
        app: App = self.server.app
        if path == "/api/ping":
            self.server.page_seen(str(d.get("page", "")))
            return {"ok": True}
        if path == "/api/bye":
            self.server.page_gone(str(d.get("page", "")))
            return {"ok": True}
        if path == "/api/tutorial/done":
            return app.finish_tutorial()
        if path == "/api/report_problem":
            report = about.save_report(app, str(d.get("note", "")))
            if d.get("reveal", True):
                subprocess.Popen(["explorer.exe", "/select,", report])
            return {"path": report}
        if path == "/api/client_error":
            # Ошибки самой страницы тоже в журнал: иначе о них никто не узнает.
            if self.server.client_errors < 50:
                self.server.client_errors += 1
                logging.error("страница: %s", str(d.get("message", ""))[:2000])
            return {"ok": True}
        if path == "/api/pick_folder":
            return self.server.picker.start()
        if path == "/api/open":
            return app.open_root(d.get("path", ""))
        if path == "/api/quit":
            threading.Thread(target=self.server.quit, daemon=True).start()
            return {"ok": True}
        if path == "/api/pause":
            return app.pause()
        if path == "/api/resume":
            return app.resume()
        if not app.store:
            return {"error": "Папка не выбрана."}
        app.need_disk()
        if path == "/api/settings":
            return app.save_settings(d)
        if path == "/api/analyze":
            return app.analyze()
        if path == "/api/cancel":
            return app.cancel()
        if path == "/api/apply/layout":
            return app.apply("layout")
        if path == "/api/rollback":
            return app.rollback(d.get("name", ""))
        rv = Review(app.store)
        if path == "/api/review/mark":
            rv.set_marks(d.get("gid"), [(int(a), bool(b)) for a, b in d.get("marks", [])])
            return {"ok": True}
        if path == "/api/review/import":
            if app.busy():
                return {"error": "Сейчас идёт работа. Загрузите разбор, когда она закончится."}
            return rv.import_marks(str(d.get("text", "")), app.root, write=bool(d.get("write")))
        if path == "/api/review/next":
            return {"pos": rv.next_unseen(int(d.get("pos", 0)), d.get("mode", "all"))}
        if path == "/api/review/plan":
            return rv.build_plan(app.root, app.settings())
        if path == "/api/review/apply":
            return app.apply("review")
        if path == "/api/check/set":
            try:
                return Undated(app.store).set_values([(int(i), v) for i, v in d.get("values", [])])
            except ValueError:
                return {"error": "Укажите год (например, 2019) или год и месяц."}
        if path == "/api/check/plan":
            return Undated(app.store).build_plan(app.root)
        if path == "/api/check/apply":
            return app.apply("check")
        if path == "/api/open_file":
            row = app.store.one("SELECT rel FROM files WHERE id=?", int(d.get("id", 0)))
            if row:
                os.startfile(os.path.join(app.root, row["rel"]))
            return {"ok": bool(row)}
        if path == "/api/reveal":
            # Только наши папки внутри выбранной: путь из запроса здесь не используется.
            name = {"drop": paths.DROP_DIR, "dups": paths.DUP_DIR, "check": paths.CHECK_DIR, "root": ""}.get(d.get("what"))
            if name is None:
                return {"error": "неизвестная папка"}
            target = os.path.join(app.root, name) if name else app.root
            if not os.path.isdir(target):
                return {"error": "Этой папки пока нет."}
            subprocess.Popen(["explorer.exe", target])
            return {"ok": True}
        return {"error": "неизвестный запрос"}

    def thumb(self, file_id):
        app: App = self.server.app
        if not app.root:
            return self.send_bytes(NO_THUMB.encode("utf-8"), "image/svg+xml", 404)
        out = app.thumb_path(file_id)
        if os.path.exists(out):     # готовая миниатюра лежит на компьютере: её можно показать и без диска
            with open(out, "rb") as f:
                return self.send_bytes(f.read(), "image/jpeg", 200, [("Cache-Control", "private, max-age=86400")])
        app.need_disk()
        row = app.store.one("SELECT rel FROM files WHERE id=? AND kind='photo'", file_id) if app.store else None
        if not row:
            return self.send_bytes(NO_THUMB.encode("utf-8"), "image/svg+xml", 404)
        if not os.path.exists(out):
            try:
                images.make_thumb(app.root, row["rel"], out)
            except Exception:
                return self.send_bytes(NO_THUMB.encode("utf-8"), "image/svg+xml", 200, [("Cache-Control", "no-store")])
        with open(out, "rb") as f:
            return self.send_bytes(f.read(), "image/jpeg", 200, [("Cache-Control", "private, max-age=86400")])


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.app = App()
        self.picker = Picker()
        self.client_errors = 0
        self.token = secrets.token_urlsafe(24)
        self.last_touch = time.time() + 120     # фора на запуск: окно ещё открывается
        self.last_request = time.time()
        self.pages = {}             # открытые окна программы: номер → когда отзывалось
        self.closed_at = 0          # когда закрыли последнее окно
        self.lock = threading.Lock()

    def touch(self):
        self.last_request = time.time()
        self.last_touch = max(self.last_touch, self.last_request)

    def page_seen(self, page):
        if page:
            with self.lock:
                self.pages[page] = time.time()

    def page_gone(self, page):
        with self.lock:
            self.pages.pop(page, None)
            if not self.pages:
                self.closed_at = time.time()

    def should_quit(self, now):
        """Закрыли последнее окно (и за CLOSE_GRACE_SECONDS не открылось новое) или окна давно молчат."""
        if self.app.busy() or self.picker.is_open():
            return False
        with self.lock:
            for page, seen in list(self.pages.items()):
                if now - seen > IDLE_LIMIT_SECONDS:
                    del self.pages[page]
            closed = self.closed_at and not self.pages and now - self.last_request > CLOSE_GRACE_SECONDS
        return closed or now - self.last_touch > IDLE_LIMIT_SECONDS

    def url(self):
        return f"http://127.0.0.1:{self.server_port}/?t={self.token}"

    def quit(self):
        time.sleep(0.2)
        instance = os.path.join(paths.app_data_dir(), INSTANCE_FILE)
        try:
            os.remove(instance)
        except OSError:
            pass
        os._exit(0)

    def watch_idle(self):
        n = 0
        while True:
            time.sleep(1)
            n += 1
            if n % 2 == 0:
                try:
                    self.app.check_disk()   # отключённый диск замечаем сами, даже если окно ничего не спрашивает
                except Exception:
                    logging.exception("проверка диска")
            if self.should_quit(time.time()):
                self.quit()


def hold_mutex():
    if os.name != "nt":
        return None
    import ctypes
    return ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)


def running_instance():
    """Уже запущенная копия: повторный запуск только открывает её окно."""
    path = os.path.join(paths.app_data_dir(), INSTANCE_FILE)
    try:
        info = json.load(open(path, encoding="utf-8"))
        with socket.create_connection(("127.0.0.1", info["port"]), timeout=1):
            return info["url"]
    except (OSError, ValueError, KeyError):
        return None


def trim_thumbs():
    """Самые старые миниатюры удаляются, когда их становится слишком много. Это только наш кэш, фото не затрагиваются."""
    folder = os.path.join(paths.app_data_dir(), "миниатюры")
    items = []
    for d, _, names in os.walk(folder):
        for n in names:
            p = os.path.join(d, n)
            try:
                st = os.stat(p)
            except OSError:
                continue
            items.append((st.st_atime, st.st_size, p))
    total = sum(x[1] for x in items)
    for _, size, p in sorted(items):
        if total <= THUMB_LIMIT_BYTES:
            break
        try:
            os.remove(p)
            total -= size
        except OSError:
            pass


def serve(open_browser=True, port=0):
    url = running_instance()
    if url:
        if open_browser:
            open_window(url)
        return None
    srv = Server(port)
    srv.mutex = hold_mutex()
    with open(os.path.join(paths.app_data_dir(), INSTANCE_FILE), "w", encoding="utf-8") as f:
        json.dump({"port": srv.server_port, "url": srv.url()}, f)
    threading.Thread(target=trim_thumbs, daemon=True).start()
    threading.Thread(target=srv.watch_idle, daemon=True).start()
    if open_browser:
        threading.Timer(0.3, open_window, args=[srv.url()]).start()
    srv.serve_forever()
    return srv
