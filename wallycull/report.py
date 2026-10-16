"""Creates an HTML report (.wallycull/report.html): a grid of all images with the reason for each one."""

from __future__ import annotations

import csv
import json
import webbrowser
from pathlib import Path

from . import database, numbers
from .classify import NAMES

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>WallyCull report</title>
<style>
:root{--bg:#f6f5f2;--card:#fff;--text:#1d1d1b;--muted:#6b6a66;--line:#e2e0da;
--highlight:#b7791f;--keep:#2f855a;--review:#2b6cb0;--reject:#c53030}
@media (prefers-color-scheme:dark){:root{--bg:#161615;--card:#22221f;--text:#ecebe6;--muted:#a3a29b;--line:#33322e;
--highlight:#ecc94b;--keep:#68d391;--review:#63b3ed;--reject:#fc8181}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif}
header{position:sticky;top:0;z-index:2;background:var(--bg);border-bottom:1px solid var(--line);padding:12px 16px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
h1{font-size:16px;margin:0 12px 0 0}
button,select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:6px 10px;cursor:pointer}
button.on{border-color:currentColor;font-weight:600}
.c-highlight{color:var(--highlight)}.c-keep{color:var(--keep)}.c-review{color:var(--review)}.c-reject{color:var(--reject)}
main{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:12px;padding:16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden;cursor:pointer}
.card img{width:100%;aspect-ratio:3/2;object-fit:cover;display:block;background:var(--line)}
.info{padding:8px 10px}.row{display:flex;justify-content:space-between;gap:6px}
.name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.reason{color:var(--muted);font-size:12.5px;margin-top:2px}
.tag{font-size:11px;padding:1px 6px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
#big{position:fixed;inset:0;background:rgba(0,0,0,.85);display:none;z-index:5;padding:16px;color:#eee;overflow:auto}
#big.open{display:flex;gap:16px;flex-wrap:wrap;justify-content:center;align-items:flex-start}
#big img{max-width:min(100%,1000px);max-height:85vh;border-radius:6px}
#big .text{max-width:380px}#big li{margin:4px 0}
table{border-collapse:collapse;font-size:12.5px}td{padding:2px 8px 2px 0}
.empty{padding:40px;color:var(--muted)}
</style></head><body>
<header><h1>WallyCull</h1><span id="filter"></span>
<select id="sort"><option value="q-">Quality, best first</option><option value="q+">Quality, worst first</option>
<option value="name">Name</option><option value="date">Capture date</option><option value="group">Bursts together</option></select>
<select id="car" hidden><option value="">All cars</option></select>
<span style="color:var(--muted)">%TITLE%</span></header>
<main id="grid"></main><div id="big"></div>
<script>
const IMAGES = %DATA%; const NAMES = %NAMES%;
let filter = "reject";
const esc = t => String(t??"").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const grid = document.getElementById("grid"), big = document.getElementById("big");
function filterBar(){
  const n = {all: IMAGES.length}; for (const b of IMAGES) n[b.c] = (n[b.c]||0)+1;
  document.getElementById("filter").innerHTML = ["all","highlight","keep","review","reject"].map(k =>
    `<button data-k="${k}" class="${k===filter?'on':''} c-${k}">${k==='all'?'All':NAMES[k]} (${n[k]||0})</button>`).join(" ");
  document.querySelectorAll("#filter button").forEach(b => b.onclick = () => {filter=b.dataset.k; draw();});
}
function draw(){
  filterBar();
  const car = document.getElementById("car").value;
  let list = IMAGES.filter(b => (filter==="all" || b.c===filter) && (!car || b.num===car));
  const s = document.getElementById("sort").value;
  list.sort((a,b) => s==="q-" ? b.q-a.q : s==="q+" ? a.q-b.q : s==="date" ? (a.d||"").localeCompare(b.d||"")
    : s==="group" ? ((a.g||1e9)-(b.g||1e9) || (a.r||0)-(b.r||0)) : a.n.localeCompare(b.n));
  grid.innerHTML = list.length ? list.map(b => `<div class="card" data-i="${b.i}">
    <img loading="lazy" src="${b.v||''}" alt="">
    <div class="info"><div class="row"><span class="name" title="${b.p}">${b.n}</span>
    <span class="tag c-${b.c}">${NAMES[b.c]}${b.s==='moved'?' ✓ moved':''}</span></div>
    <div class="reason">Quality ${b.q.toFixed(2)}${b.g?` · ${b.gt==='duplicate'?'Duplicate':'Burst'} ${b.g}, #${b.r}`:''}</div>
    ${b.nr?`<div class="reason"><b>${esc(b.nr)}</b></div>`:''}
    <div class="reason">${(b.why[0]||'')}</div></div></div>`).join("") : '<div class="empty">No images in this category.</div>';
  grid.querySelectorAll(".card").forEach(k => k.onclick = () => open_(IMAGES[k.dataset.i]));
}
function open_(b){
  const m = b.m || {}, sc = m.scores || {};
  const rows = Object.entries(sc).map(([k,v]) => `<tr><td>${k}</td><td>${v.toFixed(2)}</td></tr>`).join("");
  big.innerHTML = `<img src="${b.v||''}"><div class="text"><h2 style="margin-top:0">${b.n}</h2>
    <div class="tag c-${b.c}" style="display:inline-block">${NAMES[b.c]}</div>
    ${b.nr?`<p><b>${esc(b.nr)}</b></p>`:''}
    <p style="word-break:break-all;color:#bbb">${b.p}${b.d?'<br>Taken: '+b.d.replace('T',' '):''}</p>
    <ul>${b.why.map(g => `<li>${g}</li>`).join("")}</ul>
    <table><tr><td><b>Quality</b></td><td><b>${b.q.toFixed(2)}</b></td></tr>${rows}</table>
    <p style="color:#bbb">Click or press Esc to close</p></div>`;
  big.classList.add("open");
}
big.onclick = () => big.classList.remove("open");
document.onkeydown = e => { if (e.key === "Escape") big.classList.remove("open"); };
document.getElementById("sort").onchange = draw;
const cars = {}; for (const b of IMAGES) if (b.num) cars[b.num] = (cars[b.num]||[]).concat(b);
const carSelect = document.getElementById("car");
carSelect.innerHTML += Object.keys(cars).sort((a,b) => a-b).map(n =>
  `<option value="${n}">${esc(cars[n][0].nr)} (${cars[n].length})</option>`).join("");
carSelect.hidden = !Object.keys(cars).length; carSelect.onchange = draw;
if (!IMAGES.some(b => b.c === "reject")) filter = "all";
draw();
</script></body></html>
"""


def create(photo_dir: Path, open_browser: bool = False) -> Path:
    photo_dir = Path(photo_dir).resolve()
    db = database.open_db(photo_dir)
    rows = db.execute(
        "SELECT * FROM images WHERE status IN ('active','moved') AND category IS NOT NULL ORDER BY key"
    ).fetchall()
    db.close()
    data = []
    for i, r in enumerate(rows):
        b = database.row_to_dict(r)
        m = {k: v for k, v in (b.get("metrics") or {}).items() if k != "traceback"}
        data.append({
            "i": i, "n": Path(b["source"]).name, "p": " + ".join(b["files"]),
            "v": f"previews/{b['preview']}" if b.get("preview") else "",
            "c": b["category"], "q": b["quality"] or 0, "why": b.get("reasons") or [],
            "g": b["group_id"], "gt": b["group_type"], "r": b["group_rank"], "d": b["taken_at"],
            "s": b["status"], "m": m,
            "num": (b.get("numbers") or {}).get("number"), "nr": numbers.label(b.get("numbers") or None),
        })
    html = (
        TEMPLATE.replace("%DATA%", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
        .replace("%NAMES%", json.dumps(NAMES))
        .replace("%TITLE%", str(photo_dir).replace("<", "&lt;"))
    )
    target = database.data_dir(photo_dir) / "report.html"
    target.write_text(html, encoding="utf-8")
    if open_browser:
        webbrowser.open(target.as_uri())
    return target


def export_annotations(photo_dir: Path) -> Path:
    """Writes all your notes with the measurements to a CSV file (opens in Excel).
    Shows exactly where the analysis got it wrong."""
    photo_dir = Path(photo_dir).resolve()
    db = database.open_db(photo_dir)
    rows = db.execute(
        "SELECT a.*, i.source, i.quality, i.metrics, i.category FROM annotations a "
        "JOIN images i ON i.id = a.image_id ORDER BY a.id"
    ).fetchall()
    db.close()
    target = database.data_dir(photo_dir) / "annotations.csv"
    with open(target, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["Time", "File", "Kind", "Note", "Area (x y w h)", "Quality", "Edge width",
                    "Sharpness", "Shaky", "Direction ratio", "Subject", "Category"])
        for r in rows:
            m = json.loads(r["metrics"] or "{}")
            area = "" if r["w"] is None else " ".join(f"{v:.3f}" for v in (r["x"], r["y"], r["w"], r["h"]))
            w.writerow([
                r["time"], r["source"], database.ANNOTATION_KINDS.get(r["kind"], r["kind"]), r["text"] or "", area,
                r["quality"], m.get("edge_width_100"), round(m.get("sharpness") or 0, 1), m.get("shaky"),
                m.get("direction_ratio"), m.get("subject"), r["category"],
            ])
    return target
