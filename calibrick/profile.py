"""Calibration profile.

A profile is valid for ONE combination of printer + nozzle + layer height + filament +
locked slicer process preset. It stores *model-space* sizes: the numbers we put into the
STL so that the printed part comes out at the size that fits genuine bricks.

Nominal brick geometry for reference (mm):
    stud pitch 8.0, plate height 3.2, brick height 9.6, real stud ~4.8-4.9 dia x 1.7 high,
    exterior walls sit 0.1 inside the 8 mm grid plane, wall 1.5, cavity for one stud row 4.8,
    underside tube 6.51 OD / 4.8 ID.
LDraw draws the same parts idealised: exterior walls ON the grid plane, wall 1.6,
tube 6.4 OD, stud 4.8 x 1.6, plate cavity only 1.6 deep. The engine corrects all of that.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

LDU = 0.4          # mm per LDraw unit
PITCH = 8.0
PLATE_H = 3.2
BRICK_H = 9.6


@dataclass
class Profile:
    name: str = "nominal"
    printer: str = "a1mini"
    nozzle: float = 0.4
    layer_height: float = 0.16
    filament: str = ""
    process_preset: str = ""
    calibrated: bool = False

    # ---- model-space feature sizes (mm) --------------------------------------------
    stud_d: float = 4.85          # stud outer diameter
    stud_h: float = 1.76          # stud height; keep a multiple of layer_height
    stud_chamfer: float = 0.12    # small lead-in on the stud's top edge
    socket_d: float = 4.95        # round hole gripping ONE stud (1x1 round underside, tube bore)
    cavity_gap: float = 4.90      # inner wall face to inner wall face for one stud row (LDraw 4.8)
    tube_od: float = 6.50         # underside tube outer diameter (LDraw 6.4, real 6.51)
    post_d: float = 3.00          # underside post between studs on 1xN parts (LDraw 3.2)
    ext_clearance: float = 0.10   # each exterior side wall is pulled in this far from the grid plane
    roof_raise: float = 0.32      # extra depth for plate-height cavities (LDraw gives only 1.6)
    xy_scale: float = 1.0         # multiply XY by this to cancel shrinkage (1/measured_scale)
    z_scale: float = 1.0

    # ---- not yet covered by a calibration coupon: sensible FDM starting values ------
    pin_hole_d: float = 4.95      # Technic pin hole
    pin_d: float = 4.78           # Technic pin body
    axle_hole_grow: float = 0.08  # radial growth applied to axle-hole (cross) surfaces
    axle_shrink: float = 0.05     # radial shrink applied to axle (cross) surfaces
    bar_d: float = 3.12           # 3.18 mm bars
    bar_hole_d: float = 3.30      # hollow-stud bore that accepts a bar
    hole_grow: float = 0.06       # radial growth for unclassified round holes
    boss_shrink: float = 0.00     # radial shrink for unclassified round bosses

    notes: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    # ---- derived ---------------------------------------------------------------------
    @property
    def cavity_wall_offset(self) -> float:
        """How far each inner (cavity) wall face moves INTO the wall relative to LDraw."""
        return (self.cavity_gap - 4.8) / 2.0

    def geometry_key(self) -> str:
        """Hash of every field that changes generated geometry (cache key)."""
        skip = {"name", "filament", "process_preset", "calibrated", "notes", "history"}
        d = {f.name: getattr(self, f.name) for f in fields(self) if f.name not in skip}
        return hashlib.sha1(json.dumps(d, sort_keys=True).encode()).hexdigest()[:12]

    # ---- io ----------------------------------------------------------------------------
    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2)

    def save(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json())
        return path

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @classmethod
    def load(cls, path: str | Path) -> "Profile":
        return cls.from_dict(json.loads(Path(path).read_text()))


def load_profile(name_or_path: str | None) -> Profile:
    """Load a profile by file path or by name from the profiles directory; None -> nominal."""
    if not name_or_path:
        return Profile()
    p = Path(name_or_path)
    if p.exists():
        return Profile.load(p)
    from .config import profiles_dir

    q = profiles_dir() / f"{name_or_path}.json"
    if q.exists():
        return Profile.load(q)
    raise FileNotFoundError(f"profile {name_or_path!r} not found (looked at {p} and {q})")
