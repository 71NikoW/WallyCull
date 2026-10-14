"""Sets up the AI: PyTorch (with NVIDIA support when available), CLIP, pyiqa and the models.

The setup runs this automatically (python -m wallycull setup-ai); it can be repeated from the app.
The packages go to %LOCALAPPDATA%\\WallyCull\\ai-packages, apart from the program, so updates stay small.
Lines starting with "STEP " are shown by the setup as a heading.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .paths import AI_PACKAGES_DIR, PROGRAM_DIR

INDEX_CUDA = "https://download.pytorch.org/whl/cu126"
INDEX_CPU = "https://download.pytorch.org/whl/cpu"
MARKER = AI_PACKAGES_DIR / ".wallycull-ai.json"


def _say(text: str) -> None:
    print(text, flush=True)


def has_nvidia() -> bool:
    if shutil.which("nvidia-smi"):
        return True
    return Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "nvidia-smi.exe").exists()


def wanted(cuda: bool) -> dict:
    """What the installed AI packages must match (comments in the requirements file do not count)."""
    lines = (PROGRAM_DIR / "requirements-ai.txt").read_text(encoding="utf-8").splitlines()
    requirements = "\n".join(x.strip() for x in lines if x.strip() and not x.strip().startswith("#"))
    return {"requirements": hashlib.sha256(requirements.encode()).hexdigest()[:16], "cuda": cuda,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}"}


def _pip(target: Path, packages: list[str], index: str) -> int:
    command = [sys.executable, "-m", "pip", "install", "--target", str(target), "--upgrade", "--no-warn-script-location",
               "--disable-pip-version-check", "--progress-bar", "off", "--extra-index-url", index, *packages]
    return subprocess.run(command).returncode


def install(force: bool = False) -> int:
    if os.environ.get("WALLYCULL_SKIP_AI"):  # test install while building the setup
        _say("STEP AI setup skipped.")
        return 0
    cuda = has_nvidia()
    target = wanted(cuda)
    try:
        current = json.loads(MARKER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    if current == target and not force:
        _say("STEP AI packages are already set up.")
    else:
        _say(f"STEP 1/2: Downloading AI packages ({'for your NVIDIA graphics card, ' if cuda else ''}about 3 GB) ...")
        new = AI_PACKAGES_DIR.with_name("ai-packages.new")
        shutil.rmtree(new, ignore_errors=True)
        requirements = ["-r", str(PROGRAM_DIR / "requirements-ai.txt")]
        index = INDEX_CUDA if cuda else INDEX_CPU
        # Everything in one go, otherwise pip fetches a second (CPU) PyTorch for pyiqa
        code = _pip(new, ["torch", "torchvision", *requirements, "pyiqa"], index)
        if code:
            _say("Note: did not work with MUSIQ/NIMA (pyiqa), trying without ...")
            shutil.rmtree(new, ignore_errors=True)
            code = _pip(new, ["torch", "torchvision", *requirements], index)
        if code:
            shutil.rmtree(new, ignore_errors=True)
            _say("ERROR: The AI packages could not be installed (internet connection?).")
            return 1
        shutil.rmtree(AI_PACKAGES_DIR, ignore_errors=True)
        new.rename(AI_PACKAGES_DIR)
        MARKER.write_text(json.dumps(target), encoding="utf-8")
    _say("STEP 2/2: Downloading AI models (once, about 2 GB) ...")
    # New process so the packages that were just installed are found for sure
    code = subprocess.run([sys.executable, "-u", "-m", "wallycull", "download-models"]).returncode
    if code:
        _say("ERROR: The AI models could not be downloaded. The next analysis tries again.")
        return 2
    _say("STEP AI is ready.")
    return 0


def download_models(config: dict) -> None:
    from . import ai

    models = ai.Models(config)
    _say(f"Models loaded: CLIP, LAION aesthetics{', ' + ', '.join(models.iqa) if models.iqa else ''}.")
