"""Окно программы: Edge без адресной строки и вкладок, а если Edge нет — обычный браузер.

Edge есть в каждой Windows 10 и 11, поэтому ставить ничего не нужно. Окно открывается с отдельным профилем
в %LOCALAPPDATA%\\РазборФото\\окно: оно не смешивается с вкладками и историей самого человека.
"""
import logging
import os
import subprocess
import webbrowser

from . import paths

APP_PATHS = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe"


def find_edge():
    try:
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, APP_PATHS) as key:
                    path = winreg.QueryValue(key, None)
                if path and os.path.isfile(path):
                    return path
            except OSError:
                pass
    except ImportError:
        pass
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = os.environ.get(var)
        path = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe") if base else ""
        if path and os.path.isfile(path):
            return path
    return None


def edge_command(edge, url):
    profile = os.path.join(paths.app_data_dir(), "окно")
    # Проверено на Edge 157:
    # msImplicitSignin — иначе Edge сам входит в учётную запись Microsoft из Windows и показывает окно про синхронизацию.
    #   InPrivate тоже спасает, но пишет «[InPrivate]» в заголовке окна;
    # msEdgeStartupBoost — иначе после закрытия окна Edge перезапускает себя в фоне без окна.
    return [edge, f"--app={url}", f"--user-data-dir={profile}", "--disable-features=msImplicitSignin,msEdgeStartupBoost",
            "--no-first-run", "--no-default-browser-check", "--window-size=1280,880"]


def open_window(url):
    """Возвращает "edge" или "browser" — чем открыли."""
    edge = None if os.environ.get("RAZBOR_BROWSER") == "default" else find_edge()
    if edge:
        try:
            subprocess.Popen(edge_command(edge, url), close_fds=True)
            return "edge"
        except OSError:
            logging.exception("не удалось открыть окно Edge")
    webbrowser.open(url)
    return "browser"
