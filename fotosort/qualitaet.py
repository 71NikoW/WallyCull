"""Technischer Qualitäts-Score mit klassischer Bildanalyse (OpenCV/NumPy, ohne Training).

Jede Messung ergibt einen Rohwert und eine Teilnote zwischen 0 (schlecht) und 1 (gut).
"""

from __future__ import annotations

import math

import cv2
import numpy as np

_gesicht_kaskade = None


def _skala(wert: float, schlecht: float, gut: float, log: bool = False) -> float:
    """Bildet einen Messwert linear (oder logarithmisch) auf 0..1 ab. Funktioniert in beide Richtungen."""
    if log:
        wert, schlecht, gut = (math.log(max(v, 1e-6)) for v in (wert, schlecht, gut))
    if gut == schlecht:
        return 1.0 if wert >= gut else 0.0
    return float(min(1.0, max(0.0, (wert - schlecht) / (gut - schlecht))))


def _gesichter(grau: np.ndarray) -> list[tuple[int, int, int, int]]:
    global _gesicht_kaskade
    if not hasattr(cv2, "CascadeClassifier"):
        return []  # OpenCV 5 ohne Haar-Kaskaden: Schärfe dann über den auffälligsten Bereich
    if _gesicht_kaskade is None:
        _gesicht_kaskade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    faktor = 800 / max(grau.shape)
    klein = cv2.resize(grau, None, fx=faktor, fy=faktor, interpolation=cv2.INTER_AREA) if faktor < 1 else grau
    faktor = min(faktor, 1.0)
    mindest = max(24, int(min(klein.shape) * 0.06))
    treffer = _gesicht_kaskade.detectMultiScale(klein, scaleFactor=1.1, minNeighbors=7, minSize=(mindest, mindest))
    return [tuple(int(v / faktor) for v in t) for t in treffer]


def _auffaelligkeit(grau: np.ndarray) -> np.ndarray:
    """Spectral-Residual-Saliency (Hou & Zhang 2007): wo zieht das Bild den Blick hin?"""
    klein = cv2.resize(grau, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    spektrum = np.fft.fft2(klein)
    log_amplitude = np.log(np.abs(spektrum) + 1e-8)
    residuum = log_amplitude - cv2.blur(log_amplitude, (3, 3))
    karte = np.abs(np.fft.ifft2(np.exp(residuum + 1j * np.angle(spektrum)))) ** 2
    karte = cv2.GaussianBlur(karte.astype(np.float32), (9, 9), 2.5)
    karte = cv2.resize(karte, (grau.shape[1], grau.shape[0]), interpolation=cv2.INTER_LINEAR)
    return karte / (karte.max() + 1e-8)


def _kacheln(form: tuple[int, int], anzahl_kurz: int = 6) -> list[tuple[slice, slice]]:
    h, w = form
    seite = max(16, min(h, w) // anzahl_kurz)
    return [
        (slice(y, y + seite), slice(x, x + seite))
        for y in range(0, h - seite + 1, seite)
        for x in range(0, w - seite + 1, seite)
    ]


def _rauschen(grau: np.ndarray) -> float:
    """Rauschschätzung nach Immerkaer (1996), nur in glatten, nicht beschnittenen Flächen."""
    g = grau.astype(np.float32)
    maske = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    antwort = np.abs(cv2.filter2D(g, -1, maske))
    gradient = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    glatt = (gradient <= np.percentile(gradient, 30)) & (grau > 12) & (grau < 243)
    glatt[:2, :] = glatt[-2:, :] = False
    glatt[:, :2] = glatt[:, -2:] = False
    if glatt.sum() < 500:
        return 0.0
    return float(math.sqrt(math.pi / 2) / 6 * antwort[glatt].mean())


def _kantenbreite(stueck: np.ndarray, vorglaettung: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Breite (Sigma in Pixeln) und Richtung (Grad, 0-180) aller kräftigen Kanten eines Ausschnitts.

    Eine scharfe Kante ist etwa 1 Pixel breit, eine unscharfe deutlich breiter. Gemessen wird
    das Verhältnis von Kantenkontrast zu Kantensteilheit. Rauschen stört dabei kaum, weil nur
    kontrastreiche Kanten zählen."""
    f = cv2.GaussianBlur(stueck.astype(np.float32), (0, 0), vorglaettung)
    gx, gy = cv2.Sobel(f, cv2.CV_32F, 1, 0) / 8, cv2.Sobel(f, cv2.CV_32F, 0, 1) / 8
    steigung = cv2.magnitude(gx, gy)
    fenster = np.ones((15, 15), np.uint8)
    kontrast = cv2.dilate(f, fenster) - cv2.erode(f, fenster)
    spitze = steigung >= 0.98 * cv2.dilate(steigung, np.ones((3, 3), np.uint8))
    auswahl = spitze & (kontrast >= 30) & (steigung > 1)
    sigma = 0.399 * kontrast[auswahl] / steigung[auswahl]
    breite = np.sqrt(np.maximum(sigma * sigma - vorglaettung * vorglaettung, 0))
    richtung = np.degrees(np.arctan2(gy[auswahl], gx[auswahl])) % 180
    return breite, richtung


def _schaerfe_100(voll: np.ndarray, bereiche: list[tuple[int, int, int, int]]) -> dict | None:
    """Kantenbreite an den schärfsten Stellen des Motivs in voller Auflösung, plus Verwacklungs-Test:
    Bei Bewegungsunschärfe sind Kanten in Bewegungsrichtung breit, quer dazu aber scharf."""
    breiten, richtungen = [], []
    for x, y, bw, bh in bereiche:
        stueck = voll[max(0, y) : y + bh, max(0, x) : x + bw]
        if stueck.shape[0] >= 32 and stueck.shape[1] >= 32:
            b, r = _kantenbreite(stueck)
            breiten.append(b)
            richtungen.append(r)
    if not breiten or sum(len(b) for b in breiten) < 50:
        return None  # zu wenig Kanten (z. B. Nebel, Himmel): keine Aussage möglich
    breite, richtung = np.concatenate(breiten), np.concatenate(richtungen)
    ergebnis = {"kantenbreite": float(np.percentile(breite, 20))}
    # Kanten nach Gradientenrichtung in vier Sektoren (0°, 45°, 90°, 135°) einteilen
    sektor = (((richtung + 22.5) % 180) // 45).astype(int)
    je_richtung = {}
    for s in range(4):
        auswahl = breite[sektor == s]
        if auswahl.size >= 80:
            je_richtung[s * 45] = float(np.percentile(auswahl, 20))
    if len(je_richtung) >= 2:
        breiteste = max(je_richtung, key=je_richtung.get)
        schmalste = min(je_richtung.values())
        ergebnis["richtungsverhaeltnis"] = je_richtung[breiteste] / max(schmalste, 0.3)
        ergebnis["bewegungsrichtung"] = breiteste  # Gradient 0° = waagerechte Bewegung
        ergebnis["breite_max_richtung"] = je_richtung[breiteste]
    return ergebnis


def _richtung(gx: np.ndarray, gy: np.ndarray) -> tuple[float, float]:
    """Strukturtensor: wie stark sind die Kanten in eine Richtung ausgerichtet (0..1) und in welche (Grad)."""
    sxx, syy, sxy = float((gx * gx).sum()), float((gy * gy).sum()), float((gx * gy).sum())
    spur, det = sxx + syy, sxx * syy - sxy * sxy
    if spur <= 0:
        return 0.0, 0.0
    wurzel = math.sqrt(max(spur * spur / 4 - det, 0.0))
    gross, klein = spur / 2 + wurzel, spur / 2 - wurzel
    winkel = 0.5 * math.degrees(math.atan2(2 * sxy, sxx - syy))
    # Unschärfe verläuft quer zur dominanten Kantenrichtung
    return 1 - klein / gross if gross > 0 else 0.0, (winkel + 90) % 180


def analysieren(rgb: np.ndarray, info: dict, konfig: dict, voll: np.ndarray | None = None,
                motiv: list[float] | None = None) -> dict:
    """Berechnet alle Qualitätsmetriken eines (verkleinerten) Bildes."""
    q = konfig["qualitaet"]
    grau = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = grau.shape
    m: dict = {}

    # --- Belichtung -------------------------------------------------------
    m["helligkeit"] = float(grau.mean())
    m["anteil_ausgefressen"] = float((rgb.max(axis=2) >= 254).mean())
    m["anteil_abgesoffen"] = float((grau <= 10).mean())
    lichtquellen = float((grau >= 220).mean())
    zeit, iso = info.get("belichtungszeit") or 0, info.get("iso") or 0
    m["nacht"] = bool(
        m["helligkeit"] < 60
        and (lichtquellen > 0.0008 or zeit >= 0.5 or (iso >= 1600 and zeit >= 1 / 30))
    )
    unter_schlecht = q["unter_schlecht_nacht"] if m["nacht"] else q["unter_schlecht"]
    note_ueber = _skala(m["anteil_ausgefressen"], q["ueber_schlecht"], q["ueber_ok"])
    note_unter = _skala(m["anteil_abgesoffen"], unter_schlecht, q["unter_ok"])

    # --- Schärfe auf dem Motiv --------------------------------------------
    # Kontrast strecken, damit dunkle oder flaue Bilder nicht automatisch als unscharf gelten
    tief, hoch = np.percentile(grau, (1, 99))
    gestreckt = grau
    if hoch - tief < 200 and hoch - tief > 8:
        gestreckt = np.clip((grau.astype(np.float32) - tief) * min(255.0 / (hoch - tief), 4.0), 0, 255).astype(np.uint8)
    weich = cv2.GaussianBlur(gestreckt, (3, 3), 0)
    laplace = cv2.Laplacian(weich, cv2.CV_32F)
    kacheln = _kacheln((h, w))
    karte = _auffaelligkeit(grau)
    yy, xx = np.mgrid[0:h, 0:w]
    mitte = np.exp(-(((xx - w / 2) / (0.4 * w)) ** 2 + ((yy - h / 2) / (0.4 * h)) ** 2))
    gewicht = karte * (0.5 + 0.5 * mitte)
    kachel_schaerfe = np.array([float(laplace[k].var()) for k in kacheln])
    kachel_gewicht = np.array([float(gewicht[k].mean()) for k in kacheln])
    anzahl = max(3, len(kacheln) // 5)
    motiv_kacheln = np.argsort(kachel_gewicht)[-anzahl:]
    motiv_schaerfe = float(np.median(kachel_schaerfe[motiv_kacheln]))
    spitze = float(np.percentile(kachel_schaerfe, 90))
    m["schaerfe_motiv_bereich"] = motiv_schaerfe
    m["schaerfe_spitze"] = spitze

    gesichter = _gesichter(grau)
    m["gesichter"] = len(gesichter)
    if gesichter:
        x, y, fw, fh = max(gesichter, key=lambda g: g[2] * g[3])
        gesicht_wert = float(laplace[y : y + fh, x : x + fw].var()) * q["gesicht_faktor"]
        m["schaerfe_gesicht"] = gesicht_wert
        m["schaerfe"] = max(gesicht_wert, 0.5 * motiv_schaerfe)
        m["motiv"] = "gesicht"
    else:
        # Bokeh-tolerant: Ist irgendwo ein klar scharfer Bereich, zählt das Motiv als scharf.
        m["schaerfe"] = max(motiv_schaerfe, 0.6 * spitze)
        m["motiv"] = "bereich"
    if motiv:
        # Von dir markiertes Motiv: Schärfe genau dort messen
        mx0, my0 = int(motiv[0] * w), int(motiv[1] * h)
        mx1, my1 = max(mx0 + 8, int((motiv[0] + motiv[2]) * w)), max(my0 + 8, int((motiv[1] + motiv[3]) * h))
        m["schaerfe"] = float(laplace[my0:my1, mx0:mx1].var())
        m["motiv"] = "markiert"
        markiert = [
            i for i, (ys, xs) in enumerate(kacheln)
            if xs.start < mx1 and xs.stop > mx0 and ys.start < my1 and ys.stop > my0
        ]
        if markiert:
            motiv_kacheln = np.array(markiert)
    note_schaerfe = _skala(m["schaerfe"], q["schaerfe_unscharf"], q["schaerfe_scharf"], log=True)

    # --- Schärfe in der 100-%-Ansicht (entscheidend für große Wandbilder) ---
    if voll is not None:
        fy, fx = voll.shape[0] / h, voll.shape[1] / w
        if motiv:
            vh, vw = voll.shape
            bereiche = [(int(motiv[0] * vw), int(motiv[1] * vh), int(motiv[2] * vw), int(motiv[3] * vh))]
        elif gesichter:
            bereiche = [(int(x * fx), int(y * fy), int(fw * fx), int(fh * fy)) for x, y, fw, fh in gesichter]
        else:
            bereiche = []
            for i in motiv_kacheln:
                ys, xs = kacheln[i]
                bereiche.append((int(xs.start * fx), int(ys.start * fy), int((xs.stop - xs.start) * fx), int((ys.stop - ys.start) * fy)))
        ergebnis = _schaerfe_100(voll, bereiche)
        if ergebnis is not None:
            m["kantenbreite_100"] = round(ergebnis["kantenbreite"], 2)
            note_100 = _skala(ergebnis["kantenbreite"], q["kantenbreite_unscharf"], q["kantenbreite_scharf"])
            m["note_schaerfe_100"] = round(note_100, 3)
            m["note_schaerfe_uebersicht"] = round(note_schaerfe, 3)
            note_schaerfe = 0.3 * note_schaerfe + 0.7 * note_100
            if "richtungsverhaeltnis" in ergebnis:
                m["richtungsverhaeltnis"] = round(ergebnis["richtungsverhaeltnis"], 2)
                m["verwacklung_winkel"] = ergebnis["bewegungsrichtung"]

    # --- Verwacklung (gerichtete Bewegungsunschärfe) -----------------------
    if "richtungsverhaeltnis" in m:
        # Genaue Messung in voller Auflösung: Kantenbreite je Richtung vergleichen
        m["verwackelt"] = bool(
            m["richtungsverhaeltnis"] >= q["verwacklung_verhaeltnis"]
            and ergebnis["breite_max_richtung"] > q["verwacklung_min_breite"]
            and ergebnis["kantenbreite"] > q["verwacklung_min_kantenbreite"]
        )
        note_verwacklung = (
            _skala(m["richtungsverhaeltnis"], q["verwacklung_verhaeltnis"] * 1.6, q["verwacklung_verhaeltnis"] * 0.85)
            if m["verwackelt"] else 1.0
        )
    else:
        # Grobe Schätzung auf der Vorschau (Strukturtensor)
        gx = cv2.Sobel(weich, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(weich, cv2.CV_32F, 0, 1)
        maske = np.zeros((h, w), dtype=bool)
        for i in motiv_kacheln:
            maske[kacheln[i]] = True
        m["richtungsanteil"], m["verwacklung_winkel"] = _richtung(gx[maske], gy[maske])
        m["verwackelt"] = bool(m["richtungsanteil"] > q["verwacklung_grenze"] and note_schaerfe < 0.7)
        note_verwacklung = _skala(m["richtungsanteil"], 1.0, q["verwacklung_grenze"]) * 0.7 if m["verwackelt"] else 1.0

    # --- Rauschen ---------------------------------------------------------
    m["rauschen"] = _rauschen(grau)
    note_rauschen = _skala(m["rauschen"], q["rauschen_schlecht"], q["rauschen_ok"])

    # --- Auflösung / Wandbild-Tauglichkeit -------------------------------
    breite, hoehe = info.get("breite") or w, info.get("hoehe") or h
    m["megapixel"] = round(breite * hoehe / 1e6, 1)
    m["druck_cm"] = [round(breite / q["druck_dpi"] * 2.54), round(hoehe / q["druck_dpi"] * 2.54)]
    note_aufloesung = _skala(m["megapixel"], q["wandbild_min_megapixel"] * 0.4, q["wandbild_min_megapixel"])

    noten = {
        "schaerfe": note_schaerfe,
        "belichtung": min(note_ueber, note_unter),
        "rauschen": note_rauschen,
        "verwacklung": note_verwacklung,
        "aufloesung": note_aufloesung,
    }
    m["noten"] = {k: round(v, 3) for k, v in noten.items()}
    m["note_ueber"], m["note_unter"] = round(note_ueber, 3), round(note_unter, 3)

    # --- Gesamtnote ---------------------------------------------------------
    gewichte = q["gewichte"]
    mittel = sum(gewichte[k] * noten[k] for k in noten) / sum(gewichte.values())
    # Die gravierendsten Fehler (Schärfe, Belichtung) sollen nicht "weggemittelt" werden.
    qualitaet = 0.6 * mittel + 0.4 * min(noten["schaerfe"], noten["belichtung"])

    fehlschuss = []
    if m["helligkeit"] < 8 or m["anteil_abgesoffen"] > 0.97:
        fehlschuss.append("fast komplett schwarz")
    if m["anteil_ausgefressen"] > 0.6:
        fehlschuss.append("fast komplett weiß")
    if m["schaerfe"] < q["schaerfe_unscharf"] * 0.4 and not m["nacht"]:
        fehlschuss.append("extrem unscharf")
    m["fehlschuss"] = fehlschuss
    if fehlschuss:
        qualitaet = min(qualitaet, 0.1)
    m["qualitaet"] = round(float(qualitaet), 3)
    return m
