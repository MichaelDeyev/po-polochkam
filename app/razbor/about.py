"""Версия программы и отчёт для автора: по нему видно, какая версия у человека и что пошло не так.
В отчёте нет снимков. Пути к выбранной папке и профилю заменяются метками, но имена файлов из журнала ошибок
могут остаться — поэтому отчёт обычный текстовый файл, человек видит всё, что отправляет."""
import ctypes
import datetime as dt
import json
import os
import platform
import shutil
import sys
import uuid

from . import moves
from .paths import app_data_dir, resource_dir

VERSION_FILE = "version.json"       # кладёт build.py при сборке
LOG_LINES = 300
FOLDERID_DESKTOP = "B4BFCC3A-DB2C-424C-B029-7FE99A87C641"


def version():
    try:
        with open(os.path.join(resource_dir(), "razbor", VERSION_FILE), encoding="utf-8") as f:
            v = json.load(f)
        return {"version": v["date"], "build": v.get("commit", ""), "frozen": True}
    except (OSError, ValueError, KeyError):
        return {"version": "из исходников", "build": "", "frozen": False}


def version_text():
    v = version()
    return f"версия от {v['version']}" + (f", сборка {v['build']}" if v["build"] else "") if v["frozen"] else "версия из исходников"


def desktop_dir():
    try:
        buf = ctypes.c_wchar_p()
        guid = ctypes.create_string_buffer(uuid.UUID(FOLDERID_DESKTOP).bytes_le, 16)
        if ctypes.windll.shell32.SHGetKnownFolderPath(guid, 0, None, ctypes.byref(buf)) == 0:
            path = buf.value
            ctypes.windll.ole32.CoTaskMemFree(buf)
            if path and os.path.isdir(path):
                return path
    except (OSError, AttributeError):
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def _volume(root):
    """Тип диска и файловая система: с FAT32 и exFAT бывают свои беды."""
    drive = os.path.splitdrive(os.path.abspath(root))[0] + "\\"
    fs = ctypes.create_unicode_buffer(64)
    ctypes.windll.kernel32.GetVolumeInformationW(drive, None, 0, None, None, None, fs, 64)
    kind = {2: "съёмный", 3: "встроенный", 4: "сетевой"}.get(ctypes.windll.kernel32.GetDriveTypeW(drive), "другой")
    try:
        free = f"свободно {shutil.disk_usage(root).free / 2**30:.1f} ГБ"
    except OSError:
        free = "диск не виден"
    return f"{kind}, {fs.value or 'неизвестная файловая система'}, {free}"


def _hide(text, root):
    for path, mark in ((root, "<выбранная папка>"), (os.path.expanduser("~"), "<профиль>")):
        if path:
            text = text.replace(path, mark).replace(path.lower(), mark)
    return text


def build_report(app, note=""):
    v = version()
    lines = ["Отчёт программы «По полочкам» для автора", "",
             f"Создан: {dt.datetime.now():%d.%m.%Y %H:%M}",
             f"Программа: {version_text()}",
             f"Windows: {platform.platform()}, {platform.machine()}, процессоров {os.cpu_count()}",
             f"Python: {sys.version.split()[0]}" + ("" if v["frozen"] else " (запуск из исходников)"), ""]
    if note.strip():
        lines += ["Что случилось, со слов человека:", note.strip()[:4000], ""]
    if app.root and app.disk != "ok":
        lines.append("Папка выбрана: " + _volume(app.root))
        lines.append("Диск: " + {"gone": "отключён, программа ждёт его", "paused": "пауза, база отпущена"}.get(app.disk, app.disk))
    elif app.root:
        lines.append("Папка выбрана: " + _volume(app.root))
        analysis = app.store.get("analysis") if app.store else None
        if analysis:
            lines.append("Анализ: " + ", ".join(f"{k}={x}" for k, x in analysis.items()))
        lines.append("")
        lines.append("Операции:")
        for j in moves.list_journals(app.root):
            state = "отменена" if j["rolled_back"] else "выполнена" if j["finished"] else "не закончена"
            lines.append(f"  {j['time']}  {j['title']}: {j['moved']} из {j['total']}, {state}")
    else:
        lines.append("Папка не выбрана.")
    job = app.job.view() if app.job else None
    if job:
        lines += ["", f"Последняя работа: {job.get('name')}, {job.get('status')}, {job.get('stage') or ''}",
                  f"Ошибка: {job.get('error') or 'нет'}"]
    log = os.path.join(app_data_dir(), "журнал ошибок.txt")
    lines += ["", f"Журнал ошибок, последние {LOG_LINES} строк:"]
    try:
        with open(log, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-LOG_LINES:]
        lines += [line.rstrip("\n") for line in tail] or ["  (пусто)"]
    except OSError:
        lines.append("  (журнала нет)")
    return _hide("\n".join(lines) + "\n", app.root)


def save_report(app, note="", folder=None):
    folder = folder or os.environ.get("RAZBOR_REPORT_DIR") or desktop_dir()
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"По полочкам, отчёт {dt.datetime.now():%Y-%m-%d %H%M}.txt")
    with open(path, "w", encoding="utf-8-sig") as f:        # с меткой: Блокнот откроет кириллицу правильно
        f.write(build_report(app, note))
    return path
