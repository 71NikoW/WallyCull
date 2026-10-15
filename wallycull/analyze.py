"""Runs the analysis: find files, check the cache, measure images in parallel in the background."""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import imagehash
from PIL import Image
from tqdm import tqdm

from . import ANALYSIS_VERSION, database, loader, quality

_config: dict = {}


def low_priority() -> None:
    """Lowers this process' priority so the PC stays responsive."""
    try:
        import psutil

        process = psutil.Process()
        if os.name == "nt":
            process.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            process.nice(10)
    except Exception:
        pass


def _worker_init(config: dict) -> None:
    global _config
    _config = config
    import cv2

    cv2.setNumThreads(1)  # one core per process, otherwise they slow each other down
    if config["general"]["low_priority"]:
        low_priority()


def preview_name(key: str) -> str:
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".jpg"


def analyze_image(task: dict) -> dict:
    """Runs in a worker process. Errors on single files are reported, not raised."""
    c = _config
    photo_dir = Path(task["photo_dir"])
    result = {
        "key": task["key"], "files": task["files"], "source": task["source"], "size": task["size"],
        "mtime": task["mtime"], "version": ANALYSIS_VERSION,
        "width": None, "height": None, "taken_at": None, "camera": None,
        "phash": None, "dhash": None, "metrics": {}, "quality": None, "preview": None, "error": None,
    }
    raw_ext = set(c["files"]["raw"])
    try:
        rgb, info, full_gray = loader.load_image(
            photo_dir / task["source"], raw_ext, c["general"]["analysis_size"],
            full=c["general"].get("full_resolution", True),
            reference=c["general"].get("sharpness_reference_size", 4000),
            develop=c["general"].get("develop_raw", False),
        )
        # Camera JPG without EXIF? Then the RAW file supplies it.
        if not info.get("taken_at") and task.get("raw") and task["raw"] != task["source"]:
            try:
                _, raw_info, _ = loader.load_image(photo_dir / task["raw"], raw_ext, 256)
                info = {**raw_info, **{k: v for k, v in info.items() if v}}
            except Exception:
                pass
        metrics = quality.analyze(rgb, info, c, full_gray, task.get("subject"))
        del full_gray
        metrics["exif"] = {k: info[k] for k in ("iso", "exposure_time", "aperture", "focal_length") if k in info}
        pil = Image.fromarray(rgb)
        preview = pil.copy()
        edge = c["general"]["preview_size"]
        preview.thumbnail((edge, edge), Image.LANCZOS)
        name = preview_name(task["key"])
        preview.save(photo_dir / database.DATA_DIR_NAME / "previews" / name, quality=82)
        result.update(
            width=info["width"], height=info["height"], taken_at=info.get("taken_at"), camera=info.get("camera"),
            # Both hashes shrink to a few pixels anyway: computing them from the preview is much faster
            phash=str(imagehash.phash(preview)), dhash=str(imagehash.dhash(preview)),
            metrics=metrics, quality=metrics["quality"], preview=name,
        )
    except Exception as error:  # broken file: note it and carry on
        result["error"] = f"{type(error).__name__}: {error}"
        result["metrics"] = {"traceback": traceback.format_exc(limit=3)}
    return result


def worker_count(config: dict) -> int:
    wanted = int(config["general"].get("workers") or 0)
    return wanted if wanted > 0 else max(1, (os.cpu_count() or 2) - 2)


def run(photo_dir: Path, config: dict, recompute: bool = False) -> dict:
    """Analyzes all new or changed images. Images measured before come from the cache."""
    photo_dir = Path(photo_dir).resolve()
    (database.data_dir(photo_dir) / "previews").mkdir(exist_ok=True)
    db = database.open_db(photo_dir)
    if config["general"]["low_priority"]:
        low_priority()

    print(f"Scanning {photo_dir} ...")
    found = loader.find_images(photo_dir, config)
    known = {r["key"]: r for r in db.execute("SELECT key, size, mtime, version, status FROM images").fetchall()}
    annotations = database.annotations_by_image(db)
    subjects = {r["key"]: database.subject_area(annotations.get(r["id"])) for r in db.execute("SELECT id, key FROM images")}
    db.execute("UPDATE images SET seen = 0 WHERE status = 'active'")
    tasks = []
    for g in found:
        old = known.get(g["key"])
        current = (
            old is not None and old["status"] == "active" and old["size"] == g["size"]
            and abs((old["mtime"] or 0) - g["mtime"]) < 1 and old["version"] == ANALYSIS_VERSION
        )
        if current and not recompute:
            db.execute("UPDATE images SET seen = 1 WHERE key = ?", (g["key"],))
        else:
            tasks.append({**g, "photo_dir": str(photo_dir), "subject": subjects.get(g["key"])})
    # Images that are gone (deleted or moved by you) no longer count
    gone = db.execute("UPDATE images SET status = 'missing' WHERE status = 'active' AND seen = 0").rowcount
    db.commit()

    print(f"{len(found)} images found, {len(found) - len(tasks)} from the cache, {len(tasks)} to analyze.")
    errors = 0
    if tasks:
        # "spawn" as on Windows: safe while the AI models load in a background thread
        with ProcessPoolExecutor(max_workers=worker_count(config), mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_worker_init, initargs=(config,)) as pool:
            futures = [pool.submit(analyze_image, t) for t in tasks]
            try:
                for i, f in enumerate(tqdm(as_completed(futures), total=len(futures), desc="Analyzing", unit="img"), 1):
                    e = f.result()
                    if e["error"]:
                        errors += 1
                        tqdm.write(f"  Error in {e['source']}: {e['error']}")
                    database.save_image(db, e)
                    if i % 25 == 0:
                        db.commit()  # keep progress: cancelling is possible at any time
            except KeyboardInterrupt:
                print("\nCancelled. Progress so far is saved; the next run continues from here.")
                for f in futures:
                    f.cancel()
                db.commit()
                raise
        db.commit()
    db.close()
    return {"found": len(found), "analyzed": len(tasks), "errors": errors, "missing": gone}


def reanalyze_one(photo_dir: Path, config: dict, image_id: int) -> dict:
    """Measures a single image again right away (e.g. after you marked its subject)."""
    global _config
    _config = config
    photo_dir = Path(photo_dir).resolve()
    db = database.open_db(photo_dir)
    r = database.row_to_dict(db.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone())
    subject = database.subject_area(database.annotations_by_image(db).get(image_id))
    raw = next((f for f in r["files"] if Path(f).suffix.lower() in config["files"]["raw"]), None)
    e = analyze_image({
        "key": r["key"], "files": r["files"], "source": r["source"], "raw": raw,
        "size": r["size"], "mtime": r["mtime"], "photo_dir": str(photo_dir), "subject": subject,
    })
    if not e["error"]:
        database.save_image(db, e)
        db.commit()
    db.close()
    return e
