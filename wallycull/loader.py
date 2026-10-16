"""Find images in a folder, load them (JPG/PNG/TIFF and RAW such as Fuji .RAF) and read EXIF data."""

from __future__ import annotations

import hashlib
import io
import os
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

Image.MAX_IMAGE_PIXELS = None  # do not reject large panoramas as "decompression bombs"

# EXIF tags
_EXIF_IFD = 0x8769
_DATE_ORIGINAL = 0x9003
_DATE = 0x0132
_MODEL = 0x0110
_ISO = 0x8827
_EXPOSURE_TIME = 0x829A
_APERTURE = 0x829D
_FOCAL_LENGTH = 0x920A


def fingerprint(path: Path) -> str:
    """Size plus the first and last 64 KB: recognizes the same file after copying (when the date changed)."""
    h = hashlib.sha1()
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        h.update(str(size).encode())
        f.seek(0)
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    return h.hexdigest()[:20]


def find_images(photo_dir: Path, config: dict) -> list[dict]:
    """Finds all images and groups files with the same name (e.g. DSCF0001.RAF + .JPG + .xmp) into one."""
    f = config["files"]
    raw, image, sidecar = set(f["raw"]), set(f["image"]), set(f["sidecar"])
    ignored = {name.lower() for name in f["ignored_folders"]}
    groups: dict[str, dict] = {}

    for folder, subfolders, names in os.walk(photo_dir):
        subfolders[:] = sorted(s for s in subfolders if s.lower() not in ignored and not s.startswith("."))
        for name in sorted(names):
            ext = Path(name).suffix.lower()
            if ext not in raw | image | sidecar:
                continue
            path = Path(folder) / name
            rel = path.relative_to(photo_dir).as_posix()
            stem = rel[: -len(ext)] if ext else rel
            # "DSCF0001.RAF.xmp" belongs to DSCF0001 as well
            if ext in sidecar and Path(stem).suffix.lower() in raw | image:
                stem = stem[: -len(Path(stem).suffix)]
            g = groups.setdefault(stem.lower(), {"key": stem.lower(), "files": [], "raw": None, "image": None})
            g["files"].append(rel)
            if ext in image and g["image"] is None:
                g["image"] = rel
            elif ext in raw and g["raw"] is None:
                g["raw"] = rel

    result = []
    for g in groups.values():
        if not (g["image"] or g["raw"]):
            continue  # sidecar files without an image
        g["source"] = g["image"] or g["raw"]  # the camera JPG is faster and shows the film simulation
        # Only the image files count: a new or changed .xmp (e.g. stars from Lightroom) is no reason to measure again
        size, mtime, size_all, mtime_all = 0, 0.0, 0, 0.0
        for rel in g["files"]:
            st = (photo_dir / rel).stat()
            size_all += st.st_size
            mtime_all = max(mtime_all, st.st_mtime)
            if Path(rel).suffix.lower() not in sidecar:
                size += st.st_size
                mtime = max(mtime, st.st_mtime)
        g["size"], g["mtime"] = size, mtime
        g["size_all"], g["mtime_all"] = size_all, mtime_all  # how versions before 0.11 counted
        result.append(g)
    result.sort(key=lambda g: g["key"])
    return result


def _read_exif(exif) -> dict:
    info = {}
    if not exif:
        return info
    try:
        sub = exif.get_ifd(_EXIF_IFD)
    except Exception:
        sub = {}
    date = sub.get(_DATE_ORIGINAL) or exif.get(_DATE)
    if date:
        try:
            info["taken_at"] = datetime.strptime(str(date).strip()[:19], "%Y:%m:%d %H:%M:%S").isoformat()
        except ValueError:
            pass
    if exif.get(_MODEL):
        info["camera"] = str(exif.get(_MODEL)).strip("\x00 ")
    for tag, field in ((_ISO, "iso"), (_EXPOSURE_TIME, "exposure_time"), (_APERTURE, "aperture"), (_FOCAL_LENGTH, "focal_length")):
        value = sub.get(tag)
        if isinstance(value, (tuple, list)):
            value = value[0] if value else None
        try:
            if value is not None:
                info[field] = float(value)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
    return info


def _shrink(img: Image.Image, edge: int) -> Image.Image:
    if max(img.size) > edge:
        img = img.copy()
        img.thumbnail((edge, edge), Image.LANCZOS)
    return img


# libjpeg scales while decoding: much faster than decoding everything and shrinking afterwards
_REDUCED = ((8, cv2.IMREAD_REDUCED_COLOR_8), (4, cv2.IMREAD_REDUCED_COLOR_4), (2, cv2.IMREAD_REDUCED_COLOR_2))


def _load_pil(data, edge: int, full: bool = False):
    """Returns (downscaled image, EXIF info, original size, full-resolution grayscale or None)."""
    try:
        img = Image.open(data)
    except Exception as e:
        raise ValueError("not a readable image, the file is probably damaged") from e
    original = img.size
    info = _read_exif(img.getexif())
    if full and img.format == "JPEG" and hasattr(data, "getvalue"):
        # Fast path (OpenCV, incl. EXIF rotation): the full-resolution grayscale comes straight from the
        # JPEG's brightness channel, the color image is decoded at reduced size. About 3x faster than
        # decoding all 40 megapixels in color, with the same grayscale.
        buffer = np.frombuffer(data.getvalue(), np.uint8)
        gray = cv2.imdecode(buffer, cv2.IMREAD_GRAYSCALE)
        flag = next((flag for factor, flag in _REDUCED if max(original) / factor >= edge), cv2.IMREAD_COLOR)
        bgr = cv2.imdecode(buffer, flag) if gray is not None else None
        if bgr is not None:
            f = edge / max(gray.shape)
            if f < 1:
                # Brightness from the full resolution (exactly as before: sharpness depends on it),
                # only the colors come from the reduced decode
                luma = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
                ycc = cv2.cvtColor(cv2.resize(bgr, luma.shape[::-1], interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2YCrCb)
                ycc[..., 0] = luma
                bgr = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2BGR)
            return Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)), info, original, gray
    if not full:
        img.draft("RGB", (edge, edge))  # JPEG: decode directly at reduced size, saves a lot of time
    img = ImageOps.exif_transpose(img).convert("RGB")
    gray = np.asarray(img.convert("L")) if full else None
    return _shrink(img, edge), info, original, gray


def _align(gray: np.ndarray, small: Image.Image) -> np.ndarray:
    """Rotates the full image if it is oriented differently from the preview."""
    if (gray.shape[1] > gray.shape[0]) != (small.width > small.height) and small.width != small.height:
        return np.ascontiguousarray(np.rot90(gray, -1))
    return gray


def _load_raw(path: Path, edge: int, full: bool, reference: int, develop: bool):
    import rawpy

    with rawpy.imread(str(path)) as raw:
        width, height = raw.sizes.width, raw.sizes.height
        if raw.sizes.flip in (5, 6):
            width, height = height, width
        try:
            thumb = raw.extract_thumb()
        except Exception:
            thumb = None
        img, info, gray = None, {}, None
        if thumb is not None and thumb.format == rawpy.ThumbFormat.JPEG:
            # Embedded camera JPG: shows exposure and film simulation the way the camera rendered them.
            # On the X-T5 it is 4416 x 2944, large enough for the sharpness check as well.
            thumb_img, info, size, thumb_gray = _load_pil(io.BytesIO(thumb.data), edge, full)
            if max(size) >= min(edge, 1000):
                img = thumb_img
                if full and not develop and max(size) >= reference * 0.95:
                    gray = thumb_gray
        if (full and gray is None) or img is None:
            # Fully develop the RAW: needed for 100 % sharpness when the preview is too small (slower)
            rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
            if img is None:
                img = _shrink(Image.fromarray(rgb), edge)
            if full:
                gray = _align(cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY), img)
        return img, info, (width, height), gray


def load_image(path: Path, raw_extensions: set[str], edge: int, full: bool = False,
               reference: int = 4000, develop: bool = False):
    """Loads an image downscaled as an RGB array, plus EXIF info incl. the original size and optionally
    the full-resolution grayscale (for the sharpness check at 100 %)."""
    if path.suffix.lower() in raw_extensions:
        img, info, (width, height), gray = _load_raw(path, edge, full, reference, develop)
    else:
        with open(path, "rb") as f:
            img, info, (width, height), gray = _load_pil(io.BytesIO(f.read()), edge, full)
        # Match the original size to the rotated preview
        if (img.width > img.height) != (width > height) and img.width != img.height:
            width, height = height, width
    info["width"], info["height"] = int(width), int(height)
    if gray is not None and max(gray.shape) > reference:
        # Same scale for everyone: sharpness is always measured at the same image size
        f = reference / max(gray.shape)
        gray = cv2.resize(gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    return np.asarray(img), info, gray
