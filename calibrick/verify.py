"""Verification kit: real parts generated with a profile, laid out on one plate.

The coupons find the numbers; this checks them on actual parts before a whole set is
printed. See "Second pass" in docs/CALIBRATION.md for what to test.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import csg
from .engine import build_part
from .printers import get_printer
from .profile import Profile

# (LDraw id, copies, why it is in the kit)
VERIFY_PARTS: list[tuple[str, int, str]] = [
    ("3001", 2, "brick 2x4: studs, tubes, tall walls, stacking height"),
    ("3003", 1, "brick 2x2"),
    ("3005", 2, "brick 1x1: wall-to-wall grip on tall walls"),
    ("3022", 2, "plate 2x2: tube + walls in a plate"),
    ("3023b", 3, "plate 1x2: wall-to-wall grip, underside post"),
    ("3024", 4, "plate 1x1"),
    ("6141", 3, "round plate 1x1: ring socket"),
    ("3070b", 3, "tile 1x1: seats flush, groove"),
    ("3069b", 2, "tile 1x2"),
    ("87079", 1, "tile 2x4: long exterior, tubes"),
    ("3710", 1, "plate 1x4: pitch over 4 studs"),
    ("3020", 1, "plate 2x4"),
]


def build_verification_kit(profile: Profile, out_dir: str | Path, *, parts=None, lib=None,
                           spacing: float = 5.0) -> dict:
    """Build the parts and one combined, pre-arranged STL. Returns a summary dict."""
    out = Path(out_dir)
    printer = get_printer(profile.printer)
    usable_x = printer.bed[0] - 2 * printer.margin - 10.0
    usable_y = printer.bed[1] - 2 * printer.margin - 10.0
    results, placed = [], []
    x = y = row_h = 0.0
    for pid, copies, why in (parts or VERIFY_PARTS):
        r = build_part(pid, profile, out / "parts", lib=lib)
        results.append({"part": pid, "copies": copies, "why": why, "ok": r.ok, "tier": r.tier,
                        "size": list(r.size), "warnings": r.warnings})
        if not r.ok:
            continue
        tris = csg.read_stl(r.mesh_path)
        sx, sy = r.size[0], r.size[1]
        for _ in range(copies):
            if x + sx > usable_x:
                x, y, row_h = 0.0, y + row_h + spacing, 0.0
            if y + sy > usable_y:
                raise RuntimeError("verification kit does not fit on one plate; pass fewer parts")
            placed.append(tris + np.array([x + sx / 2.0, y + sy / 2.0, 0.0]))
            x += sx + spacing
            row_h = max(row_h, sy)
    if not placed:
        raise RuntimeError("no verification part could be built")
    allt = np.concatenate(placed)
    lo, hi = allt.reshape(-1, 3).min(axis=0), allt.reshape(-1, 3).max(axis=0)
    allt = allt - np.array([(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, 0.0])
    name = f"calibrick_verify_{profile.name}.stl".replace(" ", "_")
    stl = csg.write_stl(out / name, allt)
    summary = {"profile": profile.name, "printer": profile.printer, "file": stl.name,
               "footprint": [round(float(hi[0] - lo[0]), 1), round(float(hi[1] - lo[1]), 1)],
               "parts": results}
    (out / "verify.json").write_text(json.dumps(summary, indent=2))
    return summary
