"""Sicheres Verschieben nach "_aussortiert", Zurückholen und endgültiges Löschen (in den Papierkorb).

Es wird NIE direkt gelöscht. Jede Verschiebung steht in der Datenbank und lässt sich rückgängig machen.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from . import datenbank


def _groesse_text(byte: int) -> str:
    for einheit in ("B", "KB", "MB", "GB"):
        if byte < 1024 or einheit == "GB":
            return f"{byte:.1f} {einheit}".replace(".", ",")
        byte /= 1024
    return ""


def _freier_name(ziel: Path) -> Path:
    if not ziel.exists():
        return ziel
    for i in range(2, 10000):
        kandidat = ziel.with_name(f"{ziel.stem} ({i}){ziel.suffix}")
        if not kandidat.exists():
            return kandidat
    raise FileExistsError(ziel)


def _leere_ordner_entfernen(start: Path) -> None:
    if not start.exists():
        return
    for ordner, _, _ in sorted(os.walk(start), key=lambda t: len(t[0]), reverse=True):
        try:
            Path(ordner).rmdir()  # nur leere Ordner lassen sich entfernen
        except OSError:
            pass


def verschieben(foto_ordner: Path, konfig: dict, ausfuehren: bool = False) -> dict:
    """Verschiebt alle als "aussortiert" eingeteilten Bilder. Ohne ausfuehren=True nur Trockenlauf."""
    foto_ordner = Path(foto_ordner).resolve()
    ziel_basis = foto_ordner / konfig["dateien"]["aussortiert_ordner"]
    db = datenbank.oeffnen(foto_ordner)
    bilder = [
        datenbank.zeile_zu_dict(z)
        for z in db.execute("SELECT * FROM bilder WHERE status='aktiv' AND kategorie='aussortiert' ORDER BY schluessel")
    ]
    gesamt = sum(b["groesse"] or 0 for b in bilder)
    print(f"{len(bilder)} Bilder ({_groesse_text(gesamt)}) sind zum Aussortieren vorgesehen.")

    if not ausfuehren:
        for b in bilder[:40]:
            grund = (b.get("gruende") or ["?"])[0]
            print(f"  {b['quelle']}  ->  {konfig['dateien']['aussortiert_ordner']}/  ({grund})")
        if len(bilder) > 40:
            print(f"  ... und {len(bilder) - 40} weitere (alle im Bericht).")
        print("\nTROCKENLAUF: Es wurde nichts verschoben. Zum Ausführen denselben Befehl mit --ausfuehren starten.")
        db.close()
        return {"bilder": len(bilder), "verschoben": 0, "fehler": 0}

    lauf = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    verschoben = fehler = 0
    for b in bilder:
        erledigt: list[tuple[Path, Path]] = []
        try:
            for rel in b["dateien"]:
                quelle = foto_ordner / rel
                if not quelle.exists():
                    continue
                ziel = _freier_name(ziel_basis / rel)
                ziel.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(quelle), str(ziel))
                erledigt.append((quelle, ziel))
        except Exception as e:
            fehler += 1
            print(f"  Fehler bei {b['quelle']}: {e} – wird zurückgesetzt.")
            for quelle, ziel in reversed(erledigt):
                try:
                    shutil.move(str(ziel), str(quelle))
                except Exception:
                    print(f"  ACHTUNG: {ziel} konnte nicht zurückgelegt werden.")
            continue
        for quelle, ziel in erledigt:
            db.execute(
                "INSERT INTO verschiebungen (bild_id, quelle, ziel, lauf) VALUES (?, ?, ?, ?)",
                (b["id"], quelle.relative_to(foto_ordner).as_posix(), ziel.relative_to(foto_ordner).as_posix(), lauf),
            )
        db.execute("UPDATE bilder SET status='verschoben' WHERE id=?", (b["id"],))
        db.commit()
        verschoben += 1
    db.close()
    print(f"{verschoben} Bilder nach {ziel_basis} verschoben (Lauf {lauf}). Fehler: {fehler}.")
    print("Rückgängig machen: Befehl 'zurueckholen'.")
    return {"bilder": len(bilder), "verschoben": verschoben, "fehler": fehler, "lauf": lauf}


def zurueckholen(foto_ordner: Path, konfig: dict, datei: str | None = None, lauf: str | None = None,
                 bild_ids: list[int] | None = None) -> int:
    """Holt Bilder aus "_aussortiert" an ihren Originalort zurück (alle, eines Laufs oder passend zu "datei")."""
    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    sql = "SELECT * FROM verschiebungen WHERE zustand='verschoben'"
    werte: list = []
    if lauf:
        sql += " AND lauf=?"
        werte.append(lauf)
    zeilen = db.execute(sql, werte).fetchall()
    if datei:
        suche = datei.replace("\\", "/").lower()
        bild_ids = {z["bild_id"] for z in zeilen if suche in z["quelle"].lower()}
        zeilen = [z for z in zeilen if z["bild_id"] in bild_ids]  # RAF + JPG immer zusammen
    if bild_ids:
        zeilen = [z for z in zeilen if z["bild_id"] in set(bild_ids)]

    zurueck = set()
    for z in zeilen:
        quelle, ziel = foto_ordner / z["quelle"], foto_ordner / z["ziel"]
        if not ziel.exists():
            print(f"  Nicht mehr in {konfig['dateien']['aussortiert_ordner']}: {z['ziel']}")
            continue
        if quelle.exists():
            print(f"  Übersprungen, am Originalort liegt schon eine Datei: {z['quelle']}")
            continue
        quelle.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(ziel), str(quelle))
        db.execute("UPDATE verschiebungen SET zustand='zurueck' WHERE id=?", (z["id"],))
        # "gerettet" merken: wird in Phase 5 zum Trainingsbeispiel
        db.execute("UPDATE bilder SET status='aktiv', kategorie='behalten', nutzer='behalten' WHERE id=?", (z["bild_id"],))
        # Zurückholen ist ein klares Urteil: wird zum Trainingsbeispiel
        db.execute(
            "INSERT OR REPLACE INTO bewertungen (bild_id, urteil, quelle) VALUES (?, 1, 'zurueckgeholt')", (z["bild_id"],)
        )
        zurueck.add(z["bild_id"])
    db.commit()
    db.close()
    _leere_ordner_entfernen(foto_ordner / konfig["dateien"]["aussortiert_ordner"])
    print(f"{len(zurueck)} Bilder zurückgeholt.")
    return len(zurueck)


def endgueltig_loeschen(foto_ordner: Path, konfig: dict, ohne_rueckfrage: bool = False) -> int:
    """Schiebt alle Dateien in "_aussortiert" in den Windows-Papierkorb – erst nach ausdrücklicher Bestätigung."""
    from send2trash import send2trash

    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    zeilen = [z for z in db.execute("SELECT * FROM verschiebungen WHERE zustand='verschoben'") if (foto_ordner / z["ziel"]).exists()]
    if not zeilen:
        print("Nichts zu löschen.")
        db.close()
        return 0
    groesse = sum((foto_ordner / z["ziel"]).stat().st_size for z in zeilen)
    bilder = len({z["bild_id"] for z in zeilen})
    print(f"{bilder} Bilder ({len(zeilen)} Dateien, {_groesse_text(groesse)}) werden in den Papierkorb verschoben.")
    if not ohne_rueckfrage:
        antwort = input('Zum Bestätigen LÖSCHEN eintippen (alles andere bricht ab): ').strip().upper()
        if antwort not in ("LÖSCHEN", "LOESCHEN"):
            print("Abgebrochen, nichts gelöscht.")
            db.close()
            return 0
    geloescht = 0
    for z in zeilen:
        try:
            send2trash(str(foto_ordner / z["ziel"]))
            db.execute("UPDATE verschiebungen SET zustand='geloescht' WHERE id=?", (z["id"],))
            db.execute("UPDATE bilder SET status='geloescht' WHERE id=?", (z["bild_id"],))
            geloescht += 1
        except Exception as e:
            print(f"  Konnte nicht in den Papierkorb: {z['ziel']} ({e})")
    db.commit()
    db.close()
    _leere_ordner_entfernen(foto_ordner / konfig["dateien"]["aussortiert_ordner"])
    print(f"{geloescht} Dateien im Papierkorb. Dort lassen sie sich notfalls noch wiederherstellen.")
    return geloescht
