"""Loads config.yaml (defaults) and your own overrides as a nested dict."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

from .paths import PROGRAM_DIR, USER_DIR

DEFAULT_CONFIG = PROGRAM_DIR / "config.yaml"
# Your overrides live with your data, so updates never overwrite them.
USER_CONFIG = USER_DIR / "settings.yaml"


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def load(path: str | Path | None = None) -> dict:
    """Default settings, overlaid with settings.yaml from your data folder and an optional extra file."""
    with open(DEFAULT_CONFIG, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    for extra in (USER_CONFIG if USER_CONFIG.exists() else None, path):
        if extra:
            with open(extra, encoding="utf-8") as f:
                config = _merge(config, yaml.safe_load(f) or {})
    for group in ("raw", "image", "sidecar"):
        config["files"][group] = [e.lower() for e in config["files"][group]]
    return config
