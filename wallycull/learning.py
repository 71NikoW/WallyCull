"""Personal training: learns from your ratings which images you would keep.

Every image is described as a feature vector (all measurements plus AI scores). A logistic regression
is trained on it (pure NumPy, takes milliseconds on the CPU). With AI data, 12 principal components of
the CLIP image features are added, so the model also learns subjects and style.
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from . import database

# (name in the vector, label used in explanations)
FEATURES = [
    ("score_sharpness", "sharpness"),
    ("score_exposure", "exposure"),
    ("score_noise", "noise"),
    ("score_shake", "camera shake"),
    ("score_resolution", "resolution"),
    ("quality", "overall quality"),
    ("edge_width", "fine detail"),
    ("sharpness_log", "sharpness on the subject"),
    ("noise", "noise"),
    ("brightness", "brightness"),
    ("clipped", "blown highlights"),
    ("crushed", "crushed shadows"),
    ("direction", "camera shake"),
    ("night", "night shot"),
    ("face", "face in the image"),
    ("faces", "number of faces"),
    ("best_of_group", "best image of its burst"),
    ("in_group", "part of a burst"),
    ("iso_log", "ISO"),
    ("obstruction", "something in the way (marked by you)"),
    ("ai_aesthetic", "aesthetics (AI)"),
    ("ai_wallart", "wall-art potential (AI)"),
    ("ai_quality", "image quality (AI)"),
    ("ai_sharpness", "sharpness (AI)"),
    ("ai_content", "image content"),
    ("ai_reference", "closeness to your references"),
    ("ai_musiq", "image quality (MUSIQ)"),
    ("ai_nima", "aesthetics (NIMA)"),
]
# Plus up to STYLE_DIMENSIONS principal components of the CLIP image features ("subject and style").
STYLE_DIMENSIONS = 12


def embedding(b: dict) -> np.ndarray | None:
    e = b.get("embedding")
    return np.frombuffer(e, np.float16).astype(np.float64) if e else None


def features(b: dict, pca: dict | None = None) -> np.ndarray:
    m = b.get("metrics") or {}
    r = b.get("ai") if isinstance(b.get("ai"), dict) else {}
    scores = m.get("scores") or {}
    iso = (m.get("exif") or {}).get("iso") or 400
    values = {
        "score_sharpness": scores.get("sharpness", 0.5),
        "score_exposure": scores.get("exposure", 0.5),
        "score_noise": scores.get("noise", 0.5),
        "score_shake": scores.get("shake", 1.0),
        "score_resolution": scores.get("resolution", 1.0),
        "quality": b.get("quality") or 0.0,
        "edge_width": math.log(m.get("edge_width_100") or 1.6),
        "sharpness_log": math.log1p(m.get("sharpness") or 0),
        "noise": m.get("noise") or 0.0,
        "brightness": (m.get("brightness") or 128) / 255,
        "clipped": math.sqrt(m.get("clipped_highlights") or 0),
        "crushed": math.sqrt(m.get("crushed_shadows") or 0),
        "direction": m.get("direction_ratio") or 1.0,
        "night": float(bool(m.get("night"))),
        "face": float((m.get("faces") or 0) > 0),
        "faces": min(m.get("faces") or 0, 5),
        "best_of_group": float(not b.get("group_rank") or b["group_rank"] == 1),
        "in_group": float(bool(b.get("group_id"))),
        "iso_log": math.log(max(iso, 50)),
        "obstruction": float(any(x["kind"] == "obstruction" for x in b.get("annotations") or [])),
        "ai_aesthetic": r.get("aesthetic", 5.0),
        "ai_wallart": r.get("wallart", 0.5),
        "ai_quality": r.get("clip_quality", 0.5),
        "ai_sharpness": r.get("clip_sharpness", 0.5),
        "ai_content": r.get("content_value", 0.0),
        "ai_reference": r.get("reference", 0.0),
        "ai_musiq": r.get("musiq", 50.0) / 100,
        "ai_nima": r.get("nima", 5.0),
    }
    base = [values[n] for n, _ in FEATURES]
    if pca:
        e = embedding(b)
        style = (e - np.array(pca["mean"])) @ np.array(pca["axes"]).T if e is not None else np.zeros(len(pca["axes"]))
        base.extend(style.tolist())
    return np.array(base, dtype=np.float64)


def _pca(images: list[dict]) -> dict | None:
    """Principal components of the CLIP features of all images: condenses subject and style into a few numbers."""
    embs = [e for e in (embedding(b) for b in images) if e is not None]
    if len(embs) < STYLE_DIMENSIONS * 3:
        return None
    x = np.array(embs)
    mean = x.mean(axis=0)
    _, _, vt = np.linalg.svd(x - mean, full_matrices=False)
    return {"mean": mean.tolist(), "axes": vt[:STYLE_DIMENSIONS].tolist()}


def _logistic(x: np.ndarray, y: np.ndarray, weight: np.ndarray, l2: float) -> np.ndarray:
    """Logistic regression via Newton's method. x already contains the constant as its first column."""
    w = np.zeros(x.shape[1])
    penalty = np.full(x.shape[1], l2)
    penalty[0] = 0.0  # do not penalize the intercept
    for _ in range(50):
        p = 1 / (1 + np.exp(-np.clip(x @ w, -30, 30)))
        gradient = x.T @ (weight * (p - y)) + penalty * w
        hessian = (x * (weight * p * (1 - p))[:, None]).T @ x + np.diag(penalty) + 1e-9 * np.eye(len(w))
        step = np.linalg.solve(hessian, gradient)
        w -= step
        if np.abs(step).max() < 1e-6:
            break
    return w


def _fit(x: np.ndarray, y: np.ndarray, l2: float) -> dict:
    mean, std = x.mean(axis=0), x.std(axis=0)
    std[std < 1e-9] = 1.0
    xs = np.hstack([np.ones((len(x), 1)), (x - mean) / std])
    # Balance the classes: keep and reject count the same, no matter how often you pick either
    share = y.mean()
    weight = np.where(y == 1, 0.5 / max(share, 1e-6), 0.5 / max(1 - share, 1e-6))
    w = _logistic(xs, y, weight, l2)
    return {"mean": mean.tolist(), "std": std.tolist(), "weights": w.tolist()}


def predict(model: dict, x: np.ndarray) -> np.ndarray:
    xs = (np.atleast_2d(x) - np.array(model["mean"])) / np.array(model["std"])
    w = np.array(model["weights"])
    return 1 / (1 + np.exp(-np.clip(w[0] + xs @ w[1:], -30, 30)))


# Positive wording when a feature speaks FOR keeping
POSITIVE = {"something in the way (marked by you)": "nothing in the way", "camera shake": "no camera shake",
            "noise": "little noise", "blown highlights": "no blown highlights", "crushed shadows": "no crushed shadows",
            "ISO": "low ISO", "part of a burst": "single shot"}


def explain(model: dict, x: np.ndarray, count: int = 2) -> tuple[list[str], list[str]]:
    """Which features speak most strongly for and against keeping?"""
    xs = (x - np.array(model["mean"])) / np.array(model["std"])
    contribution = np.array(model["weights"][1:]) * xs
    names = [label for _, label in FEATURES] + ["subject and style"] * (len(contribution) - len(FEATURES))
    pro, con = [], []
    for i in np.argsort(-contribution):
        name = POSITIVE.get(names[i], names[i])
        if contribution[i] > 0.3 and name not in pro and len(pro) < count:
            pro.append(name)
    for i in np.argsort(contribution):
        if contribution[i] < -0.3 and names[i] not in con and len(con) < count:
            con.append(names[i])
    return pro, con


def model_path(photo_dir: Path) -> Path:
    return database.data_dir(photo_dir) / "model.json"


def load_model(photo_dir: Path) -> dict | None:
    try:
        model = json.loads(model_path(photo_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    # A model from an older version with different features is replaced at the next training
    return model if model.get("features") == [n for n, _ in FEATURES] and "pca" in model else None


def _with_annotations(db, rows) -> list[dict]:
    annotations = database.annotations_by_image(db)
    images = [database.row_to_dict(r) for r in rows]
    for b in images:
        b["annotations"] = annotations.get(b["id"], [])
    return images


def train(photo_dir: Path, config: dict) -> dict:
    """Trains the model from all ratings and stores the prediction for every image in the database."""
    c = config["learning"]
    db = database.open_db(photo_dir)
    examples = _with_annotations(db, db.execute(
        "SELECT i.*, r.verdict FROM images i JOIN ratings r ON r.image_id = i.id "
        "WHERE i.metrics IS NOT NULL AND i.error IS NULL AND i.version > 0"
    ).fetchall())
    y = np.array([1.0 if b["verdict"] >= 1 else 0.0 for b in examples])
    status = {"rated": len(examples), "keep": int(y.sum()), "reject": int(len(y) - y.sum()), "active": False}
    if len(examples) < c["min_ratings"] or min(status["keep"], status["reject"]) < c["min_per_side"]:
        db.close()
        status["hint"] = (
            f"Not enough ratings yet: at least {c['min_ratings']} in total and "
            f"{c['min_per_side']} each of keep and reject are needed."
        )
        return status

    images = _with_annotations(db, db.execute(
        "SELECT * FROM images WHERE metrics IS NOT NULL AND error IS NULL AND version > 0"
    ).fetchall())
    pca = _pca(images)
    x = np.array([features(b, pca) for b in examples])
    # Honest accuracy: 5-fold cross-validation
    order = np.random.default_rng(0).permutation(len(y))
    hits = 0
    for fold in range(5):
        test = order[fold::5]
        learn = np.setdiff1d(order, test)
        if len(set(y[learn])) < 2:
            continue
        m = _fit(x[learn], y[learn], c["regularization"])
        hits += int(((predict(m, x[test]) >= 0.5) == (y[test] == 1)).sum())
    model = _fit(x, y, c["regularization"])
    model.update(
        features=[n for n, _ in FEATURES], pca=pca, accuracy=round(hits / len(y), 3),
        rated=len(y), trained=datetime.now().isoformat(timespec="seconds"),
    )
    model_path(photo_dir).write_text(json.dumps(model), encoding="utf-8")

    if images:
        p = predict(model, np.array([features(b, pca) for b in images]))
        db.executemany("UPDATE images SET personal=? WHERE id=?", [(float(v), b["id"]) for v, b in zip(p, images)])
    db.commit()
    db.close()
    status.update(active=True, accuracy=model["accuracy"])
    return status


def next_batch(photo_dir: Path, config: dict, count: int = 20, exclude: set[int] | None = None) -> list[dict]:
    """Picks the next images to rate.

    Without a model: a mix across the whole quality range. With a model (active learning):
    mostly the images the model is least sure about, plus some randomness."""
    db = database.open_db(photo_dir)
    open_images = _with_annotations(db, db.execute(
        "SELECT * FROM images WHERE status='active' AND metrics IS NOT NULL AND error IS NULL AND version > 0 "
        "AND id NOT IN (SELECT image_id FROM ratings)"
    ).fetchall())
    db.close()
    exclude = exclude or set()
    open_images = [b for b in open_images if b["id"] not in exclude]
    rng = np.random.default_rng()
    if not open_images:
        return []
    with_model = load_model(photo_dir) is not None and any(b.get("personal") is not None for b in open_images)
    if with_model:
        open_images.sort(key=lambda b: abs((b.get("personal") if b.get("personal") is not None else 0.5) - 0.5))
        unsure = open_images[: max(1, int(count * (1 - config["learning"]["random_share"])))]
        rest = open_images[len(unsure):]
        extra = [rest[i] for i in rng.permutation(len(rest))[: count - len(unsure)]] if rest else []
        selection = unsure + extra
        rng.shuffle(selection)
        return selection[:count]
    # Without a model: split by quality into fifths and draw from each in turn
    open_images.sort(key=lambda b: b["quality"] or 0)
    pots = [list(t) for t in np.array_split(np.array(open_images, dtype=object), min(5, len(open_images)))]
    for t in pots:
        rng.shuffle(t)
    selection = []
    while len(selection) < count and any(pots):
        for t in pots:
            if t and len(selection) < count:
                selection.append(t.pop())
    return selection
