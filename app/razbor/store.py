"""База анализа в служебной папке рядом с фото. Файл узнаётся по id: id не меняется при перемещениях,
поэтому пометки и связи дублей переживают раскладку и откат."""
import json
import os
import sqlite3
import threading

from .paths import service_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, rel TEXT UNIQUE NOT NULL, size INTEGER, mtime_ns INTEGER, kind TEXT, meta TEXT,
  partial TEXT, sha TEXT, ph TEXT, dh TEXT, w INTEGER, h INTEGER, hist BLOB, white REAL, printed INTEGER DEFAULT 0,
  date TEXT, date_src TEXT, conflict TEXT, screenshot INTEGER DEFAULT 0, scan INTEGER DEFAULT 0, live_id TEXT,
  has_exif INTEGER DEFAULT 0, theme INTEGER DEFAULT 0, dup_kind TEXT, dup_of INTEGER);
CREATE TABLE IF NOT EXISTS plan(op TEXT, rel TEXT, dst TEXT, action TEXT, date TEXT, size INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS plan_op ON plan(op);
CREATE TABLE IF NOT EXISTS marks(id INTEGER PRIMARY KEY, keep INTEGER);
CREATE TABLE IF NOT EXISTS seen(gid TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS imported(id INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS check_dates(id INTEGER PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, root):
        self.root = root
        self.lock = threading.RLock()
        self.db = sqlite3.connect(os.path.join(service_dir(root), "база.sqlite"), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.groups_cache = {}
        self.db.executescript(SCHEMA)
        self.db.commit()

    def rows(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def one(self, sql, *args):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def run(self, sql, *args):
        with self.lock:
            self.db.execute(sql, args)
            self.db.commit()

    def many(self, sql, seq):
        with self.lock:
            self.db.executemany(sql, seq)
            self.db.commit()

    def get(self, key, default=None):
        row = self.one("SELECT v FROM kv WHERE k=?", key)
        return json.loads(row["v"]) if row else default

    def put(self, key, value):
        self.run("INSERT OR REPLACE INTO kv VALUES(?,?)", key, json.dumps(value, ensure_ascii=False))

    def close(self):
        """Отпустить файл базы. Если диск уже пропал, закрытие тоже может споткнуться: это не страшно."""
        with self.lock:
            try:
                self.db.close()
            except sqlite3.Error:
                pass
