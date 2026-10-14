"""Die App: ein eigenes Fenster mit allen Funktionen, läuft komplett lokal (127.0.0.1).

Start über die Verknüpfung auf dem Desktop (pythonw -m fotosort). Aufwendige Schritte (Analyse, Verschieben,
KI-Installation) laufen als Hintergrund-Prozess mit niedriger Priorität, die App zeigt den Fortschritt.
Beim Start und alle 6 Stunden sucht sie nach Updates und spielt sie selbst ein.
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

from . import __version__, aktualisieren, datenbank, ki, oberflaeche
from .einteilung import KATEGORIEN, NAMEN
from .konfig import DATEN_ORDNER, PROGRAMM_ORDNER

OHNE_FENSTER = aktualisieren.OHNE_FENSTER
LEERLAUF_ENDE = 300  # Sekunden ohne offenes Fenster, nach denen die App sich beendet
UPDATE_ABSTAND = 6 * 3600
FORTSCHRITT = re.compile(r"^\s*(?P<titel>[^:|]{1,40}):\s*(?P<pz>\d+)%\|.*?\|\s*(?P<i>\d+)/(?P<n>\d+)")
TITEL = {
    "analysieren": "Analyse", "einteilen": "Einteilen", "aussortieren": "Aussortieren",
    "zurueckholen": "Zurückholen", "loeschen": "In den Papierkorb", "ki-installieren": "KI installieren",
    "update": "Update",
}


class Aufgabe:
    """Ein Hintergrund-Prozess (python -m fotosort ...), dessen Ausgabe mitgelesen wird."""

    def __init__(self, art: str, argumente: list[str], abbrechbar: bool, fertig=None):
        self.art, self.abbrechbar, self.fertig_rueckruf = art, abbrechbar, fertig
        self.protokoll: deque[str] = deque(maxlen=300)
        self.fortschritt: dict | None = None
        self.status, self.rueckgabe, self.start, self.ende = "laeuft", None, time.time(), None
        self.prozess = None
        if argumente is not None:
            umgebung = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
            self.prozess = subprocess.Popen(
                [aktualisieren.python_exe(), "-u", "-m", "fotosort", *argumente], cwd=PROGRAMM_ORDNER,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=umgebung,
                creationflags=OHNE_FENSTER,
            )
            threading.Thread(target=self._lesen, daemon=True).start()

    def zeile(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        t = FORTSCHRITT.match(text)
        if t:
            self.fortschritt = {"titel": t["titel"].strip(), "i": int(t["i"]), "n": int(t["n"])}
        else:
            self.protokoll.append(text)

    def _lesen(self) -> None:
        dekoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        rest = ""
        while True:
            stueck = self.prozess.stdout.read1(4096) if hasattr(self.prozess.stdout, "read1") else self.prozess.stdout.read(1)
            if not stueck:
                break
            rest += dekoder.decode(stueck)
            teile = re.split(r"[\r\n]", rest)
            rest = teile.pop()
            for t in teile:
                self.zeile(t)
        self.zeile(rest)
        self.beenden(self.prozess.wait())

    def beenden(self, code: int) -> None:
        self.rueckgabe, self.ende = code, time.time()
        self.status = "fertig" if code == 0 else ("abgebrochen" if self.status == "abbrechen" else "fehler")
        if self.fertig_rueckruf:
            try:
                self.fertig_rueckruf(self)
            except Exception as e:
                self.protokoll.append(f"Fehler danach: {e}")

    def abbrechen(self) -> None:
        if not self.prozess or self.status != "laeuft" or not self.abbrechbar:
            return
        self.status = "abbrechen"
        if sys.platform == "win32":  # samt Unterprozessen der Analyse
            subprocess.run(["taskkill", "/PID", str(self.prozess.pid), "/T", "/F"], capture_output=True, creationflags=OHNE_FENSTER)
        else:
            self.prozess.terminate()

    def als_dict(self) -> dict:
        return {
            "art": self.art, "titel": TITEL.get(self.art, self.art), "status": self.status,
            "fortschritt": self.fortschritt, "protokoll": list(self.protokoll)[-12:], "abbrechbar": self.abbrechbar,
            "sekunden": round((self.ende or time.time()) - self.start),
        }


class App:
    def __init__(self, konfig: dict):
        self.konfig = konfig
        self.sperre = threading.Lock()
        DATEN_ORDNER.mkdir(exist_ok=True)
        self.zustand_datei = DATEN_ORDNER / "app.json"
        try:
            self.zustand = json.loads(self.zustand_datei.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.zustand = {}
        self.zustand.setdefault("zuletzt", [])
        self.aufgabe: Aufgabe | None = None
        self.sitzung: oberflaeche.Sitzung | None = None
        self.update: dict = {"status": "unbekannt", "aktuell": __version__}
        self.letzter_kontakt = time.time()
        self.fenster_offen = False
        self.server: ThreadingHTTPServer | None = None
        self.neustart = False
        self.ki = ki.verfuegbar()[0]

    # ----- Zustand -------------------------------------------------------------------------------
    @property
    def ordner(self) -> Path | None:
        o = self.zustand.get("ordner")
        return Path(o) if o and Path(o).is_dir() else None

    def ordner_setzen(self, pfad: str) -> None:
        p = Path(pfad.strip().strip('"')).expanduser()
        if not p.is_dir():
            raise ValueError(f"Ordner nicht gefunden: {p}")
        p = p.resolve()
        with self.sperre:
            self.zustand["ordner"] = str(p)
            self.zustand["zuletzt"] = [str(p)] + [z for z in self.zustand["zuletzt"] if z != str(p)][:7]
            self.sitzung = None
            self.zustand_datei.write_text(json.dumps(self.zustand, ensure_ascii=False, indent=1), encoding="utf-8")

    def hole_sitzung(self) -> oberflaeche.Sitzung:
        with self.sperre:
            if not self.ordner:
                raise RuntimeError("Kein Foto-Ordner gewählt")
            if not self.sitzung or self.sitzung.foto_ordner != self.ordner:
                self.sitzung = oberflaeche.Sitzung(self.ordner, self.konfig)
            return self.sitzung

    def _db_pfad(self) -> Path | None:
        return self.ordner / ".fotosort" / "fotosort.db" if self.ordner else None

    def statistik(self) -> dict | None:
        pfad = self._db_pfad()
        if not pfad or not pfad.exists():
            return None
        db = sqlite3.connect(pfad)
        try:
            kat = dict(db.execute("SELECT kategorie, COUNT(*) FROM bilder WHERE status='aktiv' AND kategorie IS NOT NULL GROUP BY kategorie"))
            stat = {
                "kategorien": {k: kat.get(k, 0) for k in KATEGORIEN},
                "gesamt": db.execute("SELECT COUNT(*) FROM bilder WHERE status='aktiv'").fetchone()[0],
                "verschoben": db.execute("SELECT COUNT(DISTINCT bild_id) FROM verschiebungen WHERE zustand='verschoben'").fetchone()[0],
                "bewertet": db.execute("SELECT COUNT(*) FROM bewertungen").fetchone()[0],
                "mit_ki": db.execute("SELECT COUNT(*) FROM bilder WHERE status='aktiv' AND ki IS NOT NULL").fetchone()[0],
                "stand": time.strftime("%d.%m.%Y %H:%M", time.localtime(pfad.stat().st_mtime)),
            }
        except sqlite3.Error:
            return None
        finally:
            db.close()
        try:
            modell = json.loads((pfad.parent / "modell.json").read_text(encoding="utf-8"))
            stat["modell"] = {"trefferquote": modell.get("trefferquote"), "bewertet": modell.get("bewertet")}
        except (OSError, ValueError):
            stat["modell"] = None
        return stat

    def zustand_dict(self) -> dict:
        return {
            "app": "fotosort", "version": __version__, "ordner": str(self.ordner) if self.ordner else None,
            "zuletzt": [z for z in self.zustand["zuletzt"] if Path(z).is_dir()], "statistik": self.statistik(),
            "aufgabe": self.aufgabe.als_dict() if self.aufgabe else None, "update": self.update, "ki": self.ki,
            "namen": NAMEN,
        }

    def _abfrage(self, sql: str, werte=()) -> list[dict]:
        pfad = self._db_pfad()
        if not pfad or not pfad.exists():
            return []
        db = sqlite3.connect(pfad)
        db.row_factory = sqlite3.Row
        try:
            return [dict(z) for z in db.execute(sql, werte)]
        finally:
            db.close()

    def aussortieren_vorschau(self) -> dict:
        zeilen = self._abfrage("SELECT id, quelle, vorschau, gruende, groesse FROM bilder WHERE status='aktiv' "
                               "AND kategorie='aussortiert' ORDER BY schluessel")
        for z in zeilen:
            z["grund"] = (json.loads(z.pop("gruende") or "[]") or [""])[0]
        return {"bilder": zeilen, "groesse": sum(z["groesse"] or 0 for z in zeilen)}

    def verschobene(self) -> dict:
        laeufe = self._abfrage("SELECT lauf, COUNT(DISTINCT bild_id) AS anzahl FROM verschiebungen "
                               "WHERE zustand='verschoben' GROUP BY lauf ORDER BY lauf DESC")
        bilder = self._abfrage("SELECT DISTINCT b.id, b.quelle, b.vorschau, v.lauf FROM verschiebungen v "
                               "JOIN bilder b ON b.id = v.bild_id WHERE v.zustand='verschoben' ORDER BY b.schluessel")
        return {"laeufe": laeufe, "bilder": bilder}

    # ----- Aufgaben ------------------------------------------------------------------------------
    def starte(self, art: str, daten: dict) -> dict:
        with self.sperre:
            if self.aufgabe and self.aufgabe.status in ("laeuft", "abbrechen"):
                raise RuntimeError("Es läuft schon eine Aufgabe")
            if art == "update":
                self.aufgabe = Aufgabe("update", None, False)
                threading.Thread(target=self._update_einspielen, daemon=True).start()
                return self.aufgabe.als_dict()
            if art == "ki-installieren":
                self.aufgabe = Aufgabe(art, ["ki-installieren"], True, self._nach_aufgabe)
                return self.aufgabe.als_dict()
            if not self.ordner:
                raise RuntimeError("Bitte zuerst einen Foto-Ordner wählen")
            o = str(self.ordner)
            if art == "analysieren":
                argumente, abbrechbar = ["analysieren", o, "--kein-browser"] + (["--neu"] if daten.get("neu") else []), True
            elif art == "einteilen":
                argumente, abbrechbar = ["einteilen", o, "--kein-browser"], True
            elif art == "aussortieren":
                argumente, abbrechbar = ["aussortieren", o, "--ausfuehren"], False
            elif art == "zurueckholen":
                argumente, abbrechbar = ["zurueckholen", o], False
                if daten.get("lauf"):
                    argumente += ["--lauf", str(daten["lauf"])]
                elif daten.get("ids"):
                    for i in daten["ids"]:
                        argumente += ["--bild-id", str(int(i))]
                else:
                    argumente.append("--alle")
            elif art == "loeschen":
                if str(daten.get("bestaetigung", "")).strip().upper() not in ("LÖSCHEN", "LOESCHEN"):
                    raise ValueError("Zum Bestätigen bitte LÖSCHEN eintippen")
                argumente, abbrechbar = ["loeschen", o, "--bestaetigt"], False
            else:
                raise ValueError(f"Unbekannte Aufgabe: {art}")
            self.sitzung = None  # Bewerten danach mit frischen Daten
            self.aufgabe = Aufgabe(art, argumente, abbrechbar, self._nach_aufgabe)
            return self.aufgabe.als_dict()

    def _nach_aufgabe(self, aufgabe: Aufgabe) -> None:
        with self.sperre:
            self.sitzung = None
        if aufgabe.art == "ki-installieren":
            import importlib

            importlib.invalidate_caches()
            self.ki = ki.verfuegbar()[0]
        if self.update.get("status") == "neu":  # aufgeschobenes Update jetzt einspielen
            threading.Thread(target=self._update_automatisch, daemon=True).start()

    # ----- Updates -------------------------------------------------------------------------------
    def _update_pruefen(self) -> None:
        try:
            self.update = {**aktualisieren.pruefen(), "geprueft": time.strftime("%H:%M")}
        except Exception as e:
            self.update = {"status": "fehler", "aktuell": __version__, "fehler": str(e)[:200], "geprueft": time.strftime("%H:%M")}

    def _update_automatisch(self) -> None:
        self._update_pruefen()
        if self.update.get("status") == "neu":
            try:
                self.starte("update", {})
            except RuntimeError:
                pass  # eine Aufgabe läuft: wird danach eingespielt

    def _update_einspielen(self) -> None:
        a = self.aufgabe
        try:
            a.zeile(f"Installiere Version {self.update.get('neu', '')} ...")
            aktualisieren.installieren(a.zeile)
            a.zeile("Fertig. Die App startet neu ...")
            a.beenden(0)
            self.neustart = True
            time.sleep(1.5)
            self.server.shutdown()
        except Exception as e:
            a.zeile(f"Update fehlgeschlagen: {e}")
            a.beenden(1)
            self.update = {**self.update, "status": "fehler", "fehler": str(e)[:200]}

    def _update_schleife(self) -> None:
        time.sleep(3)
        while True:
            self._update_automatisch()
            time.sleep(UPDATE_ABSTAND)

    # ----- Fenster und Leerlauf ------------------------------------------------------------------
    def _beschaeftigt(self) -> bool:
        return bool(self.aufgabe and self.aufgabe.status in ("laeuft", "abbrechen"))

    def _leerlauf_schleife(self) -> None:
        while True:
            time.sleep(10)
            if self._beschaeftigt():
                continue
            if not self.fenster_offen and time.time() - self.letzter_kontakt > LEERLAUF_ENDE:
                self.server.shutdown()
                return

    def fenster_oeffnen(self, adresse: str) -> None:
        browser = _app_browser()
        if not browser:
            webbrowser.open(adresse)
            return
        profil = DATEN_ORDNER / "fenster"
        prozess = subprocess.Popen([browser, f"--app={adresse}", f"--user-data-dir={profil}", "--window-size=1400,900",
                                    "--no-first-run", "--no-default-browser-check", "--disable-features=Translate"])

        def warten():
            start = time.time()
            self.fenster_offen = True
            prozess.wait()
            self.fenster_offen = False
            if time.time() - start > 5:  # Fenster wurde geschlossen (nicht nur an laufendes Edge übergeben)
                self.letzter_kontakt = 0
        threading.Thread(target=warten, daemon=True).start()


def _app_browser() -> str | None:
    """Edge (auf Windows 11 immer da) oder Chrome im App-Modus: eigenes Fenster ohne Adressleiste."""
    kandidaten = []
    for basis in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if basis:
            kandidaten += [Path(basis) / "Microsoft/Edge/Application/msedge.exe", Path(basis) / "Google/Chrome/Application/chrome.exe"]
    return next((str(k) for k in kandidaten if k.exists()), None)


def _ordner_dialog(start: str | None) -> str:
    skript = ("import sys,tkinter as tk;from tkinter import filedialog;r=tk.Tk();r.withdraw();r.attributes('-topmost',True);"
              "print(filedialog.askdirectory(title='Foto-Ordner wählen',initialdir=sys.argv[1] or None,mustexist=True) or '')")
    r = subprocess.run([aktualisieren.python_exe(), "-c", skript, start or ""], capture_output=True, text=True,
                       encoding="utf-8", creationflags=OHNE_FENSTER)
    return r.stdout.strip()


def _handler_klasse(app: App):
    Basis = oberflaeche._handler(app.hole_sitzung)

    class AppHandler(Basis):
        def do_GET(self):
            if not oberflaeche.erlaubt(self, False):
                return self._json({"fehler": "verboten"}, 403)
            app.letzter_kontakt = time.time()
            pfad = urlparse(self.path).path.rstrip("/") or "/"
            try:
                if pfad == "/":
                    return self._senden(200, SEITE.encode("utf-8"), "text/html; charset=utf-8")
                if pfad == "/symbol.ico":
                    return self._senden(200, (Path(__file__).parent / "symbol.ico").read_bytes(), "image/x-icon")
                if pfad == "/api/zustand":
                    return self._json(app.zustand_dict())
                if pfad == "/api/aussortieren":
                    return self._json(app.aussortieren_vorschau())
                if pfad == "/api/verschoben":
                    return self._json(app.verschobene())
                if pfad == "/bewerten":
                    app.hole_sitzung()
                    return self._senden(200, BEWERTEN_SEITE.encode("utf-8"), "text/html; charset=utf-8")
                if pfad == "/bericht":
                    html = (datenbank.daten_ordner(app.ordner) / "bericht.html").read_text(encoding="utf-8")
                    html = html.replace("<header><h1>", '<header><a href="/" class="zurueck" style="color:inherit;'
                                        'text-decoration:none;margin-right:8px">← Übersicht</a><h1>', 1)
                    return self._senden(200, html.encode("utf-8"), "text/html; charset=utf-8")
                if pfad.startswith("/vorschau/") and app.ordner:
                    name = Path(pfad).name
                    datei = datenbank.daten_ordner(app.ordner) / "vorschau" / name
                    if name.endswith(".jpg") and datei.exists():
                        return self._senden(200, datei.read_bytes(), "image/jpeg")
                    return self._json({"fehler": "nicht gefunden"}, 404)
            except Exception as e:
                return self._json({"fehler": str(e)}, 500)
            return super().do_GET()

        def do_POST(self):
            if not oberflaeche.erlaubt(self, True):
                return self._json({"fehler": "verboten"}, 403)
            app.letzter_kontakt = time.time()
            if not self.path.startswith("/app/"):
                return super().do_POST()
            laenge = int(self.headers.get("Content-Length") or 0)
            try:
                daten = json.loads(self.rfile.read(laenge) or b"{}")
                if self.path == "/app/ordner":
                    app.ordner_setzen(daten["pfad"])
                elif self.path == "/app/ordner_waehlen":
                    gewaehlt = _ordner_dialog(str(app.ordner) if app.ordner else None)
                    if gewaehlt:
                        app.ordner_setzen(gewaehlt)
                elif self.path == "/app/aufgabe":
                    app.starte(daten["art"], daten)
                elif self.path == "/app/abbrechen":
                    if app.aufgabe:
                        app.aufgabe.abbrechen()
                elif self.path == "/app/update_pruefen":
                    threading.Thread(target=app._update_automatisch, daemon=True).start()
                elif self.path == "/app/zuruecksetzen":
                    aktualisieren.zuruecksetzen()
                    app.neustart = True
                    threading.Thread(target=app.server.shutdown, daemon=True).start()
                elif self.path == "/app/ordner_zeigen" and app.ordner:
                    ziel = app.ordner / app.konfig["dateien"]["aussortiert_ordner"]
                    os.startfile(str(ziel if ziel.exists() else app.ordner))  # type: ignore[attr-defined]
                else:
                    return self._json({"fehler": "nicht gefunden"}, 404)
                self._json(app.zustand_dict())
            except Exception as e:
                self._json({"fehler": str(e)}, 400)

    return AppHandler


def _laeuft_schon(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/zustand", timeout=2) as r:
            return json.loads(r.read()).get("app") == "fotosort"
    except Exception:
        return False


def _protokoll_umleiten() -> None:
    """pythonw hat keine Konsole: Meldungen und Fehler landen in daten/app.log."""
    if sys.stdout is None or sys.stderr is None:
        DATEN_ORDNER.mkdir(exist_ok=True)
        datei = open(DATEN_ORDNER / "app.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stdout or datei
        sys.stderr = sys.stderr or datei


def starten(konfig: dict, port: int = 8765, fenster: bool = True) -> None:
    _protokoll_umleiten()
    app = App(konfig)
    if _laeuft_schon(port):  # App ist schon offen: nur ein weiteres Fenster zeigen
        app.fenster_oeffnen(f"http://127.0.0.1:{port}/")
        return
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), _handler_klasse(app))
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_klasse(app))
    server.daemon_threads = True
    app.server = server
    adresse = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} WallyCull {__version__} läuft: {adresse}")
    threading.Thread(target=app._update_schleife, daemon=True).start()
    threading.Thread(target=app._leerlauf_schleife, daemon=True).start()
    if fenster:
        app.fenster_oeffnen(adresse)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    if app.neustart:  # nach einem Update: neue Version starten, das offene Fenster lädt sich selbst neu
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        subprocess.Popen([str(pythonw if pythonw.exists() else sys.executable), "-m", "fotosort", "app", "--ohne-fenster",
                          "--port", str(server.server_address[1])], cwd=PROGRAMM_ORDNER,
                         creationflags=(0x00000008 | 0x00000200) if sys.platform == "win32" else 0)


BEWERTEN_SEITE = oberflaeche.SEITE.replace(
    "<header><b>Fotos bewerten</b>",
    '<header><a href="/" id="zurueckApp" style="color:inherit;text-decoration:none">← Übersicht</a><b>Fotos bewerten</b>', 1,
).replace(
    "nachladen();\n</script>",
    'nachladen();\ndocument.getElementById("zurueckApp").onclick=async e=>{e.preventDefault();'
    'meldung("Speichere und teile ein …",60000);await fetch("/api/fertig",{method:"POST"});location.href="/";};\n</script>', 1,
)

SEITE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WallyCull</title><link rel="icon" href="/symbol.ico">
<style>
:root{--bg:#f4f3ef;--fl:#fff;--fl2:#ecebe6;--text:#1d1d1b;--leise:#6b6a66;--rand:#dddbd4;--akz:#2b6cb0;--akz-t:#fff;
--highlight:#b7791f;--behalten:#2f855a;--pruefen:#2b6cb0;--aussortiert:#c53030;--gefahr:#c53030}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141413;--fl:#1f1f1d;--fl2:#2a2a27;--text:#ecebe6;--leise:#a3a29b;--rand:#34332f;--akz:#63b3ed;--akz-t:#0b1a26;
--highlight:#ecc94b;--behalten:#68d391;--pruefen:#63b3ed;--aussortiert:#fc8181;--gefahr:#fc8181}}
:root[data-theme="dark"]{--bg:#141413;--fl:#1f1f1d;--fl2:#2a2a27;--text:#ecebe6;--leise:#a3a29b;--rand:#34332f;--akz:#63b3ed;--akz-t:#0b1a26;
--highlight:#ecc94b;--behalten:#68d391;--pruefen:#63b3ed;--aussortiert:#fc8181;--gefahr:#fc8181}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI Variable","Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:12px;padding:14px 24px;border-bottom:1px solid var(--rand);background:var(--fl)}
header h1{font-size:17px;margin:0;font-weight:650}.leise{color:var(--leise)}.klein{font-size:12.5px}
.marke{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--rand);color:var(--leise)}
.marke.neu{border-color:var(--akz);color:var(--akz)}
main{max-width:1180px;margin:0 auto;padding:20px 24px 140px;display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:18px}
@media (max-width:900px){main{grid-template-columns:1fr;padding:16px}}
.karte{background:var(--fl);border:1px solid var(--rand);border-radius:14px;padding:18px}
.karte h2{font-size:15px;margin:0 0 10px;font-weight:650}
button{font:inherit;color:var(--text);background:var(--fl2);border:1px solid var(--rand);border-radius:10px;padding:8px 14px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--leise)}button:disabled{opacity:.45;cursor:default}
button.haupt{background:var(--akz);color:var(--akz-t);border-color:var(--akz);font-weight:600}
button.gefahr{color:var(--gefahr);border-color:var(--gefahr)}
input[type=text]{font:inherit;color:inherit;background:var(--bg);border:1px solid var(--rand);border-radius:10px;padding:8px 10px;min-width:0}
.pfad{font-size:16px;font-weight:600;word-break:break-all;margin:2px 0 10px}
.reihe{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:10px}.chips button{font-size:12.5px;padding:4px 10px;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.zahlen{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:4px 0 10px}
@media (max-width:600px){.zahlen{grid-template-columns:repeat(2,1fr)}}
.zahl{border:1px solid var(--rand);border-radius:12px;padding:10px 12px}.zahl b{display:block;font-size:24px;line-height:1.2}
.k-highlight{color:var(--highlight)}.k-behalten{color:var(--behalten)}.k-pruefen{color:var(--pruefen)}.k-aussortiert{color:var(--aussortiert)}
.schritt{display:grid;grid-template-columns:34px 1fr auto;gap:12px;align-items:center;padding:14px 0;border-top:1px solid var(--rand)}
.schritt:first-of-type{border-top:0}.nr{width:30px;height:30px;border-radius:50%;background:var(--fl2);display:grid;place-items:center;font-weight:650}
.schritt h3{margin:0;font-size:14.5px}.schritt p{margin:2px 0 0;color:var(--leise);font-size:13px}
@media (max-width:600px){.schritt{grid-template-columns:30px 1fr}.schritt>.reihe{grid-column:2}}
.raster{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;margin-top:12px;max-height:520px;overflow:auto}
.bild{border:1px solid var(--rand);border-radius:10px;overflow:hidden;font-size:12px;background:var(--bg)}
.bild img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block;background:var(--fl2)}
.bild div{padding:6px 8px}.bild .n{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bild button{font-size:12px;padding:3px 8px;margin-top:4px}
.ausklapp{display:none;margin-top:6px}.ausklapp.offen{display:block}
#aufgabe{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);width:min(760px,calc(100% - 32px));background:var(--fl);border:1px solid var(--rand);
border-radius:14px;padding:14px 16px;box-shadow:0 10px 40px rgba(0,0,0,.18);display:none}
#aufgabe.an{display:block}
.balken{height:8px;background:var(--fl2);border-radius:99px;overflow:hidden;margin:8px 0}.balken i{display:block;height:100%;background:var(--akz);width:0;transition:width .4s}
.balken.offen i{width:30%;animation:hin 1.2s ease-in-out infinite alternate}@keyframes hin{from{margin-left:0}to{margin-left:70%}}
pre{margin:6px 0 0;max-height:150px;overflow:auto;font:12px/1.45 Consolas,monospace;color:var(--leise);white-space:pre-wrap}
#hinweis{position:fixed;top:14px;left:50%;transform:translateX(-50%);background:var(--fl);border:1px solid var(--rand);border-radius:10px;padding:10px 16px;display:none;z-index:9;box-shadow:0 6px 24px rgba(0,0,0,.15)}
.leer{color:var(--leise);padding:8px 0}
.zeile{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:6px 0;border-top:1px solid var(--rand)}
</style></head><body>
<header><img src="/symbol.ico" width="26" height="26" alt=""><h1>WallyCull</h1><span class="marke" id="version"></span>
<span style="flex:1"></span><span class="marke" id="updatemarke" hidden></span></header>
<main>
<div style="display:flex;flex-direction:column;gap:18px;min-width:0">
  <section class="karte"><h2>Foto-Ordner</h2>
    <div class="pfad" id="pfad">Noch kein Ordner gewählt</div>
    <div class="reihe"><button class="haupt" id="waehlen">Ordner wählen …</button>
      <input type="text" id="pfadeingabe" placeholder="oder Pfad einfügen, z. B. E:\DCIM\100_FUJI" style="flex:1;min-width:200px">
      <button id="pfadok">Öffnen</button></div>
    <div class="chips" id="zuletzt"></div>
  </section>
  <section class="karte" id="ueberblick" hidden><h2>Überblick</h2>
    <div class="zahlen" id="zahlen"></div><div class="leise klein" id="details"></div>
  </section>
  <section class="karte"><h2>Schritte</h2>
    <div class="schritt"><div class="nr">1</div><div><h3>Analysieren</h3><p>Misst Schärfe, Belichtung, Serien und (mit KI) Ästhetik und Wandbild-Eignung. Verändert keine Dateien. Läuft im Hintergrund.</p>
      <label class="klein leise"><input type="checkbox" id="neu"> alles neu messen (sonst nur neue Bilder)</label></div>
      <div class="reihe"><button class="haupt" data-art="analysieren" data-ordner>Analysieren</button></div></div>
    <div class="schritt"><div class="nr">2</div><div><h3>Bewerten</h3><p>Wie bei Tinder: ← weg, → behalten, ↑ Favorit. Dein persönliches Modell lernt daraus.</p></div>
      <div class="reihe"><button data-link="/bewerten" data-db>Bewerten</button></div></div>
    <div class="schritt"><div class="nr">3</div><div><h3>Ergebnis ansehen</h3><p>Alle Bilder nach Kategorie, mit Begründung.</p></div>
      <div class="reihe"><button data-link="/bericht" data-db>Bericht öffnen</button></div></div>
    <div class="schritt"><div class="nr">4</div><div><h3>Aussortieren</h3><p>Verschiebt nur die Kategorie „Aussortiert“ nach <b>_aussortiert</b>. Nichts wird gelöscht.</p></div>
      <div class="reihe"><button id="vorschauknopf" data-db>Vorschau</button></div></div>
    <div class="ausklapp" id="vorschau"></div>
    <div class="schritt"><div class="nr">5</div><div><h3>Zurückholen</h3><p>Holt aussortierte Bilder an ihren Platz zurück. Zählt als „behalten“ für dein Modell.</p></div>
      <div class="reihe"><button id="rettenknopf" data-db>Anzeigen</button></div></div>
    <div class="ausklapp" id="retten"></div>
    <div class="schritt"><div class="nr">6</div><div><h3>Endgültig löschen</h3><p>Schiebt den Inhalt von _aussortiert in den Windows-Papierkorb. Nur mit Bestätigung.</p></div>
      <div class="reihe"><input type="text" id="loeschwort" placeholder="LÖSCHEN eintippen" style="width:170px"><button class="gefahr" id="loeschen" data-db>Papierkorb</button></div></div>
  </section>
</div>
<aside style="display:flex;flex-direction:column;gap:18px">
  <section class="karte"><h2>KI-Bewertung</h2><div id="kistatus" class="klein"></div></section>
  <section class="karte"><h2>Updates</h2><div id="updatestatus" class="klein"></div>
    <div class="reihe" style="margin-top:10px"><button id="updateknopf">Jetzt prüfen</button><button id="zurueckknopf" title="Zur Version vor dem letzten Update">Vorherige Version</button></div></section>
  <section class="karte klein leise">Alles läuft nur auf diesem PC. Fotos werden nie hochgeladen. Wenn du das Fenster schließt, laufen gestartete Aufgaben zu Ende.</section>
</aside>
</main>
<div id="aufgabe"><div class="reihe"><b id="atitel"></b><span class="leise klein" id="atext"></span><span style="flex:1"></span>
<button id="abbrechen">Abbrechen</button><button id="aschliessen">Schließen</button></div>
<div class="balken" id="abalken"><i></i></div><pre id="aprotokoll"></pre></div>
<div id="hinweis"></div>
<script>
const $=s=>document.querySelector(s);
let Z=null, version=null, geschlossen=null;
const esc=t=>String(t??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const mb=b=>(b/1048576).toLocaleString("de-DE",{maximumFractionDigits:0})+" MB";
function hinweis(t,ms=3500){const h=$("#hinweis");h.textContent=t;h.style.display="block";clearTimeout(h._t);h._t=setTimeout(()=>h.style.display="none",ms);}
async function post(pfad,daten={}){
  const r=await fetch(pfad,{method:"POST",headers:{"X-Fotosort":"1"},body:JSON.stringify(daten)});
  const j=await r.json(); if(j.fehler){hinweis(j.fehler,6000);return null;} zeichnen(j); return j;
}
async function laden(){
  try{const j=await fetch("/api/zustand").then(r=>r.json());
    if(version&&j.version!==version){location.reload();return;} version=j.version; zeichnen(j);}
  catch(e){$("#version").textContent="startet neu …";}
}
function zeichnen(j){
  const alt=Z; Z=j;
  $("#version").textContent="Version "+j.version;
  $("#pfad").textContent=j.ordner||"Noch kein Ordner gewählt";
  $("#zuletzt").innerHTML=j.zuletzt.filter(z=>z!==j.ordner).map(z=>`<button data-pfad="${esc(z)}" title="${esc(z)}">${esc(z)}</button>`).join("");
  const s=j.statistik, laeuft=j.aufgabe&&["laeuft","abbrechen"].includes(j.aufgabe.status);
  $("#ueberblick").hidden=!s;
  if(s){
    $("#zahlen").innerHTML=["highlight","behalten","pruefen","aussortiert"].map(k=>`<div class="zahl k-${k}"><b>${s.kategorien[k]}</b>${j.namen[k]}</div>`).join("");
    const m=s.modell?`Persönliches Modell aktiv (${Math.round(s.modell.trefferquote*100)} % Treffer)`:"Persönliches Modell ab 40 Bewertungen";
    $("#details").textContent=`${s.gesamt} Bilder · ${s.bewertet} bewertet · ${m} · KI-Werte für ${s.mit_ki} Bilder · ${s.verschoben} in _aussortiert · Stand ${s.stand}`;
  }
  document.querySelectorAll("[data-ordner]").forEach(b=>b.disabled=!j.ordner||laeuft);
  document.querySelectorAll("[data-db]").forEach(b=>b.disabled=!s||laeuft);
  $("#waehlen").disabled=$("#pfadok").disabled=laeuft;
  $("#kistatus").innerHTML=j.ki?"Installiert. Ästhetik, Wandbild-Eignung und Inhalt werden beim Analysieren mitbewertet (Grafikkarte).<br><span class='leise'>Vorbilder: Lege Lieblingsbilder in den Ordner <b>vorbilder</b> im Programmordner.</span>"
    :`<p style="margin:0 0 10px">Noch nicht installiert. Ohne KI wird nur die Technik bewertet.</p><button class="haupt" data-art="ki-installieren" ${laeuft?"disabled":""}>KI installieren (ca. 3 GB)</button>`;
  const u=j.update, um=$("#updatemarke");
  const ut={neu:`Neue Version ${u.neu} wird installiert …`,aktuell:`Aktuell (geprüft ${u.geprueft||""})`,nicht_eingerichtet:"Updates sind nicht eingerichtet (kein git-Klon).",
    fehler:"Konnte nicht prüfen: "+esc(u.fehler||""),unbekannt:"Wird geprüft …"}[u.status]||u.status;
  $("#updatestatus").innerHTML=ut; um.hidden=u.status!=="neu"; um.textContent="Update "+(u.neu||""); um.className="marke neu";
  $("#updateknopf").disabled=$("#zurueckknopf").disabled=laeuft||u.status==="nicht_eingerichtet";
  const a=j.aufgabe, box=$("#aufgabe");
  if(a&&geschlossen!==a.art+a.sekunden&&(laeuft||geschlossen!==a.art)){
    box.classList.add("an");
    $("#atitel").textContent=a.titel;
    const f=a.fortschritt, st={laeuft:"läuft",abbrechen:"wird abgebrochen …",fertig:"fertig",fehler:"fehlgeschlagen",abgebrochen:"abgebrochen"}[a.status];
    $("#atext").textContent=st+(f&&laeuft?` · ${f.titel} ${f.i}/${f.n}`:"")+` · ${Math.floor(a.sekunden/60)}:${String(a.sekunden%60).padStart(2,"0")} min`;
    const bal=$("#abalken"); bal.classList.toggle("offen",laeuft&&!f); bal.querySelector("i").style.width=f?(100*f.i/Math.max(f.n,1))+"%":(laeuft?"":"100%");
    $("#aprotokoll").textContent=a.protokoll.join("\n"); $("#aprotokoll").scrollTop=1e9;
    $("#abbrechen").hidden=!(laeuft&&a.abbrechbar); $("#aschliessen").hidden=laeuft;
    if(alt&&alt.aufgabe&&["laeuft","abbrechen"].includes(alt.aufgabe.status)&&!laeuft){
      if($("#vorschau").classList.contains("offen")) vorschau(); if($("#retten").classList.contains("offen")) retten();
    }
  } else if(!a||!laeuft) box.classList.remove("an");
}
$("#aschliessen").onclick=()=>{geschlossen=Z.aufgabe.art; $("#aufgabe").classList.remove("an");};
$("#abbrechen").onclick=()=>post("/app/abbrechen");
$("#waehlen").onclick=()=>{hinweis("Ordner-Auswahl ist geöffnet (evtl. hinter diesem Fenster).");post("/app/ordner_waehlen").then(()=>schliessen());};
$("#pfadok").onclick=()=>{const p=$("#pfadeingabe").value.trim();if(p)post("/app/ordner",{pfad:p}).then(j=>{if(j){$("#pfadeingabe").value="";schliessen();}});};
$("#pfadeingabe").onkeydown=e=>{if(e.key==="Enter")$("#pfadok").click();};
$("#zuletzt").onclick=e=>{const b=e.target.closest("button");if(b)post("/app/ordner",{pfad:b.dataset.pfad}).then(()=>schliessen());};
document.addEventListener("click",e=>{const b=e.target.closest("button");if(!b||b.disabled)return;
  if(b.dataset.art){geschlossen=null;post("/app/aufgabe",{art:b.dataset.art,neu:$("#neu").checked});}
  else if(b.dataset.link) location.href=b.dataset.link;});
function schliessen(){$("#vorschau").classList.remove("offen");$("#retten").classList.remove("offen");}
async function vorschau(){
  const v=await fetch("/api/aussortieren").then(r=>r.json()), el=$("#vorschau"); el.classList.add("offen");
  el.innerHTML=v.bilder.length?`<div class="reihe"><b>${v.bilder.length} Bilder (${mb(v.groesse)})</b> würden verschoben.<span style="flex:1"></span>
    <button class="haupt" id="verschieben">Jetzt nach _aussortiert verschieben</button></div>
    <div class="raster">${v.bilder.map(b=>`<div class="bild"><img loading="lazy" src="${b.vorschau?"/vorschau/"+esc(b.vorschau):""}" alt="">
    <div><div class="n" title="${esc(b.quelle)}">${esc(b.quelle.split("/").pop())}</div><div class="leise">${esc(b.grund)}</div></div></div>`).join("")}</div>`
    :'<div class="leer">Nichts zum Aussortieren. Prüfe den Bericht oder bewerte mehr Bilder.</div>';
  const k=$("#verschieben"); if(k) k.onclick=()=>{geschlossen=null;post("/app/aufgabe",{art:"aussortieren"});};
}
async function retten(){
  const v=await fetch("/api/verschoben").then(r=>r.json()), el=$("#retten"); el.classList.add("offen");
  if(!v.bilder.length){el.innerHTML='<div class="leer">In _aussortiert liegt nichts.</div>';return;}
  el.innerHTML=`<div class="reihe"><button id="alleretten">Alle ${v.bilder.length} zurückholen</button><button id="ordnerzeigen">Ordner im Explorer zeigen</button></div>
    ${v.laeufe.map(l=>`<div class="zeile"><span>Lauf ${esc(l.lauf.replace("_"," ").replace(/-(\d\d)-(\d\d)$/,":$1:$2"))} · ${l.anzahl} Bilder</span><button data-lauf="${esc(l.lauf)}">Diesen Lauf zurückholen</button></div>`).join("")}
    <div class="raster">${v.bilder.map(b=>`<div class="bild"><img loading="lazy" src="${b.vorschau?"/vorschau/"+esc(b.vorschau):""}" alt="">
    <div><div class="n" title="${esc(b.quelle)}">${esc(b.quelle.split("/").pop())}</div><button data-id="${b.id}">Zurückholen</button></div></div>`).join("")}</div>`;
  $("#alleretten").onclick=()=>{if(confirm("Alle aussortierten Bilder zurückholen?")){geschlossen=null;post("/app/aufgabe",{art:"zurueckholen"});}};
  $("#ordnerzeigen").onclick=()=>post("/app/ordner_zeigen");
  el.querySelectorAll("[data-lauf]").forEach(b=>b.onclick=()=>{geschlossen=null;post("/app/aufgabe",{art:"zurueckholen",lauf:b.dataset.lauf});});
  el.querySelectorAll("[data-id]").forEach(b=>b.onclick=()=>{geschlossen=null;post("/app/aufgabe",{art:"zurueckholen",ids:[+b.dataset.id]});});
}
$("#vorschauknopf").onclick=()=>$("#vorschau").classList.contains("offen")?$("#vorschau").classList.remove("offen"):vorschau();
$("#rettenknopf").onclick=()=>$("#retten").classList.contains("offen")?$("#retten").classList.remove("offen"):retten();
$("#loeschen").onclick=()=>{const w=$("#loeschwort").value.trim().toUpperCase();
  if(w!=="LÖSCHEN"&&w!=="LOESCHEN"){hinweis("Zum Bestätigen LÖSCHEN eintippen.");return;}
  geschlossen=null;post("/app/aufgabe",{art:"loeschen",bestaetigung:w}).then(()=>{$("#loeschwort").value="";});};
$("#updateknopf").onclick=()=>{hinweis("Suche nach Updates …");post("/app/update_pruefen");};
$("#zurueckknopf").onclick=()=>{if(confirm("Zur Version vor dem letzten Update zurückgehen? Die App startet neu."))post("/app/zuruecksetzen");};
laden(); setInterval(()=>{if(!document.hidden)laden();},1500); setInterval(laden,30000);
</script></body></html>
"""
