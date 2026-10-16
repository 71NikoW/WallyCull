"""Overnight run: does everything that takes long, so the morning starts with a better model.

For every photo folder you used in the app:
1. analyze new photos at full resolution, and the AI scores (sets up the AI first if it is missing)
2. read star ratings you gave in the camera or in Lightroom (training data without extra work)
3. thorough training: tries several model sizes and picks the one that predicts your verdicts best,
   and spreads your verdicts to similar photos (label spreading on the CLIP features)
4. classify everything again and write the report
The PC is kept awake while it runs. The result is saved in %LOCALAPPDATA%\\WallyCull\\night.json.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

from . import ai, analyze, classify, learning, numbers, report, signals
from .paths import USER_DIR

SUMMARY = USER_DIR / "night.json"


def keep_awake(on: bool) -> None:
    """Stops Windows from going to sleep while the run is busy (the screen may still turn off)."""
    if sys.platform != "win32":
        return
    import ctypes

    ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0))


def recent_folders() -> list[Path]:
    try:
        state = json.loads((USER_DIR / "app.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    folders = state.get("recent") or state.get("zuletzt") or []
    return [Path(f) for f in folders if Path(f).is_dir()]


def _ensure_ai(config: dict) -> None:
    ok, _ = ai.available()
    if ok or not config["ai"].get("enabled", True):
        return
    print("STEP Setting up the AI first ...", flush=True)
    import importlib

    from . import ai_setup, paths

    if ai_setup.install() == 0:
        paths.setup()
        importlib.invalidate_caches()


def _hint(result: dict, config: dict) -> str:
    m = result["model"]
    c = config["learning"]
    if m.get("active"):
        return ""
    missing = max(0, c["min_ratings"] - m.get("rated", 0))
    return (f"Your model needs {missing} more verdicts to start (at least {c['min_per_side']} keep and reject each). "
            "Rate photos in the app, or give stars in the camera or in Lightroom.")


def run_folder(folder: Path, config: dict, models: ai.Preload | None = None) -> dict:
    started = time.time()
    print(f"STEP {folder}", flush=True)
    result: dict = {"folder": str(folder)}
    a = analyze.run(folder, config)
    result["analyzed"] = a["analyzed"]
    result["errors"] = a["errors"]
    try:
        r = ai.run(folder, config, models=models)
        result["ai_new"] = r.get("new", 0)
        result["ai"] = r.get("ai")
    except Exception as e:  # AI problems must not stop the rest of the night
        print(f"AI analysis skipped: {type(e).__name__}: {e}")
        result["ai"] = "error"
    result["stars"] = signals.harvest(folder, config)
    print("Stars from camera/Lightroom: " + ", ".join(f"{k} {v}" for k, v in result["stars"].items()), flush=True)
    before = (learning.load_model(folder) or {}).get("accuracy")
    print("Training thoroughly ...", flush=True)
    model = learning.train(folder, config, thorough=True)
    result["model"] = {k: v for k, v in model.items() if k in ("active", "rated", "keep", "reject", "accuracy",
                                                                 "sources", "style_dims", "blend")}
    result["model"]["accuracy_before"] = before
    if model.get("active"):
        print(f"Model trained from {model['rated']} verdicts, accuracy {model['accuracy'] * 100:.0f}%.", flush=True)
    result["counts"] = classify.run(folder, config)
    try:
        result["numbers"] = numbers.run(folder, config)
    except Exception as e:  # start numbers are a bonus
        print(f"Start numbers skipped: {type(e).__name__}: {e}")
    report.create(folder)
    result["hint"] = _hint(result, config)
    result["minutes"] = round((time.time() - started) / 60, 1)
    return result


def run(config: dict, folders: list[Path] | None = None) -> dict:
    folders = folders or recent_folders()
    summary = {"started": datetime.now().isoformat(timespec="seconds"), "finished": None, "folders": []}
    if not folders:
        print("No photo folder yet: choose one in the app first.")
        return summary
    keep_awake(True)
    try:
        _ensure_ai(config)
        models = ai.preload(config)  # loaded once for all folders, in the background
        for folder in folders:
            try:
                summary["folders"].append(run_folder(folder, config, models))
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"Error in {folder}: {type(e).__name__}: {e}", flush=True)
                summary["folders"].append({"folder": str(folder), "error": str(e)[:300]})
            USER_DIR.mkdir(parents=True, exist_ok=True)
            SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        keep_awake(False)
    summary["finished"] = datetime.now().isoformat(timespec="seconds")
    SUMMARY.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("STEP Overnight run finished.", flush=True)
    return summary


def last_summary() -> dict | None:
    try:
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
