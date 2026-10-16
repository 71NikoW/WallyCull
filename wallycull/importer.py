"""Import from a memory card: copies everything to the PC and checks every file against the card.

The card is only ever read, never written. Card readers can deliver wrong bytes without any error message,
so every copy is checked: after copying, the card and the copy are read again (bypassing the Windows file
cache, so the bytes really come from the card and the disk) and compared with what was copied. A file that
does not match is copied again. Files that are already there with the same size are only checked, so an
interrupted import simply continues.
"""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from pathlib import Path

from tqdm import tqdm

from .database import DATA_DIR_NAME, LEGACY_DIR_NAME

CHUNK = 4 * 1024 * 1024
ATTEMPTS = 3


def default_target(source: Path, config: dict) -> Path:
    """%USERPROFILE%\\Pictures\\WallyCull\\<date of the first photo>_<card folder>"""
    base = config["files"].get("import_folder") or Path.home() / "Pictures" / "WallyCull"
    first = None
    for path in _files(source):
        try:
            t = path.stat().st_mtime
        except OSError:
            continue
        first = t if first is None else min(first, t)
    day = datetime.fromtimestamp(first).strftime("%Y-%m-%d") if first else datetime.now().strftime("%Y-%m-%d")
    return Path(base) / f"{day}_{Path(source).name}"


def _files(source: Path):
    skip = {DATA_DIR_NAME, LEGACY_DIR_NAME}
    for folder, subfolders, names in os.walk(source):
        subfolders[:] = sorted(s for s in subfolders if s not in skip and not s.startswith("."))
        for name in sorted(names):
            if not name.startswith("."):
                yield Path(folder) / name


def _hash_buffered(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        try:  # Linux/macOS: drop cached pages so the bytes are read from the device again
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        except (AttributeError, OSError):
            pass
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _hash_unbuffered_windows(path: Path) -> str:
    """Reads with FILE_FLAG_NO_BUFFERING: the bytes come from the card or disk, not from the RAM cache."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
                             ctypes.c_void_p]
    k32.ReadFile.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = k32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0x20000000 | 0x08000000, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), f"cannot open {path}")
    try:
        buffer = ctypes.create_string_buffer(CHUNK + 65536)
        address = ctypes.addressof(buffer)
        address += (-address) % 65536  # unbuffered reads need a sector-aligned buffer
        h = hashlib.sha256()
        read = wintypes.DWORD()
        while True:
            if not k32.ReadFile(handle, address, CHUNK, ctypes.byref(read), None):
                raise OSError(ctypes.get_last_error(), f"read error in {path}")
            if read.value:
                h.update(ctypes.string_at(address, read.value))
            if read.value < CHUNK:
                return h.hexdigest()
    finally:
        k32.CloseHandle(handle)


def checksum(path: Path) -> str:
    """SHA-256 of the file as it is stored on the device."""
    if os.name == "nt":
        try:
            return _hash_unbuffered_windows(path)
        except (OSError, AttributeError, ValueError):
            pass
    return _hash_buffered(path)


def _copy(source: Path, target: Path) -> str:
    """Copies via a .part file, returns the SHA-256 of the bytes written. The source is opened read-only."""
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    with open(source, "rb") as src, open(part, "wb") as dst:
        while chunk := src.read(CHUNK):
            h.update(chunk)
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
    st = source.stat()
    os.utime(part, (st.st_atime, st.st_mtime))  # keep the camera's date
    part.replace(target)
    return h.hexdigest()


def run(source: Path, target: Path | None, config: dict) -> dict:
    source = Path(source).resolve()
    target = Path(target).resolve() if target else default_target(source, config)
    if target == source or source in target.parents:
        raise ValueError("The target folder must not be on the memory card.")
    files = list(_files(source))
    total = sum(p.stat().st_size for p in files)
    print(f"Importing {len(files)} files ({total / 1e9:.1f} GB) from {source}")
    print(f"to {target}")
    print("The memory card is only read, never written.", flush=True)
    target.mkdir(parents=True, exist_ok=True)
    result = {"files": len(files), "copied": 0, "present": 0, "verified": 0, "repaired": 0, "failed": [],
              "conflicts": [], "target": str(target)}

    for path in tqdm(files, desc="Copying", unit="file"):
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if dest.exists():
                if dest.stat().st_size != path.stat().st_size:
                    result["conflicts"].append(str(path.relative_to(source)))
                    tqdm.write(f"  Skipped: {dest.name} already exists in the target with a different size")
                else:
                    result["present"] += 1
                continue
            _copy(path, dest)
            result["copied"] += 1
        except OSError as e:
            result["failed"].append(str(path.relative_to(source)))
            tqdm.write(f"  Cannot copy {path.name}: {e}")

    # Check: card and copy are read again and must be identical, bit for bit
    conflicts, failed = set(result["conflicts"]), set(result["failed"])
    to_check = [p for p in files if str(p.relative_to(source)) not in conflicts | failed]
    for path in tqdm(to_check, desc="Checking", unit="file"):
        rel = str(path.relative_to(source))
        dest = target / rel
        ok = False
        for attempt in range(ATTEMPTS):
            try:
                card = checksum(path)
                if checksum(dest) == card:
                    ok = True
                    break
                # Differs: copy again and keep the version that a second read of the card confirms
                written = _copy(path, dest)
                if written == checksum(path) == checksum(dest):
                    ok = True
                    result["repaired"] += 1
                    tqdm.write(f"  Repaired: {rel} was copied wrongly (card reader) and has been copied again")
                    break
            except OSError as e:
                tqdm.write(f"  Read error in {rel} (attempt {attempt + 1}): {e}")
        if ok:
            result["verified"] += 1
        else:
            result["failed"].append(rel)
            tqdm.write(f"  NOT OK: {rel} could not be copied correctly. Keep the card until this is solved.")

    print(f"{result['copied']} copied, {result['present']} already there, {result['verified']} of {len(files)} "
          f"checked against the card and identical, {result['repaired']} repaired.")
    if result["failed"] or result["conflicts"]:
        print(f"Problems: {len(result['failed'])} files failed, {len(result['conflicts'])} name conflicts. "
              "Do NOT format the card yet.")
    else:
        print("Everything is on the PC and identical to the card. The card can be formatted in the camera.")
    print(f"IMPORTED {target}", flush=True)
    return result
