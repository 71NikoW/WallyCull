"""Ordner durchsuchen, Bilder laden (JPG/PNG/TIFF und RAW wie Fuji .RAF) und EXIF-Daten lesen."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # große Panoramen nicht als "Bombe" ablehnen

# EXIF-Tags
_EXIF_IFD = 0x8769
_DATUM_ORIGINAL = 0x9003
_DATUM = 0x0132
_MODELL = 0x0110
_ISO = 0x8827
_BELICHTUNGSZEIT = 0x829A
_BLENDE = 0x829D
_BRENNWEITE = 0x920A


def dateien_finden(foto_ordner: Path, konfig: dict) -> list[dict]:
    """Findet alle Bilder und fasst Dateien mit gleichem Namen (z. B. DSCF0001.RAF + .JPG + .xmp) zusammen."""
    d = konfig["dateien"]
    raw, bild, begleit = set(d["raw"]), set(d["bild"]), set(d["begleit"])
    ignoriert = {name.lower() for name in d["ignorierte_ordner"]}
    gruppen: dict[str, dict] = {}

    for ordner, unterordner, namen in os.walk(foto_ordner):
        unterordner[:] = sorted(
            u for u in unterordner if u.lower() not in ignoriert and not u.startswith(".")
        )
        for name in sorted(namen):
            endung = Path(name).suffix.lower()
            if endung not in raw | bild | begleit:
                continue
            pfad = Path(ordner) / name
            rel = pfad.relative_to(foto_ordner).as_posix()
            stamm = rel[: -len(endung)] if endung else rel
            # "DSCF0001.RAF.xmp" gehört ebenfalls zu DSCF0001
            if endung in begleit and Path(stamm).suffix.lower() in raw | bild:
                stamm = stamm[: -len(Path(stamm).suffix)]
            g = gruppen.setdefault(stamm.lower(), {"schluessel": stamm.lower(), "dateien": [], "raw": None, "bild": None})
            g["dateien"].append(rel)
            if endung in bild and g["bild"] is None:
                g["bild"] = rel
            elif endung in raw and g["raw"] is None:
                g["raw"] = rel

    ergebnis = []
    for g in gruppen.values():
        if not (g["bild"] or g["raw"]):
            continue  # nur Begleitdateien ohne Bild
        g["quelle"] = g["bild"] or g["raw"]  # Kamera-JPG ist schneller und zeigt die Filmsimulation
        groesse, mtime = 0, 0.0
        for rel in g["dateien"]:
            st = (foto_ordner / rel).stat()
            groesse += st.st_size
            mtime = max(mtime, st.st_mtime)
        g["groesse"], g["mtime"] = groesse, mtime
        ergebnis.append(g)
    ergebnis.sort(key=lambda g: g["schluessel"])
    return ergebnis


def _exif_auswerten(exif) -> dict:
    info = {}
    if not exif:
        return info
    try:
        unter = exif.get_ifd(_EXIF_IFD)
    except Exception:
        unter = {}
    datum = unter.get(_DATUM_ORIGINAL) or exif.get(_DATUM)
    if datum:
        try:
            info["aufnahme"] = datetime.strptime(str(datum).strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    if exif.get(_MODELL):
        info["kamera"] = str(exif.get(_MODELL)).strip("\x00 ")
    for tag, feld in ((_ISO, "iso"), (_BELICHTUNGSZEIT, "belichtungszeit"), (_BLENDE, "blende"), (_BRENNWEITE, "brennweite")):
        wert = unter.get(tag)
        if isinstance(wert, (tuple, list)):
            wert = wert[0] if wert else None
        try:
            if wert is not None:
                info[feld] = float(wert)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return info


def _verkleinern(img: Image.Image, kante: int) -> Image.Image:
    if max(img.size) > kante:
        img = img.copy()
        img.thumbnail((kante, kante), Image.LANCZOS)
    return img


def _pil_laden(daten, kante: int, voll: bool = False):
    """Gibt (verkleinertes Bild, EXIF-Infos, Originalgröße, Graustufen in voller Auflösung oder None) zurück."""
    try:
        img = Image.open(daten)
    except Exception as e:
        raise ValueError("kein lesbares Bild, Datei vermutlich beschädigt") from e
    original = img.size
    info = _exif_auswerten(img.getexif())
    if voll and img.format == "JPEG" and hasattr(daten, "getvalue"):
        # Schneller Weg: OpenCV dekodiert (inkl. EXIF-Drehung) und verkleinert rund doppelt so schnell wie Pillow
        bgr = cv2.imdecode(np.frombuffer(daten.getvalue(), np.uint8), cv2.IMREAD_COLOR)
        if bgr is not None:
            grau = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
            f = kante / max(bgr.shape[:2])
            if f < 1:
                bgr = cv2.resize(bgr, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
            return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), info, original, grau
    if not voll:
        img.draft("RGB", (kante, kante))  # JPEG: dekodiert direkt verkleinert, spart viel Zeit
    img = ImageOps.exif_transpose(img).convert("RGB")
    grau = np.asarray(img.convert("L")) if voll else None
    return _verkleinern(img, kante), info, original, grau


def _ausrichten(grau: np.ndarray, klein: Image.Image) -> np.ndarray:
    """Dreht das Vollbild, falls es anders ausgerichtet ist als die Vorschau."""
    if (grau.shape[1] > grau.shape[0]) != (klein.width > klein.height) and klein.width != klein.height:
        return np.ascontiguousarray(np.rot90(grau, -1))
    return grau


def _raw_laden(pfad: Path, kante: int, voll: bool, referenz: int, entwickeln: bool):
    import rawpy

    with rawpy.imread(str(pfad)) as raw:
        breite, hoehe = raw.sizes.width, raw.sizes.height
        if raw.sizes.flip in (5, 6):
            breite, hoehe = hoehe, breite
        try:
            vorschau = raw.extract_thumb()
        except Exception:
            vorschau = None
        img, info, grau = None, {}, None
        if vorschau is not None and vorschau.format == rawpy.ThumbFormat.JPEG:
            # Eingebettetes Kamera-JPG: zeigt Belichtung und Filmsimulation so, wie die Kamera sie gerendert hat.
            # Bei der X-T5 ist es 4416 x 2944 groß und reicht damit auch für die Schärfeprüfung.
            vorschau_img, info, groesse, vorschau_grau = _pil_laden(io.BytesIO(vorschau.data), kante, voll)
            if max(groesse) >= min(kante, 1000):
                img = vorschau_img
                if voll and not entwickeln and max(groesse) >= referenz * 0.95:
                    grau = vorschau_grau
        if (voll and grau is None) or img is None:
            # RAW vollständig entwickeln: nötig für die Schärfe in der 100-%-Ansicht (langsamer)
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
            if img is None:
                img = _verkleinern(Image.fromarray(rgb), kante)
            if voll:
                grau = _ausrichten(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), img)
        return img, info, (breite, hoehe), grau


def bild_laden(pfad: Path, raw_endungen: set[str], kante: int, voll: bool = False,
               referenz: int = 4000, entwickeln: bool = False):
    """Lädt ein Bild verkleinert als RGB-Array, dazu EXIF-Infos samt Originalgröße und optional
    die Graustufen in voller Auflösung (für die Schärfeprüfung in der 100-%-Ansicht)."""
    if pfad.suffix.lower() in raw_endungen:
        img, info, (breite, hoehe), grau = _raw_laden(pfad, kante, voll, referenz, entwickeln)
    else:
        with open(pfad, "rb") as f:
            img, info, (breite, hoehe), grau = _pil_laden(io.BytesIO(f.read()), kante, voll)
        # Ausrichtung der Originalgröße an die gedrehte Vorschau anpassen
        if (img.width > img.height) != (breite > hoehe) and img.width != img.height:
            breite, hoehe = hoehe, breite
    info["breite"], info["hoehe"] = int(breite), int(hoehe)
    if grau is not None and max(grau.shape) > referenz:
        # Einheitlicher Maßstab: Schärfe wird immer bei gleicher Bildgröße gemessen
        f = referenz / max(grau.shape)
        grau = cv2.resize(grau, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return np.asarray(img), info, grau
