"""Lokale Bewertungs-Oberfläche im Browser: Bilder wie bei Tinder bewerten, das Modell lernt mit.

Läuft nur auf deinem PC (127.0.0.1), es wird nichts ins Internet geschickt.
Tasten: ← weg · → behalten · ↑ Favorit · 1–5 Sterne · Leertaste Zoom 100 % · Rücktaste rückgängig
        M markieren: Bereich aufziehen und notieren, was die Analyse falsch gemacht hat
"""

from __future__ import annotations

import io
import json
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from . import analyse, bericht, datenbank, einlesen, einteilung, ki, lernen

URTEIL_ZU_KATEGORIE = {0: "aussortiert", 1: "behalten", 2: "highlight"}
STERNE_ZU_URTEIL = {1: 0, 2: 0, 3: 1, 4: 1, 5: 2}


class Sitzung:
    """Gemeinsamer Zustand des Servers (ein Foto-Ordner)."""

    def __init__(self, foto_ordner: Path, konfig: dict):
        self.foto_ordner = foto_ordner
        self.konfig = konfig
        self.sperre = threading.Lock()
        self.seit_training = 0
        self.gross = datenbank.daten_ordner(foto_ordner) / "gross"
        self.gross.mkdir(exist_ok=True)
        # Immer neu trainieren: übernimmt neue KI-Werte und ersetzt Modelle älterer Versionen
        self.status = lernen.trainieren(foto_ordner, konfig)

    def _zaehlen(self) -> dict:
        db = datenbank.oeffnen(self.foto_ordner)
        z = db.execute("SELECT COUNT(*), SUM(urteil >= 1) FROM bewertungen").fetchone()
        db.close()
        return {"bewertet": z[0] or 0, "behalten": z[1] or 0, "weg": (z[0] or 0) - (z[1] or 0), "aktiv": False}

    def offen_anzahl(self) -> int:
        db = datenbank.oeffnen(self.foto_ordner)
        n = db.execute(
            "SELECT COUNT(*) FROM bilder WHERE status='aktiv' AND metriken IS NOT NULL AND fehler IS NULL "
            "AND id NOT IN (SELECT bild_id FROM bewertungen)"
        ).fetchone()[0]
        db.close()
        return n

    def stapel(self, ausser: set[int]) -> dict:
        modell = lernen.modell_laden(self.foto_ordner)
        bilder = lernen.naechste(self.foto_ordner, self.konfig, 12, ausser)
        ergebnis = []
        for b in bilder:
            m = b.get("metriken") or {}
            eintrag = {
                "id": b["id"], "name": Path(b["quelle"]).name, "datum": b.get("aufnahme"),
                "qualitaet": b.get("qualitaet"), "kategorie": b.get("kategorie"),
                "gruende": [g for g in (b.get("gruende") or []) if not g.startswith(
                    ("Dein Modell", "Inhalt:", "Ästhetik", "Wandbild-Eignung", "Sehr nah", "Weit weg"))][:4],
                "kantenbreite": m.get("kantenbreite_100"), "druck": m.get("druck_cm"),
                "ki": [g for g in ki.ki_gruende(b)],
                "prognose": None, "anmerkungen": b.get("anmerkungen") or [],
            }
            if modell and b.get("persoenlich") is not None:
                dafuer, dagegen = lernen.erklaeren(modell, lernen.merkmale(b, modell.get("pca")))
                eintrag.update(prognose=b["persoenlich"], dafuer=dafuer, dagegen=dagegen)
            ergebnis.append(eintrag)
        return {"bilder": ergebnis, "status": {**self.status, "offen": self.offen_anzahl()}}

    def bewerten(self, bild_id: int, urteil: int, sterne: int | None) -> dict:
        with self.sperre:
            db = datenbank.oeffnen(self.foto_ordner)
            db.execute(
                "INSERT OR REPLACE INTO bewertungen (bild_id, urteil, sterne, quelle) VALUES (?, ?, ?, 'wischen')",
                (bild_id, urteil, sterne),
            )
            db.execute("UPDATE bilder SET nutzer=? WHERE id=?", (URTEIL_ZU_KATEGORIE[urteil], bild_id))
            db.commit()
            db.close()
            self.seit_training += 1
            neu = False
            if self.seit_training >= self.konfig["lernen"]["neu_trainieren_alle"]:
                self.status = lernen.trainieren(self.foto_ordner, self.konfig)
                self.seit_training, neu = 0, self.status.get("aktiv", False)
            else:
                self.status = {**self.status, **{k: v for k, v in self._zaehlen().items() if k != "aktiv"}}
            return {"status": {**self.status, "offen": self.offen_anzahl()}, "neu_trainiert": neu}

    def rueckgaengig(self, bild_id: int) -> dict:
        with self.sperre:
            db = datenbank.oeffnen(self.foto_ordner)
            db.execute("DELETE FROM bewertungen WHERE bild_id=?", (bild_id,))
            db.execute("UPDATE bilder SET nutzer=NULL WHERE id=?", (bild_id,))
            db.commit()
            db.close()
            self.seit_training = max(0, self.seit_training - 1)
            self.status = {**self.status, **{k: v for k, v in self._zaehlen().items() if k != "aktiv"}}
            return {"status": {**self.status, "offen": self.offen_anzahl()}}

    def _anmerkungen(self, db, bild_id: int) -> list[dict]:
        return [dict(z) for z in db.execute("SELECT * FROM anmerkungen WHERE bild_id=? ORDER BY id", (bild_id,))]

    def anmerken(self, bild_id: int, art: str, rechteck: list | None, text: str) -> dict:
        if art not in datenbank.ANMERKUNG_ARTEN:
            raise ValueError("unbekannte Art")
        x, y, b, h = (min(max(float(v), 0.0), 1.0) for v in rechteck) if rechteck else (None,) * 4
        with self.sperre:
            db = datenbank.oeffnen(self.foto_ordner)
            db.execute(
                "INSERT INTO anmerkungen (bild_id, art, x, y, b, h, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (bild_id, art, x, y, b, h, (text or "").strip() or None),
            )
            db.commit()
            db.close()
        if art == "motiv" and rechteck:
            # Schärfe sofort am markierten Motiv neu messen
            analyse.einzeln_neu(self.foto_ordner, self.konfig, bild_id)
        return self._bildstand(bild_id)

    def anmerkung_loeschen(self, anmerkung_id: int) -> dict:
        with self.sperre:
            db = datenbank.oeffnen(self.foto_ordner)
            zeile = db.execute("SELECT bild_id, art FROM anmerkungen WHERE id=?", (anmerkung_id,)).fetchone()
            db.execute("DELETE FROM anmerkungen WHERE id=?", (anmerkung_id,))
            db.commit()
            db.close()
        if zeile and zeile["art"] == "motiv":
            analyse.einzeln_neu(self.foto_ordner, self.konfig, zeile["bild_id"])
        return self._bildstand(zeile["bild_id"]) if zeile else {}

    def _bildstand(self, bild_id: int) -> dict:
        db = datenbank.oeffnen(self.foto_ordner)
        b = datenbank.zeile_zu_dict(db.execute("SELECT * FROM bilder WHERE id=?", (bild_id,)).fetchone())
        b["anmerkungen"] = self._anmerkungen(db, bild_id)
        db.close()
        m = b.get("metriken") or {}
        return {
            "id": bild_id, "anmerkungen": b["anmerkungen"], "qualitaet": b.get("qualitaet"),
            "kantenbreite": m.get("kantenbreite_100"),
            "gruende": [g for g in einteilung.technische_gruende(b, self.konfig) if not g.startswith("Wandbild")][:4],
        }

    def fertig(self) -> dict:
        with self.sperre:
            self.status = lernen.trainieren(self.foto_ordner, self.konfig)
            self.seit_training = 0
            zaehlung = einteilung.lauf(self.foto_ordner, self.konfig)
            bericht.erstellen(self.foto_ordner)
            bericht.anmerkungen_export(self.foto_ordner)
            return {"status": {**self.status, "offen": self.offen_anzahl()}, "zaehlung": zaehlung}

    def bild(self, bild_id: int, groesse: str) -> bytes | None:
        db = datenbank.oeffnen(self.foto_ordner)
        zeile = db.execute("SELECT quelle FROM bilder WHERE id=?", (bild_id,)).fetchone()
        db.close()
        if not zeile:
            return None
        cache = self.gross / f"{bild_id}.jpg"
        if groesse == "gross" and cache.exists():
            return cache.read_bytes()
        kante = 2000 if groesse == "gross" else 12000
        rgb, _, _ = einlesen.bild_laden(self.foto_ordner / zeile["quelle"], set(self.konfig["dateien"]["raw"]), kante)
        puffer = io.BytesIO()
        Image.fromarray(rgb).save(puffer, "JPEG", quality=90)
        daten = puffer.getvalue()
        if groesse == "gross":
            cache.write_bytes(daten)
        return daten


def erlaubt(handler: BaseHTTPRequestHandler, post: bool) -> bool:
    """Schutz gegen fremde Webseiten: nur Aufrufe an 127.0.0.1/localhost, POST nur mit eigenem Kopfzeilen-Feld."""
    host = (handler.headers.get("Host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return False
    return not post or handler.headers.get("X-Fotosort") == "1"


def _handler(hole_sitzung):
    """hole_sitzung() liefert die Sitzung für den aktuellen Foto-Ordner (in der App wechselt sie)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # Konsole ruhig halten
            pass

        def _senden(self, code: int, inhalt: bytes, typ: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", typ)
            self.send_header("Content-Length", str(len(inhalt)))
            self.send_header("Cache-Control", "no-store" if typ.startswith("application/json") else "max-age=3600")
            self.end_headers()
            self.wfile.write(inhalt)

        def _json(self, obj, code: int = 200) -> None:
            self._senden(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if not erlaubt(self, False):
                return self._json({"fehler": "verboten"}, 403)
            url = urlparse(self.path)
            teile = [t for t in url.path.split("/") if t]
            try:
                sitzung = hole_sitzung()
                daten_ordner = datenbank.daten_ordner(sitzung.foto_ordner)
                if not teile:
                    self._senden(200, SEITE.encode("utf-8"), "text/html; charset=utf-8")
                elif teile == ["api", "stapel"]:
                    ausser = {int(x) for x in parse_qs(url.query).get("ausser", [""])[0].split(",") if x.isdigit()}
                    self._json(sitzung.stapel(ausser))
                elif teile[0] == "bild" and len(teile) == 3 and teile[1].isdigit():
                    inhalt = sitzung.bild(int(teile[1]), teile[2])
                    self._senden(200, inhalt, "image/jpeg") if inhalt else self._json({"fehler": "unbekannt"}, 404)
                elif teile == ["api", "arten"]:
                    self._json(datenbank.ANMERKUNG_ARTEN)
                elif teile == ["bericht"]:
                    self._senden(200, (daten_ordner / "bericht.html").read_bytes(), "text/html; charset=utf-8")
                elif teile[0] == "vorschau" and len(teile) == 2 and teile[1].endswith(".jpg") and "/" not in teile[1]:
                    self._senden(200, (daten_ordner / "vorschau" / Path(teile[1]).name).read_bytes(), "image/jpeg")
                else:
                    self._json({"fehler": "nicht gefunden"}, 404)
            except Exception as e:
                self._json({"fehler": str(e)}, 500)

        def do_POST(self):
            if not erlaubt(self, True):
                return self._json({"fehler": "verboten"}, 403)
            laenge = int(self.headers.get("Content-Length") or 0)
            try:
                daten = json.loads(self.rfile.read(laenge) or b"{}")
                sitzung = hole_sitzung()
                if self.path == "/api/bewerten":
                    sterne = daten.get("sterne")
                    urteil = STERNE_ZU_URTEIL[int(sterne)] if sterne else int(daten["urteil"])
                    self._json(sitzung.bewerten(int(daten["id"]), urteil, sterne))
                elif self.path == "/api/rueckgaengig":
                    self._json(sitzung.rueckgaengig(int(daten["id"])))
                elif self.path == "/api/anmerken":
                    self._json(sitzung.anmerken(int(daten["id"]), daten["art"], daten.get("rechteck"), daten.get("text", "")))
                elif self.path == "/api/anmerkung_loeschen":
                    self._json(sitzung.anmerkung_loeschen(int(daten["id"])))
                elif self.path == "/api/fertig":
                    self._json(sitzung.fertig())
                else:
                    self._json({"fehler": "nicht gefunden"}, 404)
            except Exception as e:
                self._json({"fehler": str(e)}, 500)

    return Handler


def starten(foto_ordner: Path, konfig: dict, port: int = 8765, browser: bool = True) -> None:
    foto_ordner = Path(foto_ordner).resolve()
    if not (datenbank.daten_ordner(foto_ordner) / "fotosort.db").exists():
        raise SystemExit("Bitte zuerst 'analysieren' für diesen Ordner ausführen.")
    sitzung = Sitzung(foto_ordner, konfig)
    server = ThreadingHTTPServer(("127.0.0.1", port), _handler(lambda: sitzung))
    adresse = f"http://127.0.0.1:{port}/"
    print(f"Bewertungs-Oberfläche läuft: {adresse}")
    print("Zum Beenden dieses Fenster schließen oder Strg+C drücken.")
    if browser:
        webbrowser.open(adresse)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Trainiere und teile ein ...")
        sitzung.fertig()
        print("Fertig. Bewertungen und Modell sind gespeichert.")


SEITE = r"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fotos bewerten</title>
<style>
:root{--bg:#121211;--fl:#1d1d1b;--rand:#33322e;--text:#ecebe6;--leise:#a3a29b;--weg:#fc8181;--ja:#68d391;--fav:#ecc94b;--akz:#63b3ed}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--rand);flex-wrap:wrap}
header b{font-size:15px}.leise{color:var(--leise)}
button{font:inherit;color:inherit;background:var(--fl);border:1px solid var(--rand);border-radius:10px;padding:8px 14px;cursor:pointer}
button:hover{border-color:var(--leise)}
#buehne{flex:1;position:relative;display:flex;align-items:center;justify-content:center;min-height:0;padding:12px}
.karte{position:absolute;inset:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;touch-action:none;user-select:none;transition:transform .25s ease,opacity .25s ease}
.karte img{max-width:100%;max-height:calc(100% - 96px);object-fit:contain;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);pointer-events:none;background:#000}
.stempel{position:absolute;top:30px;font-size:32px;font-weight:800;padding:4px 14px;border:4px solid;border-radius:10px;opacity:0;transform:rotate(-12deg)}
.stempel.weg{left:40px;color:var(--weg)}.stempel.ja{right:40px;color:var(--ja);transform:rotate(12deg)}.stempel.fav{left:50%;margin-left:-70px;color:var(--fav);transform:none}
.info{margin-top:10px;text-align:center;max-width:900px}
.info .gruende{color:var(--leise);font-size:13px}
.prognose{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:12px}
footer{display:flex;gap:10px;justify-content:center;align-items:center;padding:10px;border-top:1px solid var(--rand);flex-wrap:wrap}
footer .weg{color:var(--weg)}footer .ja{color:var(--ja)}footer .fav{color:var(--fav)}
.sterne button{padding:8px 10px}
#zoom{position:fixed;inset:0;background:#000;display:none;overflow:auto;z-index:9;cursor:zoom-out}
#zoom img{display:block;max-width:none}
#zoom .hinweis{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.7);padding:4px 12px;border-radius:8px}
#meldung{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--fl);border:1px solid var(--rand);padding:10px 16px;border-radius:10px;display:none;z-index:8;max-width:90vw}
.leer{color:var(--leise);text-align:center}
#editor{position:fixed;inset:0;background:var(--bg);z-index:7;display:none;gap:16px;padding:16px}
#editor.offen{display:flex}
#flaeche{flex:1;display:flex;align-items:center;justify-content:center;min-width:0}
#rahmen{position:relative;cursor:crosshair;touch-action:none;line-height:0}
#rahmen img{max-width:100%;max-height:calc(100vh - 32px);display:block;user-select:none;-webkit-user-drag:none}
.rechteck{position:absolute;border:2px solid var(--akz);background:rgba(99,179,237,.12);pointer-events:none}
.rechteck.alt{border-color:var(--fav);background:rgba(236,201,75,.1)}
.rechteck span{position:absolute;top:-22px;left:-2px;background:var(--fav);color:#000;font-size:11px;line-height:1.6;padding:0 6px;border-radius:4px;white-space:nowrap}
#panel{width:340px;flex-shrink:0;display:flex;flex-direction:column;gap:10px;overflow:auto}
#panel h3{margin:0}
.arten{display:flex;flex-direction:column;gap:6px}
.arten button{text-align:left}.arten button.an{border-color:var(--akz);color:var(--akz);font-weight:600}
textarea{font:inherit;color:inherit;background:var(--fl);border:1px solid var(--rand);border-radius:10px;padding:8px;min-height:90px;resize:vertical}
.liste div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid var(--rand);padding:6px 0;font-size:13px}
.liste button{padding:2px 8px}
@media (max-width:800px){#editor{flex-direction:column}#panel{width:auto}#rahmen img{max-height:50vh}}
</style></head><body>
<header><b>Fotos bewerten</b><span id="status" class="leise">lade …</span><span style="flex:1"></span>
<button id="fertig" title="Modell trainieren und alle Bilder neu einteilen">Fertig &amp; einteilen</button></header>
<div id="buehne"></div>
<footer>
<button class="weg" data-u="0">← Weg</button>
<button class="fav" data-u="2">↑ Favorit</button>
<button class="ja" data-u="1">Behalten →</button>
<span class="sterne"></span>
<button id="zurueck">⌫ Rückgängig</button><button id="zoomknopf">␣ Zoom 100 %</button><button id="markknopf">✎ Markieren (M)</button>
</footer>
<div id="zoom"><span class="hinweis">100-%-Ansicht · Maus bewegen zum Verschieben · Leertaste oder Klick schließt</span><img alt=""></div>
<div id="editor"><div id="flaeche"><div id="rahmen"><img alt=""></div></div>
<div id="panel"><h3>Was hat die Analyse falsch gemacht?</h3>
<div class="leise">1. Art wählen · 2. optional einen Bereich im Bild aufziehen (bei „Motiv“ nötig) · 3. kurz beschreiben</div>
<div class="arten"></div>
<textarea id="anmtext" placeholder="z. B. Die Schärfe sitzt auf den Augen, der unscharfe Hintergrund ist gewollt."></textarea>
<div style="display:flex;gap:8px"><button id="anmspeichern" style="flex:1">Speichern (Strg+Enter)</button><button id="anmschliessen">Fertig (Esc)</button></div>
<div id="anmstand" class="leise"></div>
<div class="liste"></div></div></div>
<div id="meldung"></div>
<script>
const _fetch=window.fetch;window.fetch=(u,o={})=>_fetch(u,{...o,headers:{...(o.headers||{}),"X-Fotosort":"1"}});
const buehne=document.getElementById("buehne"), statusEl=document.getElementById("status");
let stapel=[], verlauf=[], aktuell=null, karte=null, laedt=false;
document.querySelector(".sterne").innerHTML=[1,2,3,4,5].map(n=>`<button data-s="${n}">${n}★</button>`).join("");
const pct=v=>Math.round(v*100)+" %";
function zeigeStatus(s){
  let t=`${s.bewertet} bewertet (${s.behalten} behalten, ${s.weg} weg) · ${s.offen} offen`;
  t+= s.aktiv ? ` · Modell aktiv, Trefferquote ${pct(s.trefferquote)}` : ` · Modell lernt ab 40 Bewertungen`;
  statusEl.textContent=t;
}
function meldung(t,ms=2500){const m=document.getElementById("meldung");m.textContent=t;m.style.display="block";clearTimeout(m._t);m._t=setTimeout(()=>m.style.display="none",ms);}
async function nachladen(){
  if(laedt) return; laedt=true;
  const ausser=[...stapel.map(b=>b.id), ...(aktuell?[aktuell.id]:[])].join(",");
  const r=await fetch("/api/stapel?ausser="+ausser).then(r=>r.json());
  laedt=false; zeigeStatus(r.status);
  for(const b of r.bilder) if(!stapel.some(x=>x.id===b.id)) stapel.push(b);
  stapel.slice(0,3).forEach(b=>{const i=new Image();i.src=`/bild/${b.id}/gross`;});
  if(!aktuell) weiter();
}
function weiter(){
  aktuell=stapel.shift()||null;
  buehne.innerHTML="";
  if(!aktuell){buehne.innerHTML='<div class="leer">Alle Bilder bewertet. Klick auf „Fertig &amp; einteilen“.</div>';return;}
  if(stapel.length<4) nachladen();
  stapel.slice(0,2).forEach(b=>{const i=new Image();i.src=`/bild/${b.id}/gross`;});
  const b=aktuell;
  let prog="";
  if(b.prognose!=null){
    const farbe=b.prognose>=0.5?"var(--ja)":"var(--weg)";
    const grund=b.prognose>=0.5?(b.dafuer||[]).join(", "):(b.dagegen||[]).join(", ");
    prog=`<span class="prognose" style="color:${farbe}">Modell: ${pct(b.prognose)} behalten${grund?" · "+grund:""}</span>`;
  }
  karte=document.createElement("div"); karte.className="karte";
  karte.innerHTML=`<img src="/bild/${b.id}/gross" alt=""><span class="stempel weg">WEG</span><span class="stempel ja">BEHALTEN</span><span class="stempel fav">FAVORIT</span>
   <div class="info"><div><b>${b.name}</b> <span class="leise">${b.datum?b.datum.replace("T"," "):""} · Qualität ${(b.qualitaet||0).toFixed(2)}${b.kantenbreite?` · Kantenbreite ${b.kantenbreite} px`:""}</span>${prog}</div>
   ${b.ki&&b.ki.length?`<div class="gruende" style="color:var(--akz)">${b.ki.join(" · ")}</div>`:""}
   <div class="gruende">${b.gruende.join(" · ")}</div></div>`;
  buehne.appendChild(karte); wischbar(karte);
}
function stempel(art,staerke){karte.querySelectorAll(".stempel").forEach(s=>s.style.opacity=s.classList.contains(art)?staerke:0);}
async function urteilen(urteil,sterne){
  if(!aktuell) return;
  const b=aktuell, dx=urteil===0?-1:urteil===1?1:0;
  stempel(urteil===0?"weg":urteil===1?"ja":"fav",1);
  karte.style.transform=dx?`translateX(${dx*120}vw) rotate(${dx*20}deg)`:"translateY(-120vh)"; karte.style.opacity=0;
  verlauf.push(b);
  setTimeout(weiter,180);
  const r=await fetch("/api/bewerten",{method:"POST",body:JSON.stringify({id:b.id,urteil,sterne})}).then(r=>r.json());
  if(r.status) zeigeStatus(r.status);
  if(r.neu_trainiert){meldung(`Modell neu trainiert · Trefferquote ${pct(r.status.trefferquote)}. Jetzt kommen die Bilder, bei denen es am unsichersten ist.`,4000);stapel=[];nachladen();}
}
async function rueckgaengig(){
  const b=verlauf.pop(); if(!b) return;
  const r=await fetch("/api/rueckgaengig",{method:"POST",body:JSON.stringify({id:b.id})}).then(r=>r.json());
  if(aktuell) stapel.unshift(aktuell);
  aktuell=null; stapel.unshift(b); weiter(); if(r.status) zeigeStatus(r.status);
}
function wischbar(el){
  let start=null;
  el.addEventListener("pointerdown",e=>{start={x:e.clientX,y:e.clientY};el.setPointerCapture(e.pointerId);el.style.transition="none";});
  el.addEventListener("pointermove",e=>{if(!start)return;const dx=e.clientX-start.x,dy=e.clientY-start.y;
    el.style.transform=`translate(${dx}px,${Math.min(dy,0)}px) rotate(${dx/20}deg)`;
    if(dy<-80&&Math.abs(dx)<80) stempel("fav",Math.min(1,-dy/160)); else stempel(dx<0?"weg":"ja",Math.min(1,Math.abs(dx)/160));});
  el.addEventListener("pointerup",e=>{if(!start)return;const dx=e.clientX-start.x,dy=e.clientY-start.y;start=null;el.style.transition="";
    if(dy<-120&&Math.abs(dx)<100) urteilen(2); else if(dx>120) urteilen(1); else if(dx<-120) urteilen(0);
    else if(Math.abs(dx)<5&&Math.abs(dy)<5) zoomen(); else {el.style.transform="";stempel("x",0);}});
}
const zoom=document.getElementById("zoom"), zoomBild=zoom.querySelector("img");
function zoomen(){
  if(zoom.style.display==="block"){zoom.style.display="none";return;}
  if(!aktuell) return;
  zoom.style.display="block"; zoomBild.src=`/bild/${aktuell.id}/voll`;
  zoomBild.onload=()=>{zoom.scrollLeft=(zoomBild.width-innerWidth)/2;zoom.scrollTop=(zoomBild.height-innerHeight)/2;};
}
zoom.addEventListener("mousemove",e=>{zoom.scrollLeft=(zoomBild.width-innerWidth)*e.clientX/innerWidth;zoom.scrollTop=(zoomBild.height-innerHeight)*e.clientY/innerHeight;});
zoom.addEventListener("click",zoomen);
document.addEventListener("keydown",e=>{
  if(e.key===" "){e.preventDefault();zoomen();return;}
  if(zoom.style.display==="block"&&e.key==="Escape"){zoomen();return;}
  if(e.key==="ArrowLeft") urteilen(0);
  else if(e.key==="ArrowRight") urteilen(1);
  else if(e.key==="ArrowUp"){e.preventDefault();urteilen(2);}
  else if(e.key==="Backspace"||e.key==="u"){e.preventDefault();rueckgaengig();}
  else if("12345".includes(e.key)&&e.key.length===1){const s=+e.key;urteilen(s<=2?0:s<=4?1:2,s);}
});
document.querySelector("footer").addEventListener("click",e=>{const t=e.target.closest("button");if(!t)return;
  if(t.dataset.u) urteilen(+t.dataset.u); else if(t.dataset.s){const s=+t.dataset.s;urteilen(s<=2?0:s<=4?1:2,s);}
  else if(t.id==="zurueck") rueckgaengig(); else if(t.id==="zoomknopf") zoomen();});
document.getElementById("fertig").onclick=async()=>{
  meldung("Trainiere und teile ein …",60000);
  const r=await fetch("/api/fertig",{method:"POST"}).then(r=>r.json());
  zeigeStatus(r.status);
  const z=r.zaehlung||{};
  document.getElementById("meldung").innerHTML=`Fertig: Highlight ${z.highlight||0} · Behalten ${z.behalten||0} · Prüfen ${z.pruefen||0} · Aussortiert ${z.aussortiert||0}. <a href="/bericht" target="_blank" style="color:var(--akz)">Bericht öffnen</a>`;
};
// ---------- Markieren & Fehler melden ----------
const editor=document.getElementById("editor"), rahmen=document.getElementById("rahmen"), edBild=rahmen.querySelector("img");
let ARTEN={}, wahlArt=null, rechteck=null, neuDiv=null;
fetch("/api/arten").then(r=>r.json()).then(a=>{ARTEN=a;
  document.querySelector(".arten").innerHTML=Object.entries(a).map(([k,v])=>`<button data-art="${k}">${v}</button>`).join("");});
document.querySelector(".arten").addEventListener("click",e=>{const t=e.target.closest("button");if(!t)return;
  wahlArt=t.dataset.art;document.querySelectorAll(".arten button").forEach(b=>b.classList.toggle("an",b===t));});
function rechteckDiv(r,klasse,text){const d=document.createElement("div");d.className="rechteck "+(klasse||"");
  Object.assign(d.style,{left:r.x*100+"%",top:r.y*100+"%",width:r.b*100+"%",height:r.h*100+"%"});
  if(text){const s=document.createElement("span");s.textContent=text;d.appendChild(s);} rahmen.appendChild(d);return d;}
function zeichneAlte(){rahmen.querySelectorAll(".rechteck").forEach(d=>d.remove());
  for(const a of aktuell.anmerkungen||[]) if(a.b) rechteckDiv({x:a.x,y:a.y,b:a.b,h:a.h},"alt",ARTEN[a.art]||a.art);
  document.querySelector(".liste").innerHTML=(aktuell.anmerkungen||[]).map(a=>`<div><span><b>${ARTEN[a.art]||a.art}</b>${a.text?": "+a.text.replace(/</g,"&lt;"):""}</span><button data-weg="${a.id}">✕</button></div>`).join("");}
function markieren(){
  if(!aktuell) return;
  editor.classList.add("offen"); edBild.src=`/bild/${aktuell.id}/gross`;
  rechteck=null; neuDiv=null; document.getElementById("anmtext").value=""; document.getElementById("anmstand").textContent="";
  zeichneAlte();
}
function editorZu(){editor.classList.remove("offen");}
let ziehStart=null;
const posRel=e=>{const r=edBild.getBoundingClientRect();return{x:Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),y:Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))};};
rahmen.addEventListener("pointerdown",e=>{ziehStart=posRel(e);rahmen.setPointerCapture(e.pointerId);if(neuDiv)neuDiv.remove();neuDiv=rechteckDiv({x:ziehStart.x,y:ziehStart.y,b:0,h:0});});
rahmen.addEventListener("pointermove",e=>{if(!ziehStart)return;const p=posRel(e);
  rechteck={x:Math.min(p.x,ziehStart.x),y:Math.min(p.y,ziehStart.y),b:Math.abs(p.x-ziehStart.x),h:Math.abs(p.y-ziehStart.y)};
  Object.assign(neuDiv.style,{left:rechteck.x*100+"%",top:rechteck.y*100+"%",width:rechteck.b*100+"%",height:rechteck.h*100+"%"});});
rahmen.addEventListener("pointerup",()=>{ziehStart=null;if(rechteck&&(rechteck.b<0.01||rechteck.h<0.01)){rechteck=null;neuDiv&&neuDiv.remove();neuDiv=null;}});
async function anmSpeichern(){
  const stand=document.getElementById("anmstand");
  if(!wahlArt){stand.textContent="Bitte zuerst eine Art wählen.";return;}
  if(wahlArt==="motiv"&&!rechteck){stand.textContent="Für „Motiv“ bitte den Bereich im Bild aufziehen.";return;}
  stand.textContent=wahlArt==="motiv"?"Speichere und messe die Schärfe am Motiv neu …":"Speichere …";
  const r=await fetch("/api/anmerken",{method:"POST",body:JSON.stringify({id:aktuell.id,art:wahlArt,
    rechteck:rechteck?[rechteck.x,rechteck.y,rechteck.b,rechteck.h]:null,text:document.getElementById("anmtext").value})}).then(r=>r.json());
  if(r.fehler){stand.textContent="Fehler: "+r.fehler;return;}
  bildAktualisieren(r);
  rechteck=null;neuDiv=null;document.getElementById("anmtext").value="";
  stand.textContent="Gespeichert."+(r.kantenbreite?` Neue Messung: Qualität ${(r.qualitaet||0).toFixed(2)}, Kantenbreite ${r.kantenbreite} px.`:"");
  zeichneAlte();
}
function bildAktualisieren(r){
  Object.assign(aktuell,{anmerkungen:r.anmerkungen,qualitaet:r.qualitaet,kantenbreite:r.kantenbreite,gruende:r.gruende});
  const info=karte&&karte.querySelector(".info .gruende"); if(info) info.textContent=r.gruende.join(" · ");
}
document.querySelector(".liste").addEventListener("click",async e=>{const t=e.target.closest("button[data-weg]");if(!t)return;
  const r=await fetch("/api/anmerkung_loeschen",{method:"POST",body:JSON.stringify({id:+t.dataset.weg})}).then(r=>r.json());
  bildAktualisieren(r);zeichneAlte();});
document.getElementById("anmspeichern").onclick=anmSpeichern;
document.getElementById("anmschliessen").onclick=editorZu;
document.getElementById("markknopf").onclick=markieren;
document.addEventListener("keydown",e=>{
  if(editor.classList.contains("offen")){
    if(e.key==="Escape") editorZu(); else if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)) anmSpeichern();
    e.stopImmediatePropagation(); return;
  }
  if(e.key==="m"||e.key==="M"){e.stopImmediatePropagation();markieren();}
},true);
nachladen();
</script></body></html>
"""
