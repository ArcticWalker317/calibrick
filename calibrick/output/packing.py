"""Bed packing: turn print jobs into per-colour plates (3MF), an STL library and a manifest.

Layout rules:

* one colour per plate; quantities are expanded into instances;
* footprints are axis-aligned XY bounding boxes, packed with ``rectpack`` (MaxRects, best
  short side fit) with 90 degree rotation allowed, ``spacing`` mm between parts;
* parts stay inside ``printer.bed`` shrunk by ``printer.margin`` and at least ``spacing``
  away from every ``printer.exclude`` rectangle (those are carved out of the free space
  before packing, so they are never used);
* plates are filled one at a time from a list sorted by part height (tallest first), so
  parts of similar height share a plate; an area-sorted fill is tried as well and used only
  if it needs fewer plates;
* everything is sorted with explicit keys, so the same input gives the same files.

``PartResult`` from ``calibrick.engine`` is used structurally (``part_id``, ``ok``,
``mesh_path``, ``size``, ``volume_mm3``, ...), so this module does not import the engine.
"""
from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
from rectpack import MaxRectsBssf
from rectpack.geometry import Rectangle

from ..csg import read_stl
from ..printers import PrinterSpec
from .threemf import write_3mf

PLA_DENSITY = 1.24          # g/cm3
_Q = 100                    # packing grid: 0.01 mm integer units


@dataclass
class PrintJob:
    result: Any               # calibrick.engine.PartResult (duck typed)
    quantity: int
    color_id: int
    color_name: str
    color_rgb: str


@dataclass
class Placement:
    part_id: str
    x: float                  # bed coordinates of the part's footprint centre, mm
    y: float
    rotation: float           # degrees about Z (0 or 90)
    width: float              # footprint on the bed after rotation, mm
    depth: float
    height: float

    @property
    def footprint(self) -> tuple[float, float, float, float]:
        return (self.x - self.width / 2, self.y - self.depth / 2,
                self.x + self.width / 2, self.y + self.depth / 2)


@dataclass
class PlateInfo:
    index: int                # 1-based over all plates
    number: int               # 1-based within the colour
    color_id: int
    color_name: str
    color_rgb: str
    path: Path                # the plate 3MF
    parts: dict[str, int]     # part_id -> count on this plate
    placements: list[Placement] = field(default_factory=list)
    object_count: int = 0
    max_height: float = 0.0
    volume_cm3: float = 0.0
    filament_g: float = 0.0


@dataclass
class _Part:
    part_id: str
    stl: Path                 # library copy
    w: float                  # print-frame bbox X, Y, Z
    d: float
    h: float
    cx: float                 # bbox centre offset in X/Y (0 for engine output)
    cy: float
    volume_mm3: float


@dataclass
class _Inst:
    key: tuple                # sort key
    part: _Part


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")
    return s or "colour"


def _norm_rgb(rgb: str) -> str:
    h = str(rgb or "").strip().lstrip("#").upper()
    return "#" + h if re.fullmatch(r"[0-9A-F]{6}", h) else str(rgb or "")


def _mesh_volume(tris: np.ndarray) -> float:
    return float(abs(np.einsum("ij,ij->i", tris[:, 0], np.cross(tris[:, 1], tris[:, 2])).sum()) / 6.0)


# ---- packing core ---------------------------------------------------------------------------
class _Bed:
    """Packing frame in integer 0.01 mm units.

    Each part's rectangle is its footprint grown by spacing/2 on every side, and the frame
    is the usable area (bed minus margin) grown by spacing/2, so packed rectangles that only
    touch leave exactly ``spacing`` between parts and parts touch the margin at most.
    """

    def __init__(self, printer: PrinterSpec, spacing: float):
        self.printer = printer
        self.spacing = float(spacing)
        self.m = float(printer.margin)
        bw, bd = printer.bed
        self.uw = bw - 2 * self.m
        self.ud = bd - 2 * self.m
        if self.uw <= 0 or self.ud <= 0:
            raise ValueError(f"margin {self.m} leaves no usable bed on {printer.key}")
        self.W = int(math.floor((self.uw + self.spacing) * _Q + 1e-6))
        self.H = int(math.floor((self.ud + self.spacing) * _Q + 1e-6))
        # obstacles in frame coordinates: exclusion grown by spacing/2 (frame origin is at
        # bed (margin - spacing/2)), so a frame rectangle clear of it keeps the part
        # `spacing` away from the exclusion.
        self.obstacles: list[tuple[int, int, int, int]] = []
        for x0, y0, x1, y1 in printer.exclude:
            fx0 = math.floor((x0 - self.m) * _Q)
            fy0 = math.floor((y0 - self.m) * _Q)
            fx1 = math.ceil((x1 - self.m + self.spacing) * _Q)
            fy1 = math.ceil((y1 - self.m + self.spacing) * _Q)
            fx0, fy0 = max(fx0, 0), max(fy0, 0)
            fx1, fy1 = min(fx1, self.W), min(fy1, self.H)
            if fx1 > fx0 and fy1 > fy0:
                self.obstacles.append((fx0, fy0, fx1, fy1))

    def rect_size(self, p: _Part) -> tuple[int, int]:
        return (int(math.ceil((p.w + self.spacing) * _Q - 1e-6)),
                int(math.ceil((p.d + self.spacing) * _Q - 1e-6)))

    def fits_empty(self, p: _Part) -> bool:
        b = self.new_bin()
        w, h = self.rect_size(p)
        return b.add_rect(w, h) is not None

    def new_bin(self) -> MaxRectsBssf:
        b = MaxRectsBssf(self.W, self.H, rot=True)
        for x0, y0, x1, y1 in self.obstacles:
            b._split(Rectangle(x0, y0, x1 - x0, y1 - y0))
            b._remove_duplicates()
        return b

    def to_placement(self, p: _Part, r: Rectangle, w: int, h: int) -> Placement:
        rotated = (r.width != w) and (w != h)
        pw, pd = (p.d, p.w) if rotated else (p.w, p.d)
        x = self.m - self.spacing / 2 + (r.x + r.width / 2) / _Q
        y = self.m - self.spacing / 2 + (r.y + r.height / 2) / _Q
        return Placement(p.part_id, round(x, 3), round(y, 3), 90.0 if rotated else 0.0,
                         pw, pd, p.h)


def _fill_plates(insts: list[_Inst], bed: _Bed) -> list[list[Placement]]:
    """Fill plates one by one; each plate tries every remaining instance in list order."""
    remaining = list(insts)
    plates: list[list[Placement]] = []
    while remaining:
        b = bed.new_bin()
        placed: list[Placement] = []
        left: list[_Inst] = []
        for inst in remaining:
            w, h = bed.rect_size(inst.part)
            r = b.add_rect(w, h)
            if r is None:
                left.append(inst)
            else:
                placed.append(bed.to_placement(inst.part, r, w, h))
        if not placed:          # cannot happen: oversize parts are filtered out first
            raise RuntimeError("packing made no progress")
        plates.append(placed)
        remaining = left
    return plates


def _centre(placed: list[Placement], bed: _Bed) -> list[Placement]:
    """Shift the whole layout to the middle of the usable area if that keeps it legal."""
    x0 = min(p.footprint[0] for p in placed)
    y0 = min(p.footprint[1] for p in placed)
    x1 = max(p.footprint[2] for p in placed)
    y1 = max(p.footprint[3] for p in placed)
    bw, bd = bed.printer.bed
    dx = round((bw / 2) - (x0 + x1) / 2, 3)
    dy = round((bd / 2) - (y0 + y1) / 2, 3)
    moved = [Placement(p.part_id, round(p.x + dx, 3), round(p.y + dy, 3), p.rotation,
                       p.width, p.depth, p.height) for p in placed]
    return moved if not layout_problems(moved, bed.printer, bed.spacing) else placed


def layout_problems(placed: list[Placement], printer: PrinterSpec, spacing: float,
                    tol: float = 1e-3) -> list[str]:
    """Check a plate layout. Returns human-readable problems (empty list = valid)."""
    probs = []
    m = printer.margin
    bw, bd = printer.bed
    fps = [p.footprint for p in placed]
    for p, (x0, y0, x1, y1) in zip(placed, fps):
        if x0 < m - tol or y0 < m - tol or x1 > bw - m + tol or y1 > bd - m + tol:
            probs.append(f"{p.part_id} at ({p.x}, {p.y}) leaves the margin")
        for e in printer.exclude:
            if (x0 < e[2] + spacing - tol and x1 > e[0] - spacing + tol
                    and y0 < e[3] + spacing - tol and y1 > e[1] - spacing + tol):
                probs.append(f"{p.part_id} at ({p.x}, {p.y}) is within {spacing} mm of exclusion {e}")
    for i in range(len(fps)):
        a = fps[i]
        for j in range(i + 1, len(fps)):
            b = fps[j]
            if (a[0] < b[2] + spacing - tol and b[0] < a[2] + spacing - tol
                    and a[1] < b[3] + spacing - tol and b[1] < a[3] + spacing - tol):
                probs.append(f"{placed[i].part_id} and {placed[j].part_id} closer than {spacing} mm")
    return probs


def pack_parts(parts_qty: list[tuple[_Part, int]], printer: PrinterSpec, spacing: float
               ) -> list[list[Placement]]:
    bed = _Bed(printer, spacing)
    insts = []
    for part, q in parts_qty:
        for k in range(q):
            insts.append(_Inst((part.part_id, k), part))

    def by_height(i: _Inst):
        p = i.part
        return (-round(p.h, 2), -round(p.w * p.d, 2), p.part_id, i.key[1])

    def by_area(i: _Inst):
        p = i.part
        return (-round(p.w * p.d, 2), -round(max(p.w, p.d), 2), -round(p.h, 2), p.part_id, i.key[1])

    best = _fill_plates(sorted(insts, key=by_height), bed)
    alt = _fill_plates(sorted(insts, key=by_area), bed)
    if len(alt) < len(best):
        best = alt
    return [_centre(sorted(pl, key=lambda p: (p.y, p.x)), bed) for pl in best]


# ---- public entry point ---------------------------------------------------------------------
def build_plates(jobs: list[PrintJob], printer: PrinterSpec, out_dir: Path, *,
                 spacing: float = 4.0) -> list[PlateInfo]:
    """Pack ``jobs`` onto plates for ``printer`` and write everything under ``out_dir``.

    Writes ``plates/<colour-slug>/plate_NN.3mf``, ``stl/<part_id>.stl`` (one per unique
    part) and ``manifest.json`` / ``manifest.md``. Jobs whose result is not ok, has no
    mesh, or does not fit the printer are skipped and listed in the manifest.
    """
    out_dir = Path(out_dir)
    stl_dir = out_dir / "stl"
    plates_dir = out_dir / "plates"
    if plates_dir.exists():
        shutil.rmtree(plates_dir)          # stale plates from an earlier run would confuse
    stl_dir.mkdir(parents=True, exist_ok=True)
    bed = _Bed(printer, spacing)

    skipped: list[dict] = []
    parts: dict[str, _Part] = {}
    # colour key -> {part_id: qty}
    by_colour: dict[int, dict[str, int]] = {}
    colour_info: dict[int, tuple[str, str]] = {}

    for job in jobs:
        r = job.result
        pid = str(getattr(r, "part_id", "?"))
        qty = int(job.quantity)
        base = {"part_id": pid, "quantity": qty, "color_id": job.color_id,
                "color_name": job.color_name}
        mesh_path = getattr(r, "mesh_path", None)
        if qty <= 0:
            continue
        if not getattr(r, "ok", False):
            skipped.append({**base, "reason": "engine result not ok"})
            continue
        if not mesh_path or not Path(mesh_path).is_file():
            skipped.append({**base, "reason": "no mesh"})
            continue
        if pid not in parts:
            tris = read_stl(mesh_path)
            if len(tris) == 0:
                skipped.append({**base, "reason": "empty mesh"})
                continue
            lo = tris.reshape(-1, 3).min(axis=0)
            hi = tris.reshape(-1, 3).max(axis=0)
            dst = stl_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '_', pid)}.stl"
            if Path(mesh_path).resolve() != dst.resolve():
                shutil.copyfile(mesh_path, dst)
            vol = float(getattr(r, "volume_mm3", 0.0) or 0.0) or _mesh_volume(tris)
            parts[pid] = _Part(pid, dst, float(hi[0] - lo[0]), float(hi[1] - lo[1]),
                               float(hi[2] - lo[2]), float(hi[0] + lo[0]) / 2,
                               float(hi[1] + lo[1]) / 2, vol)
        p = parts[pid]
        if p.h > printer.max_z:
            skipped.append({**base, "reason": f"taller than printer max Z ({p.h:.1f} mm)"})
            continue
        if not bed.fits_empty(p):
            skipped.append({**base, "reason": f"footprint {p.w:.1f} x {p.d:.1f} mm does not fit the bed"})
            continue
        cid = int(job.color_id)
        colour_info.setdefault(cid, (str(job.color_name), _norm_rgb(job.color_rgb)))
        by_colour.setdefault(cid, {})
        by_colour[cid][pid] = by_colour[cid].get(pid, 0) + qty

    # colour folder names: slug of the name, made unique with the id if needed
    slugs: dict[int, str] = {}
    seen: dict[str, int] = {}
    for cid in sorted(by_colour, key=lambda c: (colour_info[c][0].lower(), c)):
        s = slugify(colour_info[cid][0])
        if s in seen and seen[s] != cid:
            s = f"{s}-{cid}"
        seen[s] = cid
        slugs[cid] = s

    plates: list[PlateInfo] = []
    for cid in sorted(by_colour, key=lambda c: (colour_info[c][0].lower(), c)):
        cname, rgb = colour_info[cid]
        pq = [(parts[pid], q) for pid, q in sorted(by_colour[cid].items())]
        layouts = pack_parts(pq, printer, spacing)
        for n, placed in enumerate(layouts, start=1):
            path = plates_dir / slugs[cid] / f"plate_{n:02d}.3mf"
            counts: dict[str, int] = {}
            for pl in placed:
                counts[pl.part_id] = counts.get(pl.part_id, 0) + 1
            items = []
            for pid in sorted(counts):
                part = parts[pid]
                pls = []
                for pl in placed:
                    if pl.part_id != pid:
                        continue
                    # move the mesh's bbox centre (rotated) onto the placement centre
                    a = math.radians(pl.rotation)
                    ox = part.cx * math.cos(a) - part.cy * math.sin(a)
                    oy = part.cx * math.sin(a) + part.cy * math.cos(a)
                    pls.append((round(pl.x - ox, 4), round(pl.y - oy, 4), pl.rotation))
                items.append({"name": pid, "mesh": part.stl, "placements": pls, "color_rgb": rgb})
            write_3mf(path, items, title=f"{cname} plate {n}")
            vol = sum(parts[pid].volume_mm3 * c for pid, c in counts.items()) / 1000.0
            plates.append(PlateInfo(
                index=len(plates) + 1, number=n, color_id=cid, color_name=cname, color_rgb=rgb,
                path=path, parts=dict(sorted(counts.items())), placements=placed,
                object_count=len(placed), max_height=max(p.height for p in placed),
                volume_cm3=round(vol, 3), filament_g=round(vol * PLA_DENSITY, 2)))

    write_manifest(out_dir, plates, skipped, printer, spacing, parts)
    return plates


# ---- manifest -------------------------------------------------------------------------------
def _rel(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def write_manifest(out_dir: Path, plates: list[PlateInfo], skipped: list[dict],
                   printer: PrinterSpec, spacing: float, parts: dict[str, _Part]) -> tuple[Path, Path]:
    totals: dict[int, dict] = {}
    for pl in plates:
        t = totals.setdefault(pl.color_id, {"color_id": pl.color_id, "color_name": pl.color_name,
                                            "color_rgb": pl.color_rgb, "plates": 0, "objects": 0,
                                            "parts": {}, "filament_cm3": 0.0, "filament_g": 0.0})
        t["plates"] += 1
        t["objects"] += pl.object_count
        for pid, c in pl.parts.items():
            t["parts"][pid] = t["parts"].get(pid, 0) + c
        t["filament_cm3"] += pl.volume_cm3
        t["filament_g"] += pl.filament_g
    for t in totals.values():
        t["filament_cm3"] = round(t["filament_cm3"], 3)
        t["filament_g"] = round(t["filament_g"], 2)
        t["parts"] = dict(sorted(t["parts"].items()))

    doc = {
        "printer": {"key": printer.key, "name": printer.name, "bed": list(printer.bed),
                    "margin": printer.margin, "exclude": [list(e) for e in printer.exclude]},
        "spacing_mm": spacing,
        "density_g_cm3": PLA_DENSITY,
        "stl": {pid: _rel(p.stl, out_dir) for pid, p in sorted(parts.items())},
        "plates": [
            {"index": pl.index, "color_id": pl.color_id, "color_name": pl.color_name,
             "color_rgb": pl.color_rgb, "number": pl.number, "file": _rel(pl.path, out_dir),
             "parts": pl.parts, "objects": pl.object_count, "max_height_mm": round(pl.max_height, 2),
             "filament_cm3": pl.volume_cm3, "filament_g": pl.filament_g,
             "placements": [asdict(p) for p in pl.placements]}
            for pl in plates
        ],
        "totals": list(totals.values()),
        "total_filament_g": round(sum(t["filament_g"] for t in totals.values()), 2),
        "skipped": skipped,
    }
    jpath = out_dir / "manifest.json"
    jpath.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")

    lines = [f"# Plates for {printer.name}", "",
             f"Bed {printer.bed[0]:g} x {printer.bed[1]:g} mm, margin {printer.margin:g} mm, "
             f"spacing {spacing:g} mm. Filament estimate: PLA at {PLA_DENSITY} g/cm3 from part volume "
             "(solid; real use depends on infill).", ""]
    lines += ["| # | Colour | RGB | File | Parts | Objects | Max Z mm | cm3 | g |",
              "|---|---|---|---|---|---|---|---|---|"]
    for pl in plates:
        ps = ", ".join(f"{pid} x{c}" for pid, c in pl.parts.items())
        lines.append(f"| {pl.index} | {pl.color_name} | {pl.color_rgb} | {_rel(pl.path, out_dir)} | {ps} "
                     f"| {pl.object_count} | {pl.max_height:.1f} | {pl.volume_cm3:.2f} | {pl.filament_g:.1f} |")
    lines += ["", "## Totals per colour", "",
              "| Colour | RGB | Plates | Objects | cm3 | g |", "|---|---|---|---|---|---|"]
    for t in totals.values():
        lines.append(f"| {t['color_name']} | {t['color_rgb']} | {t['plates']} | {t['objects']} "
                     f"| {t['filament_cm3']:.2f} | {t['filament_g']:.1f} |")
    lines.append(f"\nTotal filament: {doc['total_filament_g']:.1f} g")
    if skipped:
        lines += ["", "## Skipped", "", "| Part | Colour | Qty | Reason |", "|---|---|---|---|"]
        for s in skipped:
            lines.append(f"| {s['part_id']} | {s['color_name']} | {s['quantity']} | {s['reason']} |")
    mpath = out_dir / "manifest.md"
    mpath.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return jpath, mpath
