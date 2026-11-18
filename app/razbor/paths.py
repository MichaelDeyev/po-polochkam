"""Имена папок, длинные пути и проверка, можно ли работать с выбранной папкой."""
import ctypes
import os
import shutil
import sys
import uuid

SERVICE_DIR = "_Разбор фото"        # журналы и база лежат рядом с фото: откат работает и с другого компьютера.
                                    # Старое имя программы оставлено нарочно: в этих папках уже лежат журналы отката
DROP_DIR = "_Удалить"
DUP_DIR, CHECK_DIR, JUNK_DIR = "_Дубли", "_Проверить", "_Мусор"
PHOTO_DIR, VIDEO_DIR, SHOT_DIR = "Фото по годам", "Видео по годам", "Скриншоты"
LIVE_DIR, SCANS_DIR = "Live Photos по годам", "Сканы старых фото"
SIMILAR_SUB = "похожие"
# Из этих папок файлы при повторном анализе не переезжают: они уже разложены.
MANAGED_DIRS = {PHOTO_DIR, VIDEO_DIR, SHOT_DIR, LIVE_DIR, SCANS_DIR, DUP_DIR, CHECK_DIR, JUNK_DIR}
SKIP_DIRS = {SERVICE_DIR, DROP_DIR, ".Spotlight-V100", ".Trashes", "System Volume Information", "$RECYCLE.BIN",
             ".fseventsd", ".git"}
CLOUD_WORDS = ("onedrive", "dropbox", "google drive", "googledrive", "icloud", "яндекс.диск", "yandexdisk")
MIN_FREE_BYTES = 300 * 2**20
DRIVE_REMOTE, DRIVE_CDROM = 4, 5


def long_path(root, rel=""):
    # Префикс снимает ограничение в 260 символов и сохраняет имена как есть.
    return "\\\\?\\" + os.path.join(os.path.abspath(root), rel) if rel else "\\\\?\\" + os.path.abspath(root)


def resource_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def exiftool_path():
    frozen = os.path.join(resource_dir(), "exiftool", "exiftool.exe")
    if os.path.isfile(frozen):
        return frozen
    return os.path.join(os.path.dirname(resource_dir()), "tools", "exiftool-13.60_64", "exiftool.exe")


def app_data_dir():
    path = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "РазборФото")
    os.makedirs(path, exist_ok=True)
    return path


def service_dir(root):
    return os.path.join(os.path.abspath(root), SERVICE_DIR)


def drive_type(path):
    drive = os.path.splitdrive(os.path.abspath(path))[0]
    return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") if drive else DRIVE_REMOTE


def browse(path):
    """Подпапки для выбора; скрытые и системные не показываем."""
    out = []
    with os.scandir(path) as it:
        for e in it:
            try:
                if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith("$") \
                        and not e.stat(follow_symlinks=False).st_file_attributes & 0x2:
                    out.append(e.name)
            except OSError:
                continue
    return sorted(out, key=str.lower)


def _inside(path, parent):
    if not parent:
        return False
    path, parent = os.path.normcase(os.path.abspath(path)), os.path.normcase(os.path.abspath(parent))
    return path == parent or path.startswith(parent.rstrip("\\") + "\\")


def check_root(path):
    """(причина отказа или None, предупреждения). Все отказы — до первого изменения на диске."""
    path = os.path.abspath(path)
    if not os.path.isdir(path):
        return "Такой папки нет. Возможно, диск отключён.", []
    if path.startswith("\\\\") or drive_type(path) in (DRIVE_REMOTE, DRIVE_CDROM):
        return "Сетевые папки и диски только для чтения не подходят: при обрыве связи можно потерять порядок файлов.", []
    low = path.lower()
    if any(w in low for w in CLOUD_WORDS):
        return ("Облачные папки не подходят: часть файлов может быть не скачана на компьютер, "
                "а перемещения разойдутся по всем устройствам. Скопируйте фото на диск и выберите ту папку."), []
    env = os.environ
    for parent in (env.get("WINDIR"), env.get("PROGRAMFILES"), env.get("PROGRAMFILES(X86)"), env.get("PROGRAMDATA"),
                   env.get("APPDATA"), env.get("LOCALAPPDATA")):
        if _inside(path, parent):
            return "Это системная папка или папка программ. Выберите папку с фотографиями.", []
    names = {n.lower() for n in _safe_names(path)}
    system_drive = os.path.splitdrive(env.get("WINDIR", "C:\\"))[0].lower()
    if {"windows", "program files"} <= names or os.path.splitdrive(path)[0].lower() == system_drive and len(path) <= 3:
        return "Это корень системного диска. Выберите папку с фотографиями внутри него.", []
    if "appdata" in names:
        return "Это папка пользователя целиком, в ней есть файлы программ. Выберите папку с фотографиями внутри неё.", []
    warnings = []
    free = shutil.disk_usage(path).free
    if free < MIN_FREE_BYTES:
        return "На этом диске свободно меньше 300 МБ. Нужно место для журнала отката и базы анализа.", []
    try:
        service = service_dir(path)
        os.makedirs(service, exist_ok=True)
        probe = os.path.join(service, f"проверка-{uuid.uuid4().hex}.tmp")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.replace(probe, probe + "2")
        os.remove(probe + "2")      # только наш пробный файл
    except OSError as e:
        return f"В эту папку нельзя записывать: {e}", []
    return None, warnings


def _safe_names(path):
    try:
        return os.listdir(path)
    except OSError:
        return []
