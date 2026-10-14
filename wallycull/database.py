"""SQLite database: measurements, groups, decisions, ratings, annotations and every file move.

It lives next to the photos in <photo folder>/.wallycull/wallycull.db. Paths are stored relative
to the photo folder, so a changed drive letter breaks nothing.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

DATA_DIR_NAME = ".wallycull"
DB_NAME = "wallycull.db"
LEGACY_DIR_NAME = ".fotosort"  # versions up to 0.6 (German)

SCHEMA = """
CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY,
    key         TEXT UNIQUE NOT NULL,   -- folder/file name without extension, lower case
    files       TEXT NOT NULL,          -- JSON list of all files that belong to the image (relative)
    source      TEXT,                   -- the file that was analyzed
    size        INTEGER,
    mtime       REAL,
    version     INTEGER,
    width       INTEGER,
    height      INTEGER,
    taken_at    TEXT,                   -- ISO date from EXIF
    camera      TEXT,
    phash       TEXT,
    dhash       TEXT,
    metrics     TEXT,                   -- JSON with all measurements
    quality     REAL,
    value       REAL,                   -- value score from the AI models
    preview     TEXT,
    error       TEXT,
    group_id    INTEGER,
    group_type  TEXT,                   -- duplicate | burst
    group_rank  INTEGER,                -- 1 = best image of its group
    category    TEXT,                   -- highlight | keep | review | reject
    reasons     TEXT,                   -- JSON list of reasons
    status      TEXT DEFAULT 'active',  -- active | moved | deleted | missing
    user_choice TEXT,                   -- your own decision (rated or restored), always wins
    personal    REAL,                   -- your model's prediction: probability of "keep"
    ai          TEXT,                   -- JSON with AI scores (aesthetic, content, ...)
    embedding   BLOB,                   -- CLIP image features (768 x float16)
    ai_version  INTEGER,
    seen        INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS moves (
    id        INTEGER PRIMARY KEY,
    image_id  INTEGER NOT NULL,
    source    TEXT NOT NULL,
    target    TEXT NOT NULL,
    time      TEXT DEFAULT (datetime('now', 'localtime')),
    run       TEXT,
    state     TEXT DEFAULT 'moved'      -- moved | restored | deleted
);
CREATE TABLE IF NOT EXISTS ratings (
    image_id  INTEGER PRIMARY KEY,      -- one (the latest) rating per image
    verdict   INTEGER NOT NULL,         -- 0 = reject, 1 = keep, 2 = favorite
    stars     INTEGER,                  -- 1 to 5 when rated with stars
    origin    TEXT DEFAULT 'swipe',     -- swipe | restored
    time      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE TABLE IF NOT EXISTS annotations (
    id        INTEGER PRIMARY KEY,
    image_id  INTEGER NOT NULL,
    kind      TEXT NOT NULL,            -- see ANNOTATION_KINDS
    x REAL, y REAL, w REAL, h REAL,     -- marked area, 0..1 relative to the image (optional)
    text      TEXT,
    time      TEXT DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS idx_annotations_image ON annotations(image_id);
CREATE INDEX IF NOT EXISTS idx_images_category ON images(category);
CREATE INDEX IF NOT EXISTS idx_moves_image ON moves(image_id);
"""

ANNOTATION_KINDS = {
    "subject": "This is the actual subject",
    "obstruction": "Something in the way / distracting",
    "sharpness": "Sharpness judged wrong",
    "shake": "Camera shake detected wrongly",
    "exposure": "Exposure judged wrong",
    "burst": "Burst/duplicate detected wrongly",
    "other": "Other",
}


def data_dir(photo_dir: Path) -> Path:
    folder = Path(photo_dir) / DATA_DIR_NAME
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def db_path(photo_dir: Path) -> Path:
    return Path(photo_dir) / DATA_DIR_NAME / DB_NAME


def open_db(photo_dir: Path) -> sqlite3.Connection:
    path = data_dir(photo_dir) / DB_NAME
    is_new = not path.exists()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    if is_new:
        _migrate_legacy(Path(photo_dir), db)
    return db


def _migrate_legacy(photo_dir: Path, db: sqlite3.Connection) -> None:
    """Takes over ratings, annotations and file moves from the old German version (.fotosort).

    Measurements are not copied: every image is analyzed again with the current version."""
    old_path = photo_dir / LEGACY_DIR_NAME / "fotosort.db"
    if not old_path.exists():
        return
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    old.row_factory = sqlite3.Row
    tables = {r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "bilder" not in tables:
        old.close()
        return
    status = {"aktiv": "active", "verschoben": "moved", "geloescht": "deleted", "fehlt": "missing"}
    choice = {"highlight": "highlight", "behalten": "keep", "pruefen": "review", "aussortiert": "reject"}
    columns = {r[1] for r in old.execute("PRAGMA table_info(bilder)")}
    for r in old.execute("SELECT * FROM bilder"):
        db.execute(
            "INSERT OR IGNORE INTO images (id, key, files, source, size, mtime, version, status, user_choice) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (r["id"], r["schluessel"], r["dateien"], r["quelle"], r["groesse"], r["mtime"],
             status.get(r["status"], "active"), choice.get(r["nutzer"]) if "nutzer" in columns else None),
        )
    if "verschiebungen" in tables:
        state = {"verschoben": "moved", "zurueck": "restored", "geloescht": "deleted"}
        for r in old.execute("SELECT * FROM verschiebungen"):
            db.execute(
                "INSERT INTO moves (image_id, source, target, time, run, state) VALUES (?, ?, ?, ?, ?, ?)",
                (r["bild_id"], r["quelle"], r["ziel"], r["zeit"], r["lauf"], state.get(r["zustand"], r["zustand"])),
            )
    if "bewertungen" in tables:
        origin = {"wischen": "swipe", "zurueckgeholt": "restored"}
        for r in old.execute("SELECT * FROM bewertungen"):
            db.execute(
                "INSERT OR REPLACE INTO ratings (image_id, verdict, stars, origin, time) VALUES (?, ?, ?, ?, ?)",
                (r["bild_id"], r["urteil"], r["sterne"], origin.get(r["quelle"], r["quelle"]), r["zeit"]),
            )
    if "anmerkungen" in tables:
        kind = {"motiv": "subject", "stoerung": "obstruction", "schaerfe": "sharpness", "verwacklung": "shake",
                "belichtung": "exposure", "serie": "burst", "sonstiges": "other"}
        for r in old.execute("SELECT * FROM anmerkungen"):
            db.execute(
                "INSERT INTO annotations (image_id, kind, x, y, w, h, text, time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (r["bild_id"], kind.get(r["art"], "other"), r["x"], r["y"], r["b"], r["h"], r["text"], r["zeit"]),
            )
    db.commit()
    old.close()
    print("Took over ratings, notes and moves from the previous version.")


def save_image(db: sqlite3.Connection, e: dict) -> None:
    """Writes the analysis result of one image (new or updated)."""
    db.execute(
        """
        INSERT INTO images (key, files, source, size, mtime, version, width, height, taken_at, camera,
                            phash, dhash, metrics, quality, preview, error, status, seen)
        VALUES (:key, :files, :source, :size, :mtime, :version, :width, :height, :taken_at, :camera,
                :phash, :dhash, :metrics, :quality, :preview, :error, 'active', 1)
        ON CONFLICT(key) DO UPDATE SET
            files=excluded.files, source=excluded.source, size=excluded.size, mtime=excluded.mtime,
            version=excluded.version, width=excluded.width, height=excluded.height,
            taken_at=excluded.taken_at, camera=excluded.camera, phash=excluded.phash, dhash=excluded.dhash,
            metrics=excluded.metrics, quality=excluded.quality, preview=excluded.preview,
            error=excluded.error, status='active', seen=1
        """,
        {
            **e,
            "files": json.dumps(e["files"], ensure_ascii=False),
            "metrics": json.dumps(e.get("metrics") or {}, ensure_ascii=False),
        },
    )


def active_images(db: sqlite3.Connection) -> list[dict]:
    rows = db.execute("SELECT * FROM images WHERE status = 'active' ORDER BY key").fetchall()
    return [row_to_dict(r) for r in rows]


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for field in ("files", "metrics", "reasons", "ai"):
        if d.get(field):
            d[field] = json.loads(d[field])
    return d


def annotations_by_image(db: sqlite3.Connection) -> dict[int, list[dict]]:
    result: dict[int, list[dict]] = {}
    for r in db.execute("SELECT * FROM annotations ORDER BY id"):
        result.setdefault(r["image_id"], []).append(dict(r))
    return result


def subject_area(annotations: list[dict] | None) -> list[float] | None:
    """The subject area you marked last for an image (x, y, w, h relative), or None."""
    for a in reversed(annotations or []):
        if a["kind"] == "subject" and a.get("w") and a.get("h"):
            return [a["x"], a["y"], a["w"], a["h"]]
    return None
