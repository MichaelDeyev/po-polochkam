"""Анализ папки. Файлы с фото не меняются: всё пишется в служебную базу. Повторный запуск берёт готовое из базы."""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

import numpy as np

from .dates import STRONG, TAGS, WEAK, parse_dt, pick_date, tag
from .images import NO_WINDOW, fingerprint
from .paths import DROP_DIR, DUP_DIR, SKIP_DIRS, exiftool_path, long_path

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
PHOTO_EXT = {".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".tif", ".tiff", ".bmp", ".webp",
             ".dng", ".cr2", ".nef", ".arw"}
VIDEO_EXT = {".mov", ".mp4", ".avi", ".3gp", ".m4v", ".mkv", ".mts", ".wmv"}
SCREEN_DIRS = {"скрин", "скрины", "скриншоты", "screenshots"}
SCANNER_WORDS = ("NORITSU", "FRONTIER", "SCANNER", "PERFECTION", "CANOSCAN", "PLUSTEK", "EPSCAN")
CLOUD_ONLY = 0x00400000 | 0x00040000 | 0x1000   # файл не скачан: RECALL_ON_DATA_ACCESS, RECALL_ON_OPEN, OFFLINE
SAME_P, SAME_D = 4, 6          # пороги расстояния Хэмминга для «та же картинка»
MAX_SAME_GROUP = 8             # большая группа — скорее серия, склеенная через цепочку похожих
EXIF_BATCH = 300
SEQ = re.compile(r"^(IMG_|IMG|DSC_|DSC|DSCN|P|LRK|SBL)E?(\d{3,})", re.I)


class Cancelled(Exception):
    pass


def kind_of(name):
    ext = os.path.splitext(name)[1].lower()
    if name.startswith("._") or name.lower() in JUNK_NAMES:
        return "junk"
    return "photo" if ext in PHOTO_EXT else "video" if ext in VIDEO_EXT else "other"


def top_of(rel):
    return rel.split("\\", 1)[0]


def walk(root, ctx):
    """Файлы папки. Чужие для анализа: служебные, недоступные и не скачанные из облака."""
    found, skipped_cloud, errors = {}, 0, 0
    stack = [""]
    while stack:
        sub = stack.pop()
        if ctx.cancelled():
            raise Cancelled
        try:
            with os.scandir(long_path(root, sub) if sub else long_path(root)) as it:
                entries = list(it)
        except OSError:
            errors += 1
            continue
        for e in entries:
            rel = os.path.join(sub, e.name) if sub else e.name
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in SKIP_DIRS:
                        stack.append(rel)
                elif e.is_file(follow_symlinks=False):
                    st = e.stat(follow_symlinks=False)
                    if st.st_file_attributes & CLOUD_ONLY:
                        skipped_cloud += 1
                    else:
                        found[rel] = (st.st_size, st.st_mtime_ns)
                        if len(found) % 2000 == 0:
                            ctx.progress("Ищем файлы", len(found), 0)
            except OSError:
                errors += 1
    return found, skipped_cloud, errors


def sync_files(store, found):
    """База приводится к диску: новые файлы добавляются, изменённые считаются заново, пропавшие удаляются."""
    known = {r["rel"]: r for r in store.rows("SELECT id, rel, size, mtime_ns FROM files")}
    with store.lock:
        for rel, r in known.items():
            if rel not in found and top_of(rel) != DROP_DIR:
                store.db.execute("DELETE FROM files WHERE id=?", (r["id"],))
        for rel, (size, mtime) in found.items():
            r = known.get(rel)
            if r is None:
                store.db.execute("INSERT INTO files(rel, size, mtime_ns, kind) VALUES(?,?,?,?)",
                                 (rel, size, mtime, kind_of(os.path.basename(rel))))
            elif r["size"] != size:
                store.db.execute("UPDATE files SET size=?, mtime_ns=?, meta=NULL, partial=NULL, sha=NULL, printed=0, ph=NULL, "
                                 "dh=NULL, hist=NULL WHERE id=?", (size, mtime, r["id"]))
            elif r["mtime_ns"] != mtime:
                store.db.execute("UPDATE files SET mtime_ns=? WHERE id=?", (mtime, r["id"]))
        store.db.commit()


def read_meta_batch(root, batch):
    """Часть файлов через exiftool. Сбой одной пачки не должен останавливать анализ."""
    tags = [f"-{t}" for t in TAGS]
    args_file = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as f:
            args_file = f.name
            f.write("\n".join("./" + r.replace("\\", "/") for r in batch))
        out = subprocess.run([exiftool_path(), "-charset", "filename=utf8", "-json", "-G1", "-api", "QuickTimeUTC",
                              "-api", "WindowsLongPath=1", *tags, "-@", args_file], cwd=os.path.abspath(root), capture_output=True,
                             stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=900).stdout
        res = {}
        for m in json.loads(out.decode("utf-8", "replace") or "[]"):
            rel = m.pop("SourceFile", "")
            res[rel[2:].replace("/", "\\") if rel.startswith("./") else rel.replace("/", "\\")] = m
        return res
    except (ValueError, OSError, subprocess.SubprocessError):
        return {}
    finally:
        if args_file:
            os.remove(args_file)


def read_meta(store, root, ctx):
    todo = store.rows("SELECT id, rel FROM files WHERE meta IS NULL AND kind IN ('photo','video')")
    if not todo:
        return
    subprocess.run([exiftool_path(), "-ver"], capture_output=True, check=True, stdin=subprocess.DEVNULL,
                   creationflags=NO_WINDOW, timeout=120)
    ids = {r["rel"]: r["id"] for r in todo}
    rels = [r["rel"] for r in todo]
    batches = [rels[i:i + EXIF_BATCH] for i in range(0, len(rels), EXIF_BATCH)]
    done = 0
    with ThreadPoolExecutor(max(2, min(6, (os.cpu_count() or 2) - 1))) as tp:
        for batch, res in zip(batches, tp.map(lambda b: read_meta_batch(root, b), batches)):
            store.many("UPDATE files SET meta=? WHERE id=?",
                       [(json.dumps(res.get(rel, {}), ensure_ascii=False), ids[rel]) for rel in batch])
            done += len(batch)
            ctx.progress("Читаем даты из файлов", done, len(rels))
            if ctx.cancelled():
                tp.shutdown(wait=False, cancel_futures=True)
                raise Cancelled


def _digest(path, size, partial):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        if partial and size > 2 * 1024 * 1024:
            h.update(f.read(1024 * 1024))
            f.seek(-1024 * 1024, os.SEEK_END)
            h.update(f.read(1024 * 1024))
        else:
            for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
                h.update(chunk)
    return h.hexdigest()


def hash_files(store, root, ctx):
    """Полный хеш считается только там, где совпали размер и начало с концом файла."""
    by_size = defaultdict(list)
    for r in store.rows("SELECT id, rel, size, partial, sha FROM files WHERE kind IN ('photo','video') AND size>0"):
        by_size[r["size"]].append(r)
    groups = [g for g in by_size.values() if len(g) > 1]
    total, done = sum(len(g) for g in groups), 0
    parts_done, shas_done = [], []

    def flush():
        store.many("UPDATE files SET partial=? WHERE id=?", parts_done)
        store.many("UPDATE files SET sha=? WHERE id=?", shas_done)
        parts_done.clear()
        shas_done.clear()

    for g in groups:
        parts = defaultdict(list)
        for r in g:
            if ctx.cancelled():
                flush()
                raise Cancelled
            value = r["partial"]
            try:
                if not value:
                    value = _digest(long_path(root, r["rel"]), r["size"], True)
                    parts_done.append((value, r["id"]))
            except OSError:
                continue
            parts[value].append(r)
            done += 1
            if done % 50 == 0:
                ctx.progress("Ищем точные копии", done, total)
                flush()
        for same in parts.values():
            if len(same) < 2:
                continue
            for r in same:
                if ctx.cancelled():
                    flush()
                    raise Cancelled
                if not r["sha"]:
                    try:
                        shas_done.append((_digest(long_path(root, r["rel"]), r["size"], False), r["id"]))
                    except OSError:
                        pass
    flush()
    ctx.progress("Ищем точные копии", total, total)


def compute_prints(store, root, ctx):
    todo = store.rows("SELECT id, rel FROM files WHERE kind='photo' AND printed=0")
    if not todo:
        return
    workers = max(2, (os.cpu_count() or 2) - 1)
    batch = []
    with Pool(workers) as pool:
        for i, res in enumerate(pool.imap_unordered(fingerprint, [(r["id"], root, r["rel"]) for r in todo], chunksize=8), 1):
            batch.append(res)
            if len(batch) >= 200:
                _save_prints(store, batch)
                batch = []
            if i % 50 == 0 or i == len(todo):
                ctx.progress("Сравниваем снимки: рассматриваем каждое фото", i, len(todo))
            if ctx.cancelled():
                _save_prints(store, batch)
                pool.terminate()
                raise Cancelled
    _save_prints(store, batch)


def _save_prints(store, batch):
    store.many("UPDATE files SET ph=?, dh=?, w=?, h=?, hist=?, white=?, printed=1 WHERE id=?",
               [(p, d, w, h, blob, white, fid) for fid, p, d, w, h, blob, white in batch])


def _is_screenshot(rel, name, ext, make, m):
    parts = {p.lower() for p in rel.split("\\")[:-1]}
    return (str(tag(m, "ExifIFD:UserComment", "UserComment") or "").lower() == "screenshot"
            or re.search(r"screen ?shot|скрин|снимок экрана", name, re.I) is not None
            or bool(parts & SCREEN_DIRS) or (ext == ".png" and not make))


def in_folder(rel, folder):
    return rel == folder or rel.startswith(folder + "\\")


def describe(store, settings):
    """Даты и признаки из метаданных. Дни переноса считаются по датам файлов всей папки."""
    rows = store.rows("SELECT id, rel, kind, meta, mtime_ns FROM files WHERE kind IN ('photo','video')")
    day = lambda r: dt.datetime.fromtimestamp(r["mtime_ns"] / 1e9).date()
    counts = Counter(day(r) for r in rows)
    bar = max(50, min(300, len(rows) // 5))
    transfer = {d for d, n in counts.items() if n >= bar}
    keep = settings["keep_folders"]
    out = []
    for r in rows:
        m = json.loads(r["meta"] or "{}")
        name = os.path.basename(r["rel"])
        ext = os.path.splitext(name)[1].lower()
        make = str(tag(m, "IFD0:Make", "Make") or "")
        date, src, conflict = pick_date(m, name, transfer)
        out.append((date.strftime("%Y-%m-%d %H:%M:%S") if date else None, src, conflict,
                    int(_is_screenshot(r["rel"], name, ext, make, m)),
                    int(any(w in make.upper() for w in SCANNER_WORDS)),
                    tag(m, "Apple:ContentIdentifier", "Keys:ContentIdentifier", "ContentIdentifier"),
                    int(bool(parse_dt(tag(m, "ExifIFD:DateTimeOriginal", "Keys:CreationDate")))),
                    int(any(in_folder(r["rel"], k) for k in keep)), r["id"]))
    store.many("UPDATE files SET date=?, date_src=?, conflict=?, screenshot=?, scan=?, live_id=?, has_exif=?, theme=?, "
               "dup_kind=NULL, dup_of=NULL WHERE id=?", out)
    return len(transfer)


def find_exact(store):
    """Точные копии. Одно фото в двух папках «не трогать» остаётся в обеих, а копия рядом с собой — лишняя."""
    groups = defaultdict(list)
    for r in store.rows("SELECT id, rel, theme, has_exif, sha FROM files WHERE sha IS NOT NULL AND kind IN ('photo','video')"):
        groups[r["sha"]].append(r)
    updates = []
    for g in groups.values():
        if len(g) < 2:
            continue
        g.sort(key=lambda r: (top_of(r["rel"]) == DUP_DIR, not r["theme"], not r["has_exif"],
                              re.search(r" \(\d+\)\.\w+$", r["rel"]) is not None, len(r["rel"]), r["rel"]))
        keeper = g[0]
        for r in g[1:]:
            # Внутри папки «не трогать» подпапки бывают подборками: копию в другой подпапке не трогаем.
            if r["theme"] and os.path.dirname(r["rel"]) != os.path.dirname(keeper["rel"]):
                continue
            updates.append(("exact", keeper["id"], r["id"]))
    store.many("UPDATE files SET dup_kind=?, dup_of=? WHERE id=?", updates)
    return len(updates)


class _DSU:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def to_ts(s):
    return dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp() if s else np.nan


def same_picture(a, b, p, d, ta, tb):
    """Близкие отпечатки — ещё не копия: кадры серии и скриншоты тоже близки."""
    if p > SAME_P or d > SAME_D:
        return False
    same_size = {a["w"], a["h"]} == {b["w"], b["h"]}
    # IMG_6601 и IMG_6602 одного размера — соседние кадры одной камеры, даже если снято в одну секунду.
    # (IMG_E7716 и IMG_7716 — правка и оригинал, у них номер один.)
    na, nb = SEQ.match(os.path.basename(a["rel"])), SEQ.match(os.path.basename(b["rel"]))
    if same_size and na and nb and na[1].upper() == nb[1].upper() and na[2] != nb[2]:
        return False
    if np.isnan(ta) and np.isnan(tb):
        # Без дат и одного размера не отличить копию от соседнего кадра — только почти полное совпадение.
        if same_size and (p > 1 or d > 2):
            return False
    elif not (np.isnan(ta) or np.isnan(tb)):
        delta = abs(ta - tb)
        # Снято в разное время в пределах суток при том же размере — разные кадры.
        # Исключение — сдвиг на целое число часов: та же съёмка в другом часовом поясе.
        hours_shift = delta >= 3600 and min(delta % 3600, 3600 - delta % 3600) <= 2
        if 0 < delta <= 86400 and same_size and not hours_shift:
            return False
        # Одна секунда и один размер: копия или кадр серийной съёмки — нужно почти полное совпадение.
        if delta == 0 and same_size and (p > 2 or d > 3):
            return False
    if a["screenshot"] or b["screenshot"]:
        # Экраны одного приложения похожи при разном тексте — нужно почти полное совпадение.
        stem = lambda r: os.path.splitext(os.path.basename(r["rel"]))[0].lower()
        return stem(a) == stem(b) or (p <= 1 and d <= 2)
    return True


def find_same_pictures(store, ctx):
    """Одна и та же картинка: пережатая, уменьшенная, правка и оригинал. Лишние — в похожие дубли, даты — от копий."""
    items = store.rows("SELECT id, rel, ph, dh, w, h, size, date, date_src, screenshot, theme, dup_kind FROM files "
                       "WHERE kind='photo' AND ph IS NOT NULL")
    n = len(items)
    if n < 2:
        return 0, 0
    ph = np.array([int(r["ph"], 16) for r in items], dtype=np.uint64)
    dh = np.array([int(r["dh"], 16) for r in items], dtype=np.uint64)
    ts = np.array([to_ts(r["date"]) for r in items])
    dsu = _DSU(n)
    for i in range(n - 1):
        dp = np.bitwise_count(ph[i + 1:] ^ ph[i])
        cand = np.nonzero(dp <= SAME_P)[0]
        if len(cand):
            dd = np.bitwise_count(dh[i + 1 + cand] ^ dh[i])
            for j, p, d in zip(cand + i + 1, dp[cand], dd):
                if same_picture(items[i], items[int(j)], int(p), int(d), ts[i], ts[int(j)]):
                    dsu.union(i, int(j))
        if i % 500 == 0:
            ctx.progress("Ищем одинаковые снимки", i, n)
            if ctx.cancelled():
                raise Cancelled
    groups = defaultdict(list)
    for i in range(n):
        groups[dsu.find(i)].append(i)
    date_fixes, dups, recovered = [], [], 0
    for idx in groups.values():
        if len(idx) < 2 or len(idx) > MAX_SAME_GROUP:
            continue
        g = [items[i] for i in idx]
        dated = [r["date"] for r in g if r["date"] and not (r["date_src"] or "").startswith(WEAK)]
        for r in g:
            if dated and (not r["date"] or (r["date_src"] or "").startswith(WEAK)):
                date_fixes.append((min(dated), "дата копии этого фото", r["id"]))
                recovered += 1
        # Лучший вариант: больше разрешение, есть дата съёмки, папка «не трогать», больше файл.
        g.sort(key=lambda r: (top_of(r["rel"]) == DUP_DIR, -((r["w"] or 0) * (r["h"] or 0)), not (r["date_src"] or "").startswith(STRONG),
                              not r["theme"], -r["size"], r["rel"]))
        keeper = next((r for r in g if not r["dup_kind"]), None)
        for r in g:
            # В папках «не трогать» убираются только точные копии.
            if keeper and r["id"] != keeper["id"] and not r["dup_kind"] and not r["theme"]:
                dups.append(("similar", keeper["id"], r["id"]))
    store.many("UPDATE files SET date=?, date_src=? WHERE id=?", date_fixes)
    store.many("UPDATE files SET dup_kind=?, dup_of=? WHERE id=?", dups)
    return len(dups), recovered


def normalize_key(rel):
    return unicodedata.normalize("NFC", rel).replace("/", "\\").lower()
