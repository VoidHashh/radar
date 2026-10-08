"""Carga de config.yaml y keywords.yaml."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    path = Path(path) if path else ROOT / "config.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    base = path.resolve().parent
    for key in ("db_path", "cache_dir", "log_path"):
        if key in cfg:
            cfg[key] = str((base / cfg[key]).resolve())
    if "handoff" in cfg:
        cfg["handoff"]["dir"] = str((base / cfg["handoff"]["dir"]).resolve())
    cfg["_base_dir"] = str(base)
    return cfg


def load_keywords(path: str | Path | None = None) -> dict[str, list[str]]:
    path = Path(path) if path else ROOT / "keywords.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {str(k): [str(p) for p in v] for k, v in data.items()}
