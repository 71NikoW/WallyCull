"""Duplikate und Serienbilder über Wahrnehmungs-Hashes (pHash/dHash) und Aufnahmezeit gruppieren."""

from __future__ import annotations

from datetime import datetime

import numpy as np


def _bits(hex_hash: str) -> int:
    return int(hex_hash, 16)


def _abstaende(hashes: np.ndarray, i: int) -> np.ndarray:
    """Hamming-Abstand von Hash i zu allen anderen (64-Bit-Hashes)."""
    x = np.bitwise_xor(hashes, hashes[i])
    if hasattr(np, "bitwise_count"):
        return np.bitwise_count(x).astype(np.int32)
    return np.unpackbits(x.view(np.uint8).reshape(-1, 8), axis=1).sum(axis=1).astype(np.int32)


class _UnionFind:
    def __init__(self, n: int):
        self.eltern = list(range(n))

    def finden(self, a: int) -> int:
        while self.eltern[a] != a:
            self.eltern[a] = self.eltern[self.eltern[a]]
            a = self.eltern[a]
        return a

    def vereinen(self, a: int, b: int) -> None:
        self.eltern[self.finden(a)] = self.finden(b)


def gruppieren(bilder: list[dict], konfig: dict) -> list[dict]:
    """Gibt Gruppen zurück: {"typ": "duplikat"|"serie", "mitglieder": [Bild-dicts, bestes zuerst]}."""
    d = konfig["duplikate"]
    kandidaten = [b for b in bilder if b.get("phash") and b.get("dhash")]
    n = len(kandidaten)
    if n < 2:
        return []
    phash = np.array([_bits(b["phash"]) for b in kandidaten], dtype=np.uint64)
    dhash = np.array([_bits(b["dhash"]) for b in kandidaten], dtype=np.uint64)
    zeiten = np.array(
        [datetime.fromisoformat(b["aufnahme"]).timestamp() if b.get("aufnahme") else np.nan for b in kandidaten]
    )

    uf = _UnionFind(n)
    ist_duplikat = [False] * n
    for i in range(n - 1):
        ap = _abstaende(phash, i)[i + 1 :]
        ad = _abstaende(dhash, i)[i + 1 :]
        # Beide Hashes müssen passen: senkt Fehltreffer bei ähnlich aufgebauten, aber verschiedenen Motiven.
        abstand = np.maximum(ap, ad)
        zeitdiff = np.abs(zeiten[i + 1 :] - zeiten[i])
        dup = abstand <= d["duplikat_abstand"]
        serie = (abstand <= d["serie_abstand"]) & (zeitdiff <= d["serie_sekunden"])  # NaN-Zeiten -> False
        for j in np.nonzero(dup | serie)[0]:
            uf.vereinen(i, i + 1 + int(j))
            if dup[j]:
                ist_duplikat[i] = ist_duplikat[i + 1 + int(j)] = True

    sammlung: dict[int, list[int]] = {}
    for i in range(n):
        sammlung.setdefault(uf.finden(i), []).append(i)

    gruppen = []
    for indizes in sammlung.values():
        if len(indizes) < 2:
            continue
        mitglieder = sorted(
            (kandidaten[i] for i in indizes),
            # Bei praktisch gleicher Qualität gewinnt das Bild mit RAW-Datei
            key=lambda b: (
                round(b.get("rang", b["qualitaet"]) or 0, 2) + (0.011 if len(b["dateien"]) > 1 else 0),
                -((b.get("metriken") or {}).get("kantenbreite_100") or 9),  # bei Gleichstand: schärferes Bild
                (b["breite"] or 0) * (b["hoehe"] or 0),
            ),
            reverse=True,
        )
        typ = "duplikat" if all(ist_duplikat[i] for i in indizes) else "serie"
        gruppen.append({"typ": typ, "mitglieder": mitglieder})
    return gruppen
