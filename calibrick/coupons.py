"""Calibration coupons.

Two flat coupons, both plate height (3.2 mm), each with 9 numbered stations:

* **S, stud ladder** — 2x2 groups of studs whose diameter steps along the coupon.
  Test: press a genuine 2x2 brick/plate onto each station.
* **U, underside ladder** — three lanes of real-size plate undersides hanging from a
  common roof, printed open side down exactly like real parts:
    lane A: 1x2 cells, the wall-to-wall gap steps   (test with a genuine 1x2 or 1x1 part)
    lane B: 2x2 cells, the centre tube diameter steps (test with a genuine 2x2 part)
    lane C: rings like a 1x1 round plate, bore steps  (test with any single genuine stud)
  Cells are separated by slots so every wall is free-standing like on a real part.

`build_kit` writes the STLs plus `kit.json`, which records exactly which value sits at which
station so the solver (calibrick.calib) can turn fit ratings back into profile numbers.
"""
from __future__ import annotations

import json
from pathlib import Path

from manifold3d import Manifold

from . import csg
from .profile import PLATE_H, Profile

N_STATIONS = 9
S_PITCH = 16.0        # station pitch on the stud coupon (2 studs)
U_PITCH = 16.8        # station pitch on the underside coupon (cells are separated)
CELL = 15.8           # real exterior size of a 2-stud-long part


def ladder(center: float, step: float, n: int = N_STATIONS) -> list[float]:
    half = (n - 1) / 2.0
    return [round(center + (i - half) * step, 4) for i in range(n)]


def default_ladders(step: float = 0.05) -> dict[str, list[float]]:
    return {
        "stud_d": ladder(4.90, step),      # 4.70 .. 5.10   bigger = tighter
        "cavity_gap": ladder(4.90, step),  # 4.70 .. 5.10   bigger = looser
        "tube_od": ladder(6.50, step),     # 6.30 .. 6.70   bigger = tighter
        "socket_d": ladder(4.95, step),    # 4.75 .. 5.15   bigger = looser
    }


# +1: a larger value makes the fit tighter; -1: a larger value makes it looser
DIRECTION = {"stud_d": +1, "cavity_gap": -1, "tube_od": +1, "socket_d": -1}


def stud_coupon(p: Profile, values: list[float]) -> Manifold:
    n = len(values)
    length = S_PITCH * n
    base = csg.box(0.1, 0.1, 0.0, length - 0.1, 23.9, PLATE_H)
    studs, labels = [], []
    for i, d in enumerate(values):
        x0 = S_PITCH * i
        for sx in (4.0, 12.0):
            for sy in (4.0, 12.0):
                studs.append(csg.stud(x0 + sx, sy, PLATE_H, d, p.stud_h, p.stud_chamfer,
                                      p.layer_height))
        labels.append(csg.pixel_text(str(i + 1), x0 + 8.0, 20.0, PLATE_H - 0.48, PLATE_H + 1.0,
                                     px=0.9, underline=True))
    return (base + csg.union(studs)) - csg.union(labels)


def underside_coupon(p: Profile, cavity: list[float], tube: list[float],
                     socket: list[float], anchors: bool = True) -> Manifold:
    """Underside ladder. `anchors` adds one-layer tabs that hold the small first-layer
    islands on the bed. They sit only where nothing mates and nothing has to flex: at cell
    corners (stiff anyway), as three spokes outside each ring, and as four short spokes on
    each tube pointing between the stud positions. Fit readings are the same with or without.
    """
    n = len(cavity)
    depth = 1.6 + p.roof_raise            # cavity depth of a plate after the roof raise
    top = PLATE_H
    length = U_PITCH * (n - 1) + CELL
    ya0, ya1 = 0.0, 7.8                   # lane A: 1x2 cells
    yb0, yb1 = 10.2, 26.0                 # lane B: 2x2 cells
    yc = 31.65                           # lane C: ring centres
    yr0, yr1 = 37.3, 45.8                 # label rail (also supports the roof edge)
    lh = p.layer_height
    tabs = []
    solids = [csg.box(0.0, 0.0, depth, length, yr1, top),
              csg.box(0.0, yr0, 0.0, length, yr1, depth + 0.2)]
    cuts, adds, labels = [], [], []
    for i in range(n):
        x0 = U_PITCH * i
        cx = x0 + CELL / 2.0
        # lane A: real 1x2 plate underside, gap between the long walls = cavity[i]
        solids.append(csg.box(x0, ya0, 0.0, x0 + CELL, ya1, depth + 0.2))
        g = cavity[i]
        dl = g - 4.8
        cya = (ya0 + ya1) / 2.0
        cuts.append(csg.box(cx - (12.8 + dl) / 2.0, cya - g / 2.0, -1.0,
                            cx + (12.8 + dl) / 2.0, cya + g / 2.0, depth))
        # lane B: real 2x2 plate underside (nominal 1.5 mm walls), tube diameter = tube[i]
        solids.append(csg.box(x0, yb0, 0.0, x0 + CELL, yb1, depth + 0.2))
        cyb = (yb0 + yb1) / 2.0
        cuts.append(csg.box(cx - 6.4, cyb - 6.4, -1.0, cx + 6.4, cyb + 6.4, depth))
        adds.append(csg.ring(cx, cyb, 0.0, depth + 0.2, tube[i], 4.95))
        # lane C: 1x1-round-style ring, bore = socket[i]
        adds.append(csg.ring(cx, yc, 0.0, depth + 0.2, 6.5, socket[i]))
        if anchors:
            import math
            for (bx0, by0, bx1, by1) in ((x0, ya0, x0 + CELL, ya1), (x0, yb0, x0 + CELL, yb1)):
                for tx, ty in ((bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)):
                    tabs.append(csg.cyl(tx, ty, 0.0, lh, 4.4, 24))          # corner tab
            for k in range(3):                                              # ring: 3 outer spokes
                ang = math.radians(90 + 120 * k)
                tabs.append(_spoke(cx, yc, ang, 3.0, 4.9, 1.6, lh))
            for k in range(4):                                              # tube: between the studs
                tabs.append(_spoke(cx, cyb, math.radians(90 * k), tube[i] / 2.0 - 0.15, 5.5, 1.3, lh))
        # station numbers: on the roof (seen from above) and on the bed side (seen flipped)
        yl = (yr0 + yr1) / 2.0
        labels.append(csg.pixel_text(str(i + 1), cx, yl, top - 0.48, top + 1.0, px=1.0,
                                     underline=True))
        labels.append(csg.pixel_text(str(i + 1), cx, yl, -1.0, 0.48, px=1.0,
                                     underline=True, mirror_x=True))
    body = csg.union(solids) - csg.union(cuts)
    body = body + csg.union(adds)
    if tabs:
        body = body + csg.union(tabs)
    return body - csg.union(labels)


def _spoke(cx: float, cy: float, ang: float, r0: float, r1: float, width: float,
           height: float) -> Manifold:
    """A flat radial tab from radius r0 to r1 at angle `ang`, lying on the bed."""
    import math

    b = csg.box(r0, -width / 2.0, 0.0, r1, width / 2.0, height)
    return b.rotate([0.0, 0.0, math.degrees(ang)]).translate([cx, cy, 0.0])


def build_kit(profile: Profile, out_dir: str | Path,
              ladders: dict[str, list[float]] | None = None, tag: str = "v1") -> dict:
    """Write the coupon STLs and kit.json into out_dir. Returns the kit description."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lad = dict(default_ladders())
    if ladders:
        lad.update(ladders)
    s = stud_coupon(profile, lad["stud_d"])
    u = underside_coupon(profile, lad["cavity_gap"], lad["tube_od"], lad["socket_d"])
    for name, m in (("S", s), ("U", u)):
        if m.status().name != "NoError" or m.is_empty():
            raise RuntimeError(f"coupon {name} is not a valid solid: {m.status()}")
    both = u + s.translate([0.0, 45.8 + 8.0, 0.0])
    lo, hi = csg.bbox(both)
    both = both.translate([-(lo[0] + hi[0]) / 2.0, -(lo[1] + hi[1]) / 2.0, 0.0])
    files = {
        "studs": csg.save(s, out / f"calibrick_kit_{tag}_S_studs.stl"),
        "undersides": csg.save(u, out / f"calibrick_kit_{tag}_U_undersides.stl"),
        "both": csg.save(both, out / f"calibrick_kit_{tag}_both.stl"),
    }
    n = len(lad["stud_d"])
    kit = {
        "kit": tag,
        "profile": profile.name,
        "printer": profile.printer,
        "layer_height": profile.layer_height,
        "ladders": lad,
        "direction": DIRECTION,
        "fixed": {"tube_test_cavity": 12.8, "tube_test_wall": 1.5, "ring_od": 6.5,
                  "stud_h": profile.stud_h, "cavity_depth": 1.6 + profile.roof_raise},
        "nominal": {
            "S_length": round(S_PITCH * n - 0.2, 3), "S_width": 23.8, "S_thickness": PLATE_H,
            "S_total_height": round(PLATE_H + profile.stud_h, 3),
            "U_length": round(U_PITCH * (n - 1) + CELL, 3), "U_width": 45.8,
            "U_thickness": PLATE_H, "U_cellA_width": 7.8, "U_cellB_width": 15.8,
        },
        "files": {k: v.name for k, v in files.items()},
        "volume_cm3": {"studs": round(s.volume() / 1000.0, 2),
                       "undersides": round(u.volume() / 1000.0, 2)},
    }
    (out / "kit.json").write_text(json.dumps(kit, indent=2))
    return kit
