"""Системное окно Windows для выбора папки: то же, что в проводнике. Последнюю папку Windows запоминает сама."""
import ctypes
import logging
import threading
import uuid
from ctypes import POINTER, byref, c_long, c_void_p, wintypes

TITLE = "Выберите папку или диск с фото"

CLSID_FILE_OPEN_DIALOG = "DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7"
IID_FILE_OPEN_DIALOG = "D57C7288-D4AD-4768-BE02-9D969532D960"
CLSCTX_INPROC_SERVER = 1
COINIT_STA_NO_OLE1 = 0x2 | 0x4
FOS_NOCHANGEDIR, FOS_PICKFOLDERS, FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST = 0x8, 0x20, 0x40, 0x800
SIGDN_FILESYSPATH = 0x80058000
CANCELLED = 0x800704C7
DPI_PER_MONITOR_V2 = -4

# Номера методов в таблице COM: IUnknown, затем IModalWindow, IFileDialog, IShellItem.
RELEASE, SHOW, SET_OPTIONS, GET_OPTIONS, SET_TITLE, GET_RESULT = 2, 3, 9, 10, 17, 20
GET_DISPLAY_NAME = 5

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TOOLWINDOW, WS_EX_LAYERED = 0x8, 0x80, 0x80000
LWA_ALPHA, SW_SHOWNA = 2, 8

user32 = ctypes.WinDLL("user32")
kernel32 = ctypes.WinDLL("kernel32")
ole32 = ctypes.WinDLL("ole32")
HWND, DWORD, BOOL = wintypes.HWND, wintypes.DWORD, wintypes.BOOL
user32.GetForegroundWindow.restype = HWND
user32.GetWindowRect.argtypes = [HWND, POINTER(wintypes.RECT)]
user32.GetWindowThreadProcessId.argtypes = [HWND, POINTER(DWORD)]
user32.GetWindowThreadProcessId.restype = DWORD
user32.AttachThreadInput.argtypes = [DWORD, DWORD, BOOL]
user32.BringWindowToTop.argtypes = [HWND]
user32.SetForegroundWindow.argtypes = [HWND]
user32.CreateWindowExW.argtypes = [DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   HWND, c_void_p, c_void_p, c_void_p]
user32.CreateWindowExW.restype = HWND
user32.SetLayeredWindowAttributes.argtypes = [HWND, DWORD, wintypes.BYTE, DWORD]
user32.ShowWindow.argtypes = [HWND, ctypes.c_int]
user32.DestroyWindow.argtypes = [HWND]
user32.GetLastActivePopup.argtypes = [HWND]
user32.GetLastActivePopup.restype = HWND
user32.SetThreadDpiAwarenessContext.argtypes = [c_void_p]
user32.SetThreadDpiAwarenessContext.restype = c_void_p
kernel32.GetCurrentThreadId.restype = DWORD
ole32.CoCreateInstance.argtypes = [c_void_p, c_void_p, DWORD, c_void_p, POINTER(c_void_p)]
ole32.CoCreateInstance.restype = c_long
ole32.CoInitializeEx.argtypes = [c_void_p, DWORD]
ole32.CoInitializeEx.restype = c_long
ole32.CoTaskMemFree.argtypes = [c_void_p]


def _guid(text):
    return ctypes.create_string_buffer(uuid.UUID(text).bytes_le, 16)


def _call(obj, index, argtypes=(), *args):
    """Метод COM-объекта: код ответа без проверки."""
    table = ctypes.cast(obj, POINTER(POINTER(c_void_p))).contents
    return ctypes.WINFUNCTYPE(c_long, c_void_p, *argtypes)(table[index])(obj, *args)


def _ok(code):
    if code < 0:
        raise OSError(f"код Windows {code & 0xFFFFFFFF:#010x}")


def to_front(hwnd):
    """Окно поверх браузера и с фокусом. Windows отдаёт фокус только через ввод того окна, которое им владеет."""
    other = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
    mine = kernel32.GetCurrentThreadId()
    joined = other and other != mine and user32.AttachThreadInput(mine, other, True)
    try:
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
    finally:
        if joined:
            user32.AttachThreadInput(mine, other, False)


def _make_owner():
    """Невидимый владелец в центре текущего окна: окно выбора откроется на том же экране и поверх браузера."""
    rect = wintypes.RECT()
    active = user32.GetForegroundWindow()
    if active and user32.GetWindowRect(active, byref(rect)) and rect.right > -10000:
        x, y = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
    else:
        x, y = user32.GetSystemMetrics(0) // 2, user32.GetSystemMetrics(1) // 2
    hwnd = user32.CreateWindowExW(WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED, "STATIC", "", WS_POPUP, x, y, 1, 1, None, None, None, None)
    if not hwnd:
        raise ctypes.WinError()
    user32.SetLayeredWindowAttributes(hwnd, 0, 0, LWA_ALPHA)
    user32.ShowWindow(hwnd, SW_SHOWNA)
    to_front(hwnd)
    return hwnd


def ask_folder(title, on_owner):
    """Путь выбранной папки или None, если окно закрыли. Блокирует поток, пока окно открыто."""
    _ok(ole32.CoInitializeEx(None, COINIT_STA_NO_OLE1))
    try:
        user32.SetThreadDpiAwarenessContext(c_void_p(DPI_PER_MONITOR_V2))     # иначе на экранах с масштабом окно размыто
        dialog, item = c_void_p(), c_void_p()
        owner = None
        try:
            _ok(ole32.CoCreateInstance(_guid(CLSID_FILE_OPEN_DIALOG), None, CLSCTX_INPROC_SERVER, _guid(IID_FILE_OPEN_DIALOG), byref(dialog)))
            options = DWORD()
            _ok(_call(dialog, GET_OPTIONS, [POINTER(DWORD)], byref(options)))
            wanted = options.value | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST | FOS_NOCHANGEDIR
            _ok(_call(dialog, SET_OPTIONS, [DWORD], wanted))
            _ok(_call(dialog, SET_TITLE, [wintypes.LPCWSTR], title))
            owner = _make_owner()
            on_owner(owner)
            code = _call(dialog, SHOW, [HWND], owner)
            if code & 0xFFFFFFFF == CANCELLED:
                return None
            _ok(code)
            _ok(_call(dialog, GET_RESULT, [POINTER(c_void_p)], byref(item)))
            text = c_void_p()
            _ok(_call(item, GET_DISPLAY_NAME, [DWORD, POINTER(c_void_p)], SIGDN_FILESYSPATH, byref(text)))
            try:
                return ctypes.wstring_at(text)
            finally:
                ole32.CoTaskMemFree(text)
        finally:
            on_owner(None)
            if owner:
                user32.DestroyWindow(owner)
            for obj in (item, dialog):
                if obj:
                    _call(obj, RELEASE)
    finally:
        ole32.CoUninitialize()


class Picker:
    """Окно открыто в своём потоке, страница спрашивает о результате: выбирать можно сколько угодно долго."""

    def __init__(self, ask=ask_folder):
        self.ask = ask
        self.lock = threading.Lock()
        self.state = {"status": "idle"}
        self.owner = None

    def is_open(self):
        return self.state["status"] == "open"

    def start(self):
        """Повторное нажатие не открывает второе окно, а показывает уже открытое."""
        with self.lock:
            was_open = self.is_open()
            if not was_open:
                self.state = {"status": "open"}
                threading.Thread(target=self._run, daemon=True).start()
        if was_open:
            self.show()
        return {"status": "open"}

    def show(self):
        owner = self.owner
        if owner:
            to_front(user32.GetLastActivePopup(owner))

    def poll(self):
        """Итог отдаётся один раз."""
        with self.lock:
            out = self.state
            if not self.is_open():
                self.state = {"status": "idle"}
            return out

    def _set_owner(self, hwnd):
        self.owner = hwnd

    def _run(self):
        try:
            path = self.ask(TITLE, self._set_owner)
            result = {"status": "done", "path": path} if path else {"status": "cancel"}
        except Exception as e:
            logging.exception("окно выбора папки")
            result = {"status": "error", "message": f"Windows не открыла окно выбора папки: {e}"}
        with self.lock:
            self.state = result
