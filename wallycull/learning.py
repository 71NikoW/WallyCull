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
from .paths import USER_DIR

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
    ("shutter_log", "shutter speed"),
    ("ms_car", "race car in the frame (AI)"),
    ("ms_panning", "panning with streaked background (AI)"),
    ("ms_car_sharp", "sharp race car (AI)"),
    ("ms_wheels", "spinning wheels (AI)"),
    ("ms_framing", "room in the direction of travel (AI)"),
    ("ms_action", "racing action (AI)"),
    ("ms_background", "clean background (AI)"),
    ("ms_light", "dramatic light (AI)"),
    ("ms_clear_view", "clear view of the car (AI)"),
    ("ms_car_size", "car large in the frame (AI)"),
]
# Plus a number of principal components of the CLIP image features ("subject and style"); the training
# picks how many work best for you.
MOTORSPORT_KEYS = ("ms_car", "ms_panning", "ms_car_sharp", "ms_wheels", "ms_framing", "ms_action", "ms_background", "ms_light",
                   "ms_clear_view", "ms_car_size")
STYLE_DIMENSIONS = 12
STYLE_CHOICES = (0, 12, 32)
L2_CHOICES = (0.3, 1.0, 3.0, 10.0)
BLEND_CHOICES = (1.0, 0.75, 0.5, 0.25, 0.0)


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
        # Slow shutter = intended motion blur (panning), fast = frozen action
        "shutter_log": math.log(min(max((m.get("exif") or {}).get("exposure_time") or 1 / 250, 1 / 8000), 1.0)),
        **{key: r.get(key, 0.5) for key in MOTORSPORT_KEYS},
    }
    base = [values[n] for n, _ in FEATURES]
    if pca:
        base.extend(_style(embedding(b), pca).tolist())
    return np.array(base, dtype=np.float64)


def _style(e: np.ndarray | None, pca: dict) -> np.ndarray:
    if e is None or len(e) != len(pca["mean"]):
        return np.zeros(len(pca["axes"]))
    return (e - np.array(pca["mean"])) @ np.array(pca["axes"]).T


def _pca(embs: list[np.ndarray], dims: int = STYLE_DIMENSIONS) -> dict | None:
    """Principal components of the CLIP features of all images: condenses subject and style into a few numbers."""
    if embs:  # only features of one kind of CLIP model can be combined
        size = max({len(e) for e in embs}, key=[len(e) for e in embs].count)
        embs = [e for e in embs if len(e) == size]
    if not dims or len(embs) < dims * 3:
        return None
    x = np.array(embs)
    mean = x.mean(axis=0)
    _, _, vt = np.linalg.svd(x - mean, full_matrices=False)
    return {"mean": mean.tolist(), "axes": vt[:dims].tolist()}


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


# --- Shared training across all photo folders ---------------------------------------
# The ratings of every folder are kept (as feature vectors, without the photos) in %LOCALAPPDATA%\WallyCull\training.
# Every folder trains on its own ratings plus those of all other folders, so a new folder from the next race
# day is sorted by your taste right away.

def pool_dir() -> Path:
    return USER_DIR / "training"


def _save_pool(photo_dir: Path, examples: list[dict], y: np.ndarray) -> None:
    path = pool_dir() / f"{database.folder_key(photo_dir)}.npz"
    try:
        if not examples:
            path.unlink(missing_ok=True)
            return
        embs = [embedding(b) for b in examples]
        size = max((len(e) for e in embs if e is not None), default=0)
        emb = np.zeros((len(examples), size), np.float16)
        has = np.zeros(len(examples), bool)
        for i, e in enumerate(embs):
            if e is not None and len(e) == size:
                emb[i], has[i] = e, True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp.npz")
        np.savez_compressed(tmp, base=np.array([features(b) for b in examples], np.float32), emb=emb, has=has,
                            y=y.astype(np.float32), names=np.array([n for n, _ in FEATURES]),
                            folder=np.array(str(Path(photo_dir).resolve())))
        tmp.replace(path)
    except OSError:
        pass  # the shared training is a bonus; the folder's own model works without it


def load_pool(photo_dir: Path | None, config: dict) -> dict:
    """Ratings of all OTHER folders: base feature vectors, CLIP features (or None) and verdicts."""
    pool = {"base": [], "emb": [], "y": [], "folders": 0}
    if not config["learning"].get("shared", True) or not pool_dir().exists():
        return pool
    own = database.folder_key(photo_dir) if photo_dir else None
    names = [n for n, _ in FEATURES]
    for path in sorted(pool_dir().glob("*.npz")):
        if path.stem == own or path.name.endswith(".tmp.npz"):
            continue
        try:
            with np.load(path, allow_pickle=False) as data:
                if list(data["names"]) != names:
                    continue  # from an older version with other features: replaced when that folder is trained
                pool["base"].extend(data["base"].astype(np.float64))
                pool["emb"].extend(e.astype(np.float64) if h else None for e, h in zip(data["emb"], data["has"]))
                pool["y"].extend(data["y"].astype(np.float64))
                pool["folders"] += 1
        except (OSError, ValueError, KeyError):
            continue
    return pool


def _pool_vectors(pool: dict, pca: dict | None) -> np.ndarray:
    if not pool["y"]:
        return np.zeros((0, len(FEATURES) + (len(pca["axes"]) if pca else 0)))
    base = np.array(pool["base"])
    if not pca:
        return base
    return np.hstack([base, np.array([_style(e, pca) for e in pool["emb"]])])


def _with_annotations(db, rows) -> list[dict]:
    annotations = database.annotations_by_image(db)
    images = [database.row_to_dict(r) for r in rows]
    for b in images:
        b["annotations"] = annotations.get(b["id"], [])
    return images


def similarity_graph(emb: np.ndarray, anchors: np.ndarray | None = None, k: int = 12):
    """k nearest neighbors of every photo by CLIP similarity (reference images appended at the end)."""
    x = emb.astype(np.float32)
    if anchors is not None and len(anchors):
        x = np.vstack([x, anchors.astype(np.float32)])
    x /= np.linalg.norm(x, axis=1, keepdims=True) + 1e-8
    total = len(x)
    k = min(k, total - 1)
    neighbors = np.zeros((total, k), dtype=np.int64)
    weights = np.zeros((total, k), dtype=np.float32)
    for start in range(0, total, 1024):
        sim = x[start : start + 1024] @ x.T
        rows = np.arange(sim.shape[0])
        sim[rows, rows + start] = -1  # not its own neighbor
        idx = np.argpartition(-sim, k, axis=1)[:, :k]
        neighbors[start : start + 1024] = idx
        weights[start : start + 1024] = np.exp((np.take_along_axis(sim, idx, axis=1) - 1) / 0.05)
    weights /= weights.sum(axis=1, keepdims=True) + 1e-12
    return neighbors, weights, len(emb)


def spread(graph, labels: dict[int, float], alpha: float = 0.85, iterations: int = 40) -> np.ndarray:
    """Label spreading: your verdicts flow along the similarity graph to similar photos.

    labels: photo index -> 1.0 keep / 0.0 reject. Reference images count as "keep".
    Returns a keep probability for every photo."""
    neighbors, weights, n = graph
    seed = np.zeros((len(neighbors), 2), dtype=np.float32)  # columns: reject, keep
    for i, v in labels.items():
        seed[i] = (1 - v, v)
    seed[n:, 1] = 1.0
    f = seed.copy()
    for _ in range(iterations):
        f = alpha * (weights[:, :, None] * f[neighbors]).sum(axis=1) + (1 - alpha) * seed
    prior = float(np.mean(list(labels.values()))) if labels else 0.5
    mass = f.sum(axis=1)
    return ((f[:n, 1] + 0.02 * prior) / (mass[:n] + 0.02)).astype(np.float64)


def _log_loss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, 1e-4, 1 - 1e-4)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _reference_embeddings(config: dict) -> np.ndarray | None:
    """Features of your reference images (computed during the AI analysis), or None."""
    try:
        from .ai import references_folder

        data = np.load(references_folder(config) / ".references_cache.npz", allow_pickle=False)
        return data["emb"].astype(np.float32)
    except Exception:
        return None


def _folds(n: int) -> list[np.ndarray]:
    order = np.random.default_rng(0).permutation(n)
    return [order[f::5] for f in range(5)]


def train(photo_dir: Path, config: dict, thorough: bool = False) -> dict:
    """Trains the model from all ratings and stores the prediction for every image in the database.

    thorough=True (overnight run) tries several model sizes and picks the one that predicts your verdicts
    best in cross-validation. Otherwise the settings of the last thorough training are reused (fast)."""
    c = config["learning"]
    db = database.open_db(photo_dir)
    examples = _with_annotations(db, db.execute(
        "SELECT i.*, r.verdict, r.origin FROM images i JOIN ratings r ON r.image_id = i.id "
        "WHERE i.metrics IS NOT NULL AND i.error IS NULL AND i.version > 0"
    ).fetchall())
    y_own = np.array([1.0 if b["verdict"] >= 1 else 0.0 for b in examples])
    _save_pool(photo_dir, examples, y_own)
    pool = load_pool(photo_dir, config)
    y = np.concatenate([y_own, np.array(pool["y"])])
    origins = {}
    for b in examples:
        origins[b["origin"]] = origins.get(b["origin"], 0) + 1
    status = {"rated": len(examples), "keep": int(y_own.sum()), "reject": int(len(y_own) - y_own.sum()),
              "active": False, "sources": origins, "shared": len(pool["y"]), "shared_folders": pool["folders"]}
    keep = int(y.sum())
    if len(y) < c["min_ratings"] or min(keep, len(y) - keep) < c["min_per_side"]:
        db.close()
        status["hint"] = (
            f"Not enough ratings yet: at least {c['min_ratings']} in total and "
            f"{c['min_per_side']} each of keep and reject are needed."
        )
        return status

    images = _with_annotations(db, db.execute(
        "SELECT * FROM images WHERE metrics IS NOT NULL AND error IS NULL AND version > 0"
    ).fetchall())
    index = {b["id"]: i for i, b in enumerate(images)}
    example_idx = np.array([index[b["id"]] for b in examples])
    folds = _folds(len(y))

    # Similar photos: label spreading over the CLIP features (needs AI data)
    has_emb = np.array([embedding(b) is not None for b in images])
    spread_oof = None
    if len(example_idx) >= 10 and has_emb.sum() >= 30 and has_emb[example_idx].mean() > 0.8:
        emb_rows = np.nonzero(has_emb)[0]
        emb_pos = {int(r): i for i, r in enumerate(emb_rows)}
        emb = np.array([embedding(images[r]) for r in emb_rows])
        graph = similarity_graph(emb, _reference_embeddings(config))
        labeled = {emb_pos[int(r)]: float(v) for r, v in zip(example_idx, y_own) if int(r) in emb_pos}
        spread_oof = np.full(len(y), np.nan)
        for test in folds:
            test = test[test < len(example_idx)]  # shared ratings of other folders are not in this graph
            hidden = {emb_pos[int(example_idx[t])] for t in test if int(example_idx[t]) in emb_pos}
            scores = spread(graph, {i: v for i, v in labeled.items() if i not in hidden})
            for t in test:
                if int(example_idx[t]) in emb_pos:
                    spread_oof[t] = scores[emb_pos[int(example_idx[t])]]
        spread_all = np.full(len(images), np.nan)
        spread_all[emb_rows] = spread(graph, labeled)

    all_embs = [e for e in (embedding(b) for b in images) if e is not None] + [e for e in pool["emb"] if e is not None]

    def vectors(pca: dict | None) -> np.ndarray:
        pooled = _pool_vectors(pool, pca)
        if not examples:
            return pooled
        own = np.array([features(b, pca) for b in examples])
        return np.vstack([own, pooled]) if pool["y"] else own

    def cross_validate(dims: int, l2: float) -> tuple[np.ndarray, dict | None]:
        pca = _pca(all_embs, dims)
        x = vectors(pca)
        oof = np.full(len(y), float(y.mean()))
        for test in folds:
            learn = np.setdiff1d(np.arange(len(y)), test)
            if len(set(y[learn])) < 2:
                continue
            oof[test] = predict(_fit(x[learn], y[learn], l2), x[test])
        return oof, pca

    last = load_model(photo_dir) or {}
    if thorough:
        candidates = [(d, l2) for d in STYLE_CHOICES for l2 in L2_CHOICES]
    else:
        candidates = [(last.get("style_dims", STYLE_DIMENSIONS), last.get("l2", c["regularization"]))]
    best = None
    for dims, l2 in candidates:
        oof, pca = cross_validate(dims, l2)
        if dims and pca is None:
            continue
        blends = BLEND_CHOICES if (spread_oof is not None and thorough) else (
            [last.get("blend", 0.5) if spread_oof is not None else 1.0])
        for w in blends:
            p = oof.copy()
            if w < 1 and spread_oof is not None:
                ok = ~np.isnan(spread_oof)
                p[ok] = w * oof[ok] + (1 - w) * spread_oof[ok]
            loss = _log_loss(p, y)
            if best is None or loss < best["loss"]:
                best = {"loss": loss, "dims": dims, "l2": l2, "blend": w, "pca": pca,
                        "accuracy": float(((p >= 0.5) == (y == 1)).mean())}
    if best is None:  # too few images for any style dimensions
        oof, pca = cross_validate(0, c["regularization"])
        best = {"loss": _log_loss(oof, y), "dims": 0, "l2": c["regularization"], "blend": 1.0, "pca": None,
                "accuracy": float(((oof >= 0.5) == (y == 1)).mean())}

    pca = best["pca"]
    model = _fit(vectors(pca), y, best["l2"])
    model.update(
        features=[n for n, _ in FEATURES], pca=pca, accuracy=round(best["accuracy"], 3), rated=len(y_own),
        shared=len(pool["y"]),
        trained=datetime.now().isoformat(timespec="seconds"), style_dims=best["dims"], l2=best["l2"],
        blend=best["blend"], thorough=thorough or last.get("thorough", False), sources=origins,
    )
    model_path(photo_dir).write_text(json.dumps(model), encoding="utf-8")

    p = predict(model, np.array([features(b, pca) for b in images])) if images else np.zeros(0)
    if spread_oof is not None and best["blend"] < 1:
        ok = ~np.isnan(spread_all)
        p[ok] = best["blend"] * p[ok] + (1 - best["blend"]) * spread_all[ok]
    db.executemany("UPDATE images SET personal=? WHERE id=?", [(float(v), b["id"]) for v, b in zip(p, images)])
    db.commit()
    db.close()
    status.update(active=True, accuracy=model["accuracy"], style_dims=best["dims"], blend=best["blend"])
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
