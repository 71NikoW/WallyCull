"""Selbst-Update der App aus dem privaten GitHub-Repo (über git, Anmeldung macht der Git Credential Manager).

Der Programmordner ist ein git-Klon. Deine Daten (daten/, modelle/, vorbilder/, .venv) sind von git
ausgenommen und bleiben bei jedem Update unangetastet. Die Datenbanken liegen ohnehin in den Foto-Ordnern.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .konfig import PROGRAMM_ORDNER

ZWEIG = "main"
OHNE_FENSTER = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def _git_pfad() -> str | None:
    gefunden = shutil.which("git")
    if gefunden:
        return gefunden
    for basis in (os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        for teil in ("Git/cmd/git.exe", "Programs/Git/cmd/git.exe"):
            if basis and (Path(basis) / teil).exists():
                return str(Path(basis) / teil)
    return None


def _git(*args: str, zeit: int = 120) -> str:
    git = _git_pfad()
    if not git:
        raise RuntimeError("git ist nicht installiert")
    umgebung = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run([git, "-C", str(PROGRAMM_ORDNER), *args], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=zeit, env=umgebung, creationflags=OHNE_FENSTER)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1] if (r.stderr or r.stdout).strip() else "git-Fehler")
    return r.stdout.strip()


def _version_tupel(text: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", text)[:3])


def eingerichtet() -> bool:
    return (PROGRAMM_ORDNER / ".git").exists() and _git_pfad() is not None


def pruefen() -> dict:
    """Holt den neuesten Stand und vergleicht die Version. Ändert noch nichts an den Dateien."""
    if not eingerichtet():
        return {"status": "nicht_eingerichtet", "aktuell": __version__}
    _git("fetch", "--quiet", "origin", ZWEIG)
    init = _git("show", f"origin/{ZWEIG}:fotosort/__init__.py")
    treffer = re.search(r'__version__\s*=\s*"([^"]+)"', init)
    neu = treffer.group(1) if treffer else __version__
    status = "neu" if _version_tupel(neu) > _version_tupel(__version__) else "aktuell"
    return {"status": status, "aktuell": __version__, "neu": neu}


def installieren(protokoll=print) -> dict:
    """Spielt den neuesten Stand ein. Bei Fehlern bleibt die alte Version unverändert."""
    vorher = _git("rev-parse", "HEAD")
    _git("fetch", "--quiet", "origin", ZWEIG)
    geaendert = _git("diff", "--name-only", vorher, f"origin/{ZWEIG}").splitlines()
    _git("reset", "--hard", "--quiet", f"origin/{ZWEIG}")
    (PROGRAMM_ORDNER / "daten").mkdir(exist_ok=True)
    (PROGRAMM_ORDNER / "daten" / "vorherige-version.txt").write_text(vorher, encoding="utf-8")
    python = python_exe()
    pip = [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
    if "requirements.txt" in geaendert:
        protokoll("Aktualisiere Pakete ...")
        subprocess.run(pip + ["-r", str(PROGRAMM_ORDNER / "requirements.txt")], creationflags=OHNE_FENSTER)
    if "requirements-ki.txt" in geaendert and _ki_installiert():
        subprocess.run(pip + ["-r", str(PROGRAMM_ORDNER / "requirements-ki.txt")], creationflags=OHNE_FENSTER)
    return {"vorher": vorher, "dateien": len(geaendert)}


def zuruecksetzen() -> None:
    """Geht zur Version vor dem letzten Update zurück (falls ein Update Probleme macht)."""
    datei = PROGRAMM_ORDNER / "daten" / "vorherige-version.txt"
    if not datei.exists():
        raise RuntimeError("keine vorherige Version gespeichert")
    _git("reset", "--hard", "--quiet", datei.read_text(encoding="utf-8").strip())


def _ki_installiert() -> bool:
    import importlib.util

    return importlib.util.find_spec("torch") is not None


def python_exe() -> str:
    """python.exe neben pythonw.exe: für Hintergrund-Aufgaben, deren Ausgabe die App mitliest."""
    pfad = Path(sys.executable)
    if pfad.name.lower() == "pythonw.exe" and (pfad.parent / "python.exe").exists():
        return str(pfad.parent / "python.exe")
    return sys.executable
