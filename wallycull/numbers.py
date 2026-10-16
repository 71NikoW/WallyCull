"""Start numbers: reads the number on the car (local text recognition, no internet) and looks it up in the
entry lists of the series you photograph (DTM, Porsche Carrera Cup Deutschland, ...).

The entry lists are simple CSV files (number,driver,team,car). WallyCull ships some in wallycull/series; your
own files in %LOCALAPPDATA%\\WallyCull\\series are added and win for the same file name, so you can correct
them or add a series. Only numbers that appear in an entry list count, which filters out sponsor text,
lap times and the like.
"""

from __future__ import annotations

import csv
import json
import re
import subprocess
import sys
from pathlib import Path

from tqdm import tqdm

from . import database, loader
from .paths import OCR_PACKAGES_DIR, USER_DIR

NUMBERS_VERSION = 1
BUILTIN_SERIES = Path(__file__).parent / "series"
USER_SERIES = USER_DIR / "series"
EDGE = 2000  # long edge for the text recognition; numbers on a car far away need the detail
DIGITS = re.compile(r"^#?(\d{1,3})$")


def series_files() -> dict[str, Path]:
    files = {p.stem: p for p in sorted(BUILTIN_SERIES.glob("*.csv"))}
    if USER_SERIES.is_dir():
        files.update({p.stem: p for p in sorted(USER_SERIES.glob("*.csv"))})
    return files


def _series_name(stem: str) -> str:
    names = {"dtm": "DTM", "pccd": "Porsche Carrera Cup Deutschland"}
    head, _, year = stem.partition("-")
    return f"{names.get(head, head.upper())} {year}".strip()


def entry_lists(config: dict) -> dict[str, dict]:
    """number -> list of entries (one per series that uses the number)."""
    wanted = config.get("numbers", {}).get("series") or []
    entries: dict[str, list] = {}
    for stem, path in series_files().items():
        if wanted and stem not in wanted:
            continue
        lines = [x for x in path.read_text(encoding="utf-8-sig").splitlines() if x.strip() and not x.startswith("#")]
        for row in csv.DictReader(lines):
            number = (row.get("number") or "").strip().lstrip("#")
            if number.isdigit():
                entries.setdefault(str(int(number)), []).append({
                    "series": _series_name(stem), "driver": (row.get("driver") or "").strip(),
                    "team": (row.get("team") or "").strip(), "car": (row.get("car") or "").strip(),
                })
    return entries


# --- Text recognition (RapidOCR: small, runs on the CPU) -----------------------------

def available() -> bool:
    try:
        import rapidocr_onnxruntime  # noqa: F401

        return True
    except ImportError:
        return False


def install() -> bool:
    """Installs the text recognition (about 60 MB) into %LOCALAPPDATA%\\WallyCull\\ocr-packages."""
    print("STEP Installing start number recognition (once, about 60 MB) ...", flush=True)
    new = OCR_PACKAGES_DIR.with_name("ocr-packages.new")
    command = [sys.executable, "-m", "pip", "install", "--target", str(new), "--upgrade", "--no-warn-script-location",
               "--disable-pip-version-check", "--progress-bar", "off"]
    code = subprocess.run(command + ["--no-deps", "rapidocr-onnxruntime"]).returncode
    code = code or subprocess.run(command + ["onnxruntime", "pyclipper", "shapely", "six"]).returncode
    if code:
        print("Start numbers skipped: the text recognition could not be installed (internet connection?).")
        return False
    import shutil

    shutil.rmtree(OCR_PACKAGES_DIR, ignore_errors=True)
    new.rename(OCR_PACKAGES_DIR)
    if str(OCR_PACKAGES_DIR) not in sys.path:
        sys.path.append(str(OCR_PACKAGES_DIR))
    import importlib

    importlib.invalidate_caches()
    return available()


def _engine():
    from rapidocr_onnxruntime import RapidOCR

    return RapidOCR()


def read(engine, rgb) -> list[dict]:
    """All 1-3 digit numbers in the image, with their height in pixels and the recognition confidence."""
    result, _ = engine(rgb)
    found = []
    for box, text, score in result or []:
        m = DIGITS.match(str(text).strip().replace(" ", ""))
        if not m:
            continue
        ys = [p[1] for p in box]
        found.append({"number": str(int(m[1])), "height": float(max(ys) - min(ys)), "score": float(score)})
    return found


def identify(found: list[dict], entries: dict[str, list], min_score: float = 0.5) -> dict | None:
    """The car's number: the biggest well-read number that is in an entry list."""
    known = [f for f in found if f["number"] in entries and f["score"] >= min_score]
    if not known:
        return None
    best = max(known, key=lambda f: f["height"] * f["score"])
    return {"number": best["number"], "confidence": round(best["score"], 2), "entries": entries[best["number"]]}


def label(n: dict | None) -> str:
    """'#91 Thomas Preining (Manthey Racing, DTM 2026)', or both when two series use the number."""
    if not n or not n.get("number"):
        return ""
    parts = [f"{e['driver']} ({e['team']}, {e['series']})" for e in n["entries"]]
    return f"#{n['number']} " + " / ".join(parts)


def run(photo_dir: Path, config: dict, recompute: bool = False) -> dict:
    """Reads the start numbers of all images that are not rejected (each image only once)."""
    c = config.get("numbers", {})
    if not c.get("enabled", True) or not config.get("ai", {}).get("enabled", True):
        return {"read": 0}
    entries = entry_lists(config)
    if not entries:
        print("Start numbers skipped: no entry list.")
        return {"read": 0}
    if not available() and not install():
        return {"read": 0}
    db = database.open_db(photo_dir)
    rows = db.execute(
        "SELECT id, source, numbers FROM images WHERE status='active' AND error IS NULL AND version > 0 "
        "AND COALESCE(category, '') != 'reject'"
    ).fetchall()
    todo = [r for r in rows if recompute or not r["numbers"]
            or json.loads(r["numbers"]).get("version") != NUMBERS_VERSION]
    if not todo:
        db.close()
        return {"read": 0}
    print(f"Reading start numbers of {len(todo)} images ...", flush=True)
    engine = _engine()
    raw_ext = set(config["files"]["raw"])
    hits = 0
    try:
        for i, r in enumerate(tqdm(todo, desc="Start numbers", unit="img"), 1):
            try:
                rgb, _, _ = loader.load_image(Path(photo_dir) / r["source"], raw_ext, EDGE)
                n = identify(read(engine, rgb), entries, c.get("min_confidence", 0.5))
            except Exception as e:  # one unreadable image must not stop the rest
                n = None
                tqdm.write(f"  {r['source']}: {type(e).__name__}: {e}")
            hits += n is not None
            db.execute("UPDATE images SET numbers=? WHERE id=?",
                       (json.dumps({"version": NUMBERS_VERSION, **(n or {})}, ensure_ascii=False), r["id"]))
            if i % 25 == 0:
                db.commit()
    finally:
        db.commit()
        db.close()
    print(f"Start number recognized on {hits} of {len(todo)} images.")
    return {"read": len(todo), "found": hits}


def export_csv(photo_dir: Path) -> Path:
    """numbers.csv: every photo with number, driver and team, e.g. to send drivers their photos."""
    db = database.open_db(photo_dir)
    rows = db.execute("SELECT source, category, numbers FROM images WHERE status='active' AND numbers IS NOT NULL "
                      "ORDER BY key").fetchall()
    db.close()
    target = database.data_dir(photo_dir) / "numbers.csv"
    with open(target, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["file", "category", "number", "driver", "team", "series"])
        for r in rows:
            n = json.loads(r["numbers"])
            for e in n.get("entries") or []:
                w.writerow([r["source"], r["category"], n["number"], e["driver"], e["team"], e["series"]])
    return target
