"""Rating screen: rate images Tinder-style, the model learns along.

Runs only on your PC (127.0.0.1), nothing is sent to the internet.
Keys: ← reject · → keep · ↑ favorite · 1-5 stars · Space zoom 100% · Backspace undo
      M mark: drag an area and note what the analysis got wrong
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

from . import ai, analyze, classify, database, learning, loader, report

VERDICT_TO_CATEGORY = {0: "reject", 1: "keep", 2: "highlight"}
STARS_TO_VERDICT = {1: 0, 2: 0, 3: 1, 4: 1, 5: 2}
HEADER = "X-WallyCull"


class Session:
    """Shared server state (one photo folder)."""

    def __init__(self, photo_dir: Path, config: dict):
        self.photo_dir = photo_dir
        self.config = config
        self.lock = threading.Lock()
        self.since_training = 0
        self.large = database.data_dir(photo_dir) / "large"
        self.large.mkdir(exist_ok=True)
        # Always retrain: picks up new AI scores and replaces models from older versions
        self.status = learning.train(photo_dir, config)

    def _counts(self) -> dict:
        db = database.open_db(self.photo_dir)
        r = db.execute("SELECT COUNT(*), SUM(verdict >= 1) FROM ratings").fetchone()
        db.close()
        return {"rated": r[0] or 0, "keep": r[1] or 0, "reject": (r[0] or 0) - (r[1] or 0)}

    def open_count(self) -> int:
        db = database.open_db(self.photo_dir)
        n = db.execute(
            "SELECT COUNT(*) FROM images WHERE status='active' AND metrics IS NOT NULL AND error IS NULL "
            "AND version > 0 AND id NOT IN (SELECT image_id FROM ratings)"
        ).fetchone()[0]
        db.close()
        return n

    def _status(self) -> dict:
        return {**self.status, "open": self.open_count()}

    def batch(self, exclude: set[int]) -> dict:
        model = learning.load_model(self.photo_dir)
        result = []
        for b in learning.next_batch(self.photo_dir, self.config, 12, exclude):
            m = b.get("metrics") or {}
            entry = {
                "id": b["id"], "name": Path(b["source"]).name, "date": b.get("taken_at"),
                "quality": b.get("quality"), "category": b.get("category"),
                "reasons": [r for r in (b.get("reasons") or [])
                            if not r.startswith(("Your model",) + ai.AI_REASON_PREFIXES)][:4],
                "edge_width": m.get("edge_width_100"), "print_cm": m.get("print_cm"),
                "ai": ai.ai_reasons(b), "prediction": None, "annotations": b.get("annotations") or [],
            }
            if model and b.get("personal") is not None:
                pro, con = learning.explain(model, learning.features(b, model.get("pca")))
                entry.update(prediction=b["personal"], pro=pro, con=con)
            result.append(entry)
        return {"images": result, "status": self._status()}

    def rate(self, image_id: int, verdict: int, stars: int | None) -> dict:
        with self.lock:
            db = database.open_db(self.photo_dir)
            db.execute(
                "INSERT OR REPLACE INTO ratings (image_id, verdict, stars, origin) VALUES (?, ?, ?, 'swipe')",
                (image_id, verdict, stars),
            )
            db.execute("UPDATE images SET user_choice=? WHERE id=?", (VERDICT_TO_CATEGORY[verdict], image_id))
            db.commit()
            db.close()
            self.since_training += 1
            retrained = False
            if self.since_training >= self.config["learning"]["retrain_every"]:
                self.status = learning.train(self.photo_dir, self.config)
                self.since_training, retrained = 0, self.status.get("active", False)
            else:
                self.status = {**self.status, **self._counts()}
            return {"status": self._status(), "retrained": retrained}

    def undo(self, image_id: int) -> dict:
        with self.lock:
            db = database.open_db(self.photo_dir)
            db.execute("DELETE FROM ratings WHERE image_id=?", (image_id,))
            db.execute("UPDATE images SET user_choice=NULL WHERE id=?", (image_id,))
            db.commit()
            db.close()
            self.since_training = max(0, self.since_training - 1)
            self.status = {**self.status, **self._counts()}
            return {"status": self._status()}

    def annotate(self, image_id: int, kind: str, rect: list | None, text: str) -> dict:
        if kind not in database.ANNOTATION_KINDS:
            raise ValueError("unknown kind")
        x, y, w, h = (min(max(float(v), 0.0), 1.0) for v in rect) if rect else (None,) * 4
        with self.lock:
            db = database.open_db(self.photo_dir)
            db.execute(
                "INSERT INTO annotations (image_id, kind, x, y, w, h, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (image_id, kind, x, y, w, h, (text or "").strip() or None),
            )
            db.commit()
            db.close()
        if kind == "subject" and rect:
            # Measure the sharpness on the marked subject right away
            analyze.reanalyze_one(self.photo_dir, self.config, image_id)
        return self._image_state(image_id)

    def delete_annotation(self, annotation_id: int) -> dict:
        with self.lock:
            db = database.open_db(self.photo_dir)
            row = db.execute("SELECT image_id, kind FROM annotations WHERE id=?", (annotation_id,)).fetchone()
            db.execute("DELETE FROM annotations WHERE id=?", (annotation_id,))
            db.commit()
            db.close()
        if row and row["kind"] == "subject":
            analyze.reanalyze_one(self.photo_dir, self.config, row["image_id"])
        return self._image_state(row["image_id"]) if row else {}

    def _image_state(self, image_id: int) -> dict:
        db = database.open_db(self.photo_dir)
        b = database.row_to_dict(db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone())
        b["annotations"] = [dict(r) for r in db.execute("SELECT * FROM annotations WHERE image_id=? ORDER BY id", (image_id,))]
        db.close()
        m = b.get("metrics") or {}
        return {
            "id": image_id, "annotations": b["annotations"], "quality": b.get("quality"),
            "edge_width": m.get("edge_width_100"),
            "reasons": [r for r in classify.technical_reasons(b, self.config) if not r.startswith("Wall art")][:4],
        }

    def finish(self) -> dict:
        with self.lock:
            self.status = learning.train(self.photo_dir, self.config)
            self.since_training = 0
            counts = classify.run(self.photo_dir, self.config)
            report.create(self.photo_dir)
            report.export_annotations(self.photo_dir)
            return {"status": self._status(), "counts": counts}

    def image(self, image_id: int, size: str) -> bytes | None:
        db = database.open_db(self.photo_dir)
        row = db.execute("SELECT source FROM images WHERE id=?", (image_id,)).fetchone()
        db.close()
        if not row:
            return None
        cache = self.large / f"{image_id}.jpg"
        if size == "large" and cache.exists():
            return cache.read_bytes()
        edge = 2000 if size == "large" else 12000
        rgb, _, _ = loader.load_image(self.photo_dir / row["source"], set(self.config["files"]["raw"]), edge)
        buffer = io.BytesIO()
        Image.fromarray(rgb).save(buffer, "JPEG", quality=90)
        data = buffer.getvalue()
        if size == "large":
            cache.write_bytes(data)
        return data


def allowed(handler: BaseHTTPRequestHandler, post: bool) -> bool:
    """Protection against other web pages: only requests to 127.0.0.1/localhost, POST only with our own header."""
    host = (handler.headers.get("Host") or "").split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        return False
    return not post or handler.headers.get(HEADER) == "1"


def make_handler(get_session):
    """get_session() returns the session for the current photo folder (it changes inside the app)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the console quiet
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store" if content_type.startswith("application/json") else "max-age=3600")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self):
            if not allowed(self, False):
                return self._json({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                session = get_session()
                data_dir = database.data_dir(session.photo_dir)
                if not parts:
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif parts == ["api", "batch"]:
                    exclude = {int(x) for x in parse_qs(url.query).get("exclude", [""])[0].split(",") if x.isdigit()}
                    self._json(session.batch(exclude))
                elif parts[0] == "image" and len(parts) == 3 and parts[1].isdigit():
                    body = session.image(int(parts[1]), parts[2])
                    self._send(200, body, "image/jpeg") if body else self._json({"error": "unknown"}, 404)
                elif parts == ["api", "kinds"]:
                    self._json(database.ANNOTATION_KINDS)
                elif parts == ["report"]:
                    self._send(200, (data_dir / "report.html").read_bytes(), "text/html; charset=utf-8")
                elif parts[0] == "previews" and len(parts) == 2 and parts[1].endswith(".jpg"):
                    self._send(200, (data_dir / "previews" / Path(parts[1]).name).read_bytes(), "image/jpeg")
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

        def do_POST(self):
            if not allowed(self, True):
                return self._json({"error": "forbidden"}, 403)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
                session = get_session()
                if self.path == "/api/rate":
                    stars = data.get("stars")
                    verdict = STARS_TO_VERDICT[int(stars)] if stars else int(data["verdict"])
                    self._json(session.rate(int(data["id"]), verdict, stars))
                elif self.path == "/api/undo":
                    self._json(session.undo(int(data["id"])))
                elif self.path == "/api/annotate":
                    self._json(session.annotate(int(data["id"]), data["kind"], data.get("rect"), data.get("text", "")))
                elif self.path == "/api/delete_annotation":
                    self._json(session.delete_annotation(int(data["id"])))
                elif self.path == "/api/finish":
                    self._json(session.finish())
                else:
                    self._json({"error": "not found"}, 404)
            except Exception as e:
                self._json({"error": str(e)}, 500)

    return Handler


def serve(photo_dir: Path, config: dict, port: int = 8765, browser: bool = True) -> None:
    """Rating screen on its own, in the browser (command line: wallycull rate <folder>)."""
    photo_dir = Path(photo_dir).resolve()
    if not database.db_path(photo_dir).exists():
        raise SystemExit("Run 'analyze' for this folder first.")
    session = Session(photo_dir, config)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(lambda: session))
    address = f"http://127.0.0.1:{port}/"
    print(f"Rating screen running: {address}")
    print("Close this window or press Ctrl+C to stop.")
    if browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Training and classifying ...")
        session.finish()
        print("Done. Ratings and model are saved.")


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rate photos</title>
<style>
:root{--bg:#121211;--panel:#1d1d1b;--line:#33322e;--text:#ecebe6;--muted:#a3a29b;--no:#fc8181;--yes:#68d391;--fav:#ecc94b;--accent:#63b3ed}
*{box-sizing:border-box}html,body{height:100%}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,Segoe UI,sans-serif;display:flex;flex-direction:column;overflow:hidden}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{font-size:15px}.muted{color:var(--muted)}
button{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer}
button:hover{border-color:var(--muted)}
#stage{flex:1;position:relative;display:flex;align-items:center;justify-content:center;min-height:0;padding:12px}
.card{position:absolute;inset:12px;display:flex;flex-direction:column;align-items:center;justify-content:center;touch-action:none;user-select:none;transition:transform .25s ease,opacity .25s ease}
.card img{max-width:100%;max-height:calc(100% - 96px);object-fit:contain;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);pointer-events:none;background:#000}
.stamp{position:absolute;top:30px;font-size:32px;font-weight:800;padding:4px 14px;border:4px solid;border-radius:10px;opacity:0;transform:rotate(-12deg)}
.stamp.no{left:40px;color:var(--no)}.stamp.yes{right:40px;color:var(--yes);transform:rotate(12deg)}.stamp.fav{left:50%;margin-left:-70px;color:var(--fav);transform:none}
.info{margin-top:10px;text-align:center;max-width:900px}
.info .reasons{color:var(--muted);font-size:13px}
.prediction{display:inline-block;margin-left:8px;padding:1px 8px;border-radius:99px;border:1px solid currentColor;font-size:12px}
footer{display:flex;gap:10px;justify-content:center;align-items:center;padding:10px;border-top:1px solid var(--line);flex-wrap:wrap}
footer .no{color:var(--no)}footer .yes{color:var(--yes)}footer .fav{color:var(--fav)}
.stars button{padding:8px 10px}
#zoom{position:fixed;inset:0;background:#000;display:none;overflow:auto;z-index:9;cursor:zoom-out}
#zoom img{display:block;max-width:none}
#zoom .hint{position:fixed;top:10px;left:50%;transform:translateX(-50%);background:rgba(0,0,0,.7);padding:4px 12px;border-radius:8px}
#toast{position:fixed;bottom:80px;left:50%;transform:translateX(-50%);background:var(--panel);border:1px solid var(--line);padding:10px 16px;border-radius:10px;display:none;z-index:8;max-width:90vw}
.empty{color:var(--muted);text-align:center}
#editor{position:fixed;inset:0;background:var(--bg);z-index:7;display:none;gap:16px;padding:16px}
#editor.open{display:flex}
#canvas{flex:1;display:flex;align-items:center;justify-content:center;min-width:0}
#frame{position:relative;cursor:crosshair;touch-action:none;line-height:0}
#frame img{max-width:100%;max-height:calc(100vh - 32px);display:block;user-select:none;-webkit-user-drag:none}
.rect{position:absolute;border:2px solid var(--accent);background:rgba(99,179,237,.12);pointer-events:none}
.rect.old{border-color:var(--fav);background:rgba(236,201,75,.1)}
.rect span{position:absolute;top:-22px;left:-2px;background:var(--fav);color:#000;font-size:11px;line-height:1.6;padding:0 6px;border-radius:4px;white-space:nowrap}
#side{width:340px;flex-shrink:0;display:flex;flex-direction:column;gap:10px;overflow:auto}
#side h3{margin:0}
.kinds{display:flex;flex-direction:column;gap:6px}
.kinds button{text-align:left}.kinds button.on{border-color:var(--accent);color:var(--accent);font-weight:600}
textarea{font:inherit;color:inherit;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:8px;min-height:90px;resize:vertical}
.list div{display:flex;justify-content:space-between;gap:8px;border-bottom:1px solid var(--line);padding:6px 0;font-size:13px}
.list button{padding:2px 8px}
@media (max-width:800px){#editor{flex-direction:column}#side{width:auto}#frame img{max-height:50vh}}
</style></head><body>
<header><b>Rate photos</b><span id="status" class="muted">loading …</span><span style="flex:1"></span>
<button id="finish" title="Train the model and classify all images again">Done &amp; classify</button></header>
<div id="stage"></div>
<footer>
<button class="no" data-v="0">← Reject</button>
<button class="fav" data-v="2">↑ Favorite</button>
<button class="yes" data-v="1">Keep →</button>
<span class="stars"></span>
<button id="undo">⌫ Undo</button><button id="zoombtn">␣ Zoom 100%</button><button id="markbtn">✎ Mark (M)</button>
</footer>
<div id="zoom"><span class="hint">100% view · move the mouse to pan · Space or click closes</span><img alt=""></div>
<div id="editor"><div id="canvas"><div id="frame"><img alt=""></div></div>
<div id="side"><h3>What did the analysis get wrong?</h3>
<div class="muted">1. Pick a kind · 2. optionally drag an area in the image (required for "subject") · 3. describe it briefly</div>
<div class="kinds"></div>
<textarea id="notetext" placeholder="e.g. Focus is on the eyes, the blurry background is intended."></textarea>
<div style="display:flex;gap:8px"><button id="notesave" style="flex:1">Save (Ctrl+Enter)</button><button id="noteclose">Done (Esc)</button></div>
<div id="notestate" class="muted"></div>
<div class="list"></div></div></div>
<div id="toast"></div>
<script>
const _fetch=window.fetch;window.fetch=(u,o={})=>_fetch(u,{...o,headers:{...(o.headers||{}),"X-WallyCull":"1"}});
const stage=document.getElementById("stage"), statusEl=document.getElementById("status");
let queue=[], history=[], current=null, card=null, loading=false;
document.querySelector(".stars").innerHTML=[1,2,3,4,5].map(n=>`<button data-s="${n}">${n}★</button>`).join("");
const pct=v=>Math.round(v*100)+"%";
const esc=t=>String(t).replace(/&/g,"&amp;").replace(/</g,"&lt;");
function showStatus(s){
  let t=`${s.rated} rated (${s.keep} keep, ${s.reject} reject) · ${s.open} left`;
  t+= s.active ? ` · model active, accuracy ${pct(s.accuracy)}` : ` · model starts learning at 40 ratings`;
  statusEl.textContent=t;
}
function toast(t,ms=2500){const m=document.getElementById("toast");m.textContent=t;m.style.display="block";clearTimeout(m._t);m._t=setTimeout(()=>m.style.display="none",ms);}
async function refill(){
  if(loading) return; loading=true;
  const exclude=[...queue.map(b=>b.id), ...(current?[current.id]:[])].join(",");
  const r=await fetch("/api/batch?exclude="+exclude).then(r=>r.json());
  loading=false; showStatus(r.status);
  for(const b of r.images) if(!queue.some(x=>x.id===b.id)) queue.push(b);
  queue.slice(0,3).forEach(b=>{const i=new Image();i.src=`/image/${b.id}/large`;});
  if(!current) next();
}
function next(){
  current=queue.shift()||null;
  stage.innerHTML="";
  if(!current){stage.innerHTML='<div class="empty">All images rated. Click "Done &amp; classify".</div>';return;}
  if(queue.length<4) refill();
  queue.slice(0,2).forEach(b=>{const i=new Image();i.src=`/image/${b.id}/large`;});
  const b=current;
  let pred="";
  if(b.prediction!=null){
    const color=b.prediction>=0.5?"var(--yes)":"var(--no)";
    const why=b.prediction>=0.5?(b.pro||[]).join(", "):(b.con||[]).join(", ");
    pred=`<span class="prediction" style="color:${color}">Model: ${pct(b.prediction)} keep${why?" · "+why:""}</span>`;
  }
  card=document.createElement("div"); card.className="card";
  card.innerHTML=`<img src="/image/${b.id}/large" alt=""><span class="stamp no">REJECT</span><span class="stamp yes">KEEP</span><span class="stamp fav">FAVORITE</span>
   <div class="info"><div><b>${esc(b.name)}</b> <span class="muted">${b.date?b.date.replace("T"," "):""} · quality ${(b.quality||0).toFixed(2)}${b.edge_width?` · edge width ${b.edge_width} px`:""}</span>${pred}</div>
   ${b.ai&&b.ai.length?`<div class="reasons" style="color:var(--accent)">${b.ai.map(esc).join(" · ")}</div>`:""}
   <div class="reasons">${b.reasons.map(esc).join(" · ")}</div></div>`;
  stage.appendChild(card); swipeable(card);
}
function stamp(kind,strength){card.querySelectorAll(".stamp").forEach(s=>s.style.opacity=s.classList.contains(kind)?strength:0);}
async function rate(verdict,stars){
  if(!current) return;
  const b=current, dx=verdict===0?-1:verdict===1?1:0;
  stamp(verdict===0?"no":verdict===1?"yes":"fav",1);
  card.style.transform=dx?`translateX(${dx*120}vw) rotate(${dx*20}deg)`:"translateY(-120vh)"; card.style.opacity=0;
  history.push(b);
  setTimeout(next,180);
  const r=await fetch("/api/rate",{method:"POST",body:JSON.stringify({id:b.id,verdict,stars})}).then(r=>r.json());
  if(r.status) showStatus(r.status);
  if(r.retrained){toast(`Model retrained · accuracy ${pct(r.status.accuracy)}. Next up: the images it is least sure about.`,4000);queue=[];refill();}
}
async function undo(){
  const b=history.pop(); if(!b) return;
  const r=await fetch("/api/undo",{method:"POST",body:JSON.stringify({id:b.id})}).then(r=>r.json());
  if(current) queue.unshift(current);
  current=null; queue.unshift(b); next(); if(r.status) showStatus(r.status);
}
function swipeable(el){
  let start=null;
  el.addEventListener("pointerdown",e=>{start={x:e.clientX,y:e.clientY};el.setPointerCapture(e.pointerId);el.style.transition="none";});
  el.addEventListener("pointermove",e=>{if(!start)return;const dx=e.clientX-start.x,dy=e.clientY-start.y;
    el.style.transform=`translate(${dx}px,${Math.min(dy,0)}px) rotate(${dx/20}deg)`;
    if(dy<-80&&Math.abs(dx)<80) stamp("fav",Math.min(1,-dy/160)); else stamp(dx<0?"no":"yes",Math.min(1,Math.abs(dx)/160));});
  el.addEventListener("pointerup",e=>{if(!start)return;const dx=e.clientX-start.x,dy=e.clientY-start.y;start=null;el.style.transition="";
    if(dy<-120&&Math.abs(dx)<100) rate(2); else if(dx>120) rate(1); else if(dx<-120) rate(0);
    else if(Math.abs(dx)<5&&Math.abs(dy)<5) zoomToggle(); else {el.style.transform="";stamp("x",0);}});
}
const zoom=document.getElementById("zoom"), zoomImg=zoom.querySelector("img");
function zoomToggle(){
  if(zoom.style.display==="block"){zoom.style.display="none";return;}
  if(!current) return;
  zoom.style.display="block"; zoomImg.src=`/image/${current.id}/full`;
  zoomImg.onload=()=>{zoom.scrollLeft=(zoomImg.width-innerWidth)/2;zoom.scrollTop=(zoomImg.height-innerHeight)/2;};
}
zoom.addEventListener("mousemove",e=>{zoom.scrollLeft=(zoomImg.width-innerWidth)*e.clientX/innerWidth;zoom.scrollTop=(zoomImg.height-innerHeight)*e.clientY/innerHeight;});
zoom.addEventListener("click",zoomToggle);
document.addEventListener("keydown",e=>{
  if(e.key===" "){e.preventDefault();zoomToggle();return;}
  if(zoom.style.display==="block"&&e.key==="Escape"){zoomToggle();return;}
  if(e.key==="ArrowLeft") rate(0);
  else if(e.key==="ArrowRight") rate(1);
  else if(e.key==="ArrowUp"){e.preventDefault();rate(2);}
  else if(e.key==="Backspace"||e.key==="u"){e.preventDefault();undo();}
  else if("12345".includes(e.key)&&e.key.length===1){const s=+e.key;rate(s<=2?0:s<=4?1:2,s);}
});
document.querySelector("footer").addEventListener("click",e=>{const t=e.target.closest("button");if(!t)return;
  if(t.dataset.v) rate(+t.dataset.v); else if(t.dataset.s){const s=+t.dataset.s;rate(s<=2?0:s<=4?1:2,s);}
  else if(t.id==="undo") undo(); else if(t.id==="zoombtn") zoomToggle();});
document.getElementById("finish").onclick=async()=>{
  toast("Training and classifying …",60000);
  const r=await fetch("/api/finish",{method:"POST"}).then(r=>r.json());
  showStatus(r.status);
  const c=r.counts||{};
  document.getElementById("toast").innerHTML=`Done: Highlight ${c.highlight||0} · Keep ${c.keep||0} · Review ${c.review||0} · Reject ${c.reject||0}. <a href="/report" target="_blank" style="color:var(--accent)">Open report</a>`;
};
// ---------- Mark & report mistakes ----------
const editor=document.getElementById("editor"), frame=document.getElementById("frame"), edImg=frame.querySelector("img");
let KINDS={}, chosenKind=null, rect=null, newDiv=null;
fetch("/api/kinds").then(r=>r.json()).then(a=>{KINDS=a;
  document.querySelector(".kinds").innerHTML=Object.entries(a).map(([k,v])=>`<button data-kind="${k}">${v}</button>`).join("");});
document.querySelector(".kinds").addEventListener("click",e=>{const t=e.target.closest("button");if(!t)return;
  chosenKind=t.dataset.kind;document.querySelectorAll(".kinds button").forEach(b=>b.classList.toggle("on",b===t));});
function rectDiv(r,cls,text){const d=document.createElement("div");d.className="rect "+(cls||"");
  Object.assign(d.style,{left:r.x*100+"%",top:r.y*100+"%",width:r.w*100+"%",height:r.h*100+"%"});
  if(text){const s=document.createElement("span");s.textContent=text;d.appendChild(s);} frame.appendChild(d);return d;}
function drawOld(){frame.querySelectorAll(".rect").forEach(d=>d.remove());
  for(const a of current.annotations||[]) if(a.w) rectDiv({x:a.x,y:a.y,w:a.w,h:a.h},"old",KINDS[a.kind]||a.kind);
  document.querySelector(".list").innerHTML=(current.annotations||[]).map(a=>`<div><span><b>${KINDS[a.kind]||a.kind}</b>${a.text?": "+esc(a.text):""}</span><button data-del="${a.id}">✕</button></div>`).join("");}
function mark(){
  if(!current) return;
  editor.classList.add("open"); edImg.src=`/image/${current.id}/large`;
  rect=null; newDiv=null; document.getElementById("notetext").value=""; document.getElementById("notestate").textContent="";
  drawOld();
}
function closeEditor(){editor.classList.remove("open");}
let dragStart=null;
const relPos=e=>{const r=edImg.getBoundingClientRect();return{x:Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),y:Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))};};
frame.addEventListener("pointerdown",e=>{dragStart=relPos(e);frame.setPointerCapture(e.pointerId);if(newDiv)newDiv.remove();newDiv=rectDiv({x:dragStart.x,y:dragStart.y,w:0,h:0});});
frame.addEventListener("pointermove",e=>{if(!dragStart)return;const p=relPos(e);
  rect={x:Math.min(p.x,dragStart.x),y:Math.min(p.y,dragStart.y),w:Math.abs(p.x-dragStart.x),h:Math.abs(p.y-dragStart.y)};
  Object.assign(newDiv.style,{left:rect.x*100+"%",top:rect.y*100+"%",width:rect.w*100+"%",height:rect.h*100+"%"});});
frame.addEventListener("pointerup",()=>{dragStart=null;if(rect&&(rect.w<0.01||rect.h<0.01)){rect=null;newDiv&&newDiv.remove();newDiv=null;}});
async function saveNote(){
  const state=document.getElementById("notestate");
  if(!chosenKind){state.textContent="Pick a kind first.";return;}
  if(chosenKind==="subject"&&!rect){state.textContent="For \"subject\", drag the area in the image.";return;}
  state.textContent=chosenKind==="subject"?"Saving and measuring sharpness on the subject again …":"Saving …";
  const r=await fetch("/api/annotate",{method:"POST",body:JSON.stringify({id:current.id,kind:chosenKind,
    rect:rect?[rect.x,rect.y,rect.w,rect.h]:null,text:document.getElementById("notetext").value})}).then(r=>r.json());
  if(r.error){state.textContent="Error: "+r.error;return;}
  updateImage(r);
  rect=null;newDiv=null;document.getElementById("notetext").value="";
  state.textContent="Saved."+(r.edge_width?` New measurement: quality ${(r.quality||0).toFixed(2)}, edge width ${r.edge_width} px.`:"");
  drawOld();
}
function updateImage(r){
  Object.assign(current,{annotations:r.annotations,quality:r.quality,edge_width:r.edge_width,reasons:r.reasons});
  const info=card&&card.querySelector(".info .reasons:last-child"); if(info) info.textContent=r.reasons.join(" · ");
}
document.querySelector(".list").addEventListener("click",async e=>{const t=e.target.closest("button[data-del]");if(!t)return;
  const r=await fetch("/api/delete_annotation",{method:"POST",body:JSON.stringify({id:+t.dataset.del})}).then(r=>r.json());
  updateImage(r);drawOld();});
document.getElementById("notesave").onclick=saveNote;
document.getElementById("noteclose").onclick=closeEditor;
document.getElementById("markbtn").onclick=mark;
document.addEventListener("keydown",e=>{
  if(editor.classList.contains("open")){
    if(e.key==="Escape") closeEditor(); else if(e.key==="Enter"&&(e.ctrlKey||e.metaKey)) saveNote();
    e.stopImmediatePropagation(); return;
  }
  if(e.key==="m"||e.key==="M"){e.stopImmediatePropagation();mark();}
},true);
refill();
</script></body></html>
"""
