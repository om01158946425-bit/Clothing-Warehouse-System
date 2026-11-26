"""Starts Clothing Warehouse without a console window and opens it in the browser.

Normally you start it with "Start Warehouse.vbs" (or the desktop shortcut). Output goes to
warehouse.log. To stop it, double-click "Stop Warehouse.bat".
"""
import atexit
import logging
import os
import socket
import sys
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
HOST, PORT = "127.0.0.1", 5000
URL = f"http://{HOST}:{PORT}"
PID_FILE = os.path.join(HERE, "warehouse.pid")

handler = RotatingFileHandler(os.path.join(HERE, "warehouse.log"), maxBytes=500_000, backupCount=1, encoding="utf-8")
handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
logging.getLogger().addHandler(handler)
logging.getLogger().setLevel(logging.INFO)


class _ToLog:
    """pythonw has no console, so anything printed goes to the log file instead."""
    def write(self, text):
        if text.strip():
            logging.info(text.rstrip())
        return len(text)

    def flush(self):
        pass


if sys.stdout is None:
    sys.stdout = _ToLog()
if sys.stderr is None:
    sys.stderr = _ToLog()


def show_error(text):
    logging.error(text)
    if os.name == "nt":  # a message box, because there is no console to read
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, "Clothing Warehouse", 0x10)


def open_browser():
    try:
        webbrowser.open(URL)
    except Exception:
        logging.exception("could not open the browser")


def port_in_use():
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, PORT)) == 0


def is_warehouse():
    try:
        with urllib.request.urlopen(URL + "/login", timeout=3) as r:
            return b'name="application-name" content="Clothing Warehouse"' in r.read()
    except Exception:
        return False


def main():
    if port_in_use():
        if is_warehouse():  # already running: just open it
            open_browser()
            return 0
        show_error(f"Port {PORT} is already used by another program, so Clothing Warehouse can't start.")
        return 1
    try:
        import app as A
        from werkzeug.serving import make_server
        server = make_server(HOST, PORT, A.app, threaded=True)
    except Exception:
        logging.exception("startup failed")
        show_error("Clothing Warehouse could not start. Details are in warehouse.log in the program folder.\n"
                   "If Flask is missing, open a terminal and run: pip install flask")
        return 1
    with open(PID_FILE, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))
    logging.info("Clothing Warehouse started on %s", URL)
    open_browser()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
