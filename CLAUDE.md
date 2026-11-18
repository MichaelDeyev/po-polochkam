# По полочкам — инструкции для агента

Сначала прочитай `README.md` (о проекте) и `app/README.md` (устройство программы), затем общие правила в `..\..\README.md` (корень `MIKE_VIBECODE`).

## Главное

- **Это основное место для правок программы «По полочкам»** (решение владельца 18.11.2026). Папка `..\photo-sorting\app` — замороженная копия, её не менять.
- **Репозиторий публичный**: https://github.com/MichaelDeyev/po-polochkam. Ничего личного сюда не класть: фото, пути к файлам владельца, журналы, базы, отчёты, почту, данные диска E:. Перед коммитом проверять, что попадает в git.
- `push` — только по явной просьбе владельца.
- Автор коммитов настроен локально: `Mikhail Deev <67957756+MichaelDeyev@users.noreply.github.com>`. Не менять на рабочую или личную почту.
- Когда добавляется возможность: обновлять `app/README.md`, обучение (TOUR в `app/razbor/ui/index.html`), `app/Как пользоваться.txt` и при необходимости `README.md`.

## Сборка и тесты

- `tools\` (exiftool, Inno Setup) и `work\build-tools` (PyInstaller, Pillow и др.) в git не хранятся. Пока они лежат в `..\photo-sorting\`. Для сборки отсюда скопировать `..\photo-sorting\tools` в `tools\` и `..\photo-sorting\work\build-tools` в `work\build-tools` (обе папки в `.gitignore`), затем `set PYTHONPATH=work\build-tools` и `python app\build.py`.
- Тесты: `python -m unittest discover -s app\tests` с теми же библиотеками. В системном Python нет Pillow; 18.11.2026 тесты в этой папке не запускались.
