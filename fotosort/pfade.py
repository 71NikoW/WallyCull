"""Wo die App ihre Dateien ablegt.

Programm (wird bei jedem Update ersetzt):  %LOCALAPPDATA%\\Programs\\WallyCull
Deine Daten (bleiben immer erhalten):     %LOCALAPPDATA%\\WallyCull  (Einstellungen, KI-Pakete, Modelle, Vorbilder)
Die Messwerte und Bewertungen liegen wie bisher im jeweiligen Foto-Ordner unter .fotosort.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROGRAMM_ORDNER = Path(__file__).resolve().parent.parent
if os.environ.get("WALLYCULL_DATEN"):
    BENUTZER_ORDNER = Path(os.environ["WALLYCULL_DATEN"])
elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
    BENUTZER_ORDNER = Path(os.environ["LOCALAPPDATA"]) / "WallyCull"
else:
    BENUTZER_ORDNER = Path.home() / ".wallycull"
KI_PAKETE = BENUTZER_ORDNER / "ki-pakete"
MODELL_ORDNER = BENUTZER_ORDNER / "modelle"
VORBILDER_ORDNER = BENUTZER_ORDNER / "vorbilder"
# Mit dem Setup installiert: eigenes Python im Programmordner
INSTALLIERT = (PROGRAMM_ORDNER / "python" / "python.exe").exists()


def einrichten() -> None:
    """KI-Pakete (liegen getrennt vom Programm, damit Updates klein bleiben) und Modell-Ablage bekannt machen."""
    if KI_PAKETE.is_dir() and str(KI_PAKETE) not in sys.path:
        sys.path.append(str(KI_PAKETE))  # hinten anhängen: die mitgelieferten Pakete haben Vorrang
    os.environ.setdefault("HF_HOME", str(MODELL_ORDNER / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(MODELL_ORDNER / "torch"))
