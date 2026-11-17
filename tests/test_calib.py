"""Calibration solver: report parsing, ladder fits, profile solve, next ladders."""
from __future__ import annotations

import math
import re
from pathlib import Path

import numpy as np
import pytest

from calibrick import calib, coupons
from calibrick.calib import fit_ladder, next_ladders, parse_results, solve
from calibrick.profile import Profile

DOC = Path(__file__).resolve().parents[1] / "docs" / "CALIBRATION.md"
STUD = coupons.ladder(4.90, 0.05)      # 4.70 .. 5.10
TUBE = coupons.ladder(6.50, 0.05)


def template() -> str:
    blocks = re.findall(r"```\n(.*?)```", DOC.read_text(), re.S)
    return next(b for b in blocks if b.startswith("printer:"))


@pytest.fixture
def kit():
    lad = coupons.default_ladders()
    return {
        "kit": "v1", "ladders": lad, "direction": dict(coupons.DIRECTION),
        "fixed": {"tube_test_cavity": 12.8, "tube_test_wall": 1.5},
        "nominal": {"S_length": 143.8, "S_width": 23.8, "S_thickness": 3.2,
                    "S_total_height": 4.96, "U_length": 150.2, "U_width": 42.3,
                    "U_thickness": 3.2, "U_cellA_width": 7.8, "U_cellB_width": 15.8},
    }


# ---- parsing --------------------------------------------------------------------------------
def test_parse_template():
    r = parse_results(template())
    assert r.printer == "A1 mini"
    assert r.filament == "brand / type / colour"
    assert r.ratings["stud_d"] == [0, 0, 1, 2, 2, 3, 4, 4, 4]
    assert r.ratings["cavity_gap"] == [4, 4, 3, 2, 2, 1, 0, 0, 0]
    assert r.ratings["tube_od"] == [0, 0, 1, 2, 3, 4, 4, 4, 4]
    assert r.ratings["socket_d"] == [4, 3, 2, 2, 1, 0, 0, 0, 0]
    assert r.calipers == {
        "S_length": 143.71, "S_width": 23.74, "S_thickness": 3.18, "S_total_height": 4.93,
        "stud_1": 4.66, "stud_5": 4.87, "stud_9": 5.06, "U_length": 150.10,
        "U_cellB_width": 15.76,
    }
    assert r.notes.startswith("anything odd")


def test_parse_letters_and_untested():
    r = parse_results("S: LlGGtT---\nB: 1 2 - 3 4")
    assert r.ratings["stud_d"] == [0, 1, 2, 2, 3, 4, None, None, None]
    assert r.ratings["tube_od"] == [0, 1, None, 2, 3]


# ---- ladder fits ----------------------------------------------------------------------------
def score(s):
    return calib.parse_ratings(s)


def test_fit_centre_of_threes():
    f = fit_ladder("stud_d", STUD, score("1 1 2 3 3 4 5 5 5"), +1)
    assert f.status == "ok"
    assert f.value == pytest.approx((4.85 + 4.90) / 2)
    assert (f.lo, f.hi) == (4.85, 4.90)


def test_fit_centre_direction_minus():
    f = fit_ladder("cavity_gap", STUD, score("5 5 4 3 3 3 1 1 1"), -1)
    assert f.status == "ok"
    assert f.value == pytest.approx(4.90)


def test_fit_interpolates_between_2_and_4():
    f = fit_ladder("stud_d", STUD, score("1 1 2 4 5 5 5 5 5"), +1)
    assert f.status == "interpolated"
    assert f.value == pytest.approx(4.825)
    assert (f.lo, f.hi) == (4.80, 4.85)
    # uneven neighbours: 1 (score 0) then 4 (score 3) -> target score 2 is 2/3 of the way
    f = fit_ladder("stud_d", STUD, score("1 1 1 4 5 5 5 5 5"), +1)
    assert f.value == pytest.approx(4.80 + 0.05 * 2 / 3, abs=1e-4)


def test_fit_all_loose():
    f = fit_ladder("stud_d", STUD, score("1 1 1 1 2 2 2 2 2"), +1)
    assert f.status == "all_loose"
    assert f.value == pytest.approx(5.10)        # tightest station tried
    f = fit_ladder("socket_d", STUD, score("1 1 1 1 1 1 1 1 1"), -1)
    assert f.status == "all_loose"
    assert f.value == pytest.approx(4.70)        # smaller bore = tighter


def test_fit_all_tight():
    f = fit_ladder("stud_d", STUD, score("4 4 5 5 5 5 5 5 5"), +1)
    assert f.status == "all_tight"
    assert f.value == pytest.approx(4.70)
    f = fit_ladder("cavity_gap", STUD, score("5 5 5 5 5 5 5 5 5"), -1)
    assert f.status == "all_tight"
    assert f.value == pytest.approx(5.10)        # wider gap = looser


def test_fit_non_monotonic():
    f = fit_ladder("stud_d", STUD, score("1 2 1 3 3 4 3 5 5"), +1)
    assert "smoothed" in f.note
    assert f.status == "ok"
    assert 4.80 <= f.value <= 4.95
    # no 3 at all and out of order: pooled (4,1 -> 2.5,2.5 in ratings), then interpolated
    f = fit_ladder("stud_d", STUD, score("1 1 4 1 5 5 5 5 5"), +1)
    assert "smoothed" in f.note
    assert f.status == "interpolated"
    assert f.lo <= f.value <= f.hi
    assert (f.lo, f.hi) == (4.85, 4.90)


def test_fit_pooled_block_exactly_on_target():
    f = fit_ladder("stud_d", STUD, score("1 1 4 2 5 5 5 5 5"), +1)
    assert f.status in ("ok", "interpolated")
    assert f.value == pytest.approx((4.80 + 4.85) / 2)


def test_fit_no_data():
    f = fit_ladder("stud_d", STUD, [None] * 9, +1)
    assert f.status == "no_data" and f.value is None


# ---- solve ----------------------------------------------------------------------------------
def test_solve_template(kit):
    prof, fits, report = solve(kit, parse_results(template()), name="test")
    by = {f.key: f for f in fits}
    assert prof.name == "test" and prof.calibrated
    assert prof.stud_d == pytest.approx(4.875)
    assert prof.cavity_gap == pytest.approx(4.875)
    assert prof.socket_d == pytest.approx(4.875)
    assert by["tube_od"].value == pytest.approx(6.45)
    shift = 0.7 * math.sqrt(2) * (4.875 - 4.8)
    assert prof.tube_od == pytest.approx(6.45 + shift, abs=1e-4)
    assert any(line.startswith("tube_od: shifted") for line in report)

    # calipers: independent least-squares fit of measured vs nominal
    nom = [143.8, 23.8, 150.2, 15.8]
    meas = [143.71, 23.74, 150.10, 15.76]
    scale, off = np.polyfit(nom, meas, 1)
    assert prof.xy_scale == pytest.approx(round(1 / scale, 5), abs=1e-5)
    assert prof.ext_clearance == pytest.approx(round(0.10 + off / 2, 3), abs=1e-3)
    assert prof.history and prof.history[-1]["kit"] == "v1"


def test_solve_exact_scale_and_offset(kit):
    res = calib.Results(calipers={
        k: 0.995 * kit["nominal"][k] - 0.10
        for k in ("S_length", "S_width", "U_length", "U_cellB_width")})
    prof, _, _ = solve(kit, res, Profile())
    assert prof.xy_scale == pytest.approx(1 / 0.995, abs=1e-5)
    assert prof.ext_clearance == pytest.approx(0.05, abs=1e-3)
    assert not prof.calibrated                       # no ratings: nothing calibrated
    assert prof.stud_d == Profile().stud_d


def test_solve_no_tube_shift_at_tested_gap(kit):
    res = parse_results("S: 1 1 2 3 3 4 5 5 5\nA: 5 5 5 3 2 1 1 1 1\nB: 1 1 2 3 4 5 5 5 5")
    prof, _, report = solve(kit, res)
    assert prof.cavity_gap == pytest.approx(4.85)
    assert prof.tube_od == pytest.approx(6.45 + 0.7 * math.sqrt(2) * 0.05, abs=1e-4)
    res = parse_results("A: 5 5 3 1 1 1 1 1 1\nB: 1 1 2 3 4 5 5 5 5")   # gap 4.80 = tested
    prof, _, report = solve(kit, res)
    assert prof.cavity_gap == pytest.approx(4.80)
    assert prof.tube_od == pytest.approx(6.45)
    assert not any("shifted" in line for line in report)


# ---- next ladders ---------------------------------------------------------------------------
def test_next_ladders(kit):
    res = parse_results("S: 1 1 1 1 1 1 1 1 1\nA: 5 5 5 5 5 5 5 5 5\n"
                        "B: 1 1 2 3 4 5 5 5 5\nC: 5 4 3 3 2 1 1 1 1")
    prof, fits, _ = solve(kit, res)
    nxt = next_ladders(kit, fits)
    assert set(nxt) == set(kit["ladders"])
    # good answers: 0.02 mm steps centred on the answer
    assert nxt["tube_od"] == coupons.ladder(6.45, 0.02)
    assert nxt["socket_d"] == coupons.ladder(4.875, 0.02)
    # all loose studs: same step, shifted to the tight side (centre on the old tightest end + 4 steps)
    assert nxt["stud_d"] == coupons.ladder(5.10 + 4 * 0.05, 0.05)
    # all tight walls: wider gaps
    assert nxt["cavity_gap"] == coupons.ladder(5.10 + 4 * 0.05, 0.05)
    assert min(nxt["cavity_gap"]) >= max(kit["ladders"]["cavity_gap"])
