"""Where WallyCull keeps its files.

Program (replaced by every update):  %LOCALAPPDATA%\\Programs\\WallyCull
Your data (always kept):             %LOCALAPPDATA%\\WallyCull  (settings, AI packages, models, references)
Measurements and ratings live next to the photos, in <photo folder>\\.wallycull.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROGRAM_DIR = Path(__file__).resolve().parent.parent
if os.environ.get("WALLYCULL_DATA"):
    USER_DIR = Path(os.environ["WALLYCULL_DATA"])
elif sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
    USER_DIR = Path(os.environ["LOCALAPPDATA"]) / "WallyCull"
else:
    USER_DIR = Path.home() / ".wallycull"
AI_PACKAGES_DIR = USER_DIR / "ai-packages"
OCR_PACKAGES_DIR = USER_DIR / "ocr-packages"  # start number recognition
MODELS_DIR = USER_DIR / "models"
REFERENCES_DIR = USER_DIR / "references"
# Installed by the setup: bundled Python inside the program folder
INSTALLED = (PROGRAM_DIR / "python" / "python.exe").exists()


# Folder names used by the German versions up to 0.6
_LEGACY_NAMES = {"ki-pakete": "ai-packages", "modelle": "models", "vorbilder": "references"}


def _migrate_legacy() -> None:
    """Renames user folders from the German versions, so AI packages and models need no new download."""
    for old, new in _LEGACY_NAMES.items():
        try:
            if (USER_DIR / old).is_dir() and not (USER_DIR / new).exists():
                (USER_DIR / old).rename(USER_DIR / new)
        except OSError:
            pass
    marker = AI_PACKAGES_DIR / ".wallycull-ki.json"
    if marker.exists() and not (AI_PACKAGES_DIR / ".wallycull-ai.json").exists():
        try:
            import json

            from . import ai_setup

            d = json.loads(marker.read_text(encoding="utf-8"))
            if d.get("python") == ai_setup.wanted(d["cuda"])["python"]:
                ai_setup.MARKER.write_text(json.dumps(ai_setup.wanted(d["cuda"])), encoding="utf-8")
        except (OSError, ValueError, KeyError, ImportError):
            pass


def setup() -> None:
    """Make the AI packages (kept apart from the program so updates stay small) and the model cache known."""
    if USER_DIR.is_dir():
        _migrate_legacy()
    for folder in (AI_PACKAGES_DIR, OCR_PACKAGES_DIR):
        if folder.is_dir() and str(folder) not in sys.path:
            sys.path.append(str(folder))  # appended: the bundled packages take precedence
    os.environ.setdefault("HF_HOME", str(MODELS_DIR / "huggingface"))
    os.environ.setdefault("TORCH_HOME", str(MODELS_DIR / "torch"))
