"""Persönliches Training: lernt aus deinen Bewertungen, welche Bilder du behalten würdest.

Jedes Bild wird als Merkmalsvektor beschrieben (alle Messwerte). Darauf wird eine logistische
Regression trainiert (reines NumPy, läuft in Millisekunden auf der CPU). Ab Phase 2 kommen
CLIP-Bildmerkmale dazu, dann lernt das Modell auch Motive und Bildstil.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from . import datenbank

# (Name im Vektor, Bezeichnung für Begründungen)
MERKMALE = [
    ("note_schaerfe", "Schärfe"),
    ("note_belichtung", "Belichtung"),
    ("note_rauschen", "Rauschen"),
    ("note_verwacklung", "Verwacklung"),
    ("note_aufloesung", "Auflösung"),
    ("qualitaet", "Gesamtqualität"),
    ("kantenbreite", "Detailschärfe"),
    ("schaerfe_log", "Schärfe auf dem Motiv"),
    ("rauschen", "Rauschen"),
    ("helligkeit", "Helligkeit"),
    ("ausgefressen", "ausgefressene Lichter"),
    ("abgesoffen", "abgesoffene Schatten"),
    ("richtung", "Verwacklung"),
    ("nacht", "Nachtaufnahme"),
    ("gesicht", "Gesicht im Bild"),
    ("gesichter", "Anzahl Gesichter"),
    ("bestes_der_gruppe", "bestes Bild der Serie"),
    ("in_gruppe", "Teil einer Serie"),
    ("iso_log", "ISO"),
    ("stoerung", "Störung im Bild (von dir markiert)"),
    # Phase 2: KI-Werte
    ("ki_aesthetik", "Ästhetik (KI)"),
    ("ki_wandbild", "Wandbild-Eignung (KI)"),
    ("ki_qualitaet", "Bildqualität (KI)"),
    ("ki_schaerfe", "Schärfe (KI)"),
    ("ki_inhalt", "Bildinhalt"),
    ("ki_vorbild", "Nähe zu deinen Vorbildern"),
    ("ki_musiq", "Bildqualität (MUSIQ)"),
    ("ki_nima", "Ästhetik (NIMA)"),
]
# Dazu kommen bis zu STIL_DIMENSIONEN Hauptkomponenten der CLIP-Bildmerkmale ("Motiv und Stil").
STIL_DIMENSIONEN = 12


def embedding(b: dict) -> np.ndarray | None:
    e = b.get("embedding")
    return np.frombuffer(e, np.float16).astype(np.float64) if e else None


def merkmale(b: dict, pca: dict | None = None) -> np.ndarray:
    m = b.get("metriken") or {}
    r = b.get("ki") if isinstance(b.get("ki"), dict) else {}
    noten = m.get("noten") or {}
    iso = (m.get("exif") or {}).get("iso") or 400
    werte = {
        "note_schaerfe": noten.get("schaerfe", 0.5),
        "note_belichtung": noten.get("belichtung", 0.5),
        "note_rauschen": noten.get("rauschen", 0.5),
        "note_verwacklung": noten.get("verwacklung", 1.0),
        "note_aufloesung": noten.get("aufloesung", 1.0),
        "qualitaet": b.get("qualitaet") or 0.0,
        "kantenbreite": math.log(m.get("kantenbreite_100") or 1.6),
        "schaerfe_log": math.log1p(m.get("schaerfe") or 0),
        "rauschen": m.get("rauschen") or 0.0,
        "helligkeit": (m.get("helligkeit") or 128) / 255,
        "ausgefressen": math.sqrt(m.get("anteil_ausgefressen") or 0),
        "abgesoffen": math.sqrt(m.get("anteil_abgesoffen") or 0),
        "richtung": m.get("richtungsverhaeltnis") or 1.0,
        "nacht": float(bool(m.get("nacht"))),
        "gesicht": float((m.get("gesichter") or 0) > 0),
        "gesichter": min(m.get("gesichter") or 0, 5),
        "bestes_der_gruppe": float(not b.get("gruppe_rang") or b["gruppe_rang"] == 1),
        "in_gruppe": float(bool(b.get("gruppe"))),
        "iso_log": math.log(max(iso, 50)),
        "stoerung": float(any(x["art"] == "stoerung" for x in b.get("anmerkungen") or [])),
        "ki_aesthetik": r.get("aesthetik", 5.0),
        "ki_wandbild": r.get("wandbild", 0.5),
        "ki_qualitaet": r.get("clip_qualitaet", 0.5),
        "ki_schaerfe": r.get("clip_schaerfe", 0.5),
        "ki_inhalt": r.get("inhalt_wert", 0.0),
        "ki_vorbild": r.get("vorbild", 0.0),
        "ki_musiq": r.get("musiq", 50.0) / 100,
        "ki_nima": r.get("nima", 5.0),
    }
    basis = [werte[n] for n, _ in MERKMALE]
    if pca:
        e = embedding(b)
        stil = (e - np.array(pca["mittel"])) @ np.array(pca["achsen"]).T if e is not None else np.zeros(len(pca["achsen"]))
        basis.extend(stil.tolist())
    return np.array(basis, dtype=np.float64)


def _pca(alle: list[dict]) -> dict | None:
    """Hauptkomponenten der CLIP-Merkmale aller Bilder: verdichtet Motiv und Bildstil auf wenige Zahlen."""
    embs = [e for e in (embedding(b) for b in alle) if e is not None]
    if len(embs) < STIL_DIMENSIONEN * 3:
        return None
    x = np.array(embs)
    mittel = x.mean(axis=0)
    _, _, vt = np.linalg.svd(x - mittel, full_matrices=False)
    return {"mittel": mittel.tolist(), "achsen": vt[:STIL_DIMENSIONEN].tolist()}


def _logistisch(x: np.ndarray, y: np.ndarray, gewicht: np.ndarray, l2: float) -> np.ndarray:
    """Logistische Regression per Newton-Verfahren. x enthält bereits die Konstante als erste Spalte."""
    w = np.zeros(x.shape[1])
    strafe = np.full(x.shape[1], l2)
    strafe[0] = 0.0  # Konstante nicht bestrafen
    for _ in range(50):
        p = 1 / (1 + np.exp(-np.clip(x @ w, -30, 30)))
        gradient = x.T @ (gewicht * (p - y)) + strafe * w
        hesse = (x * (gewicht * p * (1 - p))[:, None]).T @ x + np.diag(strafe) + 1e-9 * np.eye(len(w))
        schritt = np.linalg.solve(hesse, gradient)
        w -= schritt
        if np.abs(schritt).max() < 1e-6:
            break
    return w


def _anpassen(x: np.ndarray, y: np.ndarray, l2: float) -> dict:
    mittel, streuung = x.mean(axis=0), x.std(axis=0)
    streuung[streuung < 1e-9] = 1.0
    xs = np.hstack([np.ones((len(x), 1)), (x - mittel) / streuung])
    # Klassen ausgleichen: Behalten und Löschen zählen gleich viel, egal wie oft du was wählst
    anteil = y.mean()
    gewicht = np.where(y == 1, 0.5 / max(anteil, 1e-6), 0.5 / max(1 - anteil, 1e-6))
    w = _logistisch(xs, y, gewicht, l2)
    return {"mittel": mittel.tolist(), "streuung": streuung.tolist(), "gewichte": w.tolist()}


def vorhersagen(modell: dict, x: np.ndarray) -> np.ndarray:
    xs = (np.atleast_2d(x) - np.array(modell["mittel"])) / np.array(modell["streuung"])
    w = np.array(modell["gewichte"])
    return 1 / (1 + np.exp(-np.clip(w[0] + xs @ w[1:], -30, 30)))


# Positive Formulierung, wenn ein Merkmal FÜR das Behalten spricht
POSITIV = {"Störung im Bild (von dir markiert)": "keine Störung","Verwacklung": "keine Verwacklung", "Rauschen": "wenig Rauschen",
           "ausgefressene Lichter": "keine ausgefressenen Lichter", "abgesoffene Schatten": "keine abgesoffenen Schatten",
           "ISO": "niedrige ISO", "Teil einer Serie": "Einzelbild"}


def erklaeren(modell: dict, x: np.ndarray, anzahl: int = 2) -> tuple[list[str], list[str]]:
    """Welche Merkmale sprechen am stärksten für bzw. gegen das Behalten?"""
    xs = (x - np.array(modell["mittel"])) / np.array(modell["streuung"])
    beitrag = np.array(modell["gewichte"][1:]) * xs
    namen = [label for _, label in MERKMALE] + ["Motiv und Stil"] * (len(beitrag) - len(MERKMALE))
    dafuer, dagegen = [], []
    for i in np.argsort(-beitrag):
        name = POSITIV.get(namen[i], namen[i])
        if beitrag[i] > 0.3 and name not in dafuer and len(dafuer) < anzahl:
            dafuer.append(name)
    for i in np.argsort(beitrag):
        if beitrag[i] < -0.3 and namen[i] not in dagegen and len(dagegen) < anzahl:
            dagegen.append(namen[i])
    return dafuer, dagegen


def modell_pfad(foto_ordner: Path) -> Path:
    return datenbank.daten_ordner(foto_ordner) / "modell.json"


def modell_laden(foto_ordner: Path) -> dict | None:
    try:
        modell = json.loads(modell_pfad(foto_ordner).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # Modell aus einer älteren Version mit anderen Merkmalen: wird beim nächsten Training ersetzt
    return modell if modell.get("merkmale") == [n for n, _ in MERKMALE] and "pca" in modell else None


def trainieren(foto_ordner: Path, konfig: dict) -> dict:
    """Trainiert das Modell aus allen Bewertungen und schreibt die Prognose für jedes Bild in die DB."""
    l = konfig["lernen"]
    db = datenbank.oeffnen(foto_ordner)
    zeilen = db.execute(
        "SELECT b.*, w.urteil FROM bilder b JOIN bewertungen w ON w.bild_id = b.id WHERE b.metriken IS NOT NULL AND b.fehler IS NULL"
    ).fetchall()
    anmerkungen = datenbank.anmerkungen_je_bild(db)
    beispiele = [datenbank.zeile_zu_dict(z) for z in zeilen]
    for b in beispiele:
        b["anmerkungen"] = anmerkungen.get(b["id"], [])
    y = np.array([1.0 if b["urteil"] >= 1 else 0.0 for b in beispiele])
    status = {"bewertet": len(beispiele), "behalten": int(y.sum()), "weg": int(len(y) - y.sum()), "aktiv": False}
    if len(beispiele) < l["min_bewertungen"] or min(status["behalten"], status["weg"]) < l["min_je_seite"]:
        db.close()
        status["hinweis"] = (
            f"Noch zu wenig Bewertungen: mindestens {l['min_bewertungen']} insgesamt und je "
            f"{l['min_je_seite']} Behalten und Weg nötig."
        )
        return status

    alle = [datenbank.zeile_zu_dict(z) for z in db.execute("SELECT * FROM bilder WHERE metriken IS NOT NULL AND fehler IS NULL")]
    for b in alle:
        b["anmerkungen"] = anmerkungen.get(b["id"], [])
    pca = _pca(alle)
    x = np.array([merkmale(b, pca) for b in beispiele])
    # Ehrliche Trefferquote: 5-fache Kreuzvalidierung
    reihenfolge = np.random.default_rng(0).permutation(len(y))
    treffer = 0
    for falte in range(5):
        test = reihenfolge[falte::5]
        lern = np.setdiff1d(reihenfolge, test)
        if len(set(y[lern])) < 2:
            continue
        m = _anpassen(x[lern], y[lern], l["regularisierung"])
        treffer += int(((vorhersagen(m, x[test]) >= 0.5) == (y[test] == 1)).sum())
    modell = _anpassen(x, y, l["regularisierung"])
    modell.update(
        merkmale=[n for n, _ in MERKMALE], pca=pca, trefferquote=round(treffer / len(y), 3),
        bewertet=len(y), trainiert=datetime.now().isoformat(timespec="seconds"),
    )
    modell_pfad(foto_ordner).write_text(json.dumps(modell, ensure_ascii=False), encoding="utf-8")

    if alle:
        p = vorhersagen(modell, np.array([merkmale(b, pca) for b in alle]))
        db.executemany("UPDATE bilder SET persoenlich=? WHERE id=?", [(float(v), b["id"]) for v, b in zip(p, alle)])
    db.commit()
    db.close()
    status.update(aktiv=True, trefferquote=modell["trefferquote"])
    return status


def naechste(foto_ordner: Path, konfig: dict, anzahl: int = 20, ausser: set[int] | None = None) -> list[dict]:
    """Wählt die nächsten Bilder zum Bewerten.

    Ohne Modell: bunte Mischung über die ganze Qualitätsspanne. Mit Modell (aktives Lernen):
    vor allem die Bilder, bei denen das Modell am unsichersten ist, plus etwas Zufall."""
    db = datenbank.oeffnen(foto_ordner)
    offen = [
        datenbank.zeile_zu_dict(z)
        for z in db.execute(
            "SELECT * FROM bilder WHERE status='aktiv' AND metriken IS NOT NULL AND fehler IS NULL "
            "AND id NOT IN (SELECT bild_id FROM bewertungen)"
        )
    ]
    anmerkungen = datenbank.anmerkungen_je_bild(db)
    db.close()
    for b in offen:
        b["anmerkungen"] = anmerkungen.get(b["id"], [])
    ausser = ausser or set()
    offen = [b for b in offen if b["id"] not in ausser]
    zufall = np.random.default_rng()
    if not offen:
        return []
    mit_modell = modell_laden(foto_ordner) is not None and any(b.get("persoenlich") is not None for b in offen)
    if mit_modell:
        offen.sort(key=lambda b: abs((b.get("persoenlich") if b.get("persoenlich") is not None else 0.5) - 0.5))
        unsicher = offen[: max(1, int(anzahl * (1 - konfig["lernen"]["zufallsanteil"])))]
        rest = offen[len(unsicher):]
        extra = [rest[i] for i in zufall.permutation(len(rest))[: anzahl - len(unsicher)]] if rest else []
        auswahl = unsicher + extra
        zufall.shuffle(auswahl)
        return auswahl[:anzahl]
    # Ohne Modell: nach Qualität in Fünftel teilen und reihum ziehen
    offen.sort(key=lambda b: b["qualitaet"] or 0)
    toepfe = [list(t) for t in np.array_split(np.array(offen, dtype=object), min(5, len(offen)))]
    for t in toepfe:
        zufall.shuffle(t)
    auswahl = []
    while len(auswahl) < anzahl and any(toepfe):
        for t in toepfe:
            if t and len(auswahl) < anzahl:
                auswahl.append(t.pop())
    return auswahl
