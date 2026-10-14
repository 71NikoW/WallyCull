"""Einteilung in Highlight / Behalten / Prüfen / Aussortiert – mit nachvollziehbarer Begründung.

Grundprinzip: Qualität ist nicht gleich Wert.
    hoher Wert   + technisch gut      -> highlight
    hoher Wert   + technisch schlecht -> behalten
    niedriger Wert + technisch gut    -> pruefen
    niedriger Wert + technisch schlecht -> aussortiert
Solange es noch keinen Wert-Score gibt (Phase 1), werden nur Duplikate und eindeutige
Fehlschüsse aussortiert; alles Grenzwertige landet in "pruefen".
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from . import datenbank, duplikate, ki, lernen

KATEGORIEN = ("highlight", "behalten", "pruefen", "aussortiert")
NAMEN = {"highlight": "Highlight", "behalten": "Behalten", "pruefen": "Prüfen", "aussortiert": "Aussortiert"}


TEILNOTEN = {"schaerfe": "Schärfe", "belichtung": "Belichtung", "rauschen": "Rauschen",
             "verwacklung": "Verwacklung", "aufloesung": "Auflösung"}


def _name(bild: dict) -> str:
    return PurePosixPath(bild["quelle"]).name


def _zahl(wert: float) -> str:
    return f"{wert:.2f}".replace(".", ",")


def _prozent(wert: float) -> str:
    return f"{wert * 100:.0f} %".replace(".", ",")


def technische_gruende(b: dict, konfig: dict) -> list[str]:
    q, m = konfig["qualitaet"], b.get("metriken") or {}
    noten = m.get("noten") or {}
    gruende = []
    for fehler in m.get("fehlschuss", []):
        gruende.append(f"Fehlschuss: {fehler}")
    motiv = {"gesicht": "Gesicht", "markiert": "Markiertes Motiv"}.get(m.get("motiv"), "Motiv")
    if noten.get("schaerfe", 1) < 0.5:
        gruende.append(f"{motiv} unscharf (Schärfe {m['schaerfe']:.0f}, scharf ab {q['schaerfe_scharf']})")
    elif noten.get("schaerfe", 0) >= 0.9:
        gruende.append(f"{motiv} scharf")
    if "note_schaerfe_100" in m:
        if m["note_schaerfe_100"] < 0.5:
            gruende.append(f"Detailschärfe zu weich für große Wandbilder (Kantenbreite {m['kantenbreite_100']:.1f} px)".replace(".", ",", 1))
        elif m["note_schaerfe_100"] >= 0.9:
            gruende.append(f"Detailschärfe knackig (Kantenbreite {m['kantenbreite_100']:.1f} px)".replace(".", ",", 1))
    if m.get("verwackelt"):
        w = m["verwacklung_winkel"] % 180
        richtung = "waagerecht" if w < 22.5 or w > 157.5 else "senkrecht" if 67.5 < w < 112.5 else "schräg"
        gruende.append(f"Verwackelt (Bewegungsunschärfe {richtung})")
    if m.get("note_ueber", 1) < 0.6:
        gruende.append(f"Überbelichtet: {_prozent(m['anteil_ausgefressen'])} ausgefressen")
    if m.get("note_unter", 1) < 0.6:
        gruende.append(f"Unterbelichtet: {_prozent(m['anteil_abgesoffen'])} abgesoffen")
    if m.get("nacht"):
        gruende.append("Nachtaufnahme erkannt: Dunkelheit wird nicht bestraft")
    if noten.get("rauschen", 1) < 0.5:
        gruende.append(f"Starkes Bildrauschen ({m['rauschen']:.1f})")
    if noten:
        schwach = min(noten, key=noten.get)
        if noten[schwach] < 0.8 and not any(g.startswith(("Fehlschuss", "Verwackelt")) for g in gruende):
            gruende.append(f"Schwächster Punkt: {TEILNOTEN.get(schwach, schwach)} (Note {noten[schwach]:.2f})".replace(".", ","))
    if "megapixel" in m:
        if m["megapixel"] < q["wandbild_min_megapixel"]:
            gruende.append(f"Zu klein für große Wandbilder ({m['megapixel']} MP)")
        else:
            a, c = m["druck_cm"]
            gruende.append(f"Wandbild bis ca. {a} × {c} cm ({q['druck_dpi']} dpi)")
    return gruende


def entscheiden(bilder: list[dict], konfig: dict, modell: dict | None = None) -> list[dict]:
    """Setzt kategorie, gruende, gruppe, gruppe_typ und gruppe_rang für jedes Bild.

    Mit trainiertem Modell entscheidet deine gelernte Vorliebe (persoenlich), sonst die Technik."""
    e, d = konfig["einteilung"], konfig["duplikate"]
    versatz = (float(e["strenge"]) - 0.5) * 0.2
    pruefen_unter = e["pruefen_unter"] + versatz
    aussortiert_unter = e["aussortiert_unter"] + versatz / 2

    lern = konfig["lernen"]
    ki.wert_berechnen(bilder, konfig)  # Phase 2: Wert-Score aus den KI-Werten (None, wenn keine KI-Daten)
    kombi = sorted(((b["qualitaet"] or 0) + b["wert"]) / 2 for b in bilder if b.get("wert") is not None)
    if kombi:
        kombi_ab = kombi[min(len(kombi) - 1, int(len(kombi) * (1 - float(e.get("highlight_top_anteil", 1.0)))))]
    for b in bilder:
        b.update(gruppe=None, gruppe_typ=None, gruppe_rang=None, kategorie=None, gruende=[])
        if not modell:
            b["persoenlich"] = None
        # Rangfolge in Serien: deine gelernte Vorliebe, sonst die technische Qualität
        b["rang"] = b["persoenlich"] if b.get("persoenlich") is not None else (b["qualitaet"] or 0)
    werte_p = sorted((b["persoenlich"] for b in bilder if b.get("persoenlich") is not None), reverse=True)
    if werte_p:
        platz = max(0, int(len(werte_p) * float(e.get("highlight_top_anteil", 1.0))) - 1)
        highlight_p = max(lern["highlight_ab"], werte_p[platz])

    # Highlights sind relativ: nur die besten X % aller Bilder kommen in Frage
    werte = sorted((b["qualitaet"] for b in bilder if b.get("qualitaet") is not None), reverse=True)
    if werte:
        platz = max(0, int(len(werte) * float(e.get("highlight_top_anteil", 1.0))) - 1)
        highlight_ab = max(e["highlight_ab"], werte[platz])
    else:
        highlight_ab = e["highlight_ab"]
    mangel_unter = e.get("mangel_unter", 0.5)

    for nr, gruppe in enumerate(duplikate.gruppieren(bilder, konfig), 1):
        bestes = gruppe["mitglieder"][0]
        groesse = len(gruppe["mitglieder"])
        for rang, b in enumerate(gruppe["mitglieder"], 1):
            b.update(gruppe=nr, gruppe_typ=gruppe["typ"], gruppe_rang=rang)
            art = "Duplikatgruppe" if gruppe["typ"] == "duplikat" else "Serie"
            if rang == 1:
                b["gruende"].append(f"Bestes Bild einer {art} aus {groesse} Bildern")
                continue
            noten = (b.get("metriken") or {}).get("noten") or {"x": 0}
            if b.get("persoenlich") is not None:
                wirklich_gut = b["persoenlich"] >= lern["serie_gut_ab"]
            else:
                wirklich_gut = (b["qualitaet"] or 0) >= d["serie_gut_ab"] and min(noten.values()) >= 0.5
            if gruppe["typ"] == "serie" and rang <= d["serie_behalten"] and wirklich_gut:
                b["gruende"].append(f"Ebenfalls sehr gutes Serienbild (Platz {rang} von {groesse})")
                continue  # wird unten normal eingeteilt
            abstand = bestes["rang"] - b["rang"]
            if gruppe["typ"] == "duplikat":
                b["kategorie"] = "aussortiert"
                b["gruende"].append(f"Duplikat von {_name(bestes)} (das ist besser oder gleich gut)")
            elif abstand <= d["serie_toleranz"]:
                b["kategorie"] = "pruefen"
                b["gruende"].append(f"Serienbild, fast gleich gut wie {_name(bestes)}: du entscheidest")
            else:
                b["kategorie"] = "aussortiert"
                b["gruende"].append(
                    f"Serienbild Platz {rang} von {groesse}: {_name(bestes)} ist besser "
                    f"({'Prognose' if b.get('persoenlich') is not None else 'Qualität'} {_zahl(bestes['rang'])} statt {_zahl(b['rang'])})"
                )

    for b in bilder:
        if b.get("fehler"):
            b["kategorie"] = "pruefen"
            b["gruende"].insert(0, f"Konnte nicht analysiert werden ({b['fehler']}): bitte selbst ansehen")
            continue
        q = b["qualitaet"] or 0
        for anm in b.get("anmerkungen") or []:
            text = datenbank.ANMERKUNG_ARTEN.get(anm["art"], anm["art"])
            b["gruende"].append(f"Deine Anmerkung: {text}" + (f" – {anm['text']}" if anm.get("text") else ""))
        b["gruende"].extend(ki.ki_gruende(b))
        b["gruende"].extend(technische_gruende(b, konfig))
        if b.get("nutzer"):
            b["kategorie"] = b["nutzer"]
            b["gruende"].insert(0, "Deine Entscheidung (selbst bewertet oder zurückgeholt)")
            continue
        p = b.get("persoenlich")
        if p is not None and modell:
            dafuer, dagegen = lernen.erklaeren(modell, lernen.merkmale(b, modell.get("pca")))
            text = f"Dein Modell: zu {p * 100:.0f} % behalten"
            if dagegen and p < 0.5:
                text += " (dagegen spricht: " + ", ".join(dagegen) + ")"
            elif dafuer and p >= 0.5:
                text += " (dafür spricht: " + ", ".join(dafuer) + ")"
            b["gruende"].insert(0, text)
        if b["kategorie"]:
            continue  # bereits durch Duplikat/Serie entschieden
        wert = b.get("wert")
        if wert is None and p is not None and modell:
            if (b.get("metriken") or {}).get("fehlschuss"):
                b["kategorie"] = "aussortiert"
            elif p < lern["aussortiert_unter"]:
                b["kategorie"] = "aussortiert"
            elif p < lern["pruefen_unter"]:
                b["kategorie"] = "pruefen"
            elif p >= highlight_p:
                b["kategorie"] = "highlight"
            else:
                b["kategorie"] = "behalten"
        elif wert is None:
            if q < aussortiert_unter:
                b["kategorie"] = "aussortiert"
            elif q < pruefen_unter:
                b["kategorie"] = "pruefen"
                b["gruende"].insert(0, "Technisch schwach, Wert noch unbekannt: du entscheidest")
            elif min((b["metriken"].get("noten") or {"x": 0}).values()) < mangel_unter:
                noten = b["metriken"]["noten"]
                maengel = ", ".join(TEILNOTEN.get(k, k) for k, v in noten.items() if v < mangel_unter)
                b["kategorie"] = "pruefen"
                b["gruende"].insert(0, f"Deutlicher Mangel ({maengel}), Wert noch unbekannt: du entscheidest")
            elif q >= highlight_ab:
                b["kategorie"] = "highlight"
                b["gruende"].insert(0, "Technisch eines der besten Bilder (Highlight-Kandidat)")
            else:
                b["kategorie"] = "behalten"
        else:
            hoch, gut = wert >= e["wert_hoch_ab"], q >= pruefen_unter
            b["kategorie"] = {
                (True, True): "highlight",
                (True, False): "behalten",
                (False, True): "pruefen",
                (False, False): "aussortiert",
            }[(hoch, gut)]
            text = f"Wert-Score {_zahl(wert)}, Qualität {_zahl(q)}"
            if hoch and not gut:
                b["gruende"].insert(0, f"Behalten trotz technischer Mängel: hoher Wert ({text})")
            elif not hoch and gut:
                b["gruende"].insert(0, f"Technisch in Ordnung, aber niedriger Wert ({text}): du entscheidest")
            elif not hoch and not gut:
                b["gruende"].insert(0, f"Niedriger Wert und technisch schwach ({text})")
            # Highlight nur ohne deutlichen Mangel und unter den besten Bildern (Qualität + Wert)
            if b["kategorie"] == "highlight":
                if (q + wert) / 2 < kombi_ab or min((b["metriken"].get("noten") or {"x": 0}).values()) < mangel_unter:
                    b["kategorie"] = "behalten"
                else:
                    b["gruende"].insert(0, f"Eines der besten Bilder ({text})")
    return bilder


def lauf(foto_ordner, konfig: dict) -> dict:
    """Teilt alle aktiven Bilder neu ein und speichert das Ergebnis. Fasst keine Dateien an."""
    db = datenbank.oeffnen(foto_ordner)
    bilder = datenbank.aktive_bilder(db)
    anmerkungen = datenbank.anmerkungen_je_bild(db)
    for b in bilder:
        b["anmerkungen"] = anmerkungen.get(b["id"], [])
    bilder = entscheiden(bilder, konfig, lernen.modell_laden(foto_ordner))
    for b in bilder:
        db.execute(
            "UPDATE bilder SET kategorie=?, gruende=?, gruppe=?, gruppe_typ=?, gruppe_rang=?, wert=? WHERE id=?",
            (b["kategorie"], json.dumps(b["gruende"], ensure_ascii=False), b["gruppe"], b["gruppe_typ"], b["gruppe_rang"],
             b.get("wert"), b["id"]),
        )
    db.commit()
    db.close()
    zaehlung = {k: 0 for k in KATEGORIEN}
    for b in bilder:
        zaehlung[b["kategorie"]] += 1
    return zaehlung
