"""Erzeugt einen HTML-Bericht (.fotosort/bericht.html) mit Rasteransicht und Begründung pro Bild.

Die interaktive Review-Oberfläche mit Tastenkürzeln folgt in Phase 4; dieser Bericht dient zum Testen.
"""

from __future__ import annotations

import csv
import json
import webbrowser
from pathlib import Path

from . import datenbank
from .einteilung import NAMEN

VORLAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WallyCull Bericht</title>
<style>
:root{--bg:#f6f5f2;--karte:#fff;--text:#1d1d1b;--leise:#6b6a66;--rand:#e2e0da;
--highlight:#b7791f;--behalten:#2f855a;--pruefen:#2b6cb0;--aussortiert:#c53030}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--karte:#22221f;--text:#ecebe6;--leise:#a3a29b;--rand:#33322e;
--highlight:#ecc94b;--behalten:#68d391;--pruefen:#63b3ed;--aussortiert:#fc8181}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--rand);padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
h1{font-size:16px;margin:0 12px 0 0}
button,select{font:inherit;color:inherit;background:var(--karte);border:1px solid var(--rand);border-radius:8px;padding:6px 10px;cursor:pointer}
button.an{border-color:currentColor;font-weight:600}
.k-highlight{color:var(--highlight)}.k-behalten{color:var(--behalten)}.k-pruefen{color:var(--pruefen)}.k-aussortiert{color:var(--aussortiert)}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:16px}
.karte{background:var(--karte);border:1px solid var(--rand);border-radius:10px;overflow:hidden;cursor:pointer}
.karte img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block;background:var(--rand)}
.info{padding:8px 10px}.zeile{display:flex;justify-content:space-between;gap:6px}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.grund{color:var(--leise);font-size:12.5px;margin-top:2px}
.marke{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
#gross{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;z-index:5;padding:16px;color:#eee;overflow:auto}
#gross.offen{display:flex;gap:16px;flex-wrap:wrap;justify-content:center;align-items:flex-start}
#gross img{max-width:min(100%,1000px);max-height:85vh;border-radius:6px}
#gross .text{max-width:380px}#gross li{margin:4px 0}
table{border-collapse:collapse;font-size:12.5px}td{padding:2px 8px 2px 0}
.leer{padding:40px;color:var(--leise)}
</style></head><body>
<header><h1>WallyCull</h1><span id="filter"></span>
<select id="sort"><option value="q-">Qualität absteigend</option><option value="q+">Qualität aufsteigend</option>
<option value="name">Name</option><option value="datum">Aufnahmedatum</option><option value="gruppe">Serien zusammen</option></select>
<span style="color:var(--leise)">%TITEL%</span></header>
<main id="raster"></main><div id="gross"></div>
<script>
const BILDER = %DATEN%; const NAMEN = %NAMEN%;
let filter = "aussortiert";
const raster = document.getElementById("raster"), gross = document.getElementById("gross");
function filterLeiste(){
  const z = {alle: BILDER.length}; for (const b of BILDER) z[b.k] = (z[b.k]||0)+1;
  document.getElementById("filter").innerHTML = ["alle","highlight","behalten","pruefen","aussortiert"].map(k =>
    `<button data-k="${k}" class="${k===filter?'an':''} k-${k}">${k==='alle'?'Alle':NAMEN[k]} (${z[k]||0})</button>`).join(" ");
  document.querySelectorAll("#filter button").forEach(b => b.onclick = () => {filter=b.dataset.k; zeichnen();});
}
function zeichnen(){
  filterLeiste();
  let liste = BILDER.filter(b => filter==="alle" || b.k===filter);
  const s = document.getElementById("sort").value;
  liste.sort((a,b) => s==="q-" ? b.q-a.q : s==="q+" ? a.q-b.q : s==="datum" ? (a.d||"").localeCompare(b.d||"")
    : s==="gruppe" ? ((a.g||1e9)-(b.g||1e9) || (a.r||0)-(b.r||0)) : a.n.localeCompare(b.n));
  raster.innerHTML = liste.length ? liste.map(b => `<div class="karte" data-i="${b.i}">
    <img loading="lazy" src="${b.v||''}" alt="">
    <div class="info"><div class="zeile"><span class="name" title="${b.p}">${b.n}</span>
    <span class="marke k-${b.k}">${NAMEN[b.k]}${b.s==='verschoben'?' ✓ verschoben':''}</span></div>
    <div class="grund">Qualität ${b.q.toFixed(2)}${b.g?` · ${b.gt==='duplikat'?'Duplikat':'Serie'} ${b.g}, Platz ${b.r}`:''}</div>
    <div class="grund">${(b.gr[0]||'')}</div></div></div>`).join("") : '<div class="leer">Keine Bilder in dieser Kategorie.</div>';
  raster.querySelectorAll(".karte").forEach(k => k.onclick = () => oeffnen(BILDER[k.dataset.i]));
}
function oeffnen(b){
  const m = b.m || {}, nt = m.noten || {};
  const zeilen = Object.entries(nt).map(([k,v]) => `<tr><td>${k}</td><td>${v.toFixed(2)}</td></tr>`).join("");
  gross.innerHTML = `<img src="${b.v||''}"><div class="text"><h2 style="margin-top:0">${b.n}</h2>
    <div class="marke k-${b.k}" style="display:inline-block">${NAMEN[b.k]}</div>
    <p style="word-break:break-all;color:#bbb">${b.p}${b.d?'<br>Aufnahme: '+b.d.replace('T',' '):''}</p>
    <ul>${b.gr.map(g => `<li>${g}</li>`).join("")}</ul>
    <table><tr><td><b>Qualität</b></td><td><b>${b.q.toFixed(2)}</b></td></tr>${zeilen}</table>
    <p style="color:#bbb">Klicken oder Esc zum Schließen</p></div>`;
  gross.classList.add("offen");
}
gross.onclick = () => gross.classList.remove("offen");
document.onkeydown = e => { if (e.key === "Escape") gross.classList.remove("offen"); };
document.getElementById("sort").onchange = zeichnen;
if (!BILDER.some(b => b.k === "aussortiert")) filter = "alle";
zeichnen();
</script></body></html>
"""


def erstellen(foto_ordner: Path, oeffnen: bool = False) -> Path:
    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    zeilen = db.execute(
        "SELECT * FROM bilder WHERE status IN ('aktiv','verschoben') AND kategorie IS NOT NULL ORDER BY schluessel"
    ).fetchall()
    db.close()
    daten = []
    for i, z in enumerate(zeilen):
        b = datenbank.zeile_zu_dict(z)
        m = {k: v for k, v in (b.get("metriken") or {}).items() if k != "traceback"}
        daten.append({
            "i": i, "n": Path(b["quelle"]).name, "p": " + ".join(b["dateien"]),
            "v": f"vorschau/{b['vorschau']}" if b.get("vorschau") else "",
            "k": b["kategorie"], "q": b["qualitaet"] or 0, "gr": b.get("gruende") or [],
            "g": b["gruppe"], "gt": b["gruppe_typ"], "r": b["gruppe_rang"], "d": b["aufnahme"],
            "s": b["status"], "m": m,
        })
    html = (
        VORLAGE.replace("%DATEN%", json.dumps(daten, ensure_ascii=False).replace("</", "<\\/"))
        .replace("%NAMEN%", json.dumps(NAMEN, ensure_ascii=False))
        .replace("%TITEL%", str(foto_ordner).replace("<", "&lt;"))
    )
    ziel = datenbank.daten_ordner(foto_ordner) / "bericht.html"
    ziel.write_text(html, encoding="utf-8")
    if oeffnen:
        webbrowser.open(ziel.as_uri())
    return ziel


def anmerkungen_export(foto_ordner: Path) -> Path:
    """Schreibt alle deine Anmerkungen samt Messwerten in eine CSV-Datei (für Excel, Trennzeichen ;).
    Damit lässt sich gezielt nachvollziehen, wo die Analyse danebenlag."""
    foto_ordner = Path(foto_ordner).resolve()
    db = datenbank.oeffnen(foto_ordner)
    zeilen = db.execute(
        "SELECT a.*, b.quelle, b.qualitaet, b.metriken, b.kategorie FROM anmerkungen a "
        "JOIN bilder b ON b.id = a.bild_id ORDER BY a.id"
    ).fetchall()
    db.close()
    ziel = datenbank.daten_ordner(foto_ordner) / "anmerkungen.csv"
    with open(ziel, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Zeit", "Datei", "Art", "Anmerkung", "Bereich (x;y;b;h)", "Qualität", "Kantenbreite",
                    "Schärfe", "Verwackelt", "Richtungsverhältnis", "Motiv", "Kategorie"])
        for z in zeilen:
            m = json.loads(z["metriken"] or "{}")
            bereich = "" if z["b"] is None else " ".join(f"{v:.3f}" for v in (z["x"], z["y"], z["b"], z["h"]))
            w.writerow([
                z["zeit"], z["quelle"], datenbank.ANMERKUNG_ARTEN.get(z["art"], z["art"]), z["text"] or "", bereich,
                z["qualitaet"], m.get("kantenbreite_100"), round(m.get("schaerfe") or 0, 1), m.get("verwackelt"),
                m.get("richtungsverhaeltnis"), m.get("motiv"), z["kategorie"],
            ])
    return ziel
