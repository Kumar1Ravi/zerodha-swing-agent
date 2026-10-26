"""Application configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8-sig") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError("configuration root must be a mapping")
    if config.get("app", {}).get("mode") != "paper":
        raise ValueError("only app.mode=paper is supported")
    return config
