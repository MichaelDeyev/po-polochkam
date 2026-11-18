"""Состояние программы и фоновые задачи: одна задача за раз, прогресс и остановка."""
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time

from . import analysis, moves, planner, review
from .paths import CHECK_DIR, DROP_DIR, DUP_DIR, app_data_dir, check_root, service_dir
from .store import Store

RECENT_FILE = "недавние.json"
TUTORIAL_FILE = "обучение пройдено.json"
MAX_RECENT = 6
DISK_GONE_TEXT = "Диск или папка больше не доступны. Подключите диск и запустите снова: работа продолжится с того же места."
PAUSE_WAIT_SECONDS = 20     # столько ждём, пока остановится идущая работа, прежде чем отпустить диск


class DiskUnavailable(Exception):
    """Выбранная папка пропала (отключили диск) или стоит пауза: базу трогать нельзя."""


class Job:
    def __init__(self, name):
        self.name, self.status = name, "running"
        self.stage, self.done, self.total = "Подготовка", 0, 0
        self.stage_started = time.time()
        self.started = time.time()
        self.error, self.result = None, None
        self._cancel = threading.Event()

    def cancelled(self):
        return self._cancel.is_set()

    def cancel(self):
        self._cancel.set()

    def progress(self, stage, done, total):
        if stage != self.stage:
            self.stage, self.stage_started = stage, time.time()
        self.done, self.total = done, total

    def view(self):
        spent = time.time() - self.stage_started
        eta = spent * (self.total - self.done) / self.done if self.total and self.done > 0 and spent > 3 else None
        return {"name": self.name, "status": self.status, "stage": self.stage, "done": self.done, "total": self.total,
                "eta": eta, "error": self.error, "result": self.result, "cancelling": self.cancelled()}


class App:
    def __init__(self):
        self.root, self.store, self.job = None, None, None
        self.lock = threading.Lock()
        self.disk_lock = threading.RLock()
        self.disk = "ok"            # ok — папка на месте; gone — пропала, ждём; paused — пауза, база отпущена
        self.pending_rel = []

    # ---------- папка и настройки ----------

    def recent(self):
        path = os.path.join(app_data_dir(), RECENT_FILE)
        try:
            items = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [p for p in items if isinstance(p, str)]

    def tutorial_done(self):
        return os.path.isfile(os.path.join(app_data_dir(), TUTORIAL_FILE))

    def finish_tutorial(self):
        with open(os.path.join(app_data_dir(), TUTORIAL_FILE), "w", encoding="utf-8") as f:
            json.dump({"time": time.strftime("%d.%m.%Y %H:%M")}, f)
        return {"ok": True}

    def open_root(self, path):
        if self.busy():
            return {"error": "Сначала дождитесь окончания или остановите текущую работу."}
        reason, warnings = check_root(path)
        if reason:
            return {"error": reason}
        with self.disk_lock:
            if self.store:
                self.store.close()
            self.root = os.path.abspath(path)
            self.store = Store(self.root)
            self.disk = "ok"
            self.pending_rel = []
        items = [self.root] + [p for p in self.recent() if p != self.root]
        with open(os.path.join(app_data_dir(), RECENT_FILE), "w", encoding="utf-8") as f:
            json.dump(items[:MAX_RECENT], f, ensure_ascii=False)
        return {"root": self.root, "warnings": warnings}

    def settings(self):
        return {**planner.DEFAULTS, **(self.store.get("settings", {}) if self.store else {})}

    def save_settings(self, values):
        clean = {k: values[k] for k in planner.DEFAULTS if k in values}
        clean["keep_folders"] = [f.strip("\\") for f in clean.get("keep_folders", []) if isinstance(f, str) and f.strip("\\")]
        self.store.put("settings", clean)
        self.store.put("analysis", None)
        return self.settings()

    # ---------- диск ----------

    def root_present(self):
        return bool(self.root) and os.path.isdir(service_dir(self.root))

    def need_disk(self):
        """Перед любым обращением к базе: при паузе или пропавшем диске — отказ, а не зависание."""
        if self.disk != "ok":
            raise DiskUnavailable
        if self.root and not self.root_present():
            self.disk_lost()
            raise DiskUnavailable

    def disk_lost(self):
        with self.disk_lock:
            if self.disk == "ok":
                logging.warning("папка %s пропала: диск отключён", self.root)
                self.disk = "gone"
            if self.store:
                self.store.close()

    def check_disk(self):
        """Вызывается раз в пару секунд: заметить пропажу диска и его возвращение."""
        if not self.root or self.disk == "paused":
            return self.disk
        present = self.root_present()
        if self.disk == "ok" and not present:
            self.disk_lost()
        elif self.disk == "gone" and present:
            self._reopen()
        return self.disk

    def _reopen(self):
        with self.disk_lock:
            try:
                store = Store(self.root)
            except (sqlite3.Error, OSError):
                return False        # диск ещё не готов: попробуем в следующий раз
            self.store, self.disk = store, "ok"
            logging.warning("папка %s снова доступна", self.root)
            if self.pending_rel:    # перемещения, которые не успели записать в базу перед обрывом
                try:
                    self._flush_moved()
                except (sqlite3.Error, OSError):
                    logging.exception("не удалось дописать перемещения после возвращения диска")
            return True

    def pause(self):
        """Сохранить всё и отпустить диск: после этого его можно безопасно извлечь."""
        if self.disk != "ok":
            return {"disk": self.disk}
        if self.busy():
            self.job.cancel()
            end = time.time() + PAUSE_WAIT_SECONDS
            while self.busy() and time.time() < end:
                time.sleep(0.2)
            if self.busy():
                return {"error": "Работа ещё останавливается. Попробуйте через минуту."}
        with self.disk_lock:
            self.disk = "paused"
            if self.store:
                self.store.close()
        return {"disk": "paused"}

    def resume(self):
        if self.disk == "ok":
            return {"disk": "ok"}
        if not self.root_present():
            return {"disk": self.disk, "error": "Папка пока не видна. Подключите диск и нажмите «Продолжить» ещё раз."}
        if not self._reopen():
            return {"disk": self.disk, "error": "Диск подключён, но ещё не готов. Подождите немного и нажмите «Продолжить» ещё раз."}
        return {"disk": "ok"}

    def state(self):
        out = {"root": self.root, "job": self.job.view() if self.job else None, "recent": self.recent(),
               "tutorial_done": self.tutorial_done(), "disk": self.disk}
        if self.store and self.check_disk() == "ok":
            try:
                out.update(settings=self.settings(), analysis=self.store.get("analysis"),
                           layout_moves=self.store.one("SELECT COUNT(*) n FROM plan WHERE op='layout'")["n"],
                           undated=self.store.one("SELECT COUNT(*) n FROM files WHERE rel LIKE ? ESCAPE '!' AND kind IN ('photo','video')",
                                                  f"!{CHECK_DIR}\\%")["n"],
                           **self.review_progress(),
                           open_layout=bool(self.store.get("open_layout")), open_review=bool(self.store.get("open_review")))
            except (sqlite3.Error, OSError):
                if self.root_present():
                    raise
                self.disk_lost()        # диск пропал прямо во время запроса
                out["disk"] = self.disk
        return out

    def review_progress(self):
        """Для подсказки «Следующий шаг»: сколько групп не просмотрено и сколько пометок ещё не применено.
        Пока идёт работа, не считаем: файлы переезжают, а страница спрашивает состояние каждую секунду."""
        if self.busy() or not self.store.get("analysis"):
            return {}
        rv = review.Review(self.store)
        s = rv.summary("all")
        return {"review_unseen": s["groups"] - s["seen"], "review_pending": rv.pending()}

    # ---------- фоновые задачи ----------

    def busy(self):
        return self.job is not None and self.job.status == "running"

    def start(self, name, work):
        with self.lock:
            if self.busy():
                return {"error": "Уже идёт другая работа."}
            job = self.job = Job(name)

        def runner():
            try:
                job.result = work(job)
                job.status = "done"
            except analysis.Cancelled:
                job.status = "cancelled"
            except (moves.DiskGone, DiskUnavailable):
                job.status, job.error = "failed", DISK_GONE_TEXT
                self.disk_lost()
            except Exception as e:
                if self.disk != "ok" or not self.root_present():     # база закрыта из-за отключения диска
                    job.status, job.error = "failed", DISK_GONE_TEXT
                    self.disk_lost()
                    return
                logging.exception("сбой задачи %s", name)
                job.status = "failed"
                job.error = job.error or f"{type(e).__name__}: {e}"

        threading.Thread(target=runner, daemon=True).start()
        return {"started": name}

    def _moved(self, src, dst, mtime):
        self.pending_rel.append((dst, mtime, src))
        if len(self.pending_rel) >= 200:
            self._flush_moved()

    def _flush_moved(self):
        if not self.pending_rel:
            return
        self.store.many("UPDATE OR REPLACE files SET rel=?, mtime_ns=? WHERE rel=?", self.pending_rel)
        self.pending_rel = []
        self.store.groups_cache.clear()

    def analyze(self):
        root, store, settings = self.root, self.store, self.settings()

        def work(job):
            store.put("open_layout", None)
            found, cloud, errors = analysis.walk(root, job)
            analysis.sync_files(store, found)
            job.progress("Читаем даты из файлов", 0, 0)
            analysis.read_meta(store, root, job)
            job.progress("Ищем точные копии", 0, 0)
            analysis.hash_files(store, root, job)
            job.progress("Сравниваем снимки: рассматриваем каждое фото", 0, 0)
            analysis.compute_prints(store, root, job)
            job.progress("Ищем одинаковые снимки", 0, 0)
            transfer = analysis.describe(store, settings)
            exact = analysis.find_exact(store)
            similar, recovered = analysis.find_same_pictures(store, job)
            job.progress("Собираем группы похожих", 0, 0)
            groups = review.build_groups(store, job)
            moved = planner.build_layout(store, root, settings)
            info = {"files": len(found), "cloud": cloud, "errors": errors, "transfer_days": transfer, "exact": exact,
                    "similar": similar, "recovered": recovered, "groups": groups, "moves": moved,
                    "time": time.strftime("%d.%m.%Y %H:%M")}
            store.put("analysis", info)
            return info

        return self.start("analyze", work)

    def report(self):
        return planner.report(self.store, self.root)

    def plan_rows(self, op, action, offset, limit=200):
        rows = self.store.rows("SELECT rel, dst, action, note, date FROM plan WHERE op=? AND action=? ORDER BY rel LIMIT ? OFFSET ?",
                               op, action, limit, offset)
        return [dict(r) for r in rows]

    def apply(self, op):
        """op: 'layout' — раскладка, 'review' — итоги разбора. Незавершённая операция продолжается по своему журналу."""
        root, store, settings = self.root, self.store, self.settings()
        title = {"layout": "Раскладка по папкам", "review": "Итоги разбора похожих", "check": "Даты для файлов из «_Проверить»"}[op]
        key = f"open_{op}"

        def work(job):
            rows = store.rows("SELECT * FROM plan WHERE op=? ORDER BY rel", op)
            journal = store.get(key)
            if journal and not os.path.isfile(journal):
                journal = None
            done = {e["src"] for e in moves.read_journal(journal) if e["a"] == "done"} if journal else set()
            job.progress("Проверяем файлы перед перемещением", 0, 0)
            problems = moves.preflight(root, rows, done)
            if problems:
                job.error = ("Файлы изменились после анализа, поэтому ничего не перемещено. Запустите анализ заново.\n"
                             + "\n".join(problems[:5]))
                raise RuntimeError(job.error)
            if not rows:
                return {"moved": 0, "failed": []}
            if not journal:
                journal = moves.new_journal(root, op, title, len(rows))
                store.put(key, journal)
            try:
                moved, failed = moves.run_moves(root, rows, journal, job, settings["fix_file_dates"], self._moved)
            finally:
                self._flush_moved()
            if not failed:
                store.put(key, None)
                store.run("DELETE FROM plan WHERE op=?", op)
                planner.build_layout(store, root, settings)
            return {"moved": moved + len(done), "failed": failed[:30], "failed_count": len(failed)}

        return self.start(op, work)

    def rollback(self, name):
        root, store, settings = self.root, self.store, self.settings()
        target = next((j for j in moves.list_journals(root) if j["name"] == name), None)
        if not target or not target["can_rollback"]:
            return {"error": "Отменить можно только последнюю выполненную операцию."}

        def work(job):
            try:
                res = moves.rollback(root, os.path.join(moves.journal_dir(root), name), job, self._moved)
            finally:
                self._flush_moved()
            store.put(f"open_{target['kind']}", None)
            store.run("DELETE FROM plan WHERE op='review'")
            planner.build_layout(store, root, settings)
            return res

        return self.start("rollback", work)

    def cancel(self):
        if self.job:
            self.job.cancel()
        return {"ok": True}

    # ---------- итог ----------

    def finish_info(self):
        return {"drop": review.folder_stats(self.root, DROP_DIR), "dups": review.folder_stats(self.root, DUP_DIR),
                "service": service_dir(self.root)}

    def thumb_path(self, file_id):
        key = hashlib.sha1(self.root.lower().encode("utf-8")).hexdigest()[:12]
        folder = os.path.join(app_data_dir(), "миниатюры", key)
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, f"{file_id}.jpg")
