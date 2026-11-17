"""Tests for calibrick.output (3MF writer, bed packing, manifest) with synthetic box parts."""
from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pytest

from calibrick.csg import box, to_tris, write_stl
from calibrick.output import PrintJob, build_plates, layout_problems, write_3mf
from calibrick.output.threemf import NS_CORE, read_3mf_items
from calibrick.printers import PRINTERS, PrinterSpec


@dataclass
class FakeResult:            # structural stand-in for calibrick.engine.PartResult
    part_id: str
    ok: bool
    mesh_path: Path | None
    size: tuple[float, float, float]
    tier: str = "A"
    orientation: str = "studs_up"
    needs_support: bool = False
    volume_mm3: float = 0.0
    features: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)


def make_part(tmp: Path, pid: str, x: float, y: float, z: float, ok: bool = True) -> FakeResult:
    tris = to_tris(box(-x / 2, -y / 2, 0, x / 2, y / 2, z))
    path = write_stl(tmp / "src" / f"{pid}.stl", tris)
    return FakeResult(pid, ok, path, (x, y, z), volume_mm3=x * y * z)


def job(r, q, cid=4, name="Red", rgb="C91A09"):
    return PrintJob(r, q, cid, name, rgb)


def check_plates(plates, printer, spacing):
    for pl in plates:
        assert layout_problems(pl.placements, printer, spacing) == [], pl.path
        assert pl.object_count == len(pl.placements) == sum(pl.parts.values())


# ---- 3MF writer --------------------------------------------------------------------------------
def test_write_3mf_shared_objects_and_transforms(tmp_path):
    tris = to_tris(box(-5, -2, 0, 5, 2, 3))
    stl = write_stl(tmp_path / "b.stl", tris)
    out = write_3mf(tmp_path / "t.3mf", [
        {"name": "bar", "mesh": tris, "placements": [(10, 20, 0), (30, 40, 90), (50, 60, 45)]},
        ("bar_stl", stl, [(0, 0, 0)]),
    ])
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} <= names
        root = ET.fromstring(z.read("3D/3dmodel.model"))
        ET.fromstring(z.read("[Content_Types].xml"))
        ET.fromstring(z.read("_rels/.rels"))
    ns = {"m": NS_CORE}
    assert root.get("unit") == "millimeter"
    objs = root.findall("m:resources/m:object", ns)
    assert len(objs) == 2                                   # one resource per mesh
    assert len(root.findall("m:build/m:item", ns)) == 4
    # welded: a box has 8 vertices and 12 triangles
    assert len(objs[0].findall("m:mesh/m:vertices/m:vertex", ns)) == 8
    assert len(objs[0].findall("m:mesh/m:triangles/m:triangle", ns)) == 12

    items = read_3mf_items(out)
    verts = np.array([[5.0, 2.0, 3.0, 1.0]])
    # 90 deg: (5, 2) -> (-2, 5), then + (30, 40)
    p = (items[1]["matrix"] @ verts.T).T[0]
    assert np.allclose(p[:3], [28.0, 45.0, 3.0])
    p0 = (items[0]["matrix"] @ verts.T).T[0]
    assert np.allclose(p0[:3], [15.0, 22.0, 3.0])


def test_write_3mf_rejects_bad_mesh(tmp_path):
    with pytest.raises(ValueError):
        write_3mf(tmp_path / "x.3mf", [{"name": "x", "mesh": np.zeros((3, 3)), "placements": []}])


# ---- packing -----------------------------------------------------------------------------------
def test_build_plates_basic(tmp_path):
    printer = PRINTERS["a1mini"]
    a = make_part(tmp_path, "3001", 31.8, 15.8, 11.2)       # 2x4 brick
    b = make_part(tmp_path, "3020", 31.8, 15.8, 3.2)        # 2x4 plate
    c = make_part(tmp_path, "3003", 15.8, 15.8, 11.2)       # 2x2 brick
    bad = make_part(tmp_path, "9999", 10, 10, 10, ok=False)
    nomesh = FakeResult("8888", True, None, (1, 1, 1))
    jobs = [job(a, 20), job(b, 15), job(c, 7, 1, "Blue", "#0055BF"), job(bad, 3), job(nomesh, 2)]
    out = tmp_path / "out"
    plates = build_plates(jobs, printer, out, spacing=4.0)
    check_plates(plates, printer, 4.0)

    # one colour per plate, quantities honoured
    red = [p for p in plates if p.color_id == 4]
    blue = [p for p in plates if p.color_id == 1]
    assert sum(p.parts.get("3001", 0) for p in red) == 20
    assert sum(p.parts.get("3020", 0) for p in red) == 15
    assert sum(p.parts.get("3003", 0) for p in blue) == 7
    assert all(set(p.parts) <= {"3001", "3020"} for p in red)
    assert [p.path.relative_to(out).as_posix() for p in blue] == ["plates/blue/plate_01.3mf"]
    assert (out / "plates/red/plate_01.3mf").is_file()

    # STL library: one per unique printed part
    assert sorted(f.name for f in (out / "stl").iterdir()) == ["3001.stl", "3003.stl", "3020.stl"]

    # 3MF instance count matches the plate, objects shared
    for p in plates:
        items = read_3mf_items(p.path)
        assert len(items) == p.object_count
        assert len({i["objectid"] for i in items}) == len(p.parts)

    man = json.loads((out / "manifest.json").read_text())
    assert {s["part_id"] for s in man["skipped"]} == {"9999", "8888"}
    assert len(man["plates"]) == len(plates)
    red_tot = next(t for t in man["totals"] if t["color_id"] == 4)
    exp_cm3 = (20 * 31.8 * 15.8 * 11.2 + 15 * 31.8 * 15.8 * 3.2) / 1000
    assert red_tot["filament_cm3"] == pytest.approx(exp_cm3, rel=1e-3)
    assert red_tot["filament_g"] == pytest.approx(exp_cm3 * 1.24, rel=1e-3)
    md = (out / "manifest.md").read_text()
    assert "Red" in md and "#C91A09" in md and "9999" in md


def test_mesh_bounds_in_3mf_match_layout(tmp_path):
    """Apply the 3MF transforms to the real mesh and check against margin / overlap."""
    printer = PRINTERS["a1mini"]
    a = make_part(tmp_path, "long", 60, 10, 5)
    jobs = [job(a, 9)]
    plates = build_plates(jobs, printer, tmp_path / "o")
    assert len(plates) == 1
    from calibrick.csg import read_stl
    tris = read_stl(plates[0].path.parent.parent.parent / "stl" / "long.stl").reshape(-1, 3)
    hom = np.c_[tris, np.ones(len(tris))]
    boxes = []
    for it in read_3mf_items(plates[0].path):
        pts = (it["matrix"] @ hom.T).T
        lo, hi = pts.min(axis=0), pts.max(axis=0)
        assert lo[0] >= printer.margin - 1e-3 and lo[1] >= printer.margin - 1e-3
        assert hi[0] <= printer.bed[0] - printer.margin + 1e-3
        assert hi[1] <= printer.bed[1] - printer.margin + 1e-3
        assert lo[2] == pytest.approx(0)
        boxes.append((lo, hi))
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            (a0, a1), (b0, b1) = boxes[i], boxes[j]
            sep = max(b0[0] - a1[0], a0[0] - b1[0], b0[1] - a1[1], a0[1] - b1[1])
            assert sep >= 4.0 - 1e-3


def test_exclusion_zone_and_multi_plate(tmp_path):
    printer = PRINTERS["p1p"]                               # has an exclusion rectangle
    assert printer.exclude
    a = make_part(tmp_path, "3001", 31.8, 15.8, 11.2)
    b = make_part(tmp_path, "3024", 7.8, 7.8, 3.2)
    plates = build_plates([job(a, 150), job(b, 40)], printer, tmp_path / "o", spacing=3.0)
    assert len(plates) >= 2
    check_plates(plates, printer, 3.0)
    assert sum(p.parts.get("3001", 0) for p in plates) == 150
    assert sum(p.parts.get("3024", 0) for p in plates) == 40
    assert [p.number for p in plates] == list(range(1, len(plates) + 1))


def test_big_exclusion_is_respected(tmp_path):
    printer = replace(PRINTERS["a1mini"], exclude=((40.0, 40.0, 140.0, 140.0),))
    a = make_part(tmp_path, "p", 20, 12, 4)
    plates = build_plates([job(a, 40)], printer, tmp_path / "o")
    check_plates(plates, printer, 4.0)
    assert sum(p.object_count for p in plates) == 40


def test_rotation_used_and_too_big_skipped(tmp_path):
    printer = PrinterSpec("t", "test", bed=(100.0, 60.0), max_z=50.0, margin=5.0)
    tall = make_part(tmp_path, "tall", 20, 70, 5)           # only fits rotated
    huge = make_part(tmp_path, "huge", 200, 10, 5)
    high = make_part(tmp_path, "high", 10, 10, 80)
    plates = build_plates([job(tall, 2), job(huge, 1), job(high, 1)], printer, tmp_path / "o")
    check_plates(plates, printer, 4.0)
    assert all(pl.rotation == 90.0 for p in plates for pl in p.placements)
    man = json.loads((tmp_path / "o" / "manifest.json").read_text())
    assert {s["part_id"] for s in man["skipped"]} == {"huge", "high"}


def test_heights_grouped(tmp_path):
    printer = PRINTERS["a1mini"]
    tall = make_part(tmp_path, "tall", 30, 30, 40)
    flat = make_part(tmp_path, "flat", 30, 30, 3)
    plates = build_plates([job(flat, 30), job(tall, 30)], printer, tmp_path / "o")
    # 25 of these fit per plate -> 3 plates; at most one plate mixes heights
    assert len(plates) == 3
    mixed = [p for p in plates if len(p.parts) > 1]
    assert len(mixed) <= 1
    assert plates[0].parts == {"tall": 25}


def test_deterministic(tmp_path):
    printer = PRINTERS["p1p"]
    rs = [make_part(tmp_path, f"p{i}", 8 + 3 * i, 6 + 2 * (i % 3), 2 + i) for i in range(6)]
    jobs = [job(r, 5 + i) for i, r in enumerate(rs)]
    build_plates(jobs, printer, tmp_path / "a")
    build_plates(list(reversed(jobs)), printer, tmp_path / "b")
    ja = json.loads((tmp_path / "a" / "manifest.json").read_text())["plates"]
    jb = json.loads((tmp_path / "b" / "manifest.json").read_text())["plates"]
    assert ja == jb
    za = zipfile.ZipFile(tmp_path / "a" / ja[0]["file"]).read("3D/3dmodel.model")
    zb = zipfile.ZipFile(tmp_path / "b" / jb[0]["file"]).read("3D/3dmodel.model")
    assert za == zb
