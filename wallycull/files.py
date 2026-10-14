"""Safe moving to "_rejected", restoring, and final deletion (to the Recycle Bin).

Nothing is EVER deleted directly. Every move is recorded in the database and can be undone.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from pathlib import Path

from . import database

CONFIRM_WORD = "DELETE"


def size_text(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return ""


def _free_name(target: Path) -> Path:
    if not target.exists():
        return target
    for i in range(2, 10000):
        candidate = target.with_name(f"{target.stem} ({i}){target.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(target)


def _remove_empty_dirs(start: Path) -> None:
    if not start.exists():
        return
    for folder, _, _ in sorted(os.walk(start), key=lambda t: len(t[0]), reverse=True):
        try:
            Path(folder).rmdir()  # only empty folders can be removed
        except OSError:
            pass


def move_rejected(photo_dir: Path, config: dict, execute: bool = False) -> dict:
    """Moves all images classified as "reject". Without execute=True it is only a dry run."""
    photo_dir = Path(photo_dir).resolve()
    folder_name = config["files"]["rejected_folder"]
    target_base = photo_dir / folder_name
    db = database.open_db(photo_dir)
    images = [
        database.row_to_dict(r)
        for r in db.execute("SELECT * FROM images WHERE status='active' AND category='reject' ORDER BY key")
    ]
    total = sum(b["size"] or 0 for b in images)
    print(f"{len(images)} images ({size_text(total)}) are marked for rejection.")

    if not execute:
        for b in images[:40]:
            reason = (b.get("reasons") or ["?"])[0]
            print(f"  {b['source']}  ->  {folder_name}/  ({reason})")
        if len(images) > 40:
            print(f"  ... and {len(images) - 40} more (all in the report).")
        print("\nDRY RUN: nothing was moved. Run the same command with --execute to move the files.")
        db.close()
        return {"images": len(images), "moved": 0, "errors": 0}

    run = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    moved = errors = 0
    for b in images:
        done: list[tuple[Path, Path]] = []
        try:
            for rel in b["files"]:
                source = photo_dir / rel
                if not source.exists():
                    continue
                target = _free_name(target_base / rel)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(target))
                done.append((source, target))
        except Exception as e:
            errors += 1
            print(f"  Error with {b['source']}: {e} - rolling back.")
            for source, target in reversed(done):
                try:
                    shutil.move(str(target), str(source))
                except Exception:
                    print(f"  WARNING: {target} could not be put back.")
            continue
        for source, target in done:
            db.execute(
                "INSERT INTO moves (image_id, source, target, run) VALUES (?, ?, ?, ?)",
                (b["id"], source.relative_to(photo_dir).as_posix(), target.relative_to(photo_dir).as_posix(), run),
            )
        db.execute("UPDATE images SET status='moved' WHERE id=?", (b["id"],))
        db.commit()
        moved += 1
    db.close()
    print(f"Moved {moved} images to {target_base} (run {run}). Errors: {errors}.")
    print("To undo: use 'restore'.")
    return {"images": len(images), "moved": moved, "errors": errors, "run": run}


def restore(photo_dir: Path, config: dict, file: str | None = None, run: str | None = None,
            image_ids: list[int] | None = None) -> int:
    """Puts images from "_rejected" back where they were (all, one run, matching "file", or by id)."""
    photo_dir = Path(photo_dir).resolve()
    folder_name = config["files"]["rejected_folder"]
    db = database.open_db(photo_dir)
    sql = "SELECT * FROM moves WHERE state='moved'"
    params: list = []
    if run:
        sql += " AND run=?"
        params.append(run)
    rows = db.execute(sql, params).fetchall()
    if file:
        needle = file.replace("\\", "/").lower()
        matching = {r["image_id"] for r in rows if needle in r["source"].lower()}
        rows = [r for r in rows if r["image_id"] in matching]  # RAF + JPG always together
    if image_ids:
        rows = [r for r in rows if r["image_id"] in set(image_ids)]

    restored = set()
    for r in rows:
        source, target = photo_dir / r["source"], photo_dir / r["target"]
        if not target.exists():
            print(f"  No longer in {folder_name}: {r['target']}")
            continue
        if source.exists():
            print(f"  Skipped, a file already exists at the original place: {r['source']}")
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(source))
        db.execute("UPDATE moves SET state='restored' WHERE id=?", (r["id"],))
        db.execute("UPDATE images SET status='active', category='keep', user_choice='keep' WHERE id=?", (r["image_id"],))
        # Restoring is a clear verdict: it becomes a training example
        db.execute("INSERT OR REPLACE INTO ratings (image_id, verdict, origin) VALUES (?, 1, 'restored')", (r["image_id"],))
        restored.add(r["image_id"])
    db.commit()
    db.close()
    _remove_empty_dirs(photo_dir / folder_name)
    print(f"Restored {len(restored)} images.")
    return len(restored)


def purge(photo_dir: Path, config: dict, confirmed: bool = False) -> int:
    """Sends every file in "_rejected" to the Windows Recycle Bin, only after explicit confirmation."""
    from send2trash import send2trash

    photo_dir = Path(photo_dir).resolve()
    db = database.open_db(photo_dir)
    rows = [r for r in db.execute("SELECT * FROM moves WHERE state='moved'") if (photo_dir / r["target"]).exists()]
    if not rows:
        print("Nothing to delete.")
        db.close()
        return 0
    size = sum((photo_dir / r["target"]).stat().st_size for r in rows)
    count = len({r["image_id"] for r in rows})
    print(f"{count} images ({len(rows)} files, {size_text(size)}) will be sent to the Recycle Bin.")
    if not confirmed:
        answer = input(f"Type {CONFIRM_WORD} to confirm (anything else cancels): ").strip().upper()
        if answer != CONFIRM_WORD:
            print("Cancelled, nothing deleted.")
            db.close()
            return 0
    deleted = 0
    for r in rows:
        try:
            send2trash(str(photo_dir / r["target"]))
            db.execute("UPDATE moves SET state='deleted' WHERE id=?", (r["id"],))
            db.execute("UPDATE images SET status='deleted' WHERE id=?", (r["image_id"],))
            deleted += 1
        except Exception as e:
            print(f"  Could not move to the Recycle Bin: {r['target']} ({e})")
    db.commit()
    db.close()
    _remove_empty_dirs(photo_dir / config["files"]["rejected_folder"])
    print(f"{deleted} files are in the Recycle Bin. They can still be recovered from there if needed.")
    return deleted
