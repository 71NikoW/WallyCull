"""Overnight run: star ratings as training data, thorough training, label spreading."""

import json
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from wallycull import config, database, learning, night, signals
from test_workflow import _sharp


@pytest.fixture
def cfg():
    c = config.load()
    c["general"]["workers"] = 2
    c["general"]["low_priority"] = False
    c["ai"]["enabled"] = False
    c["learning"]["min_ratings"] = 8
    c["learning"]["min_per_side"] = 3
    return c


def _save(path, rgb, taken, stars=None):
    exif = Image.Exif()
    exif[0x0132] = taken.strftime("%Y:%m:%d %H:%M:%S")
    if stars is not None:
        exif[0x4746] = stars
    Image.fromarray(rgb).save(path, quality=92, exif=exif)


@pytest.fixture
def rated_photos(tmp_path):
    folder = tmp_path / "photos"
    folder.mkdir()
    t = datetime(2026, 6, 1, 9, 0, 0)
    for i in range(14):
        rgb = _sharp(100 + i, size=(600, 900))
        if i % 2:  # every second photo is soft and gets 1 star ... or a Lightroom reject
            rgb = np.asarray(Image.fromarray(rgb).resize((60, 40)).resize((900, 600), Image.BILINEAR))
        _save(folder / f"IMG_{i:03d}.JPG", rgb, t + timedelta(hours=i), stars=None if i % 2 else 4)
    # Lightroom: rejected in a sidecar file, which wins over the image
    for i in (1, 3, 5, 7):
        (folder / f"IMG_{i:03d}.xmp").write_text('<x:xmpmeta><rdf:Description xmp:Rating="-1"/></x:xmpmeta>')
    return folder


def test_star_mapping():
    assert [signals.stars_to_verdict(s) for s in (None, -1, 0, 1, 2, 3, 4, 5)] == [None, 0, None, None, None, 1, 1, 2]


def test_night_run_learns_from_stars(rated_photos, cfg, monkeypatch, tmp_path):
    monkeypatch.setattr(night, "SUMMARY", tmp_path / "night.json")
    summary = night.run(cfg, [rated_photos])
    folder = summary["folders"][0]
    assert folder["stars"] == {"keep": 7, "favorite": 0, "reject": 4}
    assert folder["model"]["active"] is True
    assert folder["model"]["sources"] == {"stars": 11}
    assert summary["finished"]
    assert json.loads((tmp_path / "night.json").read_text())["folders"][0]["analyzed"] == 14
    model = learning.load_model(rated_photos)
    assert model["thorough"] is True and model["l2"] in learning.L2_CHOICES

    # Your own swipe wins over the stars and is not overwritten by the next harvest
    db = database.open_db(rated_photos)
    image_id = db.execute("SELECT id FROM images WHERE key='img_000'").fetchone()[0]
    db.execute("UPDATE ratings SET verdict=0, origin='swipe' WHERE image_id=?", (image_id,))
    db.commit()
    db.close()
    signals.harvest(rated_photos, cfg)
    db = database.open_db(rated_photos)
    assert tuple(db.execute("SELECT verdict, origin FROM ratings WHERE image_id=?", (image_id,)).fetchone()) == (0, "swipe")
    db.close()


def test_label_spreading_follows_similar_photos():
    rng = np.random.default_rng(3)
    centers = rng.normal(size=(2, 64))
    truth = rng.integers(0, 2, 400)
    emb = centers[truth] + rng.normal(size=(400, 64)) * 0.8
    graph = learning.similarity_graph(emb)
    p = learning.spread(graph, {i: float(truth[i]) for i in range(20)})
    assert ((p[20:] >= 0.5) == (truth[20:] == 1)).mean() > 0.9

    # Reference images alone pull similar photos up
    refs = centers[1] + rng.normal(size=(10, 64)) * 0.3
    graph = learning.similarity_graph(emb, refs)
    p = learning.spread(graph, {})
    assert p[truth == 1].mean() > p[truth == 0].mean() + 0.2


def test_rating_inside_fuji_raf(tmp_path):
    """A Fuji RAF carries the camera JPG at the offset stored in bytes 84-91 of its header."""
    import io
    import struct

    jpg = io.BytesIO()
    exif = Image.Exif()
    exif[0x4746] = 5
    Image.new("RGB", (32, 32)).save(jpg, "JPEG", exif=exif)
    header = b"FUJIFILMCCD-RAW 0201FF383501X-T5".ljust(84, b"\0") + struct.pack(">II", 160, len(jpg.getvalue()))
    raf = tmp_path / "DSCF0001.RAF"
    raf.write_bytes(header.ljust(160, b"\0") + jpg.getvalue() + b"\0" * 64)
    assert signals.read_rating(tmp_path, ["DSCF0001.RAF"], {".xmp"}) == 5


def test_new_folder_uses_ratings_of_other_folders(rated_photos, cfg, monkeypatch, tmp_path):
    """Shared training: a folder without any ratings is sorted by what was learned in another folder."""
    monkeypatch.setattr(night, "SUMMARY", tmp_path / "night.json")
    night.run(cfg, [rated_photos])
    fresh = tmp_path / "next-race-day"
    fresh.mkdir()
    for f in rated_photos.glob("*.JPG"):
        (fresh / f.name).write_bytes(f.read_bytes())
    from wallycull import analyze

    analyze.run(fresh, cfg)
    status = learning.train(fresh, cfg)
    assert status["rated"] == 0 and status["shared"] == 11 and status["active"] is True
    db = database.open_db(fresh)
    personal = {r["key"]: r["personal"] for r in db.execute("SELECT key, personal FROM images")}
    db.close()
    assert all(v is not None for v in personal.values())
    sharp = np.mean([v for k, v in personal.items() if int(k[-3:]) % 2 == 0])
    soft = np.mean([v for k, v in personal.items() if int(k[-3:]) % 2 == 1])
    assert sharp > soft

    cfg["learning"]["shared"] = False
    assert learning.train(fresh, cfg)["active"] is False
