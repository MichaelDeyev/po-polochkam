"""Что куда переедет. На диске ничего не меняет: план пишется в базу."""
import datetime as dt
import os

from .analysis import normalize_key, top_of
from .dates import WEAK
from .paths import (CHECK_DIR, DROP_DIR, DUP_DIR, JUNK_DIR, LIVE_DIR, MANAGED_DIRS, PHOTO_DIR, SCANS_DIR, SHOT_DIR,
                    SIMILAR_SUB, VIDEO_DIR, long_path)

MAX_NAME = 240
DEFAULTS = {"keep_folders": [], "separate_screens": True, "separate_live": True, "separate_scans": True, "fix_file_dates": True}
ACTION_TITLES = {"по дате": "Разложить по годам и месяцам", "скриншот": "В папку скриншотов", "live-видео": "В папку Live Photos",
                 "скан": "В папку сканов", "проверить": "В «_Проверить»: дата неясна", "дубль": "В «_Дубли»: точная копия",
                 "похожий дубль": "В «_Дубли»: та же картинка", "мусор": "В «_Мусор»: служебные файлы",
                 "убрать": "В «_Удалить»", "на место": "Вернуть из «_Дубли»", "дата указана": "Дата указана вручную"}
DATED = {"по дате", "скриншот", "live-видео"}   # у этих дата файла заменяется на дату съёмки


class Names:
    """Свободные имена: повтор получает номер, как в проводнике."""

    def __init__(self, root):
        self.root, self.used = root, set()

    def free(self, dst):
        base, ext = os.path.splitext(dst)
        n = 1
        while normalize_key(dst) in self.used or os.path.exists(long_path(self.root, dst)):
            n += 1
            dst = f"{base}_{n}{ext}"
        self.used.add(normalize_key(dst))
        return dst


def dated_name(when, name):
    stem, ext = os.path.splitext(name)
    prefix = f"{when:%Y-%m-%d_%H%M%S}_"
    return prefix + stem[:MAX_NAME - len(prefix) - len(ext)] + ext


def original_rel(rel):
    """Где файл лежал до «_Дубли»: из этого строятся подпапки при возврате."""
    parts = rel.split("\\")
    if parts[0] == DUP_DIR:
        parts = parts[1:]
        if parts and parts[0] == SIMILAR_SUB:
            parts = parts[1:]
    return "\\".join(parts)


def target(f, live_ids, settings):
    """(действие, куда, примечание) для снимка или видео вне «_Дубли»."""
    rel, name = original_rel(f["rel"]), os.path.basename(f["rel"])
    if f["kind"] == "photo" and f["scan"] and settings["separate_scans"]:
        return "скан", os.path.join(SCANS_DIR, rel), ""
    shot = f["screenshot"] and settings["separate_screens"]
    live = f["kind"] == "video" and settings["separate_live"] and f["live_id"] in live_ids
    sub = SHOT_DIR if shot else PHOTO_DIR if f["kind"] == "photo" else LIVE_DIR if live else VIDEO_DIR
    weak = (f["date_src"] or "").startswith(WEAK)
    if not f["date"] or weak:
        note = "дата не найдена" if not f["date"] else f"{f['date_src']}: {f['date'][:10]}"
        return "проверить", os.path.join(CHECK_DIR, sub, rel), note
    when = dt.datetime.strptime(f["date"], "%Y-%m-%d %H:%M:%S")
    return ("скриншот" if shot else "live-видео" if live else "по дате"), \
        os.path.join(sub, f"{when:%Y}", f"{when:%Y-%m}", dated_name(when, name)), ""


def live_photo_ids(store):
    return {r["live_id"] for r in store.rows("SELECT live_id FROM files WHERE kind='photo' AND live_id IS NOT NULL")}


def build_layout(store, root, settings):
    files = store.rows("SELECT * FROM files ORDER BY rel")
    names = Names(root)
    live_ids = live_photo_ids(store)
    keeper_rel = {f["id"]: f["rel"] for f in files}
    plan = []
    for f in files:
        rel, top = f["rel"], top_of(f["rel"])
        if top == DROP_DIR or top in MANAGED_DIRS and "\\" in rel or f["kind"] == "other":
            continue
        action, note = None, ""
        if f["kind"] == "junk":
            action, dst = "мусор", os.path.join(JUNK_DIR, rel)
        elif f["dup_kind"] == "exact":
            action, dst, note = "дубль", os.path.join(DUP_DIR, rel), f"оставлен: {keeper_rel.get(f['dup_of'], '')}"
        elif f["dup_kind"] == "similar":
            action, dst, note = "похожий дубль", os.path.join(DUP_DIR, SIMILAR_SUB, rel), f"оставлен: {keeper_rel.get(f['dup_of'], '')}"
        elif f["theme"]:
            continue
        else:
            action, dst, note = target(f, live_ids, settings)
        plan.append(("layout", rel, names.free(dst), action, f["date"] or "", f["size"], note))
    with store.lock:
        store.db.execute("DELETE FROM plan WHERE op='layout'")
        store.db.executemany("INSERT INTO plan VALUES(?,?,?,?,?,?,?)", plan)
        store.db.commit()
    return len(plan)


def report(store, root):
    plan = store.rows("SELECT action, size, date FROM plan WHERE op='layout'")
    by_action, years = {}, {}
    for r in plan:
        a = by_action.setdefault(r["action"], {"action": r["action"], "title": ACTION_TITLES.get(r["action"], r["action"]), "files": 0, "bytes": 0})
        a["files"] += 1
        a["bytes"] += r["size"] or 0
        if r["action"] in DATED and r["date"]:
            years[r["date"][:4]] = years.get(r["date"][:4], 0) + 1
    kinds = {r["kind"]: r["n"] for r in store.rows("SELECT kind, COUNT(*) n FROM files WHERE rel NOT LIKE ? ESCAPE '!' GROUP BY kind",
                                                     f"!{DROP_DIR}\\%")}
    sources = [{"source": r["date_src"] or "не найдена", "files": r["n"]} for r in store.rows(
        "SELECT date_src, COUNT(*) n FROM files WHERE kind IN ('photo','video') GROUP BY date_src ORDER BY n DESC")]
    return {"actions": sorted(by_action.values(), key=lambda a: -a["files"]), "years": sorted(years.items()),
            "kinds": kinds, "sources": sources, "moves": len(plan),
            "conflicts": store.one("SELECT COUNT(*) n FROM files WHERE conflict IS NOT NULL")["n"],
            "unreadable": store.one("SELECT COUNT(*) n FROM files WHERE kind='photo' AND printed=1 AND ph IS NULL")["n"]}
