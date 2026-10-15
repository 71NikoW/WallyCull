"""The app: its own window with every feature, running entirely on this PC (127.0.0.1).

Started from the desktop shortcut. The window is a real program window (WebView2), not a browser.
Heavy steps (analysis, moving files, AI setup) run as a low-priority background process and the app
shows their progress. At start and every 6 hours it looks for a new setup and installs it by itself.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, ai, database, night, rating, updater
from .classify import CATEGORIES, NAMES
from .files import CONFIRM_WORD
from .paths import PROGRAM_DIR, REFERENCES_DIR, USER_DIR

NO_WINDOW = updater.NO_WINDOW
IDLE_EXIT = 300  # seconds without an open window after which the app exits
UPDATE_INTERVAL = 6 * 3600
PROGRESS = re.compile(r"^\s*(?P<title>[^:|]{1,40}):\s*(?P<pct>\d+)%\|.*?\|\s*(?P<i>\d+)/(?P<n>\d+)")
TITLES = {
    "analyze": "Analysis", "classify": "Classify", "reject": "Move rejects",
    "restore": "Restore", "purge": "Recycle Bin", "setup-ai": "Set up AI",
    "update": "Update", "rollback": "Previous version", "night": "Overnight run",
}
APP_ID = "wallycull"


class Task:
    """A background process (python -m wallycull ...) whose output is read along."""

    def __init__(self, kind: str, args: list[str] | None, cancellable: bool, on_done=None):
        self.kind, self.cancellable, self.on_done = kind, cancellable, on_done
        self.log: deque[str] = deque(maxlen=300)
        self.progress: dict | None = None
        self.status, self.exit_code, self.start, self.end = "running", None, time.time(), None
        self.process = None
        if args is not None:
            env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self.process = subprocess.Popen(
                [updater.python_exe(), "-u", "-m", "wallycull", *args], cwd=PROGRAM_DIR,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                creationflags=NO_WINDOW,
            )
            threading.Thread(target=self._read, daemon=True).start()

    def line(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        m = PROGRESS.match(text)
        if m:
            self.progress = {"title": m["title"].strip(), "i": int(m["i"]), "n": int(m["n"])}
        else:
            self.log.append(text)

    def _read(self) -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        rest = ""
        while True:
            chunk = self.process.stdout.read1(4096) if hasattr(self.process.stdout, "read1") else self.process.stdout.read(1)
            if not chunk:
                break
            rest += decoder.decode(chunk)
            parts = re.split(r"[\r\n]", rest)
            rest = parts.pop()
            for p in parts:
                self.line(p)
        self.line(rest)
        self.finish(self.process.wait())

    def finish(self, code: int) -> None:
        self.exit_code, self.end = code, time.time()
        self.status = "done" if code == 0 else ("cancelled" if self.status == "cancelling" else "failed")
        if self.on_done:
            try:
                self.on_done(self)
            except Exception as e:
                self.log.append(f"Error afterwards: {e}")

    def cancel(self) -> None:
        if not self.process or self.status != "running" or not self.cancellable:
            return
        self.status = "cancelling"
        if sys.platform == "win32":  # including the analysis worker processes
            subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        else:
            self.process.terminate()

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "title": TITLES.get(self.kind, self.kind), "status": self.status,
            "progress": self.progress, "log": list(self.log)[-12:], "cancellable": self.cancellable,
            "seconds": round((self.end or time.time()) - self.start),
        }


class App:
    def __init__(self, config: dict):
        self.config = config
        self.lock = threading.Lock()
        USER_DIR.mkdir(parents=True, exist_ok=True)
        self.state_file = USER_DIR / "app.json"
        try:
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        # State files from the German versions used other keys
        self.state.setdefault("folder", self.state.pop("ordner", None))
        self.state.setdefault("recent", self.state.pop("zuletzt", []))
        self.task: Task | None = None
        self.session: rating.Session | None = None
        self.update: dict = {"status": "unknown", "current": __version__}
        self.last_contact = time.time()
        self.server: ThreadingHTTPServer | None = None
        self.stop = threading.Event()
        self.ai = ai.available()[0]

    # ----- State ---------------------------------------------------------------------------------
    @property
    def folder(self) -> Path | None:
        f = self.state.get("folder")
        return Path(f) if f and Path(f).is_dir() else None

    def set_folder(self, path: str) -> None:
        p = Path(path.strip().strip('"')).expanduser()
        if not p.is_dir():
            raise ValueError(f"Folder not found: {p}")
        p = p.resolve()
        with self.lock:
            self.state["folder"] = str(p)
            self.state["recent"] = [str(p)] + [r for r in self.state["recent"] if r != str(p)][:7]
            self.session = None
            self.state_file.write_text(json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8")

    def get_session(self) -> rating.Session:
        with self.lock:
            if not self.folder:
                raise RuntimeError("No photo folder chosen")
            if not self.session or self.session.photo_dir != self.folder:
                self.session = rating.Session(self.folder, self.config)
            return self.session

    def _db_path(self) -> Path | None:
        return database.db_path(self.folder) if self.folder else None

    def _query(self, sql: str, params=()) -> list[dict]:
        path = self._db_path()
        if not path or not path.exists():
            return []
        db = sqlite3.connect(path)
        db.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in db.execute(sql, params)]
        except sqlite3.Error:
            return []
        finally:
            db.close()

    def stats(self) -> dict | None:
        path = self._db_path()
        if not path or not path.exists():
            return None
        db = sqlite3.connect(path)
        try:
            cats = dict(db.execute("SELECT category, COUNT(*) FROM images WHERE status='active' AND category IS NOT NULL GROUP BY category"))
            stats = {
                "categories": {k: cats.get(k, 0) for k in CATEGORIES},
                "total": db.execute("SELECT COUNT(*) FROM images WHERE status='active'").fetchone()[0],
                "moved": db.execute("SELECT COUNT(DISTINCT image_id) FROM moves WHERE state='moved'").fetchone()[0],
                "rated": db.execute("SELECT COUNT(*) FROM ratings").fetchone()[0],
                "with_ai": db.execute("SELECT COUNT(*) FROM images WHERE status='active' AND ai IS NOT NULL").fetchone()[0],
                "analyzed": db.execute("SELECT COUNT(*) FROM images WHERE status='active' AND version > 0").fetchone()[0],
                "updated": time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime)),
            }
        except sqlite3.Error:
            return None
        finally:
            db.close()
        try:
            model = json.loads((path.parent / "model.json").read_text(encoding="utf-8"))
            stats["model"] = {"accuracy": model.get("accuracy"), "rated": model.get("rated")}
        except (OSError, ValueError):
            stats["model"] = None
        return stats

    def state_dict(self) -> dict:
        return {
            "app": APP_ID, "version": __version__, "folder": str(self.folder) if self.folder else None,
            "recent": [r for r in self.state["recent"] if Path(r).is_dir()], "stats": self.stats(),
            "task": self.task.as_dict() if self.task else None, "ai": self.ai,
            "update": {k: v for k, v in self.update.items() if not k.startswith("_")},
            "night": night.last_summary(), "names": NAMES, "rejected_folder": self.config["files"]["rejected_folder"], "confirm_word": CONFIRM_WORD,
        }

    def reject_preview(self) -> dict:
        rows = self._query("SELECT id, source, preview, reasons, size FROM images WHERE status='active' "
                           "AND category='reject' ORDER BY key")
        for r in rows:
            r["reason"] = (json.loads(r.pop("reasons") or "[]") or [""])[0]
        return {"images": rows, "size": sum(r["size"] or 0 for r in rows)}

    def moved_images(self) -> dict:
        runs = self._query("SELECT run, COUNT(DISTINCT image_id) AS count FROM moves "
                           "WHERE state='moved' GROUP BY run ORDER BY run DESC")
        images = self._query("SELECT DISTINCT i.id, i.source, i.preview, m.run FROM moves m "
                             "JOIN images i ON i.id = m.image_id WHERE m.state='moved' ORDER BY i.key")
        return {"runs": runs, "images": images}

    # ----- Tasks ---------------------------------------------------------------------------------
    def start_task(self, kind: str, data: dict) -> dict:
        with self.lock:
            if self.task and self.task.status in ("running", "cancelling"):
                raise RuntimeError("A task is already running")
            if kind in ("update", "rollback"):
                release = self.update.get("_release" if kind == "update" else "_previous")
                if not release:
                    raise RuntimeError("No matching version found")
                self.task = Task(kind, None, False)
                threading.Thread(target=self._install_update, args=(release,), daemon=True).start()
                return self.task.as_dict()
            if kind == "setup-ai":
                self.task = Task(kind, ["setup-ai"], True, self._after_task)
                return self.task.as_dict()
            if kind == "night":
                if not night.recent_folders():
                    raise RuntimeError("Choose a photo folder first")
                self.session = None
                self.task = Task(kind, ["night"], True, self._after_task)
                return self.task.as_dict()
            if not self.folder:
                raise RuntimeError("Choose a photo folder first")
            f = str(self.folder)
            if kind == "analyze":
                args, cancellable = ["analyze", f, "--no-browser"] + (["--recompute"] if data.get("recompute") else []), True
            elif kind == "classify":
                args, cancellable = ["classify", f, "--no-browser"], True
            elif kind == "reject":
                args, cancellable = ["reject", f, "--execute"], False
            elif kind == "restore":
                args, cancellable = ["restore", f], False
                if data.get("run"):
                    args += ["--run", str(data["run"])]
                elif data.get("ids"):
                    for i in data["ids"]:
                        args += ["--image-id", str(int(i))]
                else:
                    args.append("--all")
            elif kind == "purge":
                if str(data.get("confirm", "")).strip().upper() != CONFIRM_WORD:
                    raise ValueError(f"Type {CONFIRM_WORD} to confirm")
                args, cancellable = ["purge", f, "--confirmed"], False
            else:
                raise ValueError(f"Unknown task: {kind}")
            self.session = None  # rating afterwards uses fresh data
            self.task = Task(kind, args, cancellable, self._after_task)
            return self.task.as_dict()

    def _after_task(self, task: Task) -> None:
        with self.lock:
            self.session = None
        if task.kind == "setup-ai":
            import importlib

            from . import paths

            paths.setup()
            importlib.invalidate_caches()
            self.ai = ai.available()[0]
        if self.update.get("status") == "new":  # postponed update: install it now
            threading.Thread(target=self._auto_update, daemon=True).start()

    # ----- Updates -------------------------------------------------------------------------------
    def _check_update(self, ask: bool = False) -> None:
        try:
            self.update = {**updater.check(ask), "checked": time.strftime("%H:%M")}
        except Exception as e:
            self.update = {"status": "error", "current": __version__, "error": str(e)[:200], "checked": time.strftime("%H:%M")}

    def _auto_update(self, ask: bool = False) -> None:
        self._check_update(ask)
        if self.update.get("status") == "new":
            try:
                self.start_task("update", {})
            except RuntimeError:
                pass  # a task is running: installed afterwards

    def _install_update(self, release: dict) -> None:
        t = self.task
        try:
            setup = updater.download(release, t.line)
            t.line(f"Installing version {release['version']}. WallyCull closes for a moment and starts again ...")
            t.finish(0)
            time.sleep(2.5)
            updater.run_setup(setup)
            self.quit()
        except Exception as e:
            t.line(f"Update failed: {e}")
            t.finish(1)
            self.update = {**self.update, "status": "error", "error": str(e)[:200]}

    def _update_loop(self) -> None:
        time.sleep(3)
        while not self.stop.is_set():
            self._auto_update()
            self.stop.wait(UPDATE_INTERVAL)

    # ----- Exit ----------------------------------------------------------------------------------
    def busy(self) -> bool:
        return bool(self.task and self.task.status in ("running", "cancelling"))

    def quit(self) -> None:
        """Closes all windows and stops the server (e.g. for an update)."""
        self.stop.set()
        try:
            import webview

            for w in list(webview.windows):
                w.destroy()
        except Exception:
            pass


class WindowApi:
    """Callable from the page (window.pywebview.api): things a browser cannot do."""

    def __init__(self):
        self._window = None

    def choose_folder(self, start: str = "") -> str:
        import webview

        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        result = self._window.create_file_dialog(kind, directory=start or "")
        return result[0] if result else ""


ICON = Path(__file__).with_name("icon.ico")


def _set_icon(window) -> None:
    """Our own icon in the title bar and taskbar (instead of the Python icon)."""
    if sys.platform != "win32":
        return
    import ctypes

    try:
        hwnd = int(window.native.Handle.ToInt64())
    except Exception:
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = ctypes.c_void_p
    user32.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
    user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
    for size, big in ((16, 0), (32, 1)):  # ICON_SMALL, ICON_BIG
        icon = user32.LoadImageW(None, str(ICON), 1, size * 2 if big else size, size * 2 if big else size, 0x10)
        if icon:
            user32.SendMessageW(hwnd, 0x80, big, icon)  # WM_SETICON


def _set_app_id() -> None:
    """Same app id as the shortcut, so Windows shows the WallyCull icon in the taskbar."""
    if sys.platform == "win32":
        import ctypes

        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WallyFrame.WallyCull")
        except Exception:
            pass


def show_window(address: str) -> bool:
    """Opens the app window (WebView2, not a browser) and waits until it is closed."""
    try:
        import webview
    except ImportError:
        webbrowser.open(address)
        return False
    api = WindowApi()
    window = webview.create_window("WallyCull", address, width=1440, height=940, min_size=(960, 640),
                                   js_api=api, text_select=True)
    api._window = window
    window.events.shown += lambda: _set_icon(window)
    USER_DIR.mkdir(parents=True, exist_ok=True)
    webview.start(private_mode=False, storage_path=str(USER_DIR / "window"))
    return True


def _folder_dialog(start: str | None) -> str:
    """Only without the app window (development): folder picker via tkinter."""
    script = ("import sys,tkinter as tk;from tkinter import filedialog;r=tk.Tk();r.withdraw();r.attributes('-topmost',True);"
              "print(filedialog.askdirectory(title='Choose photo folder',initialdir=sys.argv[1] or None,mustexist=True) or '')")
    r = subprocess.run([updater.python_exe(), "-c", script, start or ""], capture_output=True, text=True,
                       encoding="utf-8", creationflags=NO_WINDOW)
    return r.stdout.strip()


def _open_in_explorer(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(str(path))  # type: ignore[attr-defined]
    else:
        subprocess.Popen(["xdg-open", str(path)])


def make_handler(app: App):
    Base = rating.make_handler(app.get_session)

    class AppHandler(Base):
        def do_GET(self):
            if not rating.allowed(self, False):
                return self._json({"error": "forbidden"}, 403)
            app.last_contact = time.time()
            path = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if path == "/":
                    return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/icon.ico":
                    return self._send(200, ICON.read_bytes(), "image/x-icon")
                if path == "/api/state":
                    return self._json(app.state_dict())
                if path == "/api/reject_preview":
                    return self._json(app.reject_preview())
                if path == "/api/moved":
                    return self._json(app.moved_images())
                if path == "/rate":
                    app.get_session()
                    return self._send(200, RATING_PAGE.encode("utf-8"), "text/html; charset=utf-8")
                if path == "/report":
                    html = (database.data_dir(app.folder) / "report.html").read_text(encoding="utf-8")
                    html = html.replace("<header><h1>", '<header><a href="/" style="color:inherit;'
                                        'text-decoration:none;margin-right:8px">← Overview</a><h1>', 1)
                    return self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if path.startswith("/previews/") and app.folder:
                    name = Path(path).name
                    file = database.data_dir(app.folder) / "previews" / name
                    if name.endswith(".jpg") and file.exists():
                        return self._send(200, file.read_bytes(), "image/jpeg")
                    return self._json({"error": "not found"}, 404)
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            return super().do_GET()

        def do_POST(self):
            if not rating.allowed(self, True):
                return self._json({"error": "forbidden"}, 403)
            app.last_contact = time.time()
            if not self.path.startswith("/app/"):
                return super().do_POST()
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/app/folder":
                    app.set_folder(data["path"])
                elif self.path == "/app/choose_folder":
                    chosen = _folder_dialog(str(app.folder) if app.folder else None)
                    if chosen:
                        app.set_folder(chosen)
                elif self.path == "/app/task":
                    app.start_task(data["kind"], data)
                elif self.path == "/app/cancel":
                    if app.task:
                        app.task.cancel()
                elif self.path in ("/app/check_update", "/app/sign_in"):
                    app.update = {**app.update, "status": "checking"}
                    threading.Thread(target=app._auto_update, args=(self.path.endswith("sign_in"),), daemon=True).start()
                elif self.path == "/app/token":
                    updater.save_token(str(data.get("token", "")))
                    app.update = {**app.update, "status": "checking"}
                    threading.Thread(target=app._auto_update, daemon=True).start()
                elif self.path == "/app/link":
                    if str(data.get("url", "")).startswith("https://github.com/"):
                        webbrowser.open(data["url"])
                elif self.path == "/app/open_references":
                    REFERENCES_DIR.mkdir(parents=True, exist_ok=True)
                    _open_in_explorer(REFERENCES_DIR)
                elif self.path == "/app/open_rejected" and app.folder:
                    target = app.folder / app.config["files"]["rejected_folder"]
                    _open_in_explorer(target if target.exists() else app.folder)
                else:
                    return self._json({"error": "not found"}, 404)
                self._json(app.state_dict())
            except Exception as e:
                self._json({"error": str(e)}, 400)

    return AppHandler


def _already_running(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/state", timeout=2) as r:
            return json.loads(r.read()).get("app") == APP_ID
    except Exception:
        return False


def _redirect_output() -> None:
    """Without a console (started from the shortcut): messages and errors go to app.log."""
    if sys.stdout is None or sys.stderr is None:
        USER_DIR.mkdir(parents=True, exist_ok=True)
        log = open(USER_DIR / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def start(config: dict, port: int = 8765, window: bool = True) -> None:
    _redirect_output()
    _set_app_id()
    if _already_running(port):  # WallyCull is already running (e.g. analysis in the background): just reopen the window
        show_window(f"http://127.0.0.1:{port}/")
        return
    app = App(config)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    server.daemon_threads = True
    app.server = server
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} WallyCull {__version__} running: {address}", flush=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=app._update_loop, daemon=True).start()
    shown = show_window(address) if window else False
    # Window closed: running tasks (e.g. an analysis) may finish. A reopened window keeps the app alive.
    grace = 90 if shown else IDLE_EXIT
    closed = time.time()
    while not app.stop.is_set():
        if not app.busy() and time.time() - max(app.last_contact, closed) > grace:
            break
        app.stop.wait(5)
    app.stop.set()
    server.shutdown()
    server.server_close()


RATING_PAGE = rating.PAGE.replace(
    "<header><b>Rate photos</b>",
    '<header><a href="/" id="backToApp" style="color:inherit;text-decoration:none">← Overview</a><b>Rate photos</b>', 1,
).replace(
    "refill();\n</script>",
    'refill();\ndocument.getElementById("backToApp").onclick=async e=>{e.preventDefault();'
    'toast("Saving and classifying …",60000);await fetch("/api/finish",{method:"POST"});location.href="/";};\n</script>', 1,
)

PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WallyCull</title><link rel="icon" href="/icon.ico">
<style>
:root{--bg:#f4f3ef;--panel:#fff;--panel2:#ecebe6;--text:#1d1d1b;--muted:#6b6a66;--line:#dddbd4;--accent:#2b6cb0;--accent-text:#fff;
--highlight:#b7791f;--keep:#2f855a;--review:#2b6cb0;--reject:#c53030;--danger:#c53030}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--panel:#1f1f1d;--panel2:#2a2a27;--text:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#63b3ed;--accent-text:#0b1a26;
--highlight:#ecc94b;--keep:#68d391;--review:#63b3ed;--reject:#fc8181;--danger:#fc8181}}
:root[data-theme="dark"]{--bg:#141413;--panel:#1f1f1d;--panel2:#2a2a27;--text:#ecebe6;--muted:#a3a29b;--line:#34332f;--accent:#63b3ed;--accent-text:#0b1a26;
--highlight:#ecc94b;--keep:#68d391;--review:#63b3ed;--reject:#fc8181;--danger:#fc8181}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 24px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{font-size:17px;margin:0;font-weight:650}.muted{color:var(--muted)}.small{font-size:12.5px}
.badge{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);color:var(--muted)}
.badge.new{border-color:var(--accent);color:var(--accent)}
main{max-width:1180px;margin:0 auto;padding:20px 24px 140px;display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px}
@media (max-width:900px){main{grid-template-columns:1fr;padding:16px}}
.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:18px}
.card h2{font-size:15px;margin:0 0 10px;font-weight:650}
button{font:inherit;color:var(--text);background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--muted)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);color:var(--accent-text);border-color:var(--accent);font-weight:600}
button.danger{color:var(--danger);border-color:var(--danger)}
input[type=text]{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--line);border-radius:10px;padding:8px 10px;min-width:0}
.path{font-size:16px;font-weight:600;word-break:break-all;margin:2px 0 10px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.chips button{font-size:12.5px;padding:4px 10px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.numbers{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:4px 0 10px}
@media (max-width:600px){.numbers{grid-template-columns:repeat(2,1fr)}}
.number{border:1px solid var(--line);border-radius:12px;padding:10px 12px}.number b{display:block;font-size:24px;line-height:1.2}
.c-highlight{color:var(--highlight)}.c-keep{color:var(--keep)}.c-review{color:var(--review)}.c-reject{color:var(--reject)}
.step{display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:center;padding:14px 0;border-top:1px solid var(--line)}
.step:first-of-type{border-top:0}.nr{width:30px;height:30px;border-radius:50%;background:var(--panel2);display:grid;place-items:center;font-weight:650}
.step h3{margin:0;font-size:14.5px}.step p{margin:2px 0 0;color:var(--muted);font-size:13px}
@media (max-width:600px){.step{grid-template-columns:30px 1fr}.step>.row{grid-column:2}}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:12px;max-height:520px;overflow:auto}
.img{border:1px solid var(--line);border-radius:10px;overflow:hidden;font-size:12px;background:var(--bg)}
.img img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block;background:var(--panel2)}
.img div{padding:6px 8px}.img .n{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.img button{font-size:12px;padding:3px 8px;margin-top:4px}
.fold{display:none;margin-top:6px}.fold.open{display:block}
#task{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:min(760px,calc(100% - 32px));background:var(--panel);border:1px solid var(--line);
border-radius:14px;padding:14px 16px;box-shadow:0 10px 40px rgba(0,0,0,.18);display:none}
#task.on{display:block}
.bar{height:8px;background:var(--panel2);border-radius:99px;overflow:hidden;margin:8px 0}.bar i{display:block;height:100%;background:var(--accent);width:0;transition:width .4s}
.bar.busy i{width:30%;animation:slide 1.2s ease-in-out infinite alternate}@keyframes slide{from{margin-left:0}to{margin-left:70%}}
pre{margin:6px 0 0;max-height:150px;overflow:auto;font:12px/1.45 Consolas,monospace;color:var(--muted);white-space:pre-wrap}
#toast{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:10px 16px;display:none;z-index:9;box-shadow:0 6px 24px rgba(0,0,0,.15)}
.empty{color:var(--muted);padding:8px 0}
.line{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--line)}
</style></head><body>
<header><img src="/icon.ico" width="26" height="26" alt=""><h1>WallyCull</h1><span class="badge" id="version"></span>
<span style="flex:1"></span><span class="badge" id="updatebadge" hidden></span></header>
<main>
<div style="display:flex;flex-direction:column;gap:18px;min-width:0">
  <section class="card"><h2>Photo folder</h2>
    <div class="path" id="path">No folder chosen yet</div>
    <div class="row"><button class="primary" id="choose">Choose folder …</button>
      <input type="text" id="pathinput" placeholder="or paste a path, e.g. E:\DCIM\100_FUJI" style="flex:1;min-width:200px">
      <button id="pathok">Open</button></div>
    <div class="chips" id="recent"></div>
  </section>
  <section class="card" id="overview" hidden><h2>Overview</h2>
    <div class="numbers" id="numbers"></div><div class="muted small" id="details"></div>
  </section>
  <section class="card"><h2>Steps</h2>
    <div class="step"><div class="nr">1</div><div><h3>Analyze</h3><p>Measures sharpness, exposure and bursts, and (with AI) aesthetics and wall-art potential. Changes no files. Runs in the background.</p>
      <label class="small muted"><input type="checkbox" id="recompute"> measure everything again (otherwise only new images)</label></div>
      <div class="row"><button class="primary" data-task="analyze" data-folder>Analyze</button></div></div>
    <div class="step"><div class="nr">2</div><div><h3>Rate</h3><p>Like Tinder: ← reject, → keep, ↑ favorite. Your personal model learns from it.</p></div>
      <div class="row"><button data-link="/rate" data-db>Rate</button></div></div>
    <div class="step"><div class="nr">3</div><div><h3>Review the result</h3><p>All images by category, each with its reason.</p></div>
      <div class="row"><button data-link="/report" data-db>Open report</button></div></div>
    <div class="step"><div class="nr">4</div><div><h3>Move rejects</h3><p>Moves only the "Reject" category to <b class="rejname">_rejected</b>. Nothing is deleted.</p></div>
      <div class="row"><button id="previewbtn" data-db>Preview</button></div></div>
    <div class="fold" id="preview"></div>
    <div class="step"><div class="nr">5</div><div><h3>Restore</h3><p>Puts rejected images back where they were. Counts as "keep" for your model.</p></div>
      <div class="row"><button id="restorebtn" data-db>Show</button></div></div>
    <div class="fold" id="restore"></div>
    <div class="step"><div class="nr">6</div><div><h3>Delete for good</h3><p>Sends the contents of <span class="rejname">_rejected</span> to the Windows Recycle Bin. Only after you confirm.</p></div>
      <div class="row"><input type="text" id="confirmword" placeholder="type DELETE" style="width:150px"><button class="danger" id="purge" data-db>Recycle Bin</button></div></div>
  </section>
</div>
<aside style="display:flex;flex-direction:column;gap:18px">
  <section class="card"><h2>Overnight training</h2>
    <p class="small muted" style="margin:0 0 10px">Start it before you go to bed. WallyCull analyzes every folder you used, reads the stars you gave in the camera or Lightroom, trains your model thoroughly and sorts everything again. The PC stays awake until it is done.</p>
    <button class="primary" data-task="night" id="nightbtn">Start overnight run</button>
    <div id="nightstatus" class="small" style="margin-top:10px"></div></section>
  <section class="card"><h2>AI rating</h2><div id="aistatus" class="small"></div></section>
  <section class="card"><h2>Updates</h2><div id="updatestatus" class="small"></div>
    <div class="row" style="margin-top:10px"><button id="updatebtn">Check now</button><button id="rollbackbtn" title="Go back to the version before the last update">Previous version</button></div></section>
  <section class="card small muted">Everything runs on this PC only, including the AI. Photos are never uploaded. If you close the window, running tasks finish in the background.</section>
</aside>
</main>
<div id="task"><div class="row"><b id="ttitle"></b><span class="muted small" id="ttext"></span><span style="flex:1"></span>
<button id="cancel">Cancel</button><button id="tclose">Close</button></div>
<div class="bar" id="tbar"><i></i></div><pre id="tlog"></pre></div>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);
let S=null, version=null, dismissed=null;
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const plural=(n,w)=>`${n} ${w}${n===1?"":"s"}`;
const mb=b=>Math.round(b/1048576).toLocaleString("en-US")+" MB";
function html(el,h){if(el._h!==h){el._h=h;el.innerHTML=h;}}
function toast(t,ms=3500){const h=$("#toast");h.textContent=t;h.style.display="block";clearTimeout(h._t);h._t=setTimeout(()=>h.style.display="none",ms);}
async function post(path,data={}){
  const r=await fetch(path,{method:"POST",headers:{"X-WallyCull":"1"},body:JSON.stringify(data)});
  const j=await r.json(); if(j.error){toast(j.error,6000);return null;} draw(j); return j;
}
async function load(){
  try{const j=await fetch("/api/state").then(r=>r.json());
    if(version&&j.version!==version){location.reload();return;} version=j.version; draw(j);}
  catch(e){$("#version").textContent="restarting …";}
}
function draw(j){
  const old=S; S=j;
  $("#version").textContent="Version "+j.version;
  $("#path").textContent=j.folder||"No folder chosen yet";
  document.querySelectorAll(".rejname").forEach(e=>e.textContent=j.rejected_folder);
  $("#recent").innerHTML=j.recent.filter(r=>r!==j.folder).map(r=>`<button data-path="${esc(r)}" title="${esc(r)}">${esc(r)}</button>`).join("");
  const s=j.stats, running=j.task&&["running","cancelling"].includes(j.task.status);
  $("#overview").hidden=!s;
  if(s){
    $("#numbers").innerHTML=["highlight","keep","review","reject"].map(k=>`<div class="number c-${k}"><b>${s.categories[k]}</b>${j.names[k]}</div>`).join("");
    const m=s.model?`personal model active (${Math.round(s.model.accuracy*100)}% accuracy)`:"personal model starts at 40 ratings";
    $("#details").textContent=`${s.total} images · ${s.rated} rated · ${m} · AI scores for ${s.with_ai} images · ${s.moved} in ${j.rejected_folder} · updated ${s.updated}`
      +(s.analyzed<s.total?` · ${s.total-s.analyzed} not analyzed yet`:"");
  }
  document.querySelectorAll("[data-folder]").forEach(b=>b.disabled=!j.folder||running);
  document.querySelectorAll("[data-db]").forEach(b=>b.disabled=!s||running);
  $("#choose").disabled=$("#pathok").disabled=running;
  html($("#aistatus"),j.ai?"Set up. Aesthetics, wall-art potential and content are rated during the analysis."
    +"<p class='muted' style='margin:8px 0'>References: put favorite images (e.g. your best sellers) into the references folder. Photos in that style gain value.</p><button id='references'>Open references folder</button>"
    :`<p style="margin:0 0 10px">Not set up yet (normally the setup does this). Without AI only the technical quality is rated.</p><button class="primary" data-task="setup-ai" ${running?"disabled":""}>Set up AI (about 5 GB)</button>`);
  $("#nightbtn").disabled=running||!j.recent.length;
  html($("#nightstatus"),nightText(j.night));
  const u=j.update, ub=$("#updatebadge");
  let ut={new:`New version ${esc(u.new)} is being installed …`,current:`Up to date (checked ${u.checked||""})`,checking:"Checking …",unknown:"Checking …",
    development:"Developer version: updates only exist in the installed app.",error:"Could not check: "+esc(u.error||"")}[u.status]||esc(u.status);
  if(u.status==="sign_in") ut=`<p style="margin:0 0 10px">For automatic updates from your private repo, WallyCull needs your GitHub sign-in once.${u.error?" "+esc(u.error):""}</p>`
    +(u.git?`<button class="primary" id="signin">Connect with GitHub</button><p class="muted" style="margin:10px 0 6px">Or paste a read-only token:</p>`:`<p class="muted" style="margin:0 0 6px">Paste a read-only token:</p>`)
    +`<div class="row"><input type="text" id="token" placeholder="github_pat_…" style="flex:1;min-width:0"><button id="tokenok">Save</button></div>`
    +`<button id="tokenlink" style="margin-top:8px">Create a token on GitHub</button>`;
  html($("#updatestatus"),ut); ub.hidden=u.status!=="new"; ub.textContent="Update "+(u.new||""); ub.className="badge new";
  $("#rollbackbtn").textContent=u.previous?`Back to ${u.previous}`:"Previous version";
  $("#rollbackbtn").disabled=running||!u.previous;
  $("#updatebtn").disabled=running||["development","sign_in","checking"].includes(u.status);
  const t=j.task, box=$("#task");
  if(t&&dismissed!==t.kind+t.seconds&&(running||dismissed!==t.kind)){
    box.classList.add("on");
    $("#ttitle").textContent=t.title;
    const p=t.progress, st={running:"running",cancelling:"cancelling …",done:"done",failed:"failed",cancelled:"cancelled"}[t.status];
    $("#ttext").textContent=st+(p&&running?` · ${p.title} ${p.i}/${p.n}`:"")+` · ${Math.floor(t.seconds/60)}:${String(t.seconds%60).padStart(2,"0")} min`;
    const bar=$("#tbar"); bar.classList.toggle("busy",running&&!p); bar.querySelector("i").style.width=p?(100*p.i/Math.max(p.n,1))+"%":(running?"":"100%");
    $("#tlog").textContent=t.log.join("\n"); $("#tlog").scrollTop=1e9;
    $("#cancel").hidden=!(running&&t.cancellable); $("#tclose").hidden=running;
    if(old&&old.task&&["running","cancelling"].includes(old.task.status)&&!running){
      if($("#preview").classList.contains("open")) showPreview(); if($("#restore").classList.contains("open")) showRestore();
    }
  } else if(!t||!running) box.classList.remove("on");
}
$("#tclose").onclick=()=>{dismissed=S.task.kind; $("#task").classList.remove("on");};
$("#cancel").onclick=()=>post("/app/cancel");
$("#choose").onclick=async()=>{
  if(window.pywebview&&window.pywebview.api){const p=await window.pywebview.api.choose_folder(S&&S.folder||"");if(p)post("/app/folder",{path:p}).then(()=>closeFolds());}
  else{toast("The folder picker is open (maybe behind this window).");post("/app/choose_folder").then(()=>closeFolds());}};
$("#pathok").onclick=()=>{const p=$("#pathinput").value.trim();if(p)post("/app/folder",{path:p}).then(j=>{if(j){$("#pathinput").value="";closeFolds();}});};
$("#pathinput").onkeydown=e=>{if(e.key==="Enter")$("#pathok").click();};
$("#recent").onclick=e=>{const b=e.target.closest("button");if(b)post("/app/folder",{path:b.dataset.path}).then(()=>closeFolds());};
document.addEventListener("click",e=>{const b=e.target.closest("button");if(!b||b.disabled)return;
  if(b.dataset.task){dismissed=null;post("/app/task",{kind:b.dataset.task,recompute:$("#recompute").checked});}
  else if(b.dataset.link) location.href=b.dataset.link;});
function nightText(n){
  if(!n) return "";
  const when=(n.finished||n.started||"").replace("T"," ").slice(0,16);
  let h=`<b>${n.finished?"Last run":"Running since"} ${esc(when)}</b>`;
  for(const f of n.folders){
    const short=esc(f.folder.split(/[\\/]/).pop()||f.folder);
    if(f.error){h+=`<p style="margin:6px 0 0">${short}: <span style="color:var(--danger)">${esc(f.error)}</span></p>`;continue;}
    const m=f.model||{}, st=f.stars||{}, c=f.counts||{};
    const stars=(st.keep||0)+(st.favorite||0)+(st.reject||0);
    let line=`${short}: ${f.analyzed} newly analyzed`+(stars?`, ${stars} star ratings found`:"");
    if(m.active){line+=`. Model learned from ${m.rated} verdicts, accuracy ${Math.round(m.accuracy*100)}%`
      +(m.accuracy_before!=null?` (before ${Math.round(m.accuracy_before*100)}%)`:"")+".";}
    else line+=". "+esc(f.hint||"");
    h+=`<p style="margin:6px 0 0">${line}</p><p class="muted" style="margin:2px 0 0">Highlight ${c.highlight||0} · Keep ${c.keep||0} · Review ${c.review||0} · Reject ${c.reject||0}</p>`;
  }
  return h;
}
function closeFolds(){$("#preview").classList.remove("open");$("#restore").classList.remove("open");}
const name=p=>esc(p.split("/").pop());
const thumb=b=>`<img loading="lazy" src="${b.preview?"/previews/"+esc(b.preview):""}" alt="">`;
async function showPreview(){
  const v=await fetch("/api/reject_preview").then(r=>r.json()), el=$("#preview"); el.classList.add("open");
  el.innerHTML=v.images.length?`<div class="row"><b>${plural(v.images.length,"image")} (${mb(v.size)})</b> would be moved.<span style="flex:1"></span>
    <button class="primary" id="moveall">Move to ${esc(S.rejected_folder)} now</button></div>
    <div class="grid">${v.images.map(b=>`<div class="img">${thumb(b)}
    <div><div class="n" title="${esc(b.source)}">${name(b.source)}</div><div class="muted">${esc(b.reason)}</div></div></div>`).join("")}</div>`
    :'<div class="empty">Nothing to reject. Check the report or rate more images.</div>';
  const k=$("#moveall"); if(k) k.onclick=()=>{dismissed=null;post("/app/task",{kind:"reject"});};
}
async function showRestore(){
  const v=await fetch("/api/moved").then(r=>r.json()), el=$("#restore"); el.classList.add("open");
  if(!v.images.length){el.innerHTML=`<div class="empty">${esc(S.rejected_folder)} is empty.</div>`;return;}
  el.innerHTML=`<div class="row"><button id="restoreall">Restore all ${v.images.length}</button><button id="openrejected">Show folder in Explorer</button></div>
    ${v.runs.map(r=>`<div class="line"><span>Run ${esc(r.run.replace("_"," ").replace(/-(\d\d)-(\d\d)$/,":$1:$2"))} · ${plural(r.count,"image")}</span><button data-run="${esc(r.run)}">Restore this run</button></div>`).join("")}
    <div class="grid">${v.images.map(b=>`<div class="img">${thumb(b)}
    <div><div class="n" title="${esc(b.source)}">${name(b.source)}</div><button data-id="${b.id}">Restore</button></div></div>`).join("")}</div>`;
  $("#restoreall").onclick=()=>{if(confirm("Restore all rejected images?")){dismissed=null;post("/app/task",{kind:"restore"});}};
  $("#openrejected").onclick=()=>post("/app/open_rejected");
  el.querySelectorAll("[data-run]").forEach(b=>b.onclick=()=>{dismissed=null;post("/app/task",{kind:"restore",run:b.dataset.run});});
  el.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{dismissed=null;post("/app/task",{kind:"restore",ids:[+b.dataset.id]});});
}
$("#previewbtn").onclick=()=>$("#preview").classList.contains("open")?$("#preview").classList.remove("open"):showPreview();
$("#restorebtn").onclick=()=>$("#restore").classList.contains("open")?$("#restore").classList.remove("open"):showRestore();
$("#purge").onclick=()=>{const w=$("#confirmword").value.trim().toUpperCase();
  if(w!==S.confirm_word){toast(`Type ${S.confirm_word} to confirm.`);return;}
  dismissed=null;post("/app/task",{kind:"purge",confirm:w}).then(()=>{$("#confirmword").value="";});};
$("#updatebtn").onclick=()=>{toast("Looking for updates …");post("/app/check_update");};
$("#rollbackbtn").onclick=()=>{if(confirm(`Go back to version ${S.update.previous}? WallyCull restarts for a moment.`)){dismissed=null;post("/app/task",{kind:"rollback"});}};
document.addEventListener("click",e=>{const b=e.target.closest("button");if(!b)return;
  if(b.id==="references") post("/app/open_references");
  else if(b.id==="signin"){toast("The GitHub sign-in opens (maybe in your browser).",6000);post("/app/sign_in");}
  else if(b.id==="tokenok"){const v=$("#token").value.trim();if(v)post("/app/token",{token:v});}
  else if(b.id==="tokenlink") post("/app/link",{url:S.update.link});});
load(); setInterval(()=>{if(!document.hidden)load();},1500); setInterval(load,30000);
</script></body></html>
"""
