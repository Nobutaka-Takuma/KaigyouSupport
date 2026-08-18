"""Configuration loading.

Both YAML files under ``config/`` are reloaded when their mtime changes, so
editing the scoring weights takes effect without restarting the API.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

_LOCK = threading.Lock()
_CACHE: dict[Path, tuple[float, dict[str, Any]]] = {}


def repo_root() -> Path:
    env = os.getenv("KAIGYOU_ROOT")
    if env:
        return Path(env).resolve()
    # server/kaigyou_core/config.py -> repo root
    return Path(__file__).resolve().parents[2]


def config_dir() -> Path:
    return Path(os.getenv("KAIGYOU_CONFIG_DIR") or (repo_root() / "config"))


def data_dir() -> Path:
    d = Path(os.getenv("KAIGYOU_DATA_DIR") or (repo_root() / "data"))
    return d


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file, re-reading it only when it has changed on disk."""
    path = path.resolve()
    mtime = path.stat().st_mtime
    with _LOCK:
        cached = _CACHE.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    with _LOCK:
        _CACHE[path] = (mtime, data)
    return data


def scoring_config() -> dict[str, Any]:
    return load_yaml(config_dir() / "scoring.yaml")


def sources_config() -> dict[str, Any]:
    return load_yaml(config_dir() / "sources.yaml")
