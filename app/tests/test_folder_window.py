"""Настоящее окно выбора папки: на несколько секунд появляется на экране, поэтому запускается отдельно от остальных тестов.

    python app\\tests\\test_folder_window.py                     программа из исходников
    python app\\tests\\test_folder_window.py --exe "dist\\....exe"  собранная программа
    python app\\tests\\test_folder_window.py --shot снимок.png      сохранить снимок окна
"""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
from test_flow import Client  # noqa: E402

EXE = None
SHOT = None
WM_SETTEXT, BM_CLICK, IDOK, IDCANCEL, NAME_FIELD = 0x000C, 0x00F5, 1, 2, 0x480
GWL_EXSTYLE, WS_EX_TOPMOST = -20, 0x8
TITLE = "Выберите папку или диск с фото"

user32 = ctypes.WinDLL("user32")
HWND = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [HWND]
user32.GetClassNameW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetDlgItem.argtypes = [HWND, ctypes.c_int]
user32.GetDlgItem.restype = HWND
user32.SendMessageW.argtypes = [HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetWindowLongPtrW.argtypes = [HWND, ctypes.c_int]
user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
user32.GetForegroundWindow.restype = HWND
user32.GetWindowRect.argtypes = [HWND, ctypes.POINTER(wintypes.RECT)]
ENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [ENUM, wintypes.LPARAM]
user32.EnumChildWindows.argtypes = [HWND, ENUM, wintypes.LPARAM]
user32.GetDlgCtrlID.argtypes = [HWND]
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))    # координаты окна те же, что у снимка экрана


def dialogs():
    """По заголовку, а не по процессу: у собранной программы окно принадлежит дочернему процессу."""
    found = []

    def check(hwnd, _):
        name = ctypes.create_unicode_buffer(128)
        user32.GetWindowTextW(hwnd, name, 128)
        if name.value == TITLE and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True
    user32.EnumWindows(ENUM(check), 0)
    return found


def wait_dialog(limit=20):
    end = time.time() + limit
    while time.time() < end:
        found = dialogs()
        if found:
            time.sleep(0.8)     # окно успевает дорисоваться
            return found[0]
        time.sleep(0.1)
    raise TimeoutError("окно выбора папки не появилось")


def name_field(dialog):
    """Поле «Папка»: глубоко внутри окна, поэтому ищем среди всех вложенных элементов."""
    found = []

    def check(hwnd, _):
        if user32.GetDlgCtrlID(hwnd) == NAME_FIELD:
            found.append(hwnd)
        return True
    user32.EnumChildWindows(dialog, ENUM(check), 0)
    return found[0] if found else None


class Window(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="razbor-window-")
        cls.chosen = os.path.join(cls.tmp, "Папка с фото")
        os.makedirs(cls.chosen)
        env = {**os.environ, "LOCALAPPDATA": os.path.join(cls.tmp, "appdata")}
        os.makedirs(env["LOCALAPPDATA"])
        cmd = [EXE, "--no-browser"] if EXE else [sys.executable, os.path.join(os.path.dirname(HERE), "run.py"), "--no-browser"]
        cls.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL)
        info = os.path.join(env["LOCALAPPDATA"], "РазборФото", "запущена.json")
        for _ in range(600):
            if os.path.exists(info):
                break
            time.sleep(0.1)
        time.sleep(0.3)
        cls.c = Client(json.load(open(info, encoding="utf-8"))["url"])

    @classmethod
    def tearDownClass(cls):
        for hwnd in dialogs():
            user32.SendMessageW(user32.GetDlgItem(hwnd, IDCANCEL), BM_CLICK, 0, 0)
        try:
            cls.c.post("/api/quit")
            cls.proc.wait(15)
        except Exception:
            cls.proc.kill()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def result(self, limit=10):
        end = time.time() + limit
        while time.time() < end:
            s = self.c.get("/api/pick_folder")
            if s["status"] != "open":
                return s
            time.sleep(0.1)
        raise TimeoutError

    def test_window(self):
        c = self.c
        self.assertEqual(c.get("/api/pick_folder"), {"status": "idle"})

        # Отмена ничего не выбирает
        self.assertEqual(c.post("/api/pick_folder"), {"status": "open"})
        dialog = wait_dialog()
        self.assertTrue(user32.GetWindowLongPtrW(dialog, GWL_EXSTYLE) & WS_EX_TOPMOST, "окно может оказаться за браузером")
        print("окно получило фокус:", user32.GetForegroundWindow() == dialog)
        if SHOT:
            from PIL import ImageGrab
            r = wintypes.RECT()
            user32.GetWindowRect(dialog, ctypes.byref(r))
            ImageGrab.grab((r.left, r.top, r.right, r.bottom), all_screens=True).save(SHOT)

        # Повторное нажатие не открывает второе окно
        self.assertEqual(c.post("/api/pick_folder"), {"status": "open"})
        time.sleep(0.5)
        self.assertEqual(dialogs(), [dialog])
        self.assertEqual(user32.GetForegroundWindow(), dialog, "второе нажатие не вывело окно вперёд")
        self.assertEqual(c.get("/api/pick_folder"), {"status": "open"})

        user32.SendMessageW(user32.GetDlgItem(dialog, IDCANCEL), BM_CLICK, 0, 0)
        self.assertEqual(self.result(), {"status": "cancel"})
        self.assertEqual(c.get("/api/pick_folder"), {"status": "idle"}, "итог отдаётся один раз")

        # Выбор папки и её открытие в программе
        c.post("/api/pick_folder")
        dialog = wait_dialog()
        chosen = ctypes.create_unicode_buffer(self.chosen)
        field = name_field(dialog)
        self.assertTrue(field, "поле с именем папки не найдено")
        user32.SendMessageW(field, WM_SETTEXT, 0, ctypes.addressof(chosen))
        time.sleep(0.3)
        user32.SendMessageW(user32.GetDlgItem(dialog, IDOK), BM_CLICK, 0, 0)
        got = self.result()
        self.assertEqual(got.get("status"), "done", got)
        self.assertEqual(os.path.normcase(got["path"]), os.path.normcase(self.chosen))
        self.assertEqual(os.path.normcase(c.post("/api/open", {"path": got["path"]})["root"]), os.path.normcase(self.chosen))


if __name__ == "__main__":
    if "--exe" in sys.argv:
        i = sys.argv.index("--exe")
        EXE = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    if "--shot" in sys.argv:
        i = sys.argv.index("--shot")
        SHOT = sys.argv[i + 1]
        del sys.argv[i:i + 2]
    unittest.main(verbosity=2)
