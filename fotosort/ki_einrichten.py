"""Richtet die KI ein: PyTorch (mit NVIDIA-Unterstützung, falls vorhanden), CLIP, pyiqa und die Modelle.

Wird vom Setup automatisch aufgerufen (python -m fotosort ki-installieren) und lässt sich in der App wiederholen.
Die Pakete landen in %LOCALAPPDATA%\\WallyCull\\ki-pakete, getrennt vom Programm: Updates bleiben dadurch klein.
Zeilen, die mit "SCHRITT " beginnen, zeigt das Setup als Überschrift an.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .pfade import KI_PAKETE, PROGRAMM_ORDNER

INDEX_CUDA = "https://download.pytorch.org/whl/cu126"
INDEX_CPU = "https://download.pytorch.org/whl/cpu"
MARKE = KI_PAKETE / ".wallycull-ki.json"


def _melden(text: str) -> None:
    # Das Setup liest die Ausgabe mit: Umlaute umschreiben, damit nichts verstümmelt ankommt
    for alt, neu in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("Ä", "Ae"), ("Ö", "Oe"), ("Ü", "Ue"), ("ß", "ss")):
        text = text.replace(alt, neu)
    print(text, flush=True)


def nvidia_vorhanden() -> bool:
    if shutil.which("nvidia-smi"):
        return True
    return Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe").exists()


def _soll(cuda: bool) -> dict:
    anforderungen = (PROGRAMM_ORDNER / "requirements-ki.txt").read_bytes()
    return {"anforderungen": hashlib.sha256(anforderungen).hexdigest()[:16], "cuda": cuda,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}"}


def _pip(ziel: Path, pakete: list[str], index: str) -> int:
    befehl = [sys.executable, "-m", "pip", "install", "--target", str(ziel), "--upgrade", "--no-warn-script-location",
              "--disable-pip-version-check", "--progress-bar", "off", "--extra-index-url", index, *pakete]
    return subprocess.run(befehl).returncode


def installieren(erzwingen: bool = False) -> int:
    if os.environ.get("WALLYCULL_KI_UEBERSPRINGEN"):  # Test beim Bauen des Setups
        _melden("SCHRITT KI-Einrichtung uebersprungen.")
        return 0
    cuda = nvidia_vorhanden()
    soll = _soll(cuda)
    try:
        ist = json.loads(MARKE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        ist = None
    if ist == soll and not erzwingen:
        _melden("SCHRITT KI-Pakete sind schon eingerichtet.")
    else:
        _melden(f"SCHRITT 1/2: KI-Pakete herunterladen ({'fuer deine NVIDIA-Grafikkarte, ' if cuda else ''}ca. 3 GB) ...")
        neu = KI_PAKETE.with_name("ki-pakete.neu")
        shutil.rmtree(neu, ignore_errors=True)
        anforderungen = ["-r", str(PROGRAMM_ORDNER / "requirements-ki.txt")]
        # Alles in einem Durchgang, sonst holt pip für pyiqa ein zweites (CPU-)PyTorch
        code = _pip(neu, ["torch", "torchvision", *anforderungen, "pyiqa"], INDEX_CUDA if cuda else INDEX_CPU)
        if code:
            _melden("Hinweis: Mit MUSIQ/NIMA (pyiqa) ging es nicht, versuche es ohne ...")
            shutil.rmtree(neu, ignore_errors=True)
            code = _pip(neu, ["torch", "torchvision", *anforderungen], INDEX_CUDA if cuda else INDEX_CPU)
        if code:
            shutil.rmtree(neu, ignore_errors=True)
            _melden("FEHLER: Die KI-Pakete liessen sich nicht installieren (Internetverbindung?).")
            return 1
        shutil.rmtree(KI_PAKETE, ignore_errors=True)
        neu.rename(KI_PAKETE)
        MARKE.write_text(json.dumps(soll), encoding="utf-8")
    _melden("SCHRITT 2/2: KI-Modelle herunterladen (einmalig ca. 2 GB) ...")
    # Neuer Prozess, damit die gerade installierten Pakete sicher gefunden werden
    code = subprocess.run([sys.executable, "-u", "-m", "fotosort", "ki-modelle-laden"]).returncode
    if code:
        _melden("FEHLER: Die KI-Modelle konnten nicht geladen werden. Beim naechsten Analysieren wird es erneut versucht.")
        return 2
    _melden("SCHRITT KI ist bereit.")
    return 0


def modelle_laden(konfig: dict) -> None:
    from . import ki

    modelle = ki.Modelle(konfig)
    _melden(f"Modelle geladen: CLIP, LAION-Aesthetik{' ' + ', '.join(modelle.iqa) if modelle.iqa else ''}.")
