"""Сборка программы «По полочкам» и её установщика: python app\\build.py
Нужны PyInstaller, библиотеки из requirements.txt и Inno Setup 6.7.4 в tools\\innosetup-6.7.4
(сценарий установщика — installer\\installer.iss, его картинки рисует make_icon.py).
Результат в dist рядом с этой папкой:
  По полочкам\\                 — готовая программа папкой (exe + _internal), её можно запускать и без установки;
  По полочкам — установка.exe   — установщик, его и раздавать;
  Как пользоваться.txt
Флаг --no-installer собирает только папку.
"""
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = os.path.dirname(HERE)
EXIFTOOL = os.path.join(TOP, "tools", "exiftool-13.60_64")
ISCC = os.path.join(TOP, "tools", "innosetup-6.7.4", "ISCC.exe")
NAME = "По полочкам"
SETUP_NAME = NAME + " — установка"
ICON = os.path.join(HERE, "razbor", "ui", "icon.ico")      # рисует make_icon.py
EXCLUDE = ["scipy", "imagehash", "pywt", "matplotlib", "tkinter", "pandas", "pytest", "IPython", "numpy.f2py"]


def write_version():
    """Дата сборки и коммит: по ним видно, какая версия у человека. Незакоммиченные правки помечаются «+»."""
    def git(*args):
        return subprocess.run(["git", "-C", TOP, *args], capture_output=True, text=True).stdout.strip()
    commit = git("rev-parse", "--short", "HEAD")
    if commit and git("status", "--porcelain", "--", "app"):
        commit += "+"
    path = os.path.join(TOP, "work", "build", "version.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    today = datetime.date.today()
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"date": today.strftime("%d.%m.%Y"), "commit": commit}, f)
    return path, today, commit


def build_app(dist, version):
    """Программа папкой: запускается сразу, без распаковки во временную папку при каждом старте."""
    data = [(os.path.join(HERE, "razbor", "ui", n), "razbor/ui") for n in ("index.html", "icon.ico", "icon.png")]
    data += [(EXIFTOOL, "exiftool"), (os.path.join(HERE, "THIRD_PARTY.txt"), "."), (version, "razbor")]
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir", "--noconsole", "--name", NAME,
           "--icon", ICON, "--paths", HERE, "--collect-all", "pillow_heif",
           "--specpath", os.path.join(TOP, "work", "build"), "--workpath", os.path.join(TOP, "work", "build"),
           "--distpath", dist]
    for src, dst in data:
        cmd += ["--add-data", f"{src}{os.pathsep}{dst}"]
    for mod in EXCLUDE:
        cmd += ["--exclude-module", mod]
    subprocess.run(cmd + [os.path.join(HERE, "run.py")], check=True)
    return os.path.join(dist, NAME)


def build_setup(dist, app_dir, today, commit):
    if not os.path.isfile(ISCC):
        sys.exit(f"нет Inno Setup: {ISCC}")
    defines = {
        "AppVer": f"{today.year}.{today.month}.{today.day}",
        "AppVerText": f"версия от {today:%d.%m.%Y}" + (f", сборка {commit}" if commit else ""),
        "SrcDir": app_dir,
        "Guide": os.path.join(HERE, "Как пользоваться.txt"),
        "IconFile": ICON,
        "OutDir": dist,
        "OutName": SETUP_NAME,
    }
    cmd = [ISCC, "/Q"] + [f"/D{k}={v}" for k, v in defines.items()] + [os.path.join(HERE, "installer", "installer.iss")]
    subprocess.run(cmd, check=True)
    return os.path.join(dist, SETUP_NAME + ".exe")


def size_mb(path):
    if os.path.isfile(path):
        return os.path.getsize(path) // 2**20
    return sum(os.path.getsize(os.path.join(d, n)) for d, _, names in os.walk(path) for n in names) // 2**20


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    # Запущенную программу Windows заменить не даёт: тогда можно собрать рядом, python app\build.py "dist\Новая версия"
    dist = os.path.abspath(args[0]) if args else os.path.join(TOP, "dist")
    version, today, commit = write_version()
    app_dir = build_app(dist, version)
    shutil.copy2(os.path.join(HERE, "Как пользоваться.txt"), dist)
    print(f"программа: {app_dir}, {size_mb(app_dir)} МБ")
    if "--no-installer" not in sys.argv:
        setup = build_setup(dist, app_dir, today, commit)
        print(f"установщик: {setup}, {size_mb(setup)} МБ")


if __name__ == "__main__":
    main()
