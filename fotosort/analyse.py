"""Steuert den Analyse-Lauf: Dateien finden, Cache prüfen, Bilder parallel im Hintergrund messen."""

from __future__ import annotations

import hashlib
import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import imagehash
from PIL import Image
from tqdm import tqdm

from . import ANALYSE_VERSION, datenbank, einlesen, qualitaet

_konfig: dict = {}


def niedrige_prioritaet() -> None:
    """Setzt den eigenen Prozess auf niedrige Priorität, damit der PC flüssig bleibt."""
    try:
        import psutil

        prozess = psutil.Process()
        if os.name == "nt":
            prozess.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            prozess.nice(10)
    except Exception:
        pass


def _arbeiter_start(konfig: dict) -> None:
    global _konfig
    _konfig = konfig
    import cv2

    cv2.setNumThreads(1)  # jeder Prozess ein Kern, sonst bremsen sie sich gegenseitig
    if konfig["allgemein"]["niedrige_prioritaet"]:
        niedrige_prioritaet()


def vorschau_name(schluessel: str) -> str:
    return hashlib.sha1(schluessel.encode("utf-8")).hexdigest()[:16] + ".jpg"


def bild_analysieren(aufgabe: dict) -> dict:
    """Läuft im Arbeiter-Prozess. Fehler bei einzelnen Dateien werden gemeldet, nicht geworfen."""
    k = _konfig
    foto_ordner = Path(aufgabe["foto_ordner"])
    ergebnis = {
        "schluessel": aufgabe["schluessel"],
        "dateien": aufgabe["dateien"],
        "quelle": aufgabe["quelle"],
        "groesse": aufgabe["groesse"],
        "mtime": aufgabe["mtime"],
        "version": ANALYSE_VERSION,
        "breite": None, "hoehe": None, "aufnahme": None, "kamera": None,
        "phash": None, "dhash": None, "metriken": {}, "qualitaet": None,
        "vorschau": None, "fehler": None,
    }
    try:
        rgb, info, voll_grau = einlesen.bild_laden(
            foto_ordner / aufgabe["quelle"], set(k["dateien"]["raw"]), k["allgemein"]["analyse_kantenlaenge"],
            voll=k["allgemein"].get("volle_aufloesung", True),
            referenz=k["allgemein"].get("schaerfe_referenz_kante", 4000),
            entwickeln=k["allgemein"].get("raw_entwickeln", False),
        )
        # Kamera-JPG fehlt die Info? Dann reicht die RAW-Datei die EXIF-Daten nach.
        if not info.get("aufnahme") and aufgabe.get("raw") and aufgabe["raw"] != aufgabe["quelle"]:
            try:
                _, raw_info, _ = einlesen.bild_laden(foto_ordner / aufgabe["raw"], set(k["dateien"]["raw"]), 256)
                info = {**raw_info, **{a: b for a, b in info.items() if b}}
            except Exception:
                pass
        metriken = qualitaet.analysieren(rgb, info, k, voll_grau, aufgabe.get("motiv"))
        del voll_grau
        metriken["exif"] = {a: info[a] for a in ("iso", "belichtungszeit", "blende", "brennweite") if a in info}
        pil = Image.fromarray(rgb)
        vorschau = pil.copy()
        kante = k["allgemein"]["vorschau_kantenlaenge"]
        vorschau.thumbnail((kante, kante), Image.LANCZOS)
        name = vorschau_name(aufgabe["schluessel"])
        vorschau.save(foto_ordner / ".fotosort" / "vorschau" / name, quality=82)
        ergebnis.update(
            breite=info["breite"], hoehe=info["hoehe"], aufnahme=info.get("aufnahme"),
            kamera=info.get("kamera"), phash=str(imagehash.phash(pil)), dhash=str(imagehash.dhash(pil)),
            metriken=metriken, qualitaet=metriken["qualitaet"], vorschau=name,
        )
    except Exception as fehler:  # kaputte Datei: festhalten und weitermachen
        ergebnis["fehler"] = f"{type(fehler).__name__}: {fehler}"
        ergebnis["metriken"] = {"traceback": traceback.format_exc(limit=3)}
    return ergebnis


def anzahl_arbeiter(konfig: dict) -> int:
    wunsch = int(konfig["allgemein"].get("arbeiter") or 0)
    return wunsch if wunsch > 0 else max(1, (os.cpu_count() or 2) - 2)


def lauf(foto_ordner: Path, konfig: dict, neu_berechnen: bool = False) -> dict:
    """Analysiert alle neuen oder geänderten Bilder. Bereits gemessene kommen aus dem Cache."""
    foto_ordner = Path(foto_ordner).resolve()
    daten = datenbank.daten_ordner(foto_ordner)
    (daten / "vorschau").mkdir(exist_ok=True)
    db = datenbank.oeffnen(foto_ordner)
    if konfig["allgemein"]["niedrige_prioritaet"]:
        niedrige_prioritaet()

    print(f"Durchsuche {foto_ordner} ...")
    gefunden = einlesen.dateien_finden(foto_ordner, konfig)
    bekannt = {
        z["schluessel"]: z
        for z in db.execute("SELECT schluessel, groesse, mtime, version, status FROM bilder").fetchall()
    }
    anmerkungen = datenbank.anmerkungen_je_bild(db)
    motive = {
        z["schluessel"]: datenbank.motiv_bereich(anmerkungen.get(z["id"]))
        for z in db.execute("SELECT id, schluessel FROM bilder")
    }
    db.execute("UPDATE bilder SET gesehen = 0 WHERE status = 'aktiv'")
    aufgaben = []
    for g in gefunden:
        alt = bekannt.get(g["schluessel"])
        aktuell = (
            alt is not None and alt["status"] == "aktiv" and alt["groesse"] == g["groesse"]
            and abs((alt["mtime"] or 0) - g["mtime"]) < 1 and alt["version"] == ANALYSE_VERSION
        )
        if aktuell and not neu_berechnen:
            db.execute("UPDATE bilder SET gesehen = 1 WHERE schluessel = ?", (g["schluessel"],))
        else:
            aufgaben.append({**g, "foto_ordner": str(foto_ordner), "motiv": motive.get(g["schluessel"])})
    # Bilder, die nicht mehr da sind (vom Nutzer gelöscht/verschoben), nicht mehr berücksichtigen
    weg = db.execute("UPDATE bilder SET status = 'fehlt' WHERE status = 'aktiv' AND gesehen = 0").rowcount
    db.commit()

    print(f"{len(gefunden)} Bilder gefunden, {len(gefunden) - len(aufgaben)} aus dem Cache, {len(aufgaben)} werden analysiert.")
    fehler = 0
    if aufgaben:
        arbeiter = anzahl_arbeiter(konfig)
        with ProcessPoolExecutor(max_workers=arbeiter, initializer=_arbeiter_start, initargs=(konfig,)) as pool:
            zukuenfte = [pool.submit(bild_analysieren, a) for a in aufgaben]
            try:
                for i, z in enumerate(tqdm(as_completed(zukuenfte), total=len(zukuenfte), desc="Analysiere", unit="Bild"), 1):
                    e = z.result()
                    if e["fehler"]:
                        fehler += 1
                        tqdm.write(f"  Fehler bei {e['quelle']}: {e['fehler']}")
                    datenbank.bild_speichern(db, e)
                    if i % 25 == 0:
                        db.commit()  # Zwischenstand sichern: Abbruch jederzeit möglich
            except KeyboardInterrupt:
                print("\nAbgebrochen. Der bisherige Fortschritt ist gespeichert, beim nächsten Start geht es weiter.")
                for z in zukuenfte:
                    z.cancel()
                db.commit()
                raise
        db.commit()
    db.close()
    return {"gefunden": len(gefunden), "analysiert": len(aufgaben), "fehler": fehler, "fehlt": weg}


def einzeln_neu(foto_ordner: Path, konfig: dict, bild_id: int) -> dict:
    """Misst ein einzelnes Bild sofort neu (z. B. nachdem du sein Motiv markiert hast)."""
    global _konfig
    _konfig = konfig
    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    z = datenbank.zeile_zu_dict(db.execute("SELECT * FROM bilder WHERE id=?", (bild_id,)).fetchone())
    motiv = datenbank.motiv_bereich(datenbank.anmerkungen_je_bild(db).get(bild_id))
    raw = next((d for d in z["dateien"] if Path(d).suffix.lower() in konfig["dateien"]["raw"]), None)
    e = bild_analysieren({
        "schluessel": z["schluessel"], "dateien": z["dateien"], "quelle": z["quelle"], "raw": raw,
        "groesse": z["groesse"], "mtime": z["mtime"], "foto_ordner": str(foto_ordner), "motiv": motiv,
    })
    if not e["fehler"]:
        datenbank.bild_speichern(db, e)
        db.commit()
    db.close()
    return e
