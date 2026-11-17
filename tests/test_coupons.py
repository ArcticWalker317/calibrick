"""Calibration coupons: files, validity, and the ladder values actually in the mesh."""
from __future__ import annotations

import json

import numpy as np
import pytest

from calibrick import coupons, csg
from calibrick.profile import PLATE_H, Profile


def load(path):
    v, f = csg.weld(csg.read_stl(path), tol=1e-4)
    return csg.from_mesh(v, f)


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    out = tmp_path_factory.mktemp("kit")
    return out, coupons.build_kit(Profile(), out)


def test_files(kit):
    out, k = kit
    stls = sorted(p.name for p in out.glob("*.stl"))
    assert stls == sorted(["calibrick_kit_v1_S_studs.stl", "calibrick_kit_v1_U_undersides.stl",
                           "calibrick_kit_v1_both.stl"])
    on_disk = json.loads((out / "kit.json").read_text())
    assert on_disk == json.loads(json.dumps(k))
    assert on_disk["ladders"] == coupons.default_ladders()
    assert set(on_disk["files"].values()) == set(stls)


@pytest.mark.parametrize("which", ["studs", "undersides"])
def test_coupon_single_valid_solid(kit, which):
    out, k = kit
    m = load(out / k["files"][which])
    assert m.status().name == "NoError"
    assert not m.is_empty()
    assert len(m.decompose()) == 1
    assert m.volume() / 1000.0 == pytest.approx(k["volume_cm3"][which], abs=0.02)


def test_stud_ladder_in_mesh(kit):
    out, k = kit
    m = load(out / k["files"]["studs"])
    sec = []
    for p in m.slice(PLATE_H + 0.8).to_polygons():
        a = np.asarray(p)
        sec.append(((a[:, 0].max() + a[:, 0].min()) / 2, (a[:, 1].max() + a[:, 1].min()) / 2,
                    a[:, 0].max() - a[:, 0].min()))
    assert len(sec) == 4 * coupons.N_STATIONS
    for i, d in enumerate(k["ladders"]["stud_d"]):
        x0 = coupons.S_PITCH * i
        here = [s for s in sec if x0 < s[0] < x0 + coupons.S_PITCH]
        assert len(here) == 4, f"station {i + 1}"
        for _, _, w in here:
            assert w == pytest.approx(d, abs=0.01), f"station {i + 1}"


def test_custom_ladder_and_tag(tmp_path):
    lad = {"stud_d": coupons.ladder(4.80, 0.02)}
    k = coupons.build_kit(Profile(), tmp_path, ladders=lad, tag="v2")
    assert k["kit"] == "v2"
    assert k["ladders"]["stud_d"] == lad["stud_d"]
    assert k["ladders"]["cavity_gap"] == coupons.default_ladders()["cavity_gap"]
    assert (tmp_path / "calibrick_kit_v2_both.stl").is_file()
