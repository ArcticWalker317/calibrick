"""Part ladders: calibrate any fit feature on REAL part geometry.

The coupon kit covers studs, walls, tubes and sockets. Everything else (bars, clips,
Technic pin holes, axle holes, hollow-stud bores ...) is calibrated the same way but on an
actual part: build one part N times with a single profile field stepped, print the row,
rate each copy against a genuine mating part, and feed the ratings back.

    calibrick calibrate ladder --part 15712 --field clip_d --center 3.25 --step 0.05 --profile P
    calibrick calibrate apply ladder.json "5 5 4 3 3 2 1 1 1" --profile P

Each copy is tied by a thin breakaway strip to a numbered tab so identical-looking parts
cannot get mixed up; snap the strip off after rating.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import numpy as np

from . import csg
from .calib import LadderFit, fit_ladder, parse_ratings
from .coupons import ladder as make_ladder
from .printers import get_printer
from .profile import Profile

# +1: a larger value makes the fit tighter; -1: a larger value makes it looser
DIRECTION = {
    "stud_d": +1, "tube_od": +1, "post_d": +1, "pin_d": +1, "bar_d": +1,
    "cavity_gap": -1, "socket_d": -1, "pin_hole_d": -1, "bar_hole_d": -1, "clip_d": -1,
    "hole_grow": -1, "axle_hole_grow": -1, "axle_shrink": -1, "boss_shrink": -1,
    "ext_clearance": -1,
}

# Suggested ladders: (field, part, centre, step, what to test it with)
SUGGESTED = [
    ("bar_d", "60478", 3.15, 0.05, "genuine clip (e.g. tile with clip, minifig hand) on the handle"),
    ("clip_d", "15712", 3.25, 0.05, "genuine 3.18 mm bar (or a calibrated printed handle) in the clip"),
    ("bar_hole_d", "85861", 3.30, 0.05, "genuine bar in the open stud"),
    ("pin_hole_d", "3700", 4.95, 0.05, "genuine Technic pin in the hole (this hole prints sideways)"),
]


def build_part_ladder(part_id: str, field: str, values: list[float], profile: Profile,
                      out_dir: str | Path, *, lib=None, gap: float = 5.0) -> dict:
    """Write one pre-arranged STL with a copy of `part_id` per value, plus ladder.json."""
    from .engine import build_mesh
    from .ldraw import LDrawLibrary, expand_part

    if field not in DIRECTION:
        raise ValueError(f"cannot ladder {field!r}; choose one of: {', '.join(sorted(DIRECTION))}")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    raw = expand_part(lib or LDrawLibrary(), part_id)
    builds = [build_mesh(raw, dataclasses.replace(profile, **{field: float(v)})) for v in values]
    sx = max(float(np.ptp(b.tris[:, :, 0])) for b in builds)
    sy = max(float(np.ptp(b.tris[:, :, 1])) for b in builds)
    pitch = max(sx + gap, 9.0)
    printer = get_printer(profile.printer)
    per_row = max(1, int((printer.bed[0] - 2 * printer.margin - 8.0) // pitch))
    row_pitch = sy + 2.0 + 8.4 + 2.0 + gap       # part + gap + tab + rail + gap
    n_rows = -(-len(values) // per_row)
    if n_rows * row_pitch > printer.bed[1] - 2 * printer.margin:
        raise ValueError(f"{len(values)} copies of this part do not fit on one plate; use fewer steps")
    tab_y0 = sy / 2.0 + 2.0                      # tabs sit behind the parts
    tab_y1 = tab_y0 + 8.4
    tris, solids = [], []
    for i, b in enumerate(builds):
        row, col = divmod(i, per_row)
        cx, cy = col * pitch, -row * row_pitch
        tris.append(b.tris + np.array([cx, cy, 0.0]))
        tab = csg.box(cx - 3.4, cy + tab_y0, 0.0, cx + 3.4, cy + tab_y1, 0.64)
        tab = tab - csg.pixel_text(str(i + 1), cx, cy + (tab_y0 + tab_y1) / 2.0, 0.32, 1.0, px=0.9,
                                   underline=True)
        solids.append(tab)
        # breakaway strip from the tab into the back edge of the part (2 layers thick)
        solids.append(csg.box(cx - 0.4, cy + sy / 2.0 - 0.35, 0.0, cx + 0.4, cy + tab_y0 + 0.2, 0.32))
    for row in range(n_rows):                    # one rail per row keeps the tabs in order
        n_in_row = min(per_row, len(values) - row * per_row)
        cy = -row * row_pitch
        solids.append(csg.box(-3.4, cy + tab_y1 - 0.1, 0.0, (n_in_row - 1) * pitch + 3.4,
                              cy + tab_y1 + 2.0, 0.64))
    tris.append(csg.to_tris(csg.union(solids)))
    allt = np.concatenate(tris)
    lo, hi = allt.reshape(-1, 3).min(axis=0), allt.reshape(-1, 3).max(axis=0)
    allt = allt - np.array([(lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0, 0.0])
    name = f"calibrick_ladder_{part_id}_{field}.stl"
    csg.write_stl(out / name, allt)
    info = {
        "part": part_id, "description": raw.meta.get("description", ""), "field": field,
        "values": [float(v) for v in values], "direction": DIRECTION[field],
        "profile": profile.name, "file": name, "tiers": [b.tier for b in builds],
        "footprint": [round(float(hi[0] - lo[0]), 1), round(float(hi[1] - lo[1]), 1)],
        "responds": len({round(b.volume, 2) for b in builds}) > 1,
    }
    (out / f"ladder_{part_id}_{field}.json").write_text(json.dumps(info, indent=2))
    return info


def default_values(field: str, profile: Profile, center: float | None = None, step: float = 0.05,
                   n: int = 9) -> list[float]:
    c = float(getattr(profile, field)) if center is None else center
    return make_ladder(c, step, n)


def apply_ladder(info: dict, ratings_text: str, profile: Profile) -> tuple[Profile, LadderFit]:
    """Set profile.<field> from ratings (same 1-5 scale as the coupon kit)."""
    fit = fit_ladder(info["field"], info["values"], parse_ratings(ratings_text), int(info["direction"]))
    if fit.value is None or fit.status in ("all_loose", "all_tight", "no_data"):
        return profile, fit
    new = dataclasses.replace(profile, **{info["field"]: fit.value})
    new.history = list(profile.history) + [{"ladder": {k: info[k] for k in ("part", "field", "values")},
                                           "ratings": ratings_text, "fit": [fit.value, fit.status]}]
    return new, fit
