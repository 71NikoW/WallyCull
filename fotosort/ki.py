"""Phase 2: fertig trainierte KI-Modelle für Ästhetik, Qualität und Inhalt (laufen lokal, GPU wenn vorhanden).

- CLIP ViT-L/14 (OpenAI): Bildmerkmale (Embedding), Inhaltstyp per Zero-Shot, Qualitäts-Fragen (CLIP-IQA-Prinzip)
- LAION Aesthetic Predictor v2: Ästhetik 1-10, trainiert auf Hunderttausenden online bewerteten Fotos
- optional pyiqa: MUSIQ (technische Qualität, KonIQ) und NIMA (Ästhetik, AVA-Fotowettbewerbe)
- Vorbilder-Ordner: Ähnlichkeit deiner Fotos zu Bildern, die du als gute Wandbilder ansiehst

Einmalig werden die Modelle aus dem Internet geladen. Deine Fotos verlassen den PC nie.
"""

from __future__ import annotations

import json
import os
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from tqdm import tqdm

from . import datenbank, einlesen
from .pfade import BENUTZER_ORDNER, MODELL_ORDNER

KI_VERSION = 1
LAION_URL = (
    "https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/main/sac+logos+ava1-l14-linearMSE.pth"
)

# Inhaltstypen: (Schlüssel, deutsche Bezeichnung, englischer Prompt, Wert: -1 wenig, 0 neutral, +1 hoch)
INHALTE = [
    ("screenshot", "Screenshot", "a screenshot of a phone or computer screen", -1),
    ("meme", "Meme", "a meme with large text", -1),
    ("beleg", "Kassenzettel/Beleg", "a photo of a receipt or invoice", -1),
    ("dokument", "Dokument", "a photo of a document or a sheet of paper with text", -1),
    ("parkplatz", "Parkplatz/Auto-Notiz", "a photo of a parking lot or a parking spot number", -1),
    ("whiteboard", "Whiteboard/Tafel", "a photo of a whiteboard or blackboard", -1),
    ("versehen", "Versehentliches Foto", "an accidental photo of the floor, the ceiling or the inside of a pocket", -1),
    ("finger", "Finger vor der Linse", "a photo with a finger covering the camera lens", -1),
    ("testbild", "Testaufnahme", "a dark, blurry test shot of nothing in particular", -1),
    ("menschen", "Menschen", "a photo of people", 1),
    ("portraet", "Porträt", "a portrait photo of a person", 1),
    ("feier", "Feier/Ereignis", "a photo of a party, wedding or celebration", 1),
    ("tier", "Tier", "a photo of an animal", 1),
    ("landschaft", "Landschaft", "a landscape photo of nature", 1),
    ("berge", "Berge", "a photo of mountains", 1),
    ("meer", "Meer/Strand", "a photo of the sea, a lake or a beach", 1),
    ("himmel", "Sonnenuntergang/Himmel", "a photo of a sunset, sunrise or dramatic sky", 1),
    ("stadt", "Stadt/Straße", "a cityscape or street photo", 1),
    ("architektur", "Architektur", "an architectural photo of a building", 1),
    ("sehenswuerdigkeit", "Sehenswürdigkeit", "a photo of a famous landmark", 1),
    ("pflanzen", "Pflanzen/Makro", "a close-up photo of flowers or plants", 1),
    ("abstrakt", "Abstrakt/Kunst", "an abstract artistic photo", 1),
    ("auto", "Fahrzeug", "a photo of a car, motorbike or vehicle", 0),
    ("essen", "Essen", "a photo of food", 0),
    ("innenraum", "Innenraum", "a photo of a room interior", 0),
    ("gegenstand", "Gegenstand", "a photo of an everyday object", 0),
]

# Ja/Nein-Fragen an CLIP (positiv, negativ)
FRAGEN = {
    "clip_schaerfe": ("a sharp, in-focus photo", "a blurry, out-of-focus photo"),
    "clip_belichtung": ("a well exposed photo", "a badly exposed photo"),
    "clip_qualitaet": ("a high quality photo", "a low quality photo"),
    "wandbild": ("a stunning photograph that would look great as a large print on a wall",
                 "an ordinary, forgettable snapshot"),
}


def raw_endungen(konfig: dict) -> set[str]:
    return set(konfig["dateien"]["raw"])


def _norm(x: float, unten: float, oben: float) -> float:
    return float(min(1.0, max(0.0, (x - unten) / (oben - unten))))


def wert_berechnen(bilder: list[dict], konfig: dict) -> None:
    """Setzt b["wert"] (0..1) aus den KI-Werten. Vorbild-Ähnlichkeit wird relativ zum Ordner bewertet."""
    gewichte = konfig["ki"]["wert_gewichte"]
    sims = sorted((b["ki"]["vorbild"] for b in bilder if isinstance(b.get("ki"), dict) and "vorbild" in b["ki"]))
    for b in bilder:
        r = b.get("ki") if isinstance(b.get("ki"), dict) else None
        if not r:
            b["wert"] = None
            continue
        teile = {}
        if "aesthetik" in r:
            teile["aesthetik"] = _norm(r["aesthetik"], 4.3, 6.5)
        if "wandbild" in r:
            teile["wandbild"] = r["wandbild"]
        if "vorbild" in r and len(sims) > 1:
            teile["vorbild"] = float(np.searchsorted(sims, r["vorbild"]) / (len(sims) - 1))
        if "inhalt_wert" in r:
            teile["inhalt"] = (r["inhalt_wert"] + 1) / 2
        if "nima" in r:
            teile["nima"] = _norm(r["nima"], 4.0, 6.5)
        summe = sum(gewichte.get(k, 0) for k in teile)
        b["wert"] = sum(gewichte.get(k, 0) * v for k, v in teile.items()) / summe if summe else None
        b["wert_teile"] = teile


def ki_gruende(b: dict) -> list[str]:
    r = b.get("ki") if isinstance(b.get("ki"), dict) else None
    if not r:
        return []
    gruende = []
    inhalt = next((x for x in INHALTE if x[0] == r.get("inhalt")), None)
    if inhalt and r.get("inhalt_sicherheit", 0) >= 0.3:
        zusatz = " (wenig Langzeitwert)" if inhalt[3] < 0 else ""
        gruende.append(f"Inhalt: {inhalt[1]}{zusatz}")
    if "aesthetik" in r:
        gruende.append(f"Ästhetik {r['aesthetik']:.1f}/10 (KI)".replace(".", ","))
    if "wandbild" in r:
        gruende.append(f"Wandbild-Eignung {r['wandbild'] * 100:.0f} %")
    teile = b.get("wert_teile") or {}
    if "vorbild" in teile:
        if teile["vorbild"] >= 0.8:
            gruende.append("Sehr nah an deinen Vorbildern")
        elif teile["vorbild"] <= 0.2:
            gruende.append("Weit weg von deinen Vorbildern")
    return gruende


def verfuegbar() -> tuple[bool, str]:
    import importlib.util

    if not all(importlib.util.find_spec(m) for m in ("torch", "open_clip")):
        return False, "KI-Pakete fehlen: in der App unter „KI installieren“ einrichten (bis dahin ohne KI)."
    return True, ""


class Modelle:
    """Lädt die Modelle einmal und hält sie im (Grafik-)Speicher."""

    def __init__(self, konfig: dict):
        import open_clip
        import torch

        self.torch = torch
        k = konfig["ki"]
        wunsch = k.get("geraet", "auto")
        self.geraet = ("cuda" if torch.cuda.is_available() else "cpu") if wunsch == "auto" else wunsch
        print(f"KI läuft auf: {'Grafikkarte (' + torch.cuda.get_device_name(0) + ')' if self.geraet == 'cuda' else 'CPU'}")
        self.clip, _, self.vorverarbeitung = open_clip.create_model_and_transforms(
            k["clip_modell"], pretrained=k["clip_gewichte"], device=self.geraet
        )
        self.clip.eval()
        tokenizer = open_clip.get_tokenizer(k["clip_modell"])
        with torch.no_grad():
            texte = [p for _, _, p, _ in INHALTE] + [t for paar in FRAGEN.values() for t in paar]
            t = self.clip.encode_text(tokenizer(texte).to(self.geraet)).float()
            t = t / t.norm(dim=-1, keepdim=True)
        self.text_inhalt = t[: len(INHALTE)]
        self.text_fragen = t[len(INHALTE):]
        self.laion = self._laion_laden()
        self.iqa = {}
        for name in k.get("pyiqa_modelle") or []:
            try:
                import pyiqa

                self.iqa[name] = pyiqa.create_metric(name, device=self.geraet)
            except Exception as e:  # optional: ohne pyiqa geht es auch
                print(f"  Hinweis: {name} nicht verfügbar ({type(e).__name__}: {e}). Läuft ohne weiter.")

    def _laion_laden(self) -> list[tuple[np.ndarray, np.ndarray]] | None:
        MODELL_ORDNER.mkdir(parents=True, exist_ok=True)
        pfad = MODELL_ORDNER / "laion_aesthetic_vit_l14.pth"
        if not pfad.exists():
            print("Lade LAION-Ästhetikmodell (einmalig, ca. 4 MB) ...")
            try:
                urllib.request.urlretrieve(LAION_URL, pfad)
            except Exception as e:
                print(f"  Download fehlgeschlagen ({e}). Ästhetik-Score wird übersprungen.")
                return None
        zustand = self.torch.load(pfad, map_location="cpu", weights_only=True)
        # Das Modell ist eine Kette linearer Schichten (ohne Aktivierung): direkt in NumPy rechnen
        schichten = []
        for i in sorted({int(k.split(".")[1]) for k in zustand if k.endswith(".weight")}):
            schichten.append((zustand[f"layers.{i}.weight"].numpy().astype(np.float64),
                              zustand[f"layers.{i}.bias"].numpy().astype(np.float64)))
        return schichten

    def _aesthetik(self, emb: np.ndarray) -> np.ndarray:
        x = emb.astype(np.float64)
        for w, b in self.laion:
            x = x @ w.T + b
        return x[:, 0]

    def stapel(self, bilder: list[Image.Image]) -> list[dict]:
        torch = self.torch
        with torch.no_grad():
            eingabe = torch.stack([self.vorverarbeitung(b) for b in bilder]).to(self.geraet)
            if self.geraet == "cuda":
                with torch.autocast("cuda", dtype=torch.float16):
                    emb = self.clip.encode_image(eingabe)
            else:
                emb = self.clip.encode_image(eingabe)
            emb = emb.float()
            emb = emb / emb.norm(dim=-1, keepdim=True)
            inhalt = (100 * emb @ self.text_inhalt.T).softmax(dim=-1).cpu().numpy()
            fragen = (100 * emb @ self.text_fragen.T).cpu().numpy()
            emb_np = emb.cpu().numpy()
            iqa_werte = {}
            for name, metrik in self.iqa.items():
                try:
                    tensoren = [torch.from_numpy(np.asarray(b, dtype=np.float32) / 255).permute(2, 0, 1) for b in bilder]
                    iqa_werte[name] = [float(metrik(t.unsqueeze(0).to(self.geraet)).item()) for t in tensoren]
                except Exception as e:
                    print(f"  {name} fehlgeschlagen: {e}")
        aesthetik = self._aesthetik(emb_np) if self.laion else [None] * len(bilder)
        ergebnisse = []
        for i in range(len(bilder)):
            r: dict = {}
            if aesthetik[i] is not None:
                r["aesthetik"] = round(float(aesthetik[i]), 3)
            for j, schluessel in enumerate(FRAGEN):
                ja, nein = fragen[i, 2 * j], fragen[i, 2 * j + 1]
                r[schluessel] = round(float(1 / (1 + np.exp(nein - ja))), 3)
            p = inhalt[i]
            beste = int(p.argmax())
            r["inhalt"] = INHALTE[beste][0]
            r["inhalt_sicherheit"] = round(float(p[beste]), 3)
            r["inhalt_wert"] = round(float(sum(p[j] * INHALTE[j][3] for j in range(len(INHALTE)))), 3)
            for name, werte in iqa_werte.items():
                r[name] = round(werte[i], 3)
            ergebnisse.append(r)
        return [(r, emb_np[i].astype(np.float16)) for i, r in enumerate(ergebnisse)]


def _bild_fuer_ki(foto_ordner: Path, quelle: str, raw: set[str], kante: int) -> Image.Image | None:
    try:
        rgb, _, _ = einlesen.bild_laden(foto_ordner / quelle, raw, kante)
        return Image.fromarray(rgb)
    except Exception:
        return None


def vorbilder_einbetten(modelle_holen, ordner: Path, konfig: dict) -> np.ndarray | None:
    """Embeddings der Vorbild-Bilder (zwischengespeichert, solange sich der Ordner nicht ändert)."""
    if not ordner.is_dir():
        return None
    endungen = set(konfig["dateien"]["bild"]) | set(konfig["dateien"]["raw"])
    dateien = sorted(p for p in ordner.rglob("*") if p.suffix.lower() in endungen)
    if not dateien:
        return None
    schluessel = json.dumps([(p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in dateien])
    cache = ordner / ".vorbilder_cache.npz"
    if cache.exists():
        daten = np.load(cache, allow_pickle=False)
        if str(daten["schluessel"]) == schluessel:
            return daten["emb"].astype(np.float32)
    print(f"Lese {len(dateien)} Vorbilder ein ...")
    modelle = modelle_holen()
    embs = []
    for i in range(0, len(dateien), 16):
        bilder = [_bild_fuer_ki(p.parent, p.name, set(konfig["dateien"]["raw"]), 1024) for p in dateien[i : i + 16]]
        bilder = [b for b in bilder if b is not None]
        if bilder:
            embs.extend(e for _, e in modelle.stapel(bilder))
    if not embs:
        return None
    emb = np.array(embs, dtype=np.float32)
    try:
        np.savez(cache, emb=emb, schluessel=np.array(schluessel))
    except OSError:
        pass
    return emb


def vorbild_aehnlichkeit(emb: np.ndarray, vorbilder: np.ndarray, top: int = 5) -> np.ndarray:
    """Mittlere Kosinus-Ähnlichkeit zu den ähnlichsten Vorbildern."""
    a = emb.astype(np.float32)
    a /= np.linalg.norm(a, axis=1, keepdims=True) + 1e-8
    v = vorbilder / (np.linalg.norm(vorbilder, axis=1, keepdims=True) + 1e-8)
    sim = a @ v.T
    k = min(top, v.shape[0])
    return np.sort(sim, axis=1)[:, -k:].mean(axis=1)


def lauf(foto_ordner: Path, konfig: dict, neu_berechnen: bool = False) -> dict:
    """Berechnet die KI-Werte für alle Bilder, die sie noch nicht haben (Cache in der Datenbank)."""
    k = konfig["ki"]
    if not k.get("aktiv", True):
        return {"ki": "aus"}
    ok, hinweis = verfuegbar()
    if not ok:
        print(hinweis)
        return {"ki": "fehlt"}
    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    bedingung = "" if neu_berechnen else "AND (ki_version IS NULL OR ki_version != ?)"
    werte = () if neu_berechnen else (KI_VERSION,)
    offen = db.execute(
        f"SELECT id, quelle FROM bilder WHERE status='aktiv' AND fehler IS NULL {bedingung}", werte
    ).fetchall()

    vorbild_ordner = Path(os.path.expandvars(k.get("vorbilder_ordner") or "")).expanduser()
    if k.get("vorbilder_ordner") and not vorbild_ordner.is_absolute():
        vorbild_ordner = BENUTZER_ORDNER / vorbild_ordner
    endungen = set(konfig["dateien"]["bild"]) | raw_endungen(konfig)
    hat_vorbilder = vorbild_ordner.is_dir() and any(p.suffix.lower() in endungen for p in vorbild_ordner.rglob("*"))
    braucht_modelle = bool(offen) or hat_vorbilder
    if not braucht_modelle:
        db.close()
        return {"ki": "aktuell", "neu": 0}

    geladen: list[Modelle] = []

    def modelle_holen() -> Modelle:
        if not geladen:
            geladen.append(Modelle(konfig))
        return geladen[0]

    raw = raw_endungen(konfig)
    groesse = int(k.get("stapel", 16))
    if offen:
        print(f"KI-Analyse für {len(offen)} Bilder ...")
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as laden, tqdm(total=len(offen), desc="KI", unit="Bild") as balken:
            for start in range(0, len(offen), groesse):
                teil = offen[start : start + groesse]
                bilder = list(laden.map(lambda z: _bild_fuer_ki(foto_ordner, z["quelle"], raw, 1024), teil))
                gueltig = [(z, b) for z, b in zip(teil, bilder) if b is not None]
                if gueltig:
                    for (z, _), (r, emb) in zip(gueltig, modelle_holen().stapel([b for _, b in gueltig])):
                        db.execute(
                            "UPDATE bilder SET ki=?, embedding=?, ki_version=? WHERE id=?",
                            (json.dumps(r), emb.tobytes(), KI_VERSION, z["id"]),
                        )
                db.commit()
                balken.update(len(teil))

    # Vorbild-Ähnlichkeit für alle Bilder neu setzen (billig, ändert sich mit dem Vorbilder-Ordner)
    vorbilder = vorbilder_einbetten(modelle_holen, vorbild_ordner, konfig) if hat_vorbilder else None
    zeilen = db.execute("SELECT id, ki, embedding FROM bilder WHERE embedding IS NOT NULL").fetchall()
    if zeilen:
        sims = (
            vorbild_aehnlichkeit(np.array([np.frombuffer(z["embedding"], np.float16) for z in zeilen]), vorbilder)
            if vorbilder is not None else [None] * len(zeilen)
        )
        for z, s in zip(zeilen, sims):
            r = json.loads(z["ki"] or "{}")
            if s is None:
                r.pop("vorbild", None)
            else:
                r["vorbild"] = round(float(s), 4)
            db.execute("UPDATE bilder SET ki=? WHERE id=?", (json.dumps(r), z["id"]))
        db.commit()
    db.close()
    return {"ki": "ok", "neu": len(offen), "vorbilder": 0 if vorbilder is None else len(vorbilder)}
