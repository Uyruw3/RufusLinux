"""Locate bundled resources both in a source checkout and in an install."""

from __future__ import annotations

import os
from pathlib import Path


def resource_dir() -> Path:
    override = os.environ.get("RUFUS_LINUX_RESOURCES")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parent.parent / "resources",
        Path("/usr/local/share/rufus-linux"),
        Path("/usr/share/rufus-linux"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def resource_path(name: str) -> str:
    return str(resource_dir() / name)
