"""Selbst-Update: holt das neueste Setup aus den Releases des privaten GitHub-Repos und spielt es still ein.

Für das private Repo braucht die App einmal eine GitHub-Anmeldung. Bevorzugt kommt sie vom Git Credential
Manager (ein Klick im Browser), sonst aus einem Lese-Schlüssel, den du in der App einfügst.
Deine Daten liegen getrennt vom Programm und bleiben bei jedem Update erhalten.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from .pfade import BENUTZER_ORDNER, INSTALLIERT

REPO = "71NikoW/foto-aussortierer"
API = f"https://api.github.com/repos/{REPO}"
OHNE_FENSTER = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
SCHLUESSEL_DATEI = BENUTZER_ORDNER / "github-schluessel.txt"
SCHLUESSEL_LINK = (
    "https://github.com/settings/personal-access-tokens/new?name=WallyCull-Updates"
    "&description=Nur+Lesen+fuer+WallyCull-Updates&target_name=71NikoW&expires_in=none&contents=read"
)


def _git_pfad() -> str | None:
    gefunden = shutil.which("git")
    if gefunden:
        return gefunden
    for basis in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for teil in ("Git/cmd/git.exe", "Programs/Git/cmd/git.exe"):
            if basis and (Path(basis) / teil).exists():
                return str(Path(basis) / teil)
    return None


def _git_anmeldung(fragen: bool) -> str | None:
    """Fragt den Git Credential Manager nach dem GitHub-Zugang (mit fragen=True öffnet er bei Bedarf das Login)."""
    git = _git_pfad()
    if not git:
        return None
    umgebung = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if not fragen:
        umgebung["GCM_INTERACTIVE"] = "never"
    eingabe = "protocol=https\nhost=github.com\n\n"
    try:
        r = subprocess.run([git, "credential", "fill"], input=eingabe, capture_output=True, text=True, timeout=300 if fragen else 20,
                           env=umgebung, creationflags=OHNE_FENSTER)
    except (OSError, subprocess.TimeoutExpired):
        return None
    werte = dict(z.split("=", 1) for z in r.stdout.splitlines() if "=" in z)
    if r.returncode or not werte.get("password"):
        return None
    if fragen:  # gespeichert wird erst nach Bestätigung
        subprocess.run([git, "credential", "approve"], input=r.stdout + "\n", text=True, env=umgebung, creationflags=OHNE_FENSTER)
    return werte["password"]


def _schluessel(fragen: bool = False) -> str | None:
    if os.environ.get("WALLYCULL_GITHUB_SCHLUESSEL"):
        return os.environ["WALLYCULL_GITHUB_SCHLUESSEL"]
    if SCHLUESSEL_DATEI.exists():
        text = SCHLUESSEL_DATEI.read_text(encoding="utf-8").strip()
        if text:
            return text
    return _git_anmeldung(fragen)


def schluessel_speichern(text: str) -> None:
    text = text.strip()
    if not re.fullmatch(r"(github_pat_|ghp_|gho_)[A-Za-z0-9_]{20,}", text):
        raise ValueError("Das sieht nicht wie ein GitHub-Schlüssel aus (beginnt mit github_pat_).")
    BENUTZER_ORDNER.mkdir(parents=True, exist_ok=True)
    SCHLUESSEL_DATEI.write_text(text, encoding="utf-8")


def _anfrage(url: str, schluessel: str, binaer: bool = False):
    req = urllib.request.Request(url, headers={"Accept": "application/octet-stream" if binaer else "application/vnd.github+json",
                                               "User-Agent": "WallyCull", "X-GitHub-Api-Version": "2022-11-28"})
    # nicht an die Download-Weiterleitung (S3) weitergeben, sonst lehnt die sie ab
    req.add_unredirected_header("Authorization", f"Bearer {schluessel}")
    return urllib.request.urlopen(req, timeout=60)


def _version_tupel(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def _releases(schluessel: str) -> list[dict]:
    with _anfrage(f"{API}/releases?per_page=10", schluessel) as r:
        alle = json.loads(r.read())
    ergebnis = []
    for rel in alle:
        if rel.get("draft") or rel.get("prerelease"):
            continue
        setup = next((a for a in rel.get("assets", []) if a["name"].lower().endswith(".exe")), None)
        if setup:
            ergebnis.append({"version": rel["tag_name"].lstrip("v"), "asset": setup["url"], "name": setup["name"],
                             "groesse": setup["size"], "notizen": (rel.get("body") or "")[:800]})
    return sorted(ergebnis, key=lambda r: _version_tupel(r["version"]), reverse=True)


def pruefen(fragen: bool = False) -> dict:
    if not INSTALLIERT:
        return {"status": "entwicklung", "aktuell": __version__}
    schluessel = _schluessel(fragen)
    if not schluessel:
        return {"status": "anmeldung", "aktuell": __version__, "git": bool(_git_pfad()), "link": SCHLUESSEL_LINK}
    try:
        releases = _releases(schluessel)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            return {"status": "anmeldung", "aktuell": __version__, "git": bool(_git_pfad()), "link": SCHLUESSEL_LINK,
                    "fehler": "GitHub hat den Zugriff abgelehnt."}
        raise
    if not releases:
        return {"status": "aktuell", "aktuell": __version__}
    neueste = releases[0]
    status = "neu" if _version_tupel(neueste["version"]) > _version_tupel(__version__) else "aktuell"
    vorher = next((r for r in releases if _version_tupel(r["version"]) < _version_tupel(__version__)), None)
    return {"status": status, "aktuell": __version__, "neu": neueste["version"], "_release": neueste,
            "vorherige": vorher["version"] if vorher else None, "_vorher": vorher, "notizen": neueste["notizen"]}


def herunterladen(release: dict, protokoll=print) -> Path:
    schluessel = _schluessel()
    if not schluessel:
        raise RuntimeError("keine GitHub-Anmeldung")
    ziel = Path(tempfile.gettempdir()) / release["name"]
    protokoll(f"Lade {release['name']} ({release['groesse'] / 1048576:.0f} MB) ...")
    with _anfrage(release["asset"], schluessel, binaer=True) as r, open(ziel, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    if ziel.stat().st_size != release["groesse"]:
        raise RuntimeError("Download unvollständig")
    return ziel


def setup_starten(setup: Path) -> None:
    """Startet das Setup unsichtbar. Es schließt die App, aktualisiert und startet sie neu."""
    subprocess.Popen([str(setup), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS"],
                     cwd=str(setup.parent), creationflags=(0x00000008 | 0x00000200) if sys.platform == "win32" else 0)


def python_exe() -> str:
    """python.exe im Programmordner: für Hintergrund-Aufgaben, deren Ausgabe die App mitliest."""
    pfad = Path(sys.executable)
    if pfad.name.lower() != "python.exe" and (pfad.parent / "python.exe").exists():
        return str(pfad.parent / "python.exe")
    return sys.executable
