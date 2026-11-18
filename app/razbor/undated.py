"""Файлы из «_Проверить»: подсказки даты и перенос по годам и месяцам, которые указал человек.
Подсказки — только предположения, решает человек. Дата самого файла при переносе не меняется: точного дня никто не знает."""
import bisect
import datetime as dt
import os
import re

from .analysis import SEQ
from .dates import WEAK
from .paths import CHECK_DIR, LIVE_DIR, PHOTO_DIR, SHOT_DIR, VIDEO_DIR
from .planner import Names

PAGE = 60
NEAR_NUMBERS = 30               # номер кадра дальше этого — уже не «соседний»
SUBS = {PHOTO_DIR, VIDEO_DIR, SHOT_DIR, LIVE_DIR}
VALUE = re.compile(r"^(\d{4})(?:-(\d{2}))?$")
YEAR_IN_NAME = re.compile(r"(?<!\d)(199\d|20[0-4]\d)(?!\d)")
MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
ACTION = "дата указана"


def check_value(value):
    """«2019» или «2019-06»; иначе ValueError."""
    m = VALUE.match(value if isinstance(value, str) else "")
    if not m or not 1900 <= int(m[1]) <= dt.date.today().year or m[2] and not 1 <= int(m[2]) <= 12:
        raise ValueError(f"не дата: {value!r}")
    return value


def label(value):
    year, _, month = value.partition("-")
    return f"{MONTHS[int(month) - 1]} {year}" if month else f"{year} год"


def split(rel):
    """(папка вида «Фото по годам» или None, путь внутри неё) для файла в «_Проверить»."""
    parts = rel.split("\\")[1:]
    if len(parts) > 1 and parts[0] in SUBS:
        return parts[0], "\\".join(parts[1:])
    return None, "\\".join(parts)


def seq_key(name):
    m = SEQ.match(name)
    return (m[1].upper().replace("_", ""), int(m[2])) if m else None


class Undated:
    def __init__(self, store):
        self.store = store

    def files(self):
        return self.store.rows("SELECT id, rel, kind, size, date, date_src FROM files WHERE rel LIKE ? ESCAPE '!' "
                               "AND kind IN ('photo','video') ORDER BY rel", f"!{CHECK_DIR}\\%")

    def values(self):
        return {r["id"]: r["value"] for r in self.store.rows("SELECT id, value FROM check_dates")}

    def seq_index(self):
        """Снимки с надёжной датой по номерам кадров: IMG → [(номер, дата, id, имя, вид)]."""
        cached = self.store.groups_cache.get("seq")
        if cached is not None:
            return cached
        index = {}
        for r in self.store.rows("SELECT id, rel, kind, date, date_src FROM files WHERE date IS NOT NULL AND kind IN ('photo','video')"):
            if (r["date_src"] or "").startswith(WEAK) or r["rel"].startswith(CHECK_DIR + "\\"):
                continue
            name = os.path.basename(r["rel"])
            key = seq_key(name.split("_", 2)[-1] if re.match(r"^\d{4}-\d\d-\d\d_\d{6}_", name) else name)
            if key:
                index.setdefault(key[0], []).append((key[1], r["date"], r["id"], name, r["kind"]))
        for items in index.values():
            items.sort()
        self.store.groups_cache["seq"] = index
        return index

    def hints(self, f, index):
        out = []
        if f["date"] and (f["date_src"] or "").startswith(WEAK):
            out.append({"value": f["date"][:7], "label": f"{label(f['date'][:7])}: {f['date_src']}, это может быть день копирования"})
        key = seq_key(os.path.basename(f["rel"]))
        items = index.get(key[0], []) if key else []
        at = bisect.bisect_left(items, (key[1],)) if key else 0
        for i in (at - 1, at):
            if 0 <= i < len(items) and abs(items[i][0] - key[1]) <= NEAR_NUMBERS:
                number, date, file_id, name, kind = items[i]
                out.append({"value": date[:7], "label": f"{label(date[:7])}: как у {name}, номер кадра рядом",
                            "thumb": file_id if kind == "photo" else None})
        folder = os.path.dirname(split(f["rel"])[1])
        years = YEAR_IN_NAME.findall(folder)
        if years:
            out.append({"value": years[-1], "label": f"{years[-1]} год: так названа папка «{folder}»"})
        seen, unique = set(), []
        for h in out:
            if h["value"] not in seen:
                seen.add(h["value"])
                unique.append(h)
        return unique

    def page(self, offset):
        files, values = self.files(), self.values()
        index = self.seq_index()
        items = []
        for f in files[offset:offset + PAGE]:
            sub, inner = split(f["rel"])
            items.append({"id": f["id"], "kind": f["kind"], "rel": f["rel"], "name": os.path.basename(f["rel"]),
                          "folder": os.path.dirname(inner), "value": values.get(f["id"]),
                          "label": label(values[f["id"]]) if f["id"] in values else "", "hints": self.hints(f, index)})
        return {"total": len(files), "offset": offset, "page": PAGE, "decided": sum(f["id"] in values for f in files), "items": items}

    def set_values(self, pairs):
        allowed = {f["id"] for f in self.files()}
        with self.store.lock:
            for file_id, value in pairs:
                if file_id not in allowed:
                    continue
                if value:
                    self.store.db.execute("INSERT OR REPLACE INTO check_dates VALUES(?,?)", (file_id, check_value(value)))
                else:
                    self.store.db.execute("DELETE FROM check_dates WHERE id=?", (file_id,))
            self.store.db.commit()
        return {"ok": True}

    def build_plan(self, root):
        names, values, plan = Names(root), self.values(), []
        for f in self.files():
            value = values.get(f["id"])
            if not value:
                continue
            sub = split(f["rel"])[0] or (PHOTO_DIR if f["kind"] == "photo" else VIDEO_DIR)
            year, _, month = value.partition("-")
            folder = os.path.join(sub, year, f"{year}-{month}") if month else os.path.join(sub, year)
            plan.append(("check", f["rel"], names.free(os.path.join(folder, os.path.basename(f["rel"]))), ACTION, "", f["size"], value))
        with self.store.lock:
            self.store.db.execute("DELETE FROM plan WHERE op='check'")
            self.store.db.executemany("INSERT INTO plan VALUES(?,?,?,?,?,?,?)", plan)
            self.store.db.commit()
        return {"moves": len(plan), "bytes": sum(p[5] or 0 for p in plan)}
