"""Установщик целиком: python app\\tests\\test_installer.py "dist\\По полочкам — установка.exe"

Ставит программу без вопросов во временную папку, прогоняет test_flow.py на установленной программе,
проверяет, что запущенную программу удалить нельзя, затем удаляет и проверяет, что ничего не осталось.
На время теста на рабочем столе и в меню «Пуск» появляется ярлык «По полочкам» — удаление его убирает.
Если программа уже установлена на этом компьютере, тест не запускается: он бы её удалил.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
import winreg

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from razbor.about import desktop_dir  # noqa: E402

APP_ID = "{27AB6785-8D7B-48B6-9908-FD6E9C12D8E8}_is1"     # как в installer.iss
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\\" + APP_ID
NAME = "По полочкам"
SETUP = None


def registered():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY) as k:
            return {n: winreg.QueryValueEx(k, n)[0] for n in ("DisplayName", "InstallLocation", "DisplayVersion")}
    except OSError:
        return None


def wait_for(cond, limit=60):
    end = time.time() + limit
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.5)
    return False


class Installer(unittest.TestCase):
    def test_install_run_uninstall(self):
        if registered():
            self.skipTest("программа уже установлена на этом компьютере — тест её не трогает")
        real_data = os.path.join(os.environ["LOCALAPPDATA"], "РазборФото")
        real_before = sorted(os.listdir(real_data)) if os.path.isdir(real_data) else None
        tmp = tempfile.mkdtemp(prefix="polochki-setup-")
        target = os.path.join(tmp, "Программа")
        desktop_lnk = os.path.join(desktop_dir(), NAME + ".lnk")
        menu = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs", NAME)
        installed = False
        try:
            # Установка
            r = subprocess.run([SETUP, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", f"/DIR={target}"], timeout=300)
            self.assertEqual(r.returncode, 0, "установщик завершился с ошибкой")
            installed = True
            exe = os.path.join(target, NAME + ".exe")
            self.assertTrue(os.path.isfile(exe))
            self.assertTrue(os.path.isfile(os.path.join(target, "Как пользоваться.txt")))
            info = registered()
            self.assertEqual(info["DisplayName"], NAME, "нет в «Приложениях» Windows")
            self.assertEqual(os.path.normcase(info["InstallLocation"].rstrip("\\")), os.path.normcase(target))
            self.assertTrue(os.path.isfile(desktop_lnk), "нет ярлыка на рабочем столе")
            self.assertTrue(os.path.isfile(os.path.join(menu, NAME + ".lnk")), "нет ярлыка в меню «Пуск»")

            # Установленная программа проходит весь путь
            r = subprocess.run([sys.executable, os.path.join(HERE, "test_flow.py"), "--exe", exe], timeout=900)
            self.assertEqual(r.returncode, 0, "test_flow.py на установленной программе не прошёл")

            # Пока программа работает, удалить её нельзя
            data = os.path.join(tmp, "appdata")
            os.makedirs(os.path.join(data, "РазборФото"))
            env = {**os.environ, "LOCALAPPDATA": data}
            proc = subprocess.Popen([exe, "--no-browser"], env=env)
            instance = os.path.join(data, "РазборФото", "запущена.json")
            self.assertTrue(wait_for(lambda: os.path.exists(instance)))
            uninstaller = os.path.join(target, "unins000.exe")
            subprocess.run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=env, timeout=120)
            time.sleep(3)
            self.assertTrue(os.path.isfile(exe), "запущенную программу удалили")
            self.assertIsNotNone(registered())
            url = json.load(open(instance, encoding="utf-8"))["url"]
            base, token = url.split("/?t=")
            urllib.request.urlopen(urllib.request.Request(base + "/api/quit", b"{}", {"X-Token": token}))
            proc.wait(15)

            # Удаление: программа, ярлыки, запись в «Приложениях» и данные программы
            with open(os.path.join(data, "РазборФото", "недавние.json"), "w", encoding="utf-8") as f:
                f.write("[]")
            subprocess.run([uninstaller, "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"], env=env, timeout=120)
            self.assertTrue(wait_for(lambda: registered() is None and not os.path.exists(exe)), "удаление не закончилось")
            installed = False
            self.assertTrue(wait_for(lambda: not os.path.exists(os.path.join(data, "РазборФото"))),
                            "данные программы не удалены")
            self.assertFalse(os.path.exists(desktop_lnk), "ярлык на рабочем столе остался")
            self.assertFalse(os.path.exists(menu), "папка в меню «Пуск» осталась")
            self.assertTrue(wait_for(lambda: not os.path.exists(target)), "папка программы осталась")
            real_after = sorted(os.listdir(real_data)) if os.path.isdir(real_data) else None
            self.assertEqual(real_after, real_before, "удаление задело настоящие данные программы на этом компьютере")
        finally:
            if installed and registered():
                subprocess.run([os.path.join(target, "unins000.exe"), "/VERYSILENT", "/SUPPRESSMSGBOXES"],
                               env={**os.environ, "LOCALAPPDATA": os.path.join(tmp, "appdata")}, timeout=120)
                wait_for(lambda: registered() is None)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    SETUP = os.path.abspath(sys.argv.pop(1)) if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(HERE)), "dist", NAME + " — установка.exe")
    unittest.main(verbosity=2)
