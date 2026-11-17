"""Geometry engine: real dimensions of generated parts, checked by slicing the output STL.

Needs the LDraw library (CALIBRICK_LDRAW); skipped when it is not there.
"""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest

from calibrick import config, csg
from calibrick.profile import Profile

LDRAW_OK = (config.ldraw_dir() / "parts").is_dir()
pytestmark = pytest.mark.skipif(not LDRAW_OK, reason="LDraw library not available (CALIBRICK_LDRAW)")

TOL = 0.011


def load(path):
    v, f = csg.weld(csg.read_stl(path), tol=1e-4)
    return csg.from_mesh(v, f)


def polys(m, z):
    """[(x_extent, y_extent, centre_x, centre_y)] of the cross-section at z, largest first."""
    out = []
    for p in m.slice(z).to_polygons():
        a = np.asarray(p)
        out.append((a[:, 0].max() - a[:, 0].min(), a[:, 1].max() - a[:, 1].min(),
                    (a[:, 0].max() + a[:, 0].min()) / 2, (a[:, 1].max() + a[:, 1].min()) / 2))
    return sorted(out, reverse=True)


@pytest.fixture(scope="module")
def lib():
    from calibrick.ldraw import LDrawLibrary

    return LDrawLibrary()


@pytest.fixture(scope="module")
def build(tmp_path_factory, lib):
    from calibrick.engine import build_part

    cache = {}

    def _build(pid, prof=None):
        prof = prof or Profile()
        key = (pid, prof.geometry_key())
        if key not in cache:
            out = tmp_path_factory.mktemp(f"p{pid}")
            r = build_part(pid, prof, out, lib=lib)
            assert r.ok, r.warnings
            cache[key] = (r, load(r.mesh_path))
        return cache[key]

    return _build


# ---- plate 2x2 ------------------------------------------------------------------------------
def test_3022_size_and_solid(build):
    r, m = build("3022")
    assert r.tier == "A"
    assert r.size == pytest.approx((15.8, 15.8, 4.96), abs=TOL)
    assert m.status().name == "NoError"
    assert len(m.decompose()) == 1
    assert r.features.get("studs") == 4 and r.features.get("tubes") == 1


def test_3022_underside(build):
    p = Profile()
    _, m = build("3022")
    xs = [round(x, 3) for x, *_ in polys(m, 1.0)]
    assert len(xs) == 4
    assert xs[0] == pytest.approx(15.8, abs=TOL)               # outside
    assert xs[1] == pytest.approx(8 + p.cavity_gap, abs=TOL)   # cavity
    assert xs[2] == pytest.approx(p.tube_od, abs=TOL)          # tube outside
    assert xs[3] == pytest.approx(p.socket_d, abs=TOL)         # tube bore


def test_3022_cavity_depth(build):
    p = Profile()
    _, m = build("3022")
    roof = 1.6 + p.roof_raise
    assert len(polys(m, roof - 0.02)) == 4                     # cavity still open
    above = polys(m, roof + 0.02)
    assert len(above) == 1                                     # closed: one solid square
    assert above[0][0] == pytest.approx(15.8, abs=TOL)


def test_3022_studs(build):
    p = Profile()
    _, m = build("3022")
    st = polys(m, 4.0)
    assert len(st) == 4
    assert sorted((round(cx), round(cy)) for _, _, cx, cy in st) == \
        [(-4, -4), (-4, 4), (4, -4), (4, 4)]
    for x, y, *_ in st:
        assert x == pytest.approx(p.stud_d, abs=TOL)
        assert y == pytest.approx(p.stud_d, abs=TOL)
    top = polys(m, 3.2 + p.stud_h - p.layer_height / 2)        # top layer of the stud
    assert len(top) == 4
    for x, *_ in top:
        assert x == pytest.approx(p.stud_d - 2 * p.stud_chamfer, abs=TOL)


# ---- other parts ----------------------------------------------------------------------------
def test_3024_plate_1x1(build):
    p = Profile()
    r, m = build("3024")
    assert r.tier == "A"
    assert r.size == pytest.approx((7.8, 7.8, 4.96), abs=TOL)
    assert r.features.get("studs") == 1
    assert len(polys(m, 4.0)) == 1
    assert polys(m, 4.0)[0][0] == pytest.approx(p.stud_d, abs=TOL)


def test_3001_brick_2x4(build):
    r, m = build("3001")
    assert r.tier == "A"
    assert r.features.get("studs") == 8
    assert r.features.get("tubes") == 3
    assert r.size == pytest.approx((31.8, 15.8, 9.6 + Profile().stud_h), abs=TOL)
    assert m.status().name == "NoError"
    assert len(polys(m, 9.6 + 0.8)) == 8


def test_6141_round_1x1(build):
    r, m = build("6141")
    assert r.tier == "A"
    assert r.size[2] == pytest.approx(3.2 + Profile().stud_h, abs=TOL)
    assert m.status().name == "NoError"


def test_3070b_tile(build):
    r, m = build("3070b")
    assert r.tier == "A"
    assert r.size == pytest.approx((7.8, 7.8, 3.2), abs=TOL)
    assert not r.features.get("studs")
    assert m.status().name == "NoError"


# ---- profile changes reach the mesh ---------------------------------------------------------
def test_profile_changes_mesh(build):
    base = Profile()
    p = replace(base, stud_d=5.0, cavity_gap=5.0, ext_clearance=0.2)
    assert p.geometry_key() != base.geometry_key()
    r, m = build("3022", p)
    assert r.size[0] == pytest.approx(15.6, abs=TOL)
    assert r.size[1] == pytest.approx(15.6, abs=TOL)
    xs = [x for x, *_ in polys(m, 1.0)]
    assert xs[0] == pytest.approx(15.6, abs=TOL)
    assert xs[1] == pytest.approx(8 + 5.0, abs=TOL)
    for x, *_ in polys(m, 4.0):
        assert x == pytest.approx(5.0, abs=TOL)


# ---- cache ----------------------------------------------------------------------------------
def test_build_part_cache(tmp_path, lib, monkeypatch):
    from calibrick.engine import core

    calls = []
    real = core.build_mesh

    def counting(raw, prof):
        calls.append(prof.geometry_key())
        return real(raw, prof)

    monkeypatch.setattr(core, "build_mesh", counting)
    a = core.build_part("3024", Profile(), tmp_path, lib=lib)
    assert a.ok and len(calls) == 1
    b = core.build_part("3024", Profile(), tmp_path, lib=lib)
    assert b.ok and len(calls) == 1                     # served from the cache
    assert b.size == a.size and b.tier == a.tier and b.mesh_path == a.mesh_path
    c = core.build_part("3024", replace(Profile(), stud_d=5.0), tmp_path, lib=lib)
    assert c.ok and len(calls) == 2                     # different geometry key: rebuilt
    core.build_part("3024", Profile(), tmp_path, lib=lib, force=True)
    assert len(calls) == 3
