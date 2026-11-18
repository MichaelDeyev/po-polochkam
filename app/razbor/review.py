"""Группы похожих кадров для разбора глазами и применение пометок.

Группа — все похожие кадры одной съёмки: одна и та же картинка, соседние по времени кадры и кадры той же сцены
(близкие по времени и цвету, поза может отличаться). Группы идут по дате.
"""
import hashlib
import os
import re
from collections import defaultdict

import numpy as np

from .analysis import Cancelled, _DSU, normalize_key, to_ts, top_of
from .images import hist_array
from .paths import DROP_DIR, DUP_DIR, JUNK_DIR, long_path
from .planner import Names, live_photo_ids, target

LINK_P, LINK_SECONDS = 12, 1800     # похожие кадры с разницей до получаса — одна съёмка
LINK_P_UNDATED = 8                  # без дат — только очень похожие
LINK_P_ANY_DATE = 10                # даты бывают испорчены: почти одинаковые связываем при любой разнице
SCENE_SIM = 0.65                    # насколько совпадают цвета кадров одной сцены
SCENE_GAP, SCENE_SPAN = 3 * 3600, 6 * 3600     # разрыв между частями сцены и вся её длина
SCENE_CAP = 60                      # больше фото на один экран не собираем
NEAR_SECONDS = 600                  # соседние кадры: та же сцена, другая поза
WHITE_SCREEN = 0.5                  # доля белого, выше которой это экран с текстом, а не снимок
# Айфон хранит правку отдельным файлом: IMG_E0059 рядом с оригиналом IMG_0059. Приставка даты и номер копии не мешают.
IPHONE_NAME = re.compile(r"^(?:\d{4}-\d\d-\d\d_\d{6}_)?IMG_(E?)(\d{4})(?: ?\(\d+\)|_\d+)?\.[A-Za-z0-9]+$", re.I)


def iphone_edits(paths):
    """{путь правки: имя оригинала} для правок айфона, у которых в той же группе есть оригинал с тем же номером."""
    originals, edits = {}, []
    for path in paths:
        m = IPHONE_NAME.match(os.path.basename(path))
        if not m:
            continue
        if m[1]:
            edits.append((path, m[2]))
        else:
            originals.setdefault(m[2], re.sub(r"^\d{4}-\d\d-\d\d_\d{6}_", "", os.path.basename(path)))   # имя без приставки даты короче
    return {path: originals[number] for path, number in edits if number in originals}


IMPORT_ACTIONS = {"оставить": True, "убрать": False}


def parse_marks(text):
    """Строки «оставить/убрать<TAB>путь» из файла разбора: «Сохранить результат» и «Сохранить весь прогресс»
    страниц «Разбор похожих.html» и «Разбор дублей.html». Строки с «#» — пояснения. Возвращает (пары, непонятных строк)."""
    pairs, bad = [], 0
    for raw in text.lstrip("﻿").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        act, _, path = line.partition("\t")
        act, path = act.strip().lower(), path.strip().strip('"')
        if act not in IMPORT_ACTIONS or not path:
            bad += 1
            continue
        pairs.append((IMPORT_ACTIONS[act], path))
    return pairs, bad


def import_rel(path, root):
    """Путь из файла — относительно выбранной папки. Полный путь с той же папкой или другой буквой диска тоже годится."""
    p = path.replace("/", "\\")
    base = os.path.abspath(root).rstrip("\\") + "\\"
    if normalize_key(p).startswith(normalize_key(base)):
        return p[len(base):]
    if re.match(r"^[A-Za-z]:\\", p):
        return p[3:]
    while p.startswith(".\\"):
        p = p[2:]
    return p.lstrip("\\")


def _merge_scenes(atoms, ctx):
    """Жадно сливает самые близкие по цвету группы одной съёмки. Экраны с текстом и группы с разбросом дат не трогает."""
    fixed, live = [], []
    for a in atoms:
        if a["fixed"] or a["b"] - a["a"] > SCENE_SPAN:
            fixed.append(a["ids"])
        else:
            live.append(a)
    live.sort(key=lambda x: x["a"])
    i = 0
    while i < len(live):
        if ctx.cancelled():
            raise Cancelled
        j, end = i + 1, live[i]["b"]
        while j < len(live) and live[j]["a"] - end <= SCENE_GAP:
            end = max(end, live[j]["b"])
            j += 1
        part = live[i:j]
        i = j
        h = np.array([x["h"] for x in part], dtype=np.float32)
        a, b = np.array([x["a"] for x in part]), np.array([x["b"] for x in part])
        n = np.array([len(x["ids"]) for x in part])
        alive = np.ones(len(part), bool)
        sim = np.array([np.minimum(h, row).sum(1) for row in h], dtype=np.float32)
        np.fill_diagonal(sim, -1)
        while True:
            if ctx.cancelled():
                raise Cancelled
            allowed = (np.maximum.outer(a, a) - np.minimum.outer(b, b) <= SCENE_GAP) \
                & (np.maximum.outer(b, b) - np.minimum.outer(a, a) <= SCENE_SPAN) \
                & (np.add.outer(n, n) <= SCENE_CAP) & np.outer(alive, alive)
            m = np.where(allowed, sim, -1)
            x, y = np.unravel_index(m.argmax(), m.shape)
            if m[x, y] < SCENE_SIM:
                break
            h[x] = (h[x] * n[x] + h[y] * n[y]) / (n[x] + n[y])
            n[x] += n[y]
            a[x], b[x] = min(a[x], a[y]), max(b[x], b[y])
            alive[y] = False
            part[x]["ids"] += part[y]["ids"]
            sim[x] = np.minimum(h, h[x]).sum(1)
            sim[:, x] = sim[x]
            sim[x, x] = -1
        fixed += [x["ids"] for x, keep in zip(part, alive) if keep]
    return fixed


def build_groups(store, ctx):
    rows = store.rows("SELECT id, rel, ph, date, hist, white, screenshot, theme, dup_kind, dup_of FROM files "
                      "WHERE kind='photo' AND ph IS NOT NULL AND (dup_kind IS NULL OR dup_kind='similar')")
    rows = [r for r in rows if top_of(r["rel"]) not in (DROP_DIR, JUNK_DIR)]
    keepers = {r["dup_of"] for r in rows if r["dup_kind"] == "similar"}
    # Папки «не трогать» в разборе только как образец рядом с найденным дублем.
    rows = [r for r in rows if not r["theme"] or r["id"] in keepers]
    store.groups_cache.clear()
    if not rows:
        store.put("groups", [])
        return 0
    rows.sort(key=lambda r: (r["date"] or "9", r["rel"]))
    idx = {r["id"]: i for i, r in enumerate(rows)}
    ph = np.array([int(r["ph"], 16) for r in rows], dtype=np.uint64)
    ts = np.array([to_ts(r["date"]) for r in rows])
    white = np.array([r["white"] or 0 for r in rows])
    screen = np.array([bool(r["screenshot"]) for r in rows])
    dsu = _DSU(len(rows))
    linked = set()

    def link(i, j):
        dsu.union(i, j)
        linked.update((i, j))

    for r in rows:
        if r["dup_kind"] == "similar" and r["dup_of"] in idx:
            link(idx[r["id"]], idx[r["dup_of"]])
    usable = ~screen
    photo_like = usable & (white <= WHITE_SCREEN)
    for i in range(len(rows) - 1):
        if i % 500 == 0:
            ctx.progress("Собираем группы похожих", i, len(rows))
            if ctx.cancelled():
                raise Cancelled
        if not usable[i]:
            continue
        far = np.bitwise_count(ph[i + 1:] ^ ph[i]).astype(np.int32)
        gap = np.abs(ts[i + 1:] - ts[i])
        both_undated = np.isnan(ts[i]) & np.isnan(ts[i + 1:])
        hit = usable[i + 1:] & (((far <= LINK_P) & (gap <= LINK_SECONDS)) | (both_undated & (far <= LINK_P_UNDATED))
                                | (photo_like[i] & photo_like[i + 1:] & (far <= LINK_P_ANY_DATE)))
        for j in np.nonzero(hit)[0]:
            link(i, i + 1 + int(j))

    # Соседние кадры без похожего отпечатка: могут войти в группу через сходство сцены.
    dated = np.sort(ts[[i for i in linked if not np.isnan(ts[i])]]) if linked else np.array([])
    extra = []
    if len(dated):
        for i in range(len(rows)):
            if i in linked or not usable[i] or not photo_like[i] or np.isnan(ts[i]):
                continue
            k = int(np.searchsorted(dated, ts[i]))
            if any(0 <= n < len(dated) and abs(dated[n] - ts[i]) <= NEAR_SECONDS for n in (k - 1, k)):
                extra.append(i)
    clusters = defaultdict(list)
    for i in sorted(linked) + extra:
        clusters[dsu.find(i)].append(i)
    atoms = []
    for members in clusters.values():
        tt = ts[members]
        tt = tt[~np.isnan(tt)]
        fixed = len(tt) == 0 or all(screen[m] or white[m] > WHITE_SCREEN for m in members)
        atoms.append({"ids": [rows[m]["id"] for m in members], "fixed": fixed, "a": float(tt.min()) if len(tt) else 0.0,
                      "b": float(tt.max()) if len(tt) else 0.0,
                      "h": None if fixed else np.mean([hist_array(rows[m]["hist"]) for m in members], axis=0)})
    merged = _merge_scenes(atoms, ctx)
    by_id = {r["id"]: r for r in rows}
    groups = []
    for ids in merged:
        if len(ids) < 2:
            continue
        ids.sort(key=lambda x: (by_id[x]["date"] or "9", by_id[x]["rel"]))
        groups.append({"id": hashlib.sha1(",".join(map(str, sorted(ids))).encode()).hexdigest()[:16], "ids": ids,
                       "date": by_id[ids[0]]["date"] or "", "dups": any(by_id[x]["dup_kind"] == "similar" for x in ids)})
    groups.sort(key=lambda g: (g["date"] or "9", g["id"]))
    store.put("groups", groups)
    store.groups_cache.clear()
    return len(groups)


class Review:
    """Текущие группы: без файлов, уже перенесённых в «_Удалить», и групп, в которых не осталось выбора."""

    def __init__(self, store):
        self.store = store

    def default_keep(self, f):
        return f["dup_kind"] != "similar" and top_of(f["rel"]) != DUP_DIR

    def live_groups(self, mode="all"):
        cached = self.store.groups_cache.get(mode)
        if cached is not None:
            return cached
        files = {r["id"]: r for r in self.store.rows("SELECT id, rel, dup_kind FROM files WHERE kind='photo'")}
        out = []
        for g in self.store.get("groups", []):
            ids = [i for i in g["ids"] if i in files and top_of(files[i]["rel"]) != DROP_DIR]
            if len(ids) >= 2 and (mode == "all" or g["dups"]):
                out.append({**g, "ids": ids})
        self.store.groups_cache[mode] = out
        return out

    def summary(self, mode="all"):
        groups = self.live_groups(mode)
        seen = {r["gid"] for r in self.store.rows("SELECT gid FROM seen")}
        first = next((i for i, g in enumerate(groups) if g["id"] not in seen), None)
        return {"groups": len(groups), "seen": sum(g["id"] in seen for g in groups), "first_unseen": first,
                "with_dups": len(self.live_groups("dups")), "all": len(self.live_groups("all")),
                "photos": sum(len(g["ids"]) for g in groups), "changes": self.store.one("SELECT COUNT(*) n FROM marks")["n"]}

    def group(self, pos, mode="all"):
        groups = self.live_groups(mode)
        if not groups:
            return None
        pos = max(0, min(len(groups) - 1, pos))
        g = groups[pos]
        marks = {r["id"]: r["keep"] for r in self.store.rows("SELECT id, keep FROM marks")}
        rows = {r["id"]: r for r in self.store.rows(
            f"SELECT id, rel, size, date, dup_kind, w, h FROM files WHERE id IN ({','.join(map(str, g['ids']))})")}
        seen = self.store.one("SELECT 1 FROM seen WHERE gid=?", g["id"]) is not None
        photos = [{"id": i, "path": rows[i]["rel"], "mb": round((rows[i]["size"] or 0) / 2**20, 2), "date": rows[i]["date"] or "",
                   "w": rows[i]["w"], "h": rows[i]["h"], "keep": bool(marks.get(i, self.default_keep(rows[i])))} for i in g["ids"]]
        edits = iphone_edits([p["path"] for p in photos])
        for p in photos:
            p["edit_of"] = edits.get(p["path"])
        return {"pos": pos, "total": len(groups), "id": g["id"], "date": g["date"], "seen": seen, "photos": photos}

    def next_unseen(self, pos, mode="all"):
        seen = {r["gid"] for r in self.store.rows("SELECT gid FROM seen")}
        groups = self.live_groups(mode)
        return next((i for i in range(pos + 1, len(groups)) if groups[i]["id"] not in seen), None)

    def set_marks(self, gid, values):
        """Отмеченная группа считается просмотренной: решение по ней принято."""
        with self.store.lock:
            for file_id, keep in values:
                self.store.db.execute("INSERT OR REPLACE INTO marks VALUES(?,?)", (file_id, int(keep)))
            if gid:
                self.store.db.execute("INSERT OR IGNORE INTO seen VALUES(?)", (gid,))
            self.store.db.commit()

    def decided(self):
        """(группа, снимки, что оставить) только для просмотренных групп: непросмотренные не трогаем.
        Последним идёт (None, None, ...) — решения из загруженного файла разбора по снимкам вне просмотренных групп:
        человек их уже принял на старой странице, а группы программы с её группами не совпадают."""
        seen = {r["gid"] for r in self.store.rows("SELECT gid FROM seen")}
        marks = {r["id"]: bool(r["keep"]) for r in self.store.rows("SELECT id, keep FROM marks")}
        files = {r["id"]: r for r in self.store.rows("SELECT * FROM files WHERE kind='photo'")}
        covered = set()
        for pos, g in enumerate(self.live_groups()):
            if g["id"] in seen:
                covered.update(g["ids"])
                yield pos, g, {i: marks.get(i, self.default_keep(files[i])) for i in g["ids"]}, files
        extra = {r["id"]: marks.get(r["id"], self.default_keep(files[r["id"]]))
                 for r in self.store.rows("SELECT id FROM imported")
                 if r["id"] in files and r["id"] not in covered and top_of(files[r["id"]]["rel"]) != DROP_DIR}
        if extra:
            yield None, None, extra, files

    def import_marks(self, text, root, write=False):
        """Пометки из файла разбора. write=False — только посчитать, что будет. Ничего не перемещает:
        снимки переедут, когда человек применит итоги разбора."""
        pairs, bad = parse_marks(text)
        by_rel = {normalize_key(r["rel"]): r for r in self.store.rows("SELECT id, rel, kind FROM files")}
        found, missing, removed, not_photo = {}, [], 0, 0
        for keep, path in pairs:
            rel = import_rel(path, root)
            f = by_rel.get(normalize_key(rel))
            if f is None:
                if os.path.isfile(long_path(root, os.path.join(DROP_DIR, rel))):
                    removed += 1            # уже перенесён в «_Удалить» раньше
                else:
                    missing.append(path)
            elif top_of(f["rel"]) == DROP_DIR:
                removed += 1
            elif f["kind"] != "photo":
                not_photo += 1
            else:
                found[f["id"]] = keep        # если строк про один снимок несколько, главнее последняя
        groups = self.live_groups()
        done = [g["id"] for g in groups if all(i in found for i in g["ids"])]
        partial = sum(1 for g in groups if g["id"] not in done and any(i in found for i in g["ids"]))
        in_groups = {i for g in groups for i in g["ids"]}
        if write and found:
            with self.store.lock:
                db = self.store.db
                db.executemany("INSERT OR REPLACE INTO marks VALUES(?,?)", [(i, int(v)) for i, v in found.items()])
                db.executemany("INSERT OR IGNORE INTO imported VALUES(?)", [(i,) for i in found])
                db.executemany("INSERT OR IGNORE INTO seen VALUES(?)", [(g,) for g in done])
                db.commit()
        return {"lines": len(pairs) + bad, "bad": bad, "found": len(found), "drop": sum(not v for v in found.values()),
                "keep": sum(found.values()), "already_removed": removed, "not_photo": not_photo, "missing": len(missing),
                "missing_examples": missing[:15], "groups_done": len(done), "groups_partial": partial,
                "outside_groups": sum(1 for i in found if i not in in_groups), "written": bool(write and found)}

    def pending(self):
        """Сколько файлов переедет, если применить пометки сейчас: те же правила, что в build_plan, но без записи плана."""
        n = 0
        for _, _, keep, files in self.decided():
            n += sum(1 for i, value in keep.items() if not value or top_of(files[i]["rel"]) == DUP_DIR)
        return n

    def all_removed(self):
        """Группы, где не осталось ни одного снимка: их показываем перед применением."""
        out = []
        for pos, g, keep, files in self.decided():
            if g is not None and not any(keep.values()):
                out.append({"pos": pos, "date": g["date"], "photos": [{"id": i, "path": files[i]["rel"]} for i in g["ids"]]})
        return out

    def build_plan(self, root, settings):
        """План по пометкам: «Убрать» → «_Удалить» с прежними подпапками, «Оставить» из «_Дубли» → по дате."""
        store = self.store
        names = Names(root)
        live_ids = live_photo_ids(store)
        plan, stay = [], 0
        for _, _, keep, files in self.decided():
            for i, value in keep.items():
                f = files[i]
                if not value:
                    plan.append(("review", f["rel"], names.free(os.path.join(DROP_DIR, f["rel"])), "убрать", "", f["size"], ""))
                elif top_of(f["rel"]) == DUP_DIR:
                    action, dst, note = target(f, live_ids, settings)
                    plan.append(("review", f["rel"], names.free(dst), action if action in ("по дате", "скриншот") else "на место",
                                 f["date"] or "", f["size"], note))
                else:
                    stay += 1
        with store.lock:
            store.db.execute("DELETE FROM plan WHERE op='review'")
            store.db.executemany("INSERT INTO plan VALUES(?,?,?,?,?,?,?)", plan)
            store.db.commit()
        return {"remove": sum(p[3] == "убрать" for p in plan), "restore": sum(p[3] != "убрать" for p in plan),
                "remove_bytes": sum(p[5] or 0 for p in plan if p[3] == "убрать"), "stay": stay,
                "unseen": sum(1 for g in self.live_groups() if not store.one("SELECT 1 FROM seen WHERE gid=?", g["id"]))}


def folder_stats(root, name):
    n, size = 0, 0
    for d, _, files in os.walk(long_path(root, name)):
        for x in files:
            n += 1
            try:
                size += os.path.getsize(os.path.join(d, x))
            except OSError:
                pass
    return {"files": n, "bytes": size}
