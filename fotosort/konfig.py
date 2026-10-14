"""Lädt config.yaml und stellt die Einstellungen als verschachteltes dict bereit."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .pfade import BENUTZER_ORDNER, PROGRAMM_ORDNER

STANDARD_KONFIG = PROGRAMM_ORDNER / "config.yaml"
# Alles, was dir gehört (eigene Einstellungen, App-Zustand), liegt hier und bleibt bei Updates erhalten.
DATEN_ORDNER = BENUTZER_ORDNER
EIGENE_KONFIG = DATEN_ORDNER / "eigene-einstellungen.yaml"


def _zusammenfuehren(basis: dict, neu: dict) -> dict:
    ergebnis = copy.deepcopy(basis)
    for schluessel, wert in (neu or {}).items():
        if isinstance(wert, dict) and isinstance(ergebnis.get(schluessel), dict):
            ergebnis[schluessel] = _zusammenfuehren(ergebnis[schluessel], wert)
        else:
            ergebnis[schluessel] = wert
    return ergebnis


def laden(pfad: str | Path | None = None) -> dict:
    """Lädt die Standard-Konfiguration und überlagert sie optional mit einer eigenen Datei."""
    with open(STANDARD_KONFIG, encoding="utf-8") as f:
        konfig = yaml.safe_load(f) or {}
    for datei in (EIGENE_KONFIG if EIGENE_KONFIG.exists() else None, pfad):
        if datei:
            with open(datei, encoding="utf-8") as f:
                konfig = _zusammenfuehren(konfig, yaml.safe_load(f) or {})
    for gruppe in ("raw", "bild", "begleit"):
        konfig["dateien"][gruppe] = [e.lower() for e in konfig["dateien"][gruppe]]
    return konfig
