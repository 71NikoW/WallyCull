"""Judgments you already made elsewhere, used as training data without any extra work.

- Star ratings set in the camera (Fuji: Play button > rating) or in Lightroom, Capture One, Bridge, ...
  They are read from the EXIF "Rating" tag, the XMP data inside the image, and .xmp sidecar files.
- Lightroom's "rejected" flag (rating -1).

Mapping: rejected -> reject, 3-4 stars -> keep, 5 stars -> favorite. 1-2 stars are ambiguous and ignored.
Your swipes in WallyCull always win over stars.
"""

from __future__ import annotations

import io
import re
import struct
from pathlib import Path

from PIL import Image

from . import database

_EXIF_RATING = 0x4746
_XMP_RATING = re.compile(rb'xmp:Rating(?:="|>)\s*(-?\d)')
HEAD_BYTES = 256 * 1024  # EXIF and XMP sit at the start of the JPEG


def _jpeg_head(path: Path) -> bytes | None:
    """The first bytes of the JPEG: the file itself, or the camera JPG embedded in a Fuji RAF."""
    with open(path, "rb") as f:
        start = f.read(100)
        if start.startswith(b"FUJIFILMCCD-RAW"):
            offset, length = struct.unpack(">II", start[84:92])
            f.seek(offset)
            return f.read(min(length, HEAD_BYTES))
        if start.startswith(b"\xff\xd8"):
            return start + f.read(HEAD_BYTES - 100)
    return None


def _rating_from_bytes(data: bytes) -> int | None:
    m = _XMP_RATING.search(data)
    if m:
        return int(m.group(1))
    try:
        value = Image.open(io.BytesIO(data)).getexif().get(_EXIF_RATING)
        return int(value) if value is not None else None
    except Exception:
        return None


def read_rating(photo_dir: Path, files: list[str], sidecar_ext: set[str]) -> int | None:
    """Star rating of a photo (-1 = rejected, 0 = none, 1-5). Sidecar files win, they are the newest edit."""
    found = None
    for rel in files:
        path = photo_dir / rel
        try:
            if path.suffix.lower() in sidecar_ext:
                m = _XMP_RATING.search(path.read_bytes())
                if m:
                    return int(m.group(1))
            elif found is None:
                head = _jpeg_head(path)
                if head:
                    found = _rating_from_bytes(head)
        except OSError:
            continue
    return found


def stars_to_verdict(stars: int | None) -> int | None:
    if stars is None:
        return None
    if stars < 0:
        return 0
    if stars >= 5:
        return 2
    if stars >= 3:
        return 1
    return None


def harvest(photo_dir: Path, config: dict) -> dict:
    """Reads star ratings of all active photos into the ratings table (origin 'stars')."""
    photo_dir = Path(photo_dir).resolve()
    sidecar = set(config["files"]["sidecar"])
    db = database.open_db(photo_dir)
    rows = db.execute("SELECT id, files FROM images WHERE status='active'").fetchall()
    origins = dict(db.execute("SELECT image_id, origin FROM ratings").fetchall())
    counts = {"keep": 0, "favorite": 0, "reject": 0}
    for r in rows:
        if origins.get(r["id"]) not in (None, "stars"):
            continue  # your own swipe or restore wins
        files = database.row_to_dict(r)["files"]
        stars = read_rating(photo_dir, files, sidecar)
        verdict = stars_to_verdict(stars)
        if verdict is None:
            if origins.get(r["id"]) == "stars":  # stars were removed in the meantime
                db.execute("DELETE FROM ratings WHERE image_id=? AND origin='stars'", (r["id"],))
            continue
        db.execute("INSERT OR REPLACE INTO ratings (image_id, verdict, stars, origin) VALUES (?, ?, ?, 'stars')",
                   (r["id"], verdict, max(stars, 0)))
        counts[("reject", "keep", "favorite")[verdict]] += 1
    db.commit()
    db.close()
    return counts
