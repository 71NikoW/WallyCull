"""Sorts images into Highlight / Keep / Review / Reject, with a reason you can follow.

Principle: quality is not the same as value.
    high value + technically good -> highlight
    high value + technically poor -> keep
    low value  + technically good -> review
    low value  + technically poor -> reject
Without a value score (no AI data yet) only duplicates and clear failures are rejected;
anything borderline goes to "review".
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath

from . import ai, database, duplicates, learning

CATEGORIES = ("highlight", "keep", "review", "reject")
NAMES = {"highlight": "Highlight", "keep": "Keep", "review": "Review", "reject": "Reject"}

SCORE_NAMES = {"sharpness": "sharpness", "exposure": "exposure", "noise": "noise",
               "shake": "camera shake", "resolution": "resolution"}


def _name(image: dict) -> str:
    return PurePosixPath(image["source"]).name


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def technical_reasons(b: dict, config: dict) -> list[str]:
    q, m = config["quality"], b.get("metrics") or {}
    scores = m.get("scores") or {}
    reasons = []
    for failure in m.get("failed", []):
        reasons.append(f"Failed shot: {failure}")
    subject = {"face": "Face", "marked": "Marked subject"}.get(m.get("subject"), "Subject")
    if scores.get("sharpness", 1) < 0.5:
        reasons.append(f"{subject} blurry (sharpness {m['sharpness']:.0f}, sharp from {q['sharpness_sharp']})")
    elif scores.get("sharpness", 0) >= 0.9:
        reasons.append(f"{subject} sharp")
    if "score_sharpness_100" in m:
        if m["score_sharpness_100"] < 0.5:
            reasons.append(f"Fine detail too soft for large prints (edge width {m['edge_width_100']:.1f} px)")
        elif m["score_sharpness_100"] >= 0.9:
            reasons.append(f"Crisp fine detail (edge width {m['edge_width_100']:.1f} px)")
    if m.get("shaky"):
        angle = m["shake_angle"] % 180
        direction = "horizontal" if angle < 22.5 or angle > 157.5 else "vertical" if 67.5 < angle < 112.5 else "diagonal"
        reasons.append(f"Camera shake ({direction} motion blur)")
    if m.get("score_over", 1) < 0.6:
        reasons.append(f"Overexposed: {_pct(m['clipped_highlights'])} blown out")
    if m.get("score_under", 1) < 0.6:
        reasons.append(f"Underexposed: {_pct(m['crushed_shadows'])} crushed")
    if m.get("night"):
        reasons.append("Night shot detected: darkness is not penalized")
    if scores.get("noise", 1) < 0.5:
        reasons.append(f"Heavy noise ({m['noise']:.1f})")
    if scores:
        weakest = min(scores, key=scores.get)
        if scores[weakest] < 0.8 and not any(r.startswith(("Failed shot", "Camera shake")) for r in reasons):
            reasons.append(f"Weakest point: {SCORE_NAMES.get(weakest, weakest)} (score {scores[weakest]:.2f})")
    if "megapixels" in m:
        if m["megapixels"] < q["wallart_min_megapixels"]:
            reasons.append(f"Too small for large wall art ({m['megapixels']} MP)")
        else:
            w, h = m["print_cm"]
            reasons.append(f"Wall art up to about {w} × {h} cm ({q['print_dpi']} dpi)")
    return reasons


def _min_score(b: dict) -> float:
    return min(((b.get("metrics") or {}).get("scores") or {"x": 0}).values())


def decide(images: list[dict], config: dict, model: dict | None = None) -> list[dict]:
    """Sets category, reasons, group_id, group_type and group_rank for every image.

    With a trained model your learned taste (personal) decides, otherwise the technical quality."""
    c, d, lc = config["classification"], config["duplicates"], config["learning"]
    shift = (float(c["strictness"]) - 0.5) * 0.2
    review_below = c["review_below"] + shift
    reject_below = c["reject_below"] + shift / 2
    top_share = float(c.get("highlight_top_share", 1.0))
    flaw_below = c.get("flaw_below", 0.5)

    ai.compute_value(images, config)  # value score from the AI scores (None without AI data)
    combined = sorted(((b["quality"] or 0) + b["value"]) / 2 for b in images if b.get("value") is not None)
    combined_from = combined[min(len(combined) - 1, int(len(combined) * (1 - top_share)))] if combined else 1.0
    for b in images:
        b.update(group_id=None, group_type=None, group_rank=None, category=None, reasons=[])
        if not model:
            b["personal"] = None
        # Ranking within bursts: your learned taste, otherwise the technical quality
        b["rank"] = b["personal"] if b.get("personal") is not None else (b["quality"] or 0)
    personal = sorted((b["personal"] for b in images if b.get("personal") is not None), reverse=True)
    highlight_personal = max(lc["highlight_from"], personal[max(0, int(len(personal) * top_share) - 1)]) if personal else 1.0

    # Highlights are relative: only the best X % of all images qualify
    qualities = sorted((b["quality"] for b in images if b.get("quality") is not None), reverse=True)
    highlight_from = c["highlight_from"]
    if qualities:
        highlight_from = max(highlight_from, qualities[max(0, int(len(qualities) * top_share) - 1)])

    for number, group in enumerate(duplicates.find_groups(images, config), 1):
        best = group["members"][0]
        size = len(group["members"])
        kind = "duplicate group" if group["type"] == "duplicate" else "burst"
        for rank, b in enumerate(group["members"], 1):
            b.update(group_id=number, group_type=group["type"], group_rank=rank)
            if rank == 1:
                b["reasons"].append(f"Best image of a {kind} of {size}")
                continue
            if b.get("personal") is not None:
                really_good = b["personal"] >= lc["burst_good_from"]
            else:
                really_good = (b["quality"] or 0) >= d["burst_good_from"] and _min_score(b) >= 0.5
            if group["type"] == "burst" and rank <= d["burst_keep"] and really_good:
                b["reasons"].append(f"Another very good burst shot (#{rank} of {size})")
                continue  # classified normally below
            gap = best["rank"] - b["rank"]
            if group["type"] == "duplicate":
                b["category"] = "reject"
                b["reasons"].append(f"Duplicate of {_name(best)} (which is as good or better)")
            elif gap <= d["burst_tolerance"]:
                b["category"] = "review"
                b["reasons"].append(f"Burst shot almost as good as {_name(best)}: your call")
            else:
                b["category"] = "reject"
                b["reasons"].append(
                    f"Burst shot #{rank} of {size}: {_name(best)} is better "
                    f"({'prediction' if b.get('personal') is not None else 'quality'} {best['rank']:.2f} vs {b['rank']:.2f})"
                )

    for b in images:
        if b.get("error"):
            b["category"] = "review"
            b["reasons"].insert(0, f"Could not be analyzed ({b['error']}): please look at it yourself")
            continue
        q = b["quality"] or 0
        for note in b.get("annotations") or []:
            text = database.ANNOTATION_KINDS.get(note["kind"], note["kind"])
            b["reasons"].append(f"Your note: {text}" + (f" – {note['text']}" if note.get("text") else ""))
        b["reasons"].extend(ai.ai_reasons(b))
        b["reasons"].extend(technical_reasons(b, config))
        if b.get("user_choice"):
            b["category"] = b["user_choice"]
            b["reasons"].insert(0, "Your decision (rated or restored by you)")
            continue
        p = b.get("personal")
        if p is not None and model:
            pro, con = learning.explain(model, learning.features(b, model.get("pca")))
            text = f"Your model: {p * 100:.0f}% keep"
            if con and p < 0.5:
                text += " (against: " + ", ".join(con) + ")"
            elif pro and p >= 0.5:
                text += " (for: " + ", ".join(pro) + ")"
            b["reasons"].insert(0, text)
        if b["category"]:
            continue  # already decided as duplicate/burst
        value = b.get("value")
        if p is not None and model:
            # Your trained model knows your taste (and already uses the AI scores): it decides
            if (b.get("metrics") or {}).get("failed"):
                b["category"] = "reject"
            elif p < lc["reject_below"]:
                b["category"] = "reject"
            elif p < lc["review_below"]:
                b["category"] = "review"
            elif p >= highlight_personal:
                b["category"] = "highlight"
            else:
                b["category"] = "keep"
        elif value is None:
            if q < reject_below:
                b["category"] = "reject"
            elif q < review_below:
                b["category"] = "review"
                b["reasons"].insert(0, "Technically weak, value unknown: your call")
            elif _min_score(b) < flaw_below:
                flaws = ", ".join(SCORE_NAMES.get(k, k) for k, v in b["metrics"]["scores"].items() if v < flaw_below)
                b["category"] = "review"
                b["reasons"].insert(0, f"Clear flaw ({flaws}), value unknown: your call")
            elif q >= highlight_from:
                b["category"] = "highlight"
                b["reasons"].insert(0, "Technically one of the best images (highlight candidate)")
            else:
                b["category"] = "keep"
        else:
            high, good = value >= c["value_high_from"], q >= review_below
            b["category"] = {
                (True, True): "highlight",
                (True, False): "keep",
                (False, True): "review",
                (False, False): "reject",
            }[(high, good)]
            text = f"value {value:.2f}, quality {q:.2f}"
            if high and not good:
                b["reasons"].insert(0, f"Kept despite technical flaws: high value ({text})")
            elif not high and good:
                b["reasons"].insert(0, f"Technically fine but low value ({text}): your call")
            elif not high and not good:
                b["reasons"].insert(0, f"Low value and technically weak ({text})")
            # Highlight only without a clear flaw and among the best images (quality + value)
            if b["category"] == "highlight":
                if (q + value) / 2 < combined_from or _min_score(b) < flaw_below:
                    b["category"] = "keep"
                else:
                    b["reasons"].insert(0, f"One of the best images ({text})")
    return images


def run(photo_dir, config: dict) -> dict:
    """Classifies all active images again and stores the result. Does not touch any files."""
    db = database.open_db(photo_dir)
    images = [b for b in database.active_images(db) if b.get("version")]
    annotations = database.annotations_by_image(db)
    for b in images:
        b["annotations"] = annotations.get(b["id"], [])
    images = decide(images, config, learning.load_model(photo_dir))
    for b in images:
        db.execute(
            "UPDATE images SET category=?, reasons=?, group_id=?, group_type=?, group_rank=?, value=? WHERE id=?",
            (b["category"], json.dumps(b["reasons"], ensure_ascii=False), b["group_id"], b["group_type"],
             b["group_rank"], b.get("value"), b["id"]),
        )
    db.commit()
    database.backup(db, photo_dir)
    db.close()
    counts = {k: 0 for k in CATEGORIES}
    for b in images:
        counts[b["category"]] += 1
    return counts
