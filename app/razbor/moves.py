"""Перемещения с журналом и откат. Только переименование внутри диска, файлы не удаляются и не перезаписываются.

Каждый шаг сначала пишется в журнал (с прежними датами файла), затем выполняется, поэтому откат вернёт всё
обратно даже после обрыва. Журналы лежат рядом с фото и хранят пути от выбранной папки: буква диска может смениться.
"""
import ctypes
import datetime as dt
import json
import os
from ctypes import wintypes

from .analysis import Cancelled
from .paths import long_path, service_dir
from .planner import DATED

JOURNAL_DIR = "журналы"
MAX_FAILS_IN_ROW = 20
FILETIME_EPOCH_S = 11644473600
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateFileW.restype = wintypes.HANDLE
_k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                             wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
_k32.SetFileTime.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.LPVOID, wintypes.LPVOID]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


class DiskGone(Exception):
    pass


def set_times(path, created_ns, modified_ns):
    """Дата создания и дата изменения файла; содержимое файла не затрагивается."""
    to_ft = lambda ns: ctypes.c_ulonglong(ns // 100 + FILETIME_EPOCH_S * 10**7)
    # FILE_WRITE_ATTRIBUTES, общий доступ, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS
    h = _k32.CreateFileW(path, 0x100, 7, None, 3, 0x02000000, None)
    if h == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        c, m = to_ft(created_ns), to_ft(modified_ns)
        if not _k32.SetFileTime(h, ctypes.byref(c), None, ctypes.byref(m)):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        _k32.CloseHandle(h)


def journal_dir(root):
    path = os.path.join(service_dir(root), JOURNAL_DIR)
    os.makedirs(path, exist_ok=True)
    return path


def read_journal(path):
    entries = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except ValueError:      # недописанная последняя строка после обрыва
                pass
    return entries


def list_journals(root):
    """Новые первыми. Откатить можно только последнюю неотменённую операцию: более ранние могут зависеть от неё."""
    out = []
    for name in sorted(os.listdir(journal_dir(root)), reverse=True):
        if not name.endswith(".jsonl"):
            continue
        entries = read_journal(os.path.join(journal_dir(root), name))
        head = next((e for e in entries if e["a"] == "meta"), {})
        out.append({"name": name, "kind": head.get("kind", ""), "title": head.get("title", name), "time": head.get("time", ""),
                    "moved": sum(e["a"] == "done" for e in entries), "total": head.get("total", 0),
                    "finished": any(e["a"] == "finished" for e in entries),
                    "rolled_back": any(e["a"] == "rolled_back" for e in entries)})
    active = next((j for j in out if not j["rolled_back"]), None)
    for j in out:
        j["can_rollback"] = j is active and j["moved"] > 0
    return out


def new_journal(root, kind, title, total):
    d = journal_dir(root)
    number = 1 + max([int(n.split("-")[0]) for n in os.listdir(d) if n.endswith(".jsonl") and n.split("-")[0].isdigit()], default=0)
    path = os.path.join(d, f"{number:04d}-{kind}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"a": "meta", "kind": kind, "title": title, "total": total,
                            "time": dt.datetime.now().strftime("%d.%m.%Y %H:%M")}, ensure_ascii=False) + "\n")
    return path


def preflight(root, rows, done):
    """Ни одного изменения: источники на месте и того же размера, места назначения свободны."""
    problems, seen = [], set()
    for r in rows:
        key = r["dst"].lower()
        if key in seen:
            problems.append(f"одно место назначения у двух файлов: {r['dst']}")
        seen.add(key)
        if r["rel"] in done:
            continue
        src = long_path(root, r["rel"])
        if not os.path.isfile(src):
            problems.append(f"нет файла: {r['rel']}")
        elif r["size"] is not None and os.path.getsize(src) != r["size"]:
            problems.append(f"файл изменился после анализа: {r['rel']}")
        if os.path.exists(long_path(root, r["dst"])):
            problems.append(f"место занято: {r['dst']}")
    return problems


def run_moves(root, rows, journal, ctx, fix_dates, on_moved):
    """Выполняет перемещения. Повторный запуск с тем же журналом продолжает с места остановки."""
    done = {e["src"] for e in read_journal(journal) if e["a"] == "done"}
    todo = [r for r in rows if r["rel"] not in done]
    moved, failed, fails_in_row = 0, [], 0
    parents = set()
    with open(journal, "a", encoding="utf-8") as j:
        def log(entry):
            j.write(json.dumps(entry, ensure_ascii=False) + "\n")
            j.flush()
            os.fsync(j.fileno())

        for i, r in enumerate(todo, 1):
            if ctx.cancelled():
                raise Cancelled
            rel, dst_rel = r["rel"], r["dst"]
            src, dst = long_path(root, rel), long_path(root, dst_rel)
            try:
                st = os.stat(src)
                log({"a": "intent", "src": rel, "dst": dst_rel, "created_ns": st.st_birthtime_ns, "modified_ns": st.st_mtime_ns})
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if os.path.exists(dst):     # rename на Windows и сам не перезапишет
                    raise FileExistsError(dst_rel)
                os.rename(src, dst)
                note = ""
                if fix_dates and r["action"] in DATED and r["date"]:
                    try:
                        ns = int(dt.datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S").timestamp()) * 10**9
                        set_times(dst, ns, ns)
                    except OSError as e:    # файл уже на месте, дата файла не критична
                        note = str(e)
                log({"a": "done", "src": rel, "dst": dst_rel, "time_error": note})
                on_moved(rel, dst_rel, os.stat(dst).st_mtime_ns)
                parents.add(os.path.dirname(rel))
                moved += 1
                fails_in_row = 0
            except OSError as e:
                if not os.path.isdir(long_path(root)):
                    raise DiskGone from e
                failed.append({"file": rel, "error": str(e)})
                log({"a": "error", "src": rel, "dst": dst_rel, "error": str(e)})
                fails_in_row += 1
                if fails_in_row >= MAX_FAILS_IN_ROW:
                    raise DiskGone from e
            if i % 25 == 0 or i == len(todo):
                ctx.progress("Перемещаем файлы", i, len(todo))
        for d in sorted(parents, key=len, reverse=True):    # папки, которые опустели после перемещений
            while d:
                try:
                    os.rmdir(long_path(root, d))
                    log({"a": "rmdir", "dir": d})
                except OSError:
                    break
                d = os.path.dirname(d)
        if not failed:
            log({"a": "finished"})
    return moved, failed


def rollback(root, journal, ctx, on_moved):
    """Возвращает файлы на прежние места и прежние даты. Удаляет только опустевшие папки, созданные раскладкой."""
    entries = read_journal(journal)
    intents = [e for e in entries if e["a"] == "intent"]
    back, nothing, deleted, problems, parents = 0, 0, 0, [], set()
    for i, e in enumerate(reversed(intents), 1):
        if ctx.cancelled():
            raise Cancelled
        src, dst = long_path(root, e["src"]), long_path(root, e["dst"])
        in_src, in_dst = os.path.exists(src), os.path.exists(dst)
        if in_src and in_dst:
            problems.append(f"файл есть в обоих местах, не трогаю: {e['src']}")
        elif in_dst:
            try:
                os.makedirs(os.path.dirname(src), exist_ok=True)
                os.rename(dst, src)
                set_times(src, e["created_ns"], e["modified_ns"])
                on_moved(e["dst"], e["src"], e["modified_ns"])
            except OSError as err:
                if not os.path.isdir(long_path(root)):
                    raise DiskGone from err
                problems.append(f"{e['dst']}: {err}")
                continue
            back += 1
            parents.add(os.path.dirname(e["dst"]))
        elif in_src:
            nothing += 1            # перемещение не успело случиться
        else:
            deleted += 1            # файл уже удалили, например вместе с папкой «_Удалить»
        if i % 25 == 0 or i == len(intents):
            ctx.progress("Возвращаем файлы", i, len(intents))
    # Папка, в которой лежал хотя бы один исходный файл, существовала до раскладки.
    src_dirs = {os.path.dirname(e["src"]) for e in intents}
    for d in sorted(parents, key=len, reverse=True):
        while d and d not in src_dirs:
            try:
                os.rmdir(long_path(root, d))
            except OSError:
                break
            d = os.path.dirname(d)
    if not problems:
        with open(journal, "a", encoding="utf-8") as j:
            j.write(json.dumps({"a": "rolled_back", "time": dt.datetime.now().strftime("%d.%m.%Y %H:%M"),
                                "back": back, "deleted": deleted}, ensure_ascii=False) + "\n")
    return {"back": back, "not_moved": nothing, "already_deleted": deleted, "problems": problems[:30]}
