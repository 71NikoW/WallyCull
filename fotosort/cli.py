"""Befehlszeile: python -m fotosort <befehl> "D:\\Fotos" """

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, analyse, bericht, dateien, einteilung, ki, konfig, lernen, oberflaeche

KI_PAKETE_INDEX = "https://download.pytorch.org/whl/cu126"


def _ordner(pfad: str) -> Path:
    p = Path(pfad).expanduser().resolve()
    if not p.is_dir():
        sys.exit(f"Ordner nicht gefunden: {p}")
    return p


def main(argv: list[str] | None = None) -> None:
    if sys.platform == "win32":
        for strom in (sys.stdout, sys.stderr):
            try:
                strom.reconfigure(encoding="utf-8")
            except Exception:
                pass

    p = argparse.ArgumentParser(prog="fotosort", description="WallyCull: Qualität und Wert bewerten, sicher aussortieren.")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--konfig", help="eigene Einstellungsdatei (überschreibt config.yaml)")
    unter = p.add_subparsers(dest="befehl", required=True)

    a = unter.add_parser("analysieren", help="Bilder messen, einteilen, Bericht erzeugen (fasst keine Dateien an)")
    a.add_argument("ordner")
    a.add_argument("--neu", action="store_true", help="Cache ignorieren und alles neu messen")
    a.add_argument("--kein-browser", action="store_true", help="Bericht nicht automatisch öffnen")
    a.add_argument("--ohne-ki", action="store_true", help="KI-Modelle (Phase 2) diesmal überspringen")

    e = unter.add_parser("einteilen", help="nur neu einteilen (z. B. nach Änderung der config.yaml)")
    e.add_argument("ordner")
    e.add_argument("--kein-browser", action="store_true")

    v = unter.add_parser("aussortieren", help="Aussortierte nach _aussortiert verschieben (Standard: Trockenlauf)")
    v.add_argument("ordner")
    v.add_argument("--ausfuehren", action="store_true", help="wirklich verschieben statt nur anzeigen")

    z = unter.add_parser("zurueckholen", help="Bilder aus _aussortiert zurückholen")
    z.add_argument("ordner")
    z.add_argument("--datei", help="nur Bilder, deren Pfad diesen Text enthält (z. B. DSCF1234)")
    z.add_argument("--lauf", help="nur Bilder eines bestimmten Laufs (Datum_Uhrzeit)")
    z.add_argument("--bild-id", type=int, action="append", help=argparse.SUPPRESS)
    z.add_argument("--alle", action="store_true", help="ohne Rückfrage alle zurückholen")

    l = unter.add_parser("loeschen", help="Inhalt von _aussortiert endgültig in den Papierkorb (mit Rückfrage)")
    l.add_argument("ordner")
    l.add_argument("--bestaetigt", action="store_true", help=argparse.SUPPRESS)

    w = unter.add_parser("bewerten", help="Bilder wie bei Tinder bewerten, das Modell lernt mit")
    w.add_argument("ordner")
    w.add_argument("--port", type=int, default=8765)

    t = unter.add_parser("trainieren", help="Modell aus allen Bewertungen trainieren und neu einteilen")
    t.add_argument("ordner")

    f = unter.add_parser("fehlerliste", help="Deine Anmerkungen (falsch analysierte Bilder) als CSV exportieren")
    f.add_argument("ordner")

    b = unter.add_parser("bericht", help="Bericht neu erzeugen und öffnen")
    b.add_argument("ordner")

    ap = unter.add_parser("app", help="App-Fenster öffnen (Standard, wenn kein Befehl angegeben ist)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--ohne-fenster", action="store_true", help=argparse.SUPPRESS)

    unter.add_parser("ki-installieren", help="PyTorch (Grafikkarte) und KI-Modelle installieren")

    argv = sys.argv[1:] if argv is None else argv
    args = p.parse_args(argv or ["app"])
    k = konfig.laden(args.konfig)

    if args.befehl == "app":
        from . import app
        return app.starten(k, port=args.port, fenster=not args.ohne_fenster)
    if args.befehl == "ki-installieren":
        return ki_installieren()
    ordner = _ordner(args.ordner)

    if args.befehl == "analysieren":
        try:
            stat = analyse.lauf(ordner, k, neu_berechnen=args.neu)
        except KeyboardInterrupt:
            sys.exit(1)
        if stat["fehler"]:
            print(f"{stat['fehler']} Dateien konnten nicht gelesen werden (stehen im Bericht unter 'Prüfen').")
        if not args.ohne_ki:
            try:
                ki.lauf(ordner, k, neu_berechnen=args.neu)
            except KeyboardInterrupt:
                sys.exit(1)
            except Exception as e:  # KI-Probleme dürfen die klassische Analyse nicht verhindern
                print(f"KI-Analyse übersprungen: {type(e).__name__}: {e}")
    if args.befehl in ("analysieren", "einteilen"):
        zaehlung = einteilung.lauf(ordner, k)
        print("Ergebnis: " + ", ".join(f"{einteilung.NAMEN[n]}: {zaehlung[n]}" for n in einteilung.KATEGORIEN))
        ziel = bericht.erstellen(ordner, oeffnen=not args.kein_browser)
        print(f"Bericht: {ziel}")
        print("Es wurden keine Dateien verändert. Nächster Schritt: 'aussortieren' (zeigt erst nur an, was passieren würde).")
    elif args.befehl == "aussortieren":
        dateien.verschieben(ordner, k, ausfuehren=args.ausfuehren)
        if args.ausfuehren:
            bericht.erstellen(ordner)
    elif args.befehl == "zurueckholen":
        if not args.datei and not args.lauf and not args.alle and not args.bild_id:
            antwort = input("Wirklich ALLE aussortierten Bilder zurückholen? (j/n) ").strip().lower()
            if antwort not in ("j", "ja", "y"):
                sys.exit("Abgebrochen.")
        if dateien.zurueckholen(ordner, k, datei=args.datei, lauf=args.lauf, bild_ids=args.bild_id):
            einteilung.lauf(ordner, k)
            bericht.erstellen(ordner)
    elif args.befehl == "loeschen":
        dateien.endgueltig_loeschen(ordner, k, ohne_rueckfrage=args.bestaetigt)
    elif args.befehl == "bewerten":
        oberflaeche.starten(ordner, k, port=args.port)
    elif args.befehl == "trainieren":
        status = lernen.trainieren(ordner, k)
        if status["aktiv"]:
            print(f"Modell trainiert aus {status['bewertet']} Bewertungen, Trefferquote {status['trefferquote'] * 100:.0f} %.")
        else:
            print(status["hinweis"])
        zaehlung = einteilung.lauf(ordner, k)
        print("Ergebnis: " + ", ".join(f"{einteilung.NAMEN[n]}: {zaehlung[n]}" for n in einteilung.KATEGORIEN))
        bericht.erstellen(ordner)
    elif args.befehl == "fehlerliste":
        print(f"Gespeichert: {bericht.anmerkungen_export(ordner)}")
    elif args.befehl == "bericht":
        print(bericht.erstellen(ordner, oeffnen=True))


def ki_installieren() -> None:
    """Installiert PyTorch mit NVIDIA-Unterstützung und die KI-Pakete in die Umgebung der App."""
    import subprocess

    pip = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    print("Installiere PyTorch mit Grafikkarten-Unterstützung (Download ca. 3 GB) ...", flush=True)
    subprocess.run(pip + ["torch", "torchvision", "--index-url", KI_PAKETE_INDEX], check=True)
    subprocess.run(pip + ["-r", str(konfig.PROGRAMM_ORDNER / "requirements-ki.txt")], check=True)
    print("Installiere optionale Zusatzmodelle (MUSIQ, NIMA) ...", flush=True)
    if subprocess.run(pip + ["pyiqa"]).returncode:
        print("Hinweis: pyiqa ließ sich nicht installieren. Es geht ohne MUSIQ/NIMA weiter.")
    subprocess.run([sys.executable, "-c", "import torch;print('Grafikkarte:', torch.cuda.get_device_name(0) "
                    "if torch.cuda.is_available() else 'nicht erkannt, KI läuft auf der CPU (langsamer)')"])
    print("Fertig. Beim nächsten Analysieren werden die KI-Modelle einmalig geladen (ca. 2 GB).")
