"""End-to-end smoke test on synthetic photos: analyze, classify, move, restore, training, legacy migration."""

import sqlite3
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from wallycull import analyze, classify, config, database, files, learning, report


@pytest.fixture
def cfg():
    c = config.load()
    c["general"]["workers"] = 2
    c["general"]["low_priority"] = False
    c["ai"]["enabled"] = False
    return c


def _save(path, rgb, taken: datetime):
    img = Image.fromarray(rgb)
    exif = Image.Exif()
    exif[0x0132] = taken.strftime("%Y:%m:%d %H:%M:%S")
    img.save(path, quality=95, exif=exif)


def _sharp(seed: int, size=(1200, 1800)) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros((*size, 3), np.uint8) + 90
    for _ in range(60):
        y, x = rng.integers(0, size[0] - 200), rng.integers(0, size[1] - 200)
        h, w = rng.integers(20, 200, 2)
        img[y : y + h, x : x + w] = rng.integers(20, 235, 3)
    return img


@pytest.fixture
def photos(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    t = datetime(2026, 5, 1, 12, 0, 0)
    for i in range(6):
        _save(folder / f"DSCF{i:04d}.JPG", _sharp(i), t + timedelta(minutes=10 * i))
    # an exact duplicate, a nearly black failed shot and a blurry version
    _save(folder / "DSCF0100.JPG", _sharp(0), t + timedelta(hours=5))
    _save(folder / "DSCF0101.JPG", np.full((1200, 1800, 3), 2, np.uint8), t + timedelta(hours=6))
    blurry = np.asarray(Image.fromarray(_sharp(3)).resize((90, 60)).resize((1800, 1200), Image.BILINEAR))
    _save(folder / "DSCF0102.JPG", blurry, t + timedelta(hours=7))
    (folder / "DSCF0101.xmp").write_text("<x/>")
    return folder


def _categories(folder):
    db = database.open_db(folder)
    rows = {r["key"]: r["category"] for r in db.execute("SELECT key, category FROM images")}
    db.close()
    return rows


def test_full_workflow(photos, cfg):
    stats = analyze.run(photos, cfg)
    assert stats == {"found": 9, "analyzed": 9, "errors": 0, "missing": 0}
    assert analyze.run(photos, cfg)["analyzed"] == 0  # second run comes from the cache

    counts = classify.run(photos, cfg)
    assert sum(counts.values()) == 9
    cats = _categories(photos)
    assert cats["dscf0101"] == "reject"  # black frame
    assert "reject" in (cats["dscf0000"], cats["dscf0100"])  # one of the duplicates
    assert report.create(photos).exists()

    # Dry run changes nothing
    assert files.move_rejected(photos, cfg)["moved"] == 0
    result = files.move_rejected(photos, cfg, execute=True)
    assert result["moved"] == counts["reject"]
    rejected = photos / "_rejected"
    assert (rejected / "DSCF0101.JPG").exists() and (rejected / "DSCF0101.xmp").exists()
    assert not (photos / "DSCF0101.JPG").exists()

    # Moved images are not picked up again by the next analysis
    assert analyze.run(photos, cfg)["found"] == 9 - counts["reject"]

    assert files.restore(photos, cfg, file="DSCF0101") == 1
    assert (photos / "DSCF0101.JPG").exists() and (photos / "DSCF0101.xmp").exists()
    db = database.open_db(photos)
    row = db.execute("SELECT status, user_choice FROM images WHERE key='dscf0101'").fetchone()
    rating = db.execute("SELECT verdict, origin FROM ratings").fetchone()
    db.close()
    assert tuple(row) == ("active", "keep")
    assert tuple(rating) == (1, "restored")

    # Your decision always wins
    classify.run(photos, cfg)
    assert _categories(photos)["dscf0101"] == "keep"


def test_training_needs_enough_ratings(photos, cfg):
    analyze.run(photos, cfg)
    status = learning.train(photos, cfg)
    assert status["active"] is False and "Not enough ratings" in status["hint"]
    assert len(learning.next_batch(photos, cfg, 5)) == 5


def test_legacy_database_is_migrated(photos, cfg):
    legacy = photos / ".fotosort"
    legacy.mkdir()
    old = sqlite3.connect(legacy / "fotosort.db")
    old.executescript("""
        CREATE TABLE bilder (id INTEGER PRIMARY KEY, schluessel TEXT, dateien TEXT, quelle TEXT, groesse INTEGER,
                             mtime REAL, status TEXT, nutzer TEXT);
        CREATE TABLE bewertungen (bild_id INTEGER PRIMARY KEY, urteil INTEGER, sterne INTEGER, quelle TEXT, zeit TEXT);
        CREATE TABLE anmerkungen (id INTEGER PRIMARY KEY, bild_id INTEGER, art TEXT, x REAL, y REAL, b REAL, h REAL,
                                  text TEXT, zeit TEXT);
        INSERT INTO bilder VALUES (7, 'dscf0002', '["DSCF0002.JPG"]', 'DSCF0002.JPG', 1, 0, 'aktiv', 'highlight');
        INSERT INTO bewertungen VALUES (7, 2, 5, 'wischen', '2026-01-01 10:00:00');
        INSERT INTO anmerkungen VALUES (1, 7, 'motiv', 0.1, 0.2, 0.3, 0.4, 'eyes', '2026-01-01 10:00:00');
    """)
    old.commit()
    old.close()

    analyze.run(photos, cfg)
    db = database.open_db(photos)
    image = db.execute("SELECT id, user_choice, version, metrics FROM images WHERE key='dscf0002'").fetchone()
    note = db.execute("SELECT kind, w, text FROM annotations").fetchone()
    rating = db.execute("SELECT verdict, origin FROM ratings").fetchone()
    db.close()
    assert image["id"] == 7 and image["user_choice"] == "highlight"
    assert image["version"] > 0 and image["metrics"]  # analyzed again with the current version
    assert tuple(note) == ("subject", 0.3, "eyes")
    assert tuple(rating) == (2, "swipe")


def test_ratings_survive_a_damaged_database(tmp_path, cfg):
    """Ratings are backed up on the PC; after the folder database is damaged, the next analysis brings them back."""
    start = datetime(2026, 5, 1, 12)
    for i in range(3):
        _save(tmp_path / f"IMG_{i}.jpg", _sharp(i, (600, 900)), start + timedelta(minutes=i))
    analyze.run(tmp_path, cfg)
    db = database.open_db(tmp_path)
    ids = {r["key"]: r["id"] for r in db.execute("SELECT id, key FROM images")}
    db.execute("INSERT INTO ratings (image_id, verdict) VALUES (?, 0)", (ids["img_0"],))
    db.execute("INSERT INTO annotations (image_id, kind, text) VALUES (?, 'other', 'horizon')", (ids["img_1"],))
    db.execute("UPDATE images SET user_choice='reject' WHERE id=?", (ids["img_0"],))
    db.commit()
    database.backup(db, tmp_path)
    db.close()

    path = database.db_path(tmp_path)
    path.write_bytes(b"USBC" + bytes(8188))  # garbage like a failing card reader writes
    for suffix in ("-wal", "-shm"):
        path.with_name(path.name + suffix).unlink(missing_ok=True)
    analyze.run(tmp_path, cfg)

    db = database.open_db(tmp_path)
    rating = db.execute("SELECT i.key, r.verdict, i.user_choice FROM ratings r JOIN images i ON i.id = r.image_id").fetchall()
    assert [tuple(r) for r in rating] == [("img_0", 0, "reject")]
    assert db.execute("SELECT text FROM annotations").fetchone()[0] == "horizon"
    db.close()
    assert any(".damaged-" in p.name for p in path.parent.iterdir())
