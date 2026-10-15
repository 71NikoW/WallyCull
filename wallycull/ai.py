"""Pretrained AI models for aesthetics, quality and content (run locally, on the GPU when available).

- CLIP ViT-L/14 (OpenAI): image features (embedding), content type via zero-shot, quality questions (CLIP-IQA idea)
- LAION aesthetic predictor v2: aesthetics 1-10, trained on hundreds of thousands of rated photos
- optional pyiqa: MUSIQ (technical quality, KonIQ) and NIMA (aesthetics, AVA photo contests)
- references folder: how close your photos are to images you consider great wall art

The models are downloaded once. Your photos never leave the PC.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image
from tqdm import tqdm

from . import database, loader
from .paths import MODELS_DIR, USER_DIR

AI_VERSION = 3
LAION_URL = "https://github.com/christophschuhmann/improved-aesthetic-predictor/raw/main/sac+logos+ava1-l14-linearMSE.pth"

# Content types: (key, label, prompt, value: -1 low, 0 neutral, +1 high)
CONTENT = [
    ("screenshot", "Screenshot", "a screenshot of a phone or computer screen", -1),
    ("meme", "Meme", "a meme with large text", -1),
    ("receipt", "Receipt", "a photo of a receipt or invoice", -1),
    ("document", "Document", "a photo of a document or a sheet of paper with text", -1),
    ("parking", "Parking note", "a photo of a parking lot or a parking spot number", -1),
    ("whiteboard", "Whiteboard", "a photo of a whiteboard or blackboard", -1),
    ("accidental", "Accidental shot", "an accidental photo of the floor, the ceiling or the inside of a pocket", -1),
    ("finger", "Finger over the lens", "a photo with a finger covering the camera lens", -1),
    ("test_shot", "Test shot", "a dark, blurry test shot of nothing in particular", -1),
    ("people", "People", "a photo of people", 1),
    ("portrait", "Portrait", "a portrait photo of a person", 1),
    ("event", "Party/event", "a photo of a party, wedding or celebration", 1),
    ("animal", "Animal", "a photo of an animal", 1),
    ("landscape", "Landscape", "a landscape photo of nature", 1),
    ("mountains", "Mountains", "a photo of mountains", 1),
    ("sea", "Sea/beach", "a photo of the sea, a lake or a beach", 1),
    ("sky", "Sunset/sky", "a photo of a sunset, sunrise or dramatic sky", 1),
    ("city", "City/street", "a cityscape or street photo", 1),
    ("architecture", "Architecture", "an architectural photo of a building", 1),
    ("landmark", "Landmark", "a photo of a famous landmark", 1),
    ("plants", "Plants/macro", "a close-up photo of flowers or plants", 1),
    ("abstract", "Abstract/art", "an abstract artistic photo", 1),
    ("motorsport", "Motorsport", "a photo of a race car on a race track", 1),
    ("vehicle", "Vehicle", "a photo of a car, motorbike or vehicle", 0),
    ("food", "Food", "a photo of food", 0),
    ("interior", "Interior", "a photo of a room interior", 0),
    ("object", "Object", "a photo of an everyday object", 0),
]

# Yes/no questions for CLIP (positive, negative)
QUESTIONS = {
    "clip_sharpness": ("a sharp, in-focus photo", "a blurry, out-of-focus photo"),
    "clip_exposure": ("a well exposed photo", "a badly exposed photo"),
    "clip_quality": ("a high quality photo", "a low quality photo"),
    "wallart": ("a stunning photograph that would look great as a large print on a wall",
                "an ordinary, forgettable snapshot"),
}

# Motorsport: what makes a race track photo good, after advice from motorsport photographers
# (sharp car with streaked background when panning, spinning wheels, room in the direction of travel,
# clean background, action moments, good light) and the typical misses of a burst (car not in the frame).
MOTORSPORT_QUESTIONS = {
    "ms_car": ("a race car on a race track", "an empty race track or a photo without a car"),
    "ms_panning": ("a panning shot of a race car: a sharp car and a motion-blurred, streaked background",
                   "a race car photo with a frozen, completely sharp background"),
    "ms_car_sharp": ("a race car in crisp sharp focus with readable sponsor decals", "a blurry, out-of-focus race car"),
    "ms_wheels": ("a racing car with spinning, motion-blurred wheels", "a racing car with frozen wheels that looks parked"),
    "ms_framing": ("a race car well framed with space in front of it in the direction of travel",
                   "a race car cut off at the edge of the frame"),
    "ms_action": ("dramatic racing action with sparks, smoke, a kerb strike, a wheel lift or an overtake",
                  "an uneventful car simply driving on a track"),
    "ms_background": ("a race car in front of a clean, calm background", "a race car in front of a cluttered, distracting background"),
    "ms_light": ("a race car in warm, dramatic golden-hour light", "a race car in flat, dull light"),
    # The most common misses in practice: something right in front of the car, or the car far too small
    "ms_clear_view": ("an unobstructed view of the race car", "a race car hidden behind a pole, fence, loudspeaker or crane"),
    "ms_car_size": ("a race car filling a large part of the frame", "a tiny race car far away in a large empty frame"),
}
QUESTIONS.update(MOTORSPORT_QUESTIONS)


def _norm(x: float, low: float, high: float) -> float:
    return float(min(1.0, max(0.0, (x - low) / (high - low))))


def compute_value(images: list[dict], config: dict) -> None:
    """Sets b["value"] (0..1) from the AI scores. Reference similarity is ranked within the folder."""
    weights = config["ai"]["value_weights"]
    sims = sorted(b["ai"]["reference"] for b in images if isinstance(b.get("ai"), dict) and "reference" in b["ai"])
    for b in images:
        r = b.get("ai") if isinstance(b.get("ai"), dict) else None
        if not r:
            b["value"] = None
            continue
        parts = {}
        if "aesthetic" in r:
            parts["aesthetic"] = _norm(r["aesthetic"], 4.3, 6.5)
        if "wallart" in r:
            parts["wallart"] = r["wallart"]
        if "reference" in r and len(sims) > 1:
            parts["reference"] = float(np.searchsorted(sims, r["reference"]) / (len(sims) - 1))
        if "content_value" in r:
            parts["content"] = (r["content_value"] + 1) / 2
        if "nima" in r:
            parts["nima"] = _norm(r["nima"], 4.0, 6.5)
        total = sum(weights.get(k, 0) for k in parts)
        b["value"] = sum(weights.get(k, 0) * v for k, v in parts.items()) / total if total else None
        b["value_parts"] = parts


def ai_reasons(b: dict) -> list[str]:
    r = b.get("ai") if isinstance(b.get("ai"), dict) else None
    if not r:
        return []
    reasons = []
    content = next((c for c in CONTENT if c[0] == r.get("content")), None)
    if content and r.get("content_confidence", 0) >= 0.3:
        reasons.append(f"Content: {content[1]}" + (" (little lasting value)" if content[3] < 0 else ""))
    if "aesthetic" in r:
        reasons.append(f"Aesthetics {r['aesthetic']:.1f}/10 (AI)")
    if "wallart" in r:
        reasons.append(f"Wall-art potential {r['wallart'] * 100:.0f}%")
    if r.get("content") == "motorsport" or r.get("ms_car", 0) >= 0.5:
        if r.get("ms_car", 1) < 0.25:
            reasons.append("No race car in the frame (AI)")
        elif r.get("ms_clear_view", 0.5) < 0.25:
            reasons.append("Something in front of the race car (AI)")
        elif r.get("ms_car_size", 0.5) < 0.25:
            reasons.append("Race car very small in the frame (AI)")
        elif r.get("ms_framing", 0.5) < 0.25:
            reasons.append("Race car cut off at the edge (AI)")
        if r.get("ms_panning", 0) >= 0.75 and r.get("ms_car_sharp", 0) >= 0.6:
            reasons.append("Good pan: sharp car, streaked background (AI)")
        if r.get("ms_action", 0) >= 0.75:
            reasons.append("Racing action (AI)")
    parts = b.get("value_parts") or {}
    if "reference" in parts:
        if parts["reference"] >= 0.8:
            reasons.append("Very close to your references")
        elif parts["reference"] <= 0.2:
            reasons.append("Far from your references")
    return reasons


AI_REASON_PREFIXES = ("Content:", "Aesthetics", "Wall-art potential", "Very close to", "Far from", "No race car",
                      "Race car cut off", "Good pan", "Racing action",
                      "Something in front", "Race car very small")


def available() -> tuple[bool, str]:
    import importlib.util

    if not all(importlib.util.find_spec(m) for m in ("torch", "open_clip")):
        return False, "AI packages missing: set them up in the app under \"Set up AI\" (until then: no AI)."
    return True, ""


# Measured on an RTX 4060 Ti: stacking images of the same size makes NIMA almost 3x faster with the same
# values, but makes MUSIQ slower. Metrics listed here are therefore run image by image.
IQA_ONE_BY_ONE = {"musiq"}


class Preload:
    """Loads the models in the background (about 12 s), e.g. while the classic analysis runs.
    Calling the object waits for them and returns the models."""

    def __init__(self, config: dict):
        self._config = config
        self._models: Models | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._load, name="ai-preload", daemon=True)
        self._thread.start()

    def _load(self) -> None:
        try:
            self._models = Models(self._config)
        except BaseException as e:  # reported when the models are actually needed
            self._error = e

    def __call__(self) -> Models:
        self._thread.join()
        if self._error is not None:
            raise self._error
        return self._models


def preload(config: dict) -> Preload | None:
    """Starts loading the models in the background when the AI is on and installed."""
    if not config["ai"].get("enabled", True) or not available()[0]:
        return None
    return Preload(config)


class Models:
    """Loads the models once and keeps them in (GPU) memory."""

    def __init__(self, config: dict):
        import open_clip
        import torch

        self.torch = torch
        a = config["ai"]
        wanted = a.get("device", "auto")
        self.device = ("cuda" if torch.cuda.is_available() else "cpu") if wanted == "auto" else wanted
        print(f"AI runs on: {'GPU (' + torch.cuda.get_device_name(0) + ')' if self.device == 'cuda' else 'CPU'}")
        self.clip, _, self.preprocess = open_clip.create_model_and_transforms(
            a["clip_model"], pretrained=a["clip_weights"], device=self.device
        )
        self.clip.eval()
        tokenizer = open_clip.get_tokenizer(a["clip_model"])
        with torch.no_grad():
            texts = [p for _, _, p, _ in CONTENT] + [t for pair in QUESTIONS.values() for t in pair]
            t = self.clip.encode_text(tokenizer(texts).to(self.device)).float()
            t = t / t.norm(dim=-1, keepdim=True)
        self.text_content = t[: len(CONTENT)]
        self.text_questions = t[len(CONTENT):]
        self.laion = self._load_laion()
        self.iqa = {}
        for name in a.get("pyiqa_models") or []:
            try:
                import pyiqa

                self.iqa[name] = pyiqa.create_metric(name, device=self.device)
            except Exception as e:  # optional: works without pyiqa as well
                print(f"  Note: {name} not available ({type(e).__name__}: {e}). Continuing without it.")

    def _load_laion(self) -> list[tuple[np.ndarray, np.ndarray]] | None:
        MODELS_DIR.mkdir(parents=True, exist_ok=True)
        path = MODELS_DIR / "laion_aesthetic_vit_l14.pth"
        if not path.exists():
            print("Downloading the LAION aesthetic model (once, about 4 MB) ...")
            try:
                urllib.request.urlretrieve(LAION_URL, path)
            except Exception as e:
                print(f"  Download failed ({e}). Skipping the aesthetic score.")
                return None
        state = self.torch.load(path, map_location="cpu", weights_only=True)
        # The model is a chain of linear layers (no activations): compute it directly in NumPy
        layers = []
        for i in sorted({int(k.split(".")[1]) for k in state if k.endswith(".weight")}):
            layers.append((state[f"layers.{i}.weight"].numpy().astype(np.float64),
                           state[f"layers.{i}.bias"].numpy().astype(np.float64)))
        return layers

    def _aesthetic(self, emb: np.ndarray) -> np.ndarray:
        x = emb.astype(np.float64)
        for w, b in self.laion:
            x = x @ w.T + b
        return x[:, 0]

    def batch(self, images: list[Image.Image]) -> list[tuple[dict, np.ndarray]]:
        torch = self.torch
        with torch.no_grad():
            batch = torch.stack([self.preprocess(b) for b in images]).to(self.device)
            if self.device == "cuda":
                with torch.autocast("cuda", dtype=torch.float16):
                    emb = self.clip.encode_image(batch)
            else:
                emb = self.clip.encode_image(batch)
            emb = emb.float()
            emb = emb / emb.norm(dim=-1, keepdim=True)
            content = (100 * emb @ self.text_content.T).softmax(dim=-1).cpu().numpy()
            answers = (100 * emb @ self.text_questions.T).cpu().numpy()
            emb_np = emb.cpu().numpy()
            iqa_scores = {}
            if self.iqa:
                tensors = [torch.from_numpy(np.asarray(b, dtype=np.float32) / 255).permute(2, 0, 1) for b in images]
                same_size: dict[tuple, list[int]] = {}
                for i, b in enumerate(images):
                    same_size.setdefault(b.size, []).append(i)
            for name, metric in self.iqa.items():
                try:
                    groups = [[i] for i in range(len(images))] if name in IQA_ONE_BY_ONE else list(same_size.values())
                    scores = [0.0] * len(images)
                    for group in groups:
                        out = metric(torch.stack([tensors[i] for i in group]).to(self.device))
                        for i, value in zip(group, out.flatten().tolist()):
                            scores[i] = float(value)
                    iqa_scores[name] = scores
                except Exception as e:
                    print(f"  {name} failed: {e}")
        aesthetic = self._aesthetic(emb_np) if self.laion else [None] * len(images)
        results = []
        for i in range(len(images)):
            r: dict = {}
            if aesthetic[i] is not None:
                r["aesthetic"] = round(float(aesthetic[i]), 3)
            for j, key in enumerate(QUESTIONS):
                yes, no = answers[i, 2 * j], answers[i, 2 * j + 1]
                r[key] = round(float(1 / (1 + np.exp(no - yes))), 3)
            p = content[i]
            best = int(p.argmax())
            r["content"] = CONTENT[best][0]
            r["content_confidence"] = round(float(p[best]), 3)
            r["content_value"] = round(float(sum(p[j] * CONTENT[j][3] for j in range(len(CONTENT)))), 3)
            for name, scores in iqa_scores.items():
                r[name] = round(scores[i], 3)
            results.append(r)
        return [(r, emb_np[i].astype(np.float16)) for i, r in enumerate(results)]


def _image_for_ai(photo_dir: Path, source: str, raw: set[str], edge: int) -> Image.Image | None:
    try:
        rgb, _, _ = loader.load_image(photo_dir / source, raw, edge)
        return Image.fromarray(rgb)
    except Exception:
        return None


def embed_references(get_models, folder: Path, config: dict) -> np.ndarray | None:
    """Embeddings of the reference images (cached as long as the folder does not change)."""
    if not folder.is_dir():
        return None
    extensions = set(config["files"]["image"]) | set(config["files"]["raw"])
    files = sorted(p for p in folder.rglob("*") if p.suffix.lower() in extensions)
    if not files:
        return None
    key = json.dumps([(p.name, p.stat().st_size, int(p.stat().st_mtime)) for p in files])
    cache = folder / ".references_cache.npz"
    if cache.exists():
        data = np.load(cache, allow_pickle=False)
        if str(data["key"]) == key:
            return data["emb"].astype(np.float32)
    print(f"Reading {len(files)} reference images ...")
    models = get_models()
    embs = []
    for i in range(0, len(files), 16):
        images = [_image_for_ai(p.parent, p.name, set(config["files"]["raw"]), 1024) for p in files[i : i + 16]]
        images = [b for b in images if b is not None]
        if images:
            embs.extend(e for _, e in models.batch(images))
    if not embs:
        return None
    emb = np.array(embs, dtype=np.float32)
    try:
        np.savez(cache, emb=emb, key=np.array(key))
    except OSError:
        pass
    return emb


def reference_similarity(emb: np.ndarray, references: np.ndarray, top: int = 5) -> np.ndarray:
    """Mean cosine similarity to the most similar reference images."""
    a = emb.astype(np.float32)
    a /= np.linalg.norm(a, axis=1, keepdims=True) + 1e-8
    v = references / (np.linalg.norm(references, axis=1, keepdims=True) + 1e-8)
    sim = a @ v.T
    k = min(top, v.shape[0])
    return np.sort(sim, axis=1)[:, -k:].mean(axis=1)


def references_folder(config: dict) -> Path:
    folder = Path(os.path.expandvars(config["ai"].get("references_folder") or "")).expanduser()
    return folder if folder.is_absolute() else USER_DIR / folder


def run(photo_dir: Path, config: dict, recompute: bool = False, models: Preload | None = None) -> dict:
    """Computes the AI scores for all images that do not have them yet (cached in the database).
    models: models already loading in the background (see preload), otherwise they are loaded here."""
    a = config["ai"]
    if not a.get("enabled", True):
        return {"ai": "off"}
    ok, hint = available()
    if not ok:
        print(hint)
        return {"ai": "missing"}
    photo_dir = Path(photo_dir).resolve()
    db = database.open_db(photo_dir)
    condition = "" if recompute else "AND (ai_version IS NULL OR ai_version != ?)"
    params = () if recompute else (AI_VERSION,)
    todo = db.execute(f"SELECT id, source FROM images WHERE status='active' AND error IS NULL {condition}", params).fetchall()

    ref_folder = references_folder(config) if a.get("references_folder") else None
    extensions = set(config["files"]["image"]) | set(config["files"]["raw"])
    has_references = bool(ref_folder and ref_folder.is_dir() and any(p.suffix.lower() in extensions for p in ref_folder.rglob("*")))
    if not todo and not has_references:
        db.close()
        return {"ai": "current", "new": 0}

    loaded: list[Models] = []

    def get_models() -> Models:
        if not loaded:
            loaded.append(models() if models is not None else Models(config))
        return loaded[0]

    raw = set(config["files"]["raw"])
    size = int(a.get("batch_size", 16))
    if todo:
        print(f"AI analysis of {len(todo)} images ...")
        parts = [todo[start : start + size] for start in range(0, len(todo), size)]
        with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as pool, tqdm(total=len(todo), desc="AI", unit="img") as bar:

            def load(part):
                return [pool.submit(_image_for_ai, photo_dir, r["source"], raw, 1024) for r in part]

            pending = load(parts[0])
            for n, part in enumerate(parts):
                images = [f.result() for f in pending]
                if n + 1 < len(parts):
                    pending = load(parts[n + 1])  # the next images load while the graphics card works
                valid = [(r, b) for r, b in zip(part, images) if b is not None]
                if valid:
                    for (r, _), (scores, emb) in zip(valid, get_models().batch([b for _, b in valid])):
                        db.execute("UPDATE images SET ai=?, embedding=?, ai_version=? WHERE id=?",
                                   (json.dumps(scores), emb.tobytes(), AI_VERSION, r["id"]))
                db.commit()
                bar.update(len(part))

    # Reference similarity for all images (cheap, changes with the references folder)
    references = embed_references(get_models, ref_folder, config) if has_references else None
    rows = db.execute("SELECT id, ai, embedding FROM images WHERE embedding IS NOT NULL").fetchall()
    if rows:
        sims = (
            reference_similarity(np.array([np.frombuffer(r["embedding"], np.float16) for r in rows]), references)
            if references is not None else [None] * len(rows)
        )
        for r, s in zip(rows, sims):
            scores = json.loads(r["ai"] or "{}")
            if s is None:
                scores.pop("reference", None)
            else:
                scores["reference"] = round(float(s), 4)
            db.execute("UPDATE images SET ai=? WHERE id=?", (json.dumps(scores), r["id"]))
        db.commit()
    db.close()
    return {"ai": "ok", "new": len(todo), "references": 0 if references is None else len(references)}
