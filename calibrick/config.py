"""Paths and environment configuration.

Everything the tool downloads or generates lives under one data directory:

    ~/.calibrick/            (override with CALIBRICK_DATA)
        ldraw/               LDraw parts library   (override with CALIBRICK_LDRAW)
        rebrickable/         Rebrickable CSV dumps (override with CALIBRICK_REBRICKABLE)
        profiles/            calibration profiles (*.json)
        cache/               generated part meshes, keyed by profile hash
"""
from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    p = Path(os.environ.get("CALIBRICK_DATA") or (Path.home() / ".calibrick"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sub(env: str, name: str, create: bool = True) -> Path:
    v = os.environ.get(env)
    p = Path(v) if v else data_dir() / name
    if create and not v:
        p.mkdir(parents=True, exist_ok=True)
    return p


def ldraw_dir() -> Path:
    """Root of the LDraw library (the folder that contains ``parts/`` and ``p/``)."""
    return _sub("CALIBRICK_LDRAW", "ldraw")


def rebrickable_dir() -> Path:
    return _sub("CALIBRICK_REBRICKABLE", "rebrickable")


def profiles_dir() -> Path:
    return _sub("CALIBRICK_PROFILES", "profiles")


def cache_dir() -> Path:
    return _sub("CALIBRICK_CACHE", "cache")
