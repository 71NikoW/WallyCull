"""SQLite-Datenbank: speichert Messwerte, Gruppen, Entscheidungen und jede Dateiverschiebung.

Die Datenbank liegt im Foto-Ordner unter .fotosort/fotosort.db. Pfade werden relativ
zum Foto-Ordner gespeichert, damit ein geänderter Laufwerksbuchstabe nichts kaputt macht.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS bilder (
    id            INTEGER PRIMARY KEY,
    schluessel    TEXT UNIQUE NOT NULL,   -- Ordner/Dateiname ohne Endung (klein)
    dateien       TEXT NOT NULL,          -- JSON-Liste aller zugehörigen Dateien (relativ)
    quelle        TEXT,                   -- Datei, die analysiert wurde
    groesse       INTEGER,
    mtime         REAL,
    version       INTEGER,
    breite        INTEGER,
    hoehe         INTEGER,
    aufnahme      TEXT,                   -- ISO-Datum der Aufnahme (EXIF)
    kamera        TEXT,
    phash         TEXT,
    dhash         TEXT,
    metriken      TEXT,                   -- JSON mit allen Messwerten
    qualitaet     REAL,
    wert          REAL,                   -- ab Phase 2
    vorschau      TEXT,
    fehler        TEXT,
    gruppe        INTEGER,
    gruppe_typ    TEXT,                   -- duplikat | serie
    gruppe_rang   INTEGER,                -- 1 = bestes Bild der Gruppe
    kategorie     TEXT,                   -- highlight | behalten | pruefen | aussortiert
    gruende       TEXT,                   -- JSON-Liste von Begründungen
    status        TEXT DEFAULT 'aktiv',   -- aktiv | verschoben | geloescht | fehlt
    nutzer        TEXT,                   -- eigene Entscheidung (z. B. 'behalten' nach Zurückholen)
    persoenlich   REAL,                   -- Prognose deines Lernmodells: Wahrscheinlichkeit "behalten"
    ki            TEXT,                   -- JSON mit KI-Werten (Ästhetik, Inhalt, ...), Phase 2
    embedding     BLOB,                   -- CLIP-Bildmerkmale (768 x float16)
    ki_version    INTEGER,
    gesehen       INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS verschiebungen (
    id          INTEGER PRIMARY KEY,
    bild_id     INTEGER NOT NULL,
    quelle      TEXT NOT NULL,
    ziel        TEXT NOT NULL,
    zeit        TEXT DEFAULT (datetime('now', 'localtime')),
    lauf        TEXT,
    zustand     TEXT DEFAULT 'verschoben' -- verschoben | zurueck | geloescht
);
CREATE TABLE IF NOT EXISTS bewertungen (
    bild_id     INTEGER PRIMARY KEY,      -- eine (die letzte) Bewertung pro Bild
    urteil      INTEGER NOT NULL,         -- 0 = weg, 1 = behalten, 2 = Favorit
    sterne      INTEGER,                  -- 1 bis 5, falls mit Sternen bewertet
    quelle      TEXT DEFAULT 'wischen',   -- wischen | zurueckgeholt | bericht
    zeit        TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS anmerkungen (
    id          INTEGER PRIMARY KEY,
    bild_id     INTEGER NOT NULL,
    art         TEXT NOT NULL,            -- motiv | stoerung | schaerfe | verwacklung | belichtung | serie | sonstiges
    x REAL, y REAL, b REAL, h REAL,       -- markierter Bereich, 0..1 relativ zur Bildgröße (optional)
    text        TEXT,
    zeit        TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_anm_bild ON anmerkungen(bild_id);
CREATE INDEX IF NOT EXISTS idx_bilder_kategorie ON bilder(kategorie);
CREATE INDEX IF NOT EXISTS idx_versch_bild ON verschiebungen(bild_id);
"""


def daten_ordner(foto_ordner: Path) -> Path:
    ordner = Path(foto_ordner) / ".fotosort"
    ordner.mkdir(parents=True, exist_ok=True)
    return ordner


def oeffnen(foto_ordner: Path) -> sqlite3.Connection:
    pfad = daten_ordner(foto_ordner) / "fotosort.db"
    verbindung = sqlite3.connect(pfad)
    verbindung.row_factory = sqlite3.Row
    verbindung.execute("PRAGMA journal_mode=WAL")
    verbindung.executescript(SCHEMA)
    # Ältere Datenbanken um neue Spalten ergänzen
    vorhanden = {z[1] for z in verbindung.execute("PRAGMA table_info(bilder)")}
    for spalte, typ in (("nutzer", "TEXT"), ("persoenlich", "REAL"), ("ki", "TEXT"), ("embedding", "BLOB"), ("ki_version", "INTEGER")):
        if spalte not in vorhanden:
            verbindung.execute(f"ALTER TABLE bilder ADD COLUMN {spalte} {typ}")
    return verbindung


def bild_speichern(db: sqlite3.Connection, e: dict) -> None:
    """Schreibt das Analyse-Ergebnis eines Bildes (neu oder aktualisiert)."""
    db.execute(
        """
        INSERT INTO bilder (schluessel, dateien, quelle, groesse, mtime, version, breite, hoehe,
                            aufnahme, kamera, phash, dhash, metriken, qualitaet, vorschau, fehler,
                            status, gesehen)
        VALUES (:schluessel, :dateien, :quelle, :groesse, :mtime, :version, :breite, :hoehe,
                :aufnahme, :kamera, :phash, :dhash, :metriken, :qualitaet, :vorschau, :fehler,
                'aktiv', 1)
        ON CONFLICT(schluessel) DO UPDATE SET
            dateien=excluded.dateien, quelle=excluded.quelle, groesse=excluded.groesse,
            mtime=excluded.mtime, version=excluded.version, breite=excluded.breite,
            hoehe=excluded.hoehe, aufnahme=excluded.aufnahme, kamera=excluded.kamera,
            phash=excluded.phash, dhash=excluded.dhash, metriken=excluded.metriken,
            qualitaet=excluded.qualitaet, vorschau=excluded.vorschau, fehler=excluded.fehler,
            status='aktiv', gesehen=1
        """,
        {
            **e,
            "dateien": json.dumps(e["dateien"], ensure_ascii=False),
            "metriken": json.dumps(e.get("metriken") or {}, ensure_ascii=False),
        },
    )


def aktive_bilder(db: sqlite3.Connection) -> list[dict]:
    zeilen = db.execute("SELECT * FROM bilder WHERE status = 'aktiv' ORDER BY schluessel").fetchall()
    return [zeile_zu_dict(z) for z in zeilen]


def zeile_zu_dict(zeile: sqlite3.Row) -> dict:
    d = dict(zeile)
    for feld in ("dateien", "metriken", "gruende", "ki"):
        if d.get(feld):
            d[feld] = json.loads(d[feld])
    return d


ANMERKUNG_ARTEN = {
    "motiv": "Hier ist das eigentliche Motiv",
    "stoerung": "Etwas im Weg / störend",
    "schaerfe": "Schärfe falsch bewertet",
    "verwacklung": "Verwacklung falsch erkannt",
    "belichtung": "Belichtung falsch bewertet",
    "serie": "Serie/Duplikat falsch erkannt",
    "sonstiges": "Sonstiges",
}


def anmerkungen_je_bild(db: sqlite3.Connection) -> dict[int, list[dict]]:
    ergebnis: dict[int, list[dict]] = {}
    for z in db.execute("SELECT * FROM anmerkungen ORDER BY id"):
        ergebnis.setdefault(z["bild_id"], []).append(dict(z))
    return ergebnis


def motiv_bereich(anmerkungen: list[dict] | None) -> list[float] | None:
    """Der zuletzt von dir markierte Motiv-Bereich eines Bildes (x, y, b, h relativ) oder None."""
    for a in reversed(anmerkungen or []):
        if a["art"] == "motiv" and a.get("b") and a.get("h"):
            return [a["x"], a["y"], a["b"], a["h"]]
    return None
