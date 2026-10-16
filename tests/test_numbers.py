"""Start numbers: entry lists, picking the car's number, and the real text recognition on a test image."""

from datetime import datetime

import cv2
import numpy as np
import pytest
from PIL import Image

from wallycull import analyze, classify, config, database, numbers, report


def test_entry_lists_are_loaded():
    entries = numbers.entry_lists(config.load())
    assert entries["91"][0]["driver"] and len(entries["91"]) == 2  # DTM and Porsche Carrera Cup use #91
    assert {e["series"] for e in entries["91"]} == {"DTM 2026", "Porsche Carrera Cup Deutschland 2026"}
    cfg = config.load()
    cfg["numbers"]["series"] = ["dtm-2026"]
    assert len(numbers.entry_lists(cfg)["91"]) == 1


def test_the_biggest_known_number_wins():
    entries = {"22": [{"series": "DTM 2026", "driver": "Lucas Auer", "team": "Landgraf", "car": ""}],
               "7": [{"series": "DTM 2026", "driver": "Nicki Thiim", "team": "Comtoyou", "car": ""}]}
    found = [{"number": "7", "height": 20, "score": 0.9},     # small, e.g. on the windscreen of another car
             {"number": "22", "height": 140, "score": 0.8},   # the big door number
             {"number": "250", "height": 300, "score": 0.95}]  # not in any entry list: sponsor text, ignored
    n = numbers.identify(found, entries)
    assert n["number"] == "22" and numbers.label(n) == "#22 Lucas Auer (Landgraf, DTM 2026)"
    assert numbers.identify([{"number": "22", "height": 99, "score": 0.2}], entries) is None
    assert numbers.label({"version": 1}) == ""


def test_start_number_is_read_from_a_photo(tmp_path):
    pytest.importorskip("rapidocr_onnxruntime")
    folder = tmp_path / "race"
    folder.mkdir()
    img = np.full((1600, 2400, 3), (40, 120, 200), np.uint8)
    cv2.rectangle(img, (800, 500), (1400, 1100), (255, 255, 255), -1)
    cv2.putText(img, "91", (870, 960), cv2.FONT_HERSHEY_DUPLEX, 11, (0, 0, 0), 28)
    cv2.putText(img, "MANTHEY 2026", (300, 1400), cv2.FONT_HERSHEY_SIMPLEX, 3, (255, 255, 255), 6)
    exif = Image.Exif()
    exif[0x0132] = datetime(2026, 9, 1, 14, 0).strftime("%Y:%m:%d %H:%M:%S")
    Image.fromarray(img[:, :, ::-1]).save(folder / "DSCF0091.JPG", quality=92, exif=exif)
    cfg = config.load()
    cfg["general"]["workers"] = 1
    cfg["general"]["low_priority"] = False
    analyze.run(folder, cfg)
    classify.run(folder, cfg)
    db = database.open_db(folder)
    db.execute("UPDATE images SET category='keep'")  # a plain test card may count as a reject
    db.commit()
    db.close()
    assert numbers.run(folder, cfg)["found"] == 1
    assert numbers.run(folder, cfg)["read"] == 0  # read only once
    db = database.open_db(folder)
    n = database.row_to_dict(db.execute("SELECT numbers FROM images").fetchone())["numbers"]
    db.close()
    assert n["number"] == "91" and "Preining" in numbers.label(n)
    assert "Preining" in numbers.export_csv(folder).read_text(encoding="utf-8-sig")
    assert "#91" in report.create(folder).read_text(encoding="utf-8")
