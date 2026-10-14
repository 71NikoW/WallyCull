"""Technical quality score from classic image analysis (OpenCV/NumPy, no training needed).

Every measurement yields a raw value and a sub-score between 0 (bad) and 1 (good).
"""

from __future__ import annotations

import math

import cv2
import numpy as np

_face_cascade = None


def _scale(value: float, bad: float, good: float, log: bool = False) -> float:
    """Maps a measurement linearly (or logarithmically) to 0..1. Works in both directions."""
    if log:
        value, bad, good = (math.log(max(v, 1e-6)) for v in (value, bad, good))
    if good == bad:
        return 1.0 if value >= good else 0.0
    return float(min(1.0, max(0.0, (value - bad) / (good - bad))))


def _faces(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    global _face_cascade
    if not hasattr(cv2, "CascadeClassifier"):
        return []  # OpenCV 5 without Haar cascades: sharpness then uses the most salient area
    if _face_cascade is None:
        _face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    factor = 800 / max(gray.shape)
    small = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA) if factor < 1 else gray
    factor = min(factor, 1.0)
    minimum = max(24, int(min(small.shape) * 0.06))
    hits = _face_cascade.detectMultiScale(small, scaleFactor=1.1, minNeighbors=7, minSize=(minimum, minimum))
    return [tuple(int(v / factor) for v in h) for h in hits]


def _saliency(gray: np.ndarray) -> np.ndarray:
    """Spectral residual saliency (Hou & Zhang 2007): where does the image draw the eye?"""
    small = cv2.resize(gray, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float32)
    spectrum = np.fft.fft2(small)
    log_amplitude = np.log(np.abs(spectrum) + 1e-8)
    residual = log_amplitude - cv2.blur(log_amplitude, (3, 3))
    saliency = np.abs(np.fft.ifft2(np.exp(residual + 1j * np.angle(spectrum)))) ** 2
    saliency = cv2.GaussianBlur(saliency.astype(np.float32), (9, 9), 2.5)
    saliency = cv2.resize(saliency, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    return saliency / (saliency.max() + 1e-8)


def _tiles(shape: tuple[int, int], count_short: int = 6) -> list[tuple[slice, slice]]:
    h, w = shape
    side = max(16, min(h, w) // count_short)
    return [
        (slice(y, y + side), slice(x, x + side))
        for y in range(0, h - side + 1, side)
        for x in range(0, w - side + 1, side)
    ]


def _noise(gray: np.ndarray) -> float:
    """Noise estimate after Immerkaer (1996), only in smooth, unclipped areas."""
    g = gray.astype(np.float32)
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float32)
    response = np.abs(cv2.filter2D(g, -1, kernel))
    gradient = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    smooth = (gradient <= np.percentile(gradient, 30)) & (gray > 12) & (gray < 243)
    smooth[:2, :] = smooth[-2:, :] = False
    smooth[:, :2] = smooth[:, -2:] = False
    if smooth.sum() < 500:
        return 0.0
    return float(math.sqrt(math.pi / 2) / 6 * response[smooth].mean())


def _edge_widths(patch: np.ndarray, presmooth: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Width (sigma in pixels) and direction (degrees, 0-180) of all strong edges in a patch.

    A sharp edge is about 1 pixel wide, a soft one much wider. The ratio of edge contrast to edge
    slope is measured; noise barely matters because only high-contrast edges count."""
    f = cv2.GaussianBlur(patch.astype(np.float32), (0, 0), presmooth)
    gx, gy = cv2.Sobel(f, cv2.CV_32F, 1, 0) / 8, cv2.Sobel(f, cv2.CV_32F, 0, 1) / 8
    slope = cv2.magnitude(gx, gy)
    window = np.ones((15, 15), np.uint8)
    contrast = cv2.dilate(f, window) - cv2.erode(f, window)
    peak = slope >= 0.98 * cv2.dilate(slope, np.ones((3, 3), np.uint8))
    selected = peak & (contrast >= 30) & (slope > 1)
    sigma = 0.399 * contrast[selected] / slope[selected]
    width = np.sqrt(np.maximum(sigma * sigma - presmooth * presmooth, 0))
    direction = np.degrees(np.arctan2(gy[selected], gx[selected])) % 180
    return width, direction


def _sharpness_100(full: np.ndarray, areas: list[tuple[int, int, int, int]]) -> dict | None:
    """Edge width at the sharpest spots of the subject at full resolution, plus a camera shake test:
    with motion blur, edges along the motion are wide while edges across it stay sharp."""
    widths, directions = [], []
    for x, y, bw, bh in areas:
        patch = full[max(0, y) : y + bh, max(0, x) : x + bw]
        if patch.shape[0] >= 32 and patch.shape[1] >= 32:
            w, d = _edge_widths(patch)
            widths.append(w)
            directions.append(d)
    if not widths or sum(len(w) for w in widths) < 50:
        return None  # too few edges (fog, sky): no statement possible
    width, direction = np.concatenate(widths), np.concatenate(directions)
    result = {"edge_width": float(np.percentile(width, 20))}
    # Sort edges by gradient direction into four sectors (0°, 45°, 90°, 135°)
    sector = (((direction + 22.5) % 180) // 45).astype(int)
    per_direction = {}
    for s in range(4):
        selected = width[sector == s]
        if selected.size >= 80:
            per_direction[s * 45] = float(np.percentile(selected, 20))
    if len(per_direction) >= 2:
        widest = max(per_direction, key=per_direction.get)
        narrowest = min(per_direction.values())
        result["direction_ratio"] = per_direction[widest] / max(narrowest, 0.3)
        result["motion_direction"] = widest  # gradient 0° = horizontal motion
        result["widest_direction_width"] = per_direction[widest]
    return result


def _orientation(gx: np.ndarray, gy: np.ndarray) -> tuple[float, float]:
    """Structure tensor: how strongly the edges point one way (0..1) and which way (degrees)."""
    sxx, syy, sxy = float((gx * gx).sum()), float((gy * gy).sum()), float((gx * gy).sum())
    trace, det = sxx + syy, sxx * syy - sxy * sxy
    if trace <= 0:
        return 0.0, 0.0
    root = math.sqrt(max(trace * trace / 4 - det, 0.0))
    large, small = trace / 2 + root, trace / 2 - root
    angle = 0.5 * math.degrees(math.atan2(2 * sxy, sxx - syy))
    # Blur runs across the dominant edge direction
    return 1 - small / large if large > 0 else 0.0, (angle + 90) % 180


def analyze(rgb: np.ndarray, info: dict, config: dict, full: np.ndarray | None = None,
            subject: list[float] | None = None) -> dict:
    """Computes all quality metrics of a (downscaled) image."""
    q = config["quality"]
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    m: dict = {}

    # --- Exposure ---------------------------------------------------------
    m["brightness"] = float(gray.mean())
    m["clipped_highlights"] = float((rgb.max(axis=2) >= 254).mean())
    m["crushed_shadows"] = float((gray <= 10).mean())
    lights = float((gray >= 220).mean())
    exposure, iso = info.get("exposure_time") or 0, info.get("iso") or 0
    m["night"] = bool(m["brightness"] < 60 and (lights > 0.0008 or exposure >= 0.5 or (iso >= 1600 and exposure >= 1 / 30)))
    under_bad = q["underexposed_bad_night"] if m["night"] else q["underexposed_bad"]
    score_over = _scale(m["clipped_highlights"], q["overexposed_bad"], q["overexposed_ok"])
    score_under = _scale(m["crushed_shadows"], under_bad, q["underexposed_ok"])

    # --- Sharpness on the subject -----------------------------------------
    # Stretch contrast so dark or flat images are not automatically judged as blurry
    low, high = np.percentile(gray, (1, 99))
    stretched = gray
    if 8 < high - low < 200:
        stretched = np.clip((gray.astype(np.float32) - low) * min(255.0 / (high - low), 4.0), 0, 255).astype(np.uint8)
    soft = cv2.GaussianBlur(stretched, (3, 3), 0)
    laplace = cv2.Laplacian(soft, cv2.CV_32F)
    tiles = _tiles((h, w))
    saliency = _saliency(gray)
    yy, xx = np.mgrid[0:h, 0:w]
    center = np.exp(-(((xx - w / 2) / (0.4 * w)) ** 2 + ((yy - h / 2) / (0.4 * h)) ** 2))
    weight = saliency * (0.5 + 0.5 * center)
    tile_sharpness = np.array([float(laplace[t].var()) for t in tiles])
    tile_weight = np.array([float(weight[t].mean()) for t in tiles])
    count = max(3, len(tiles) // 5)
    subject_tiles = np.argsort(tile_weight)[-count:]
    subject_sharpness = float(np.median(tile_sharpness[subject_tiles]))
    peak = float(np.percentile(tile_sharpness, 90))
    m["sharpness_subject_area"] = subject_sharpness
    m["sharpness_peak"] = peak

    faces = _faces(gray)
    m["faces"] = len(faces)
    if faces:
        x, y, fw, fh = max(faces, key=lambda f: f[2] * f[3])
        face_value = float(laplace[y : y + fh, x : x + fw].var()) * q["face_factor"]
        m["sharpness_face"] = face_value
        m["sharpness"] = max(face_value, 0.5 * subject_sharpness)
        m["subject"] = "face"
    else:
        # Bokeh-tolerant: if some area is clearly sharp, the subject counts as sharp.
        m["sharpness"] = max(subject_sharpness, 0.6 * peak)
        m["subject"] = "area"
    if subject:
        # Subject you marked yourself: measure sharpness exactly there
        sx0, sy0 = int(subject[0] * w), int(subject[1] * h)
        sx1, sy1 = max(sx0 + 8, int((subject[0] + subject[2]) * w)), max(sy0 + 8, int((subject[1] + subject[3]) * h))
        m["sharpness"] = float(laplace[sy0:sy1, sx0:sx1].var())
        m["subject"] = "marked"
        marked = [
            i for i, (ys, xs) in enumerate(tiles)
            if xs.start < sx1 and xs.stop > sx0 and ys.start < sy1 and ys.stop > sy0
        ]
        if marked:
            subject_tiles = np.array(marked)
    score_sharpness = _scale(m["sharpness"], q["sharpness_blurry"], q["sharpness_sharp"], log=True)

    # --- Sharpness at 100 % (what matters for large wall art) --------------
    result = None
    if full is not None:
        fy, fx = full.shape[0] / h, full.shape[1] / w
        if subject:
            vh, vw = full.shape
            areas = [(int(subject[0] * vw), int(subject[1] * vh), int(subject[2] * vw), int(subject[3] * vh))]
        elif faces:
            areas = [(int(x * fx), int(y * fy), int(fw * fx), int(fh * fy)) for x, y, fw, fh in faces]
        else:
            areas = []
            for i in subject_tiles:
                ys, xs = tiles[i]
                areas.append((int(xs.start * fx), int(ys.start * fy), int((xs.stop - xs.start) * fx), int((ys.stop - ys.start) * fy)))
        result = _sharpness_100(full, areas)
        if result is not None:
            m["edge_width_100"] = round(result["edge_width"], 2)
            score_100 = _scale(result["edge_width"], q["edge_width_blurry"], q["edge_width_sharp"])
            m["score_sharpness_100"] = round(score_100, 3)
            m["score_sharpness_overview"] = round(score_sharpness, 3)
            score_sharpness = 0.3 * score_sharpness + 0.7 * score_100
            if "direction_ratio" in result:
                m["direction_ratio"] = round(result["direction_ratio"], 2)
                m["shake_angle"] = result["motion_direction"]

    # --- Camera shake (directional motion blur) ----------------------------
    if "direction_ratio" in m:
        # Precise test at full resolution: compare edge widths per direction
        m["shaky"] = bool(
            m["direction_ratio"] >= q["shake_ratio"]
            and result["widest_direction_width"] > q["shake_min_width"]
            and result["edge_width"] > q["shake_min_edge_width"]
        )
        score_shake = _scale(m["direction_ratio"], q["shake_ratio"] * 1.6, q["shake_ratio"] * 0.85) if m["shaky"] else 1.0
    else:
        # Rough estimate on the preview (structure tensor)
        gx = cv2.Sobel(soft, cv2.CV_32F, 1, 0)
        gy = cv2.Sobel(soft, cv2.CV_32F, 0, 1)
        mask = np.zeros((h, w), dtype=bool)
        for i in subject_tiles:
            mask[tiles[i]] = True
        m["direction_strength"], m["shake_angle"] = _orientation(gx[mask], gy[mask])
        m["shaky"] = bool(m["direction_strength"] > q["shake_limit"] and score_sharpness < 0.7)
        score_shake = _scale(m["direction_strength"], 1.0, q["shake_limit"]) * 0.7 if m["shaky"] else 1.0

    # --- Noise ------------------------------------------------------------
    m["noise"] = _noise(gray)
    score_noise = _scale(m["noise"], q["noise_bad"], q["noise_ok"])

    # --- Resolution / wall-art suitability ---------------------------------
    width, height = info.get("width") or w, info.get("height") or h
    m["megapixels"] = round(width * height / 1e6, 1)
    m["print_cm"] = [round(width / q["print_dpi"] * 2.54), round(height / q["print_dpi"] * 2.54)]
    score_resolution = _scale(m["megapixels"], q["wallart_min_megapixels"] * 0.4, q["wallart_min_megapixels"])

    scores = {
        "sharpness": score_sharpness,
        "exposure": min(score_over, score_under),
        "noise": score_noise,
        "shake": score_shake,
        "resolution": score_resolution,
    }
    m["scores"] = {k: round(v, 3) for k, v in scores.items()}
    m["score_over"], m["score_under"] = round(score_over, 3), round(score_under, 3)

    # --- Overall score -----------------------------------------------------
    weights = q["weights"]
    mean = sum(weights[k] * scores[k] for k in scores) / sum(weights.values())
    # The worst flaws (sharpness, exposure) must not be averaged away.
    quality = 0.6 * mean + 0.4 * min(scores["sharpness"], scores["exposure"])

    failed = []
    if m["brightness"] < 8 or m["crushed_shadows"] > 0.97:
        failed.append("almost completely black")
    if m["clipped_highlights"] > 0.6:
        failed.append("almost completely white")
    if m["sharpness"] < q["sharpness_blurry"] * 0.4 and not m["night"]:
        failed.append("extremely blurry")
    m["failed"] = failed
    if failed:
        quality = min(quality, 0.1)
    m["quality"] = round(float(quality), 3)
    return m
