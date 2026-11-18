"""Точка входа программы «По полочкам»."""
import logging
import multiprocessing
import os
import sys


def main():
    multiprocessing.freeze_support()    # без этого рабочие процессы в собранной программе запускали бы её заново
    if sys.stdout is None:              # в собранной программе нет консоли
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = sys.stdout
    from razbor.paths import app_data_dir
    logging.basicConfig(filename=os.path.join(app_data_dir(), "журнал ошибок.txt"), level=logging.WARNING, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    from razbor.server import serve
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else 0
    serve(open_browser="--no-browser" not in sys.argv, port=port)


if __name__ == "__main__":
    main()
