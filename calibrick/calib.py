"""Turn coupon test results into a calibration Profile.

Input is the kit description written by `coupons.build_kit` (kit.json) plus the user's
report: one fit rating per station for each ladder and a few caliper readings.

Fit ratings, one character per station (station 1 first):
    L  loose: no real grip, falls off
    l  holds, but lighter than a genuine connection
    G  good: feels like genuine
    t  tighter than genuine, but seats fully and comes apart by hand
    T  too tight: does not seat fully, or needs real force
    -  not tested
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field, replace
from pathlib import Path

from .profile import Profile

# Numeric form (preferred, unambiguous to type): 1 loose, 2 slightly loose, 3 like genuine,
# 4 slightly tight, 5 too tight. The letter form L l G t T is accepted too.
SCORE = {"1": 0.0, "2": 1.0, "3": 2.0, "4": 3.0, "5": 4.0,
         "L": 0.0, "l": 1.0, "G": 2.0, "t": 3.0, "T": 4.0}
TARGET = 2.0
# How much of the geometric wall->tube coupling to apply when the calibrated wall gap
# differs from the gap the tube ladder was printed with (see docs/CALIBRATION.md).
TUBE_WALL_COUPLING = 0.7

LADDER_ALIASES = {
    "stud_d": ("s", "stud", "studs", "s studs"),
    "cavity_gap": ("a", "cavity", "a cavity", "gap", "lane a"),
    "tube_od": ("b", "tube", "b tube", "tubes", "lane b"),
    "socket_d": ("c", "socket", "c socket", "ring", "rings", "sockets", "lane c"),
}


@dataclass
class LadderFit:
    key: str
    value: float | None            # recommended model-space value
    status: str                    # "ok" | "interpolated" | "all_loose" | "all_tight" | "no_data"
    lo: float | None = None        # bracket around the answer
    hi: float | None = None
    note: str = ""


def parse_ratings(text: str) -> list[float | None]:
    """'1 1 2 3 3 4 5 5 5' or '112334555' (or the letter form). Stops at the first other token."""
    toks = text.replace(",", " ").split()
    if toks and len(toks[0]) > 1 and all(c in SCORE or c in "-.?" for c in toks[0]):
        toks = list(toks[0])
    out: list[float | None] = []
    for t in toks:
        if t in SCORE:
            out.append(SCORE[t])
        elif t in ("-", ".", "?", "0"):
            out.append(None)
        else:
            break
    return out


def _isotonic(y: list[float]) -> list[float]:
    """Pool-adjacent-violators: closest non-decreasing sequence."""
    blocks = [[v, 1] for v in y]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] > blocks[i + 1][0] + 1e-12:
            tot = blocks[i][0] * blocks[i][1] + blocks[i + 1][0] * blocks[i + 1][1]
            cnt = blocks[i][1] + blocks[i + 1][1]
            blocks[i:i + 2] = [[tot / cnt, cnt]]
            i = max(i - 1, 0)
        else:
            i += 1
    out: list[float] = []
    for v, c in blocks:
        out += [v] * c
    return out


def fit_ladder(key: str, values: list[float], ratings: list[float | None],
               direction: int) -> LadderFit:
    """Find the value where tightness crosses 'good'. direction +1: bigger = tighter."""
    pts = [(v, r) for v, r in zip(values, ratings) if r is not None]
    if not pts:
        return LadderFit(key, None, "no_data", note="no ratings given")
    pts.sort(key=lambda p: direction * p[0])            # loosest first
    xs = [p[0] for p in pts]
    raw = [p[1] for p in pts]
    ys = _isotonic(raw)
    note = "" if all(abs(a - b) < 1e-9 for a, b in zip(raw, ys)) else \
        "ratings were not monotonic; smoothed"
    good = [x for x, r in zip(xs, raw) if r == TARGET]
    if good:
        # centre of the good band, taken from the raw ratings
        return LadderFit(key, round(sum(good) / len(good), 4), "ok", min(good), max(good), note)
    pooled = [x for x, y in zip(xs, ys) if abs(y - TARGET) < 1e-9]
    if pooled:                                   # e.g. a 4 and a 2 next to each other
        return LadderFit(key, round(sum(pooled) / len(pooled), 4), "interpolated", min(pooled),
                         max(pooled), note)
    if ys[0] > TARGET:
        return LadderFit(key, xs[0], "all_tight", None, xs[0],
                         "every tested station was tighter than genuine: extend the ladder "
                         "to the loose side")
    if ys[-1] < TARGET:
        return LadderFit(key, xs[-1], "all_loose", xs[-1], None,
                         "every tested station was looser than genuine: extend the ladder "
                         "to the tight side")
    for i in range(len(xs) - 1):
        if ys[i] < TARGET < ys[i + 1]:
            f = (TARGET - ys[i]) / (ys[i + 1] - ys[i])
            v = xs[i] + f * (xs[i + 1] - xs[i])
            return LadderFit(key, round(v, 4), "interpolated", min(xs[i], xs[i + 1]),
                             max(xs[i], xs[i + 1]), note)
    return LadderFit(key, None, "no_data", note="could not locate a crossing")


def fit_linear(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """measured = scale * nominal + offset (least squares)."""
    n = len(pairs)
    if n == 0:
        return 1.0, 0.0
    sx = sum(p[0] for p in pairs)
    sy = sum(p[1] for p in pairs)
    if n == 1 or max(p[0] for p in pairs) - min(p[0] for p in pairs) < 20.0:
        # not enough spread to separate scale from offset: treat everything as offset
        return 1.0, (sy - sx) / n
    sxx = sum(p[0] * p[0] for p in pairs)
    sxy = sum(p[0] * p[1] for p in pairs)
    scale = (n * sxy - sx * sy) / (n * sxx - sx * sx)
    return scale, (sy - scale * sx) / n


@dataclass
class Results:
    ratings: dict[str, list[float | None]] = field(default_factory=dict)
    calipers: dict[str, float] = field(default_factory=dict)
    printer: str = ""
    filament: str = ""
    notes: str = ""


_NUM = r"[-+]?\d+(?:\.\d+)?"
CALIPER_KEYS = {
    "s length": "S_length", "s width": "S_width", "s thickness": "S_thickness",
    "s total height": "S_total_height", "s height": "S_total_height",
    "u length": "U_length", "u width": "U_width", "u thickness": "U_thickness",
    "u cell a": "U_cellA_width", "cell a": "U_cellA_width", "a cell": "U_cellA_width",
    "u cell b": "U_cellB_width", "cell b": "U_cellB_width", "b cell": "U_cellB_width",
    "stud 1": "stud_1", "stud 5": "stud_5", "stud 9": "stud_9",
    "st1": "stud_1", "st5": "stud_5", "st9": "stud_9",
}


def parse_results(text: str) -> Results:
    """Tolerant parser for the pasted report (see docs/CALIBRATION.md for the template)."""
    res = Results()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            head, body = line.split(":", 1)
        elif "=" in line:
            head, body = line.split("=", 1)
        else:
            continue
        h = re.sub(r"[^a-z0-9 ]", " ", head.lower()).strip()
        h = re.sub(r"\s+", " ", h)
        body = body.strip()
        if h in ("printer",):
            res.printer = body
            continue
        if h in ("filament", "material"):
            res.filament = body
            continue
        if h in ("notes", "note"):
            res.notes = body
            continue
        matched = False
        for key, names in LADDER_ALIASES.items():
            if h in names:
                r = parse_ratings(body.split("(")[0])
                if r:
                    res.ratings[key] = r
                    matched = True
                break
        if matched:
            continue
        # caliper lines: either "S length: 143.71" or "calipers: S length=143.71, ..."
        for m in re.finditer(r"([A-Za-z][A-Za-z0-9 \-]*?)\s*[=:]\s*(" + _NUM + ")", line):
            k = re.sub(r"[^a-z0-9 ]", " ", m.group(1).lower()).strip()
            k = re.sub(r"\s+", " ", k)
            k = k.removeprefix("calipers ").removeprefix("mm ").strip()
            for alias, canon in CALIPER_KEYS.items():
                if k == alias or k.endswith(" " + alias) or k.startswith(alias + " "):
                    res.calipers[canon] = float(m.group(2))
                    break
    return res


def solve(kit: dict, res: Results, base: Profile | None = None, name: str | None = None
          ) -> tuple[Profile, list[LadderFit], list[str]]:
    """Build a calibrated Profile. Returns (profile, per-ladder fits, human-readable report)."""
    base = base or Profile()
    lad = kit["ladders"]
    direction = kit["direction"]
    fits = [fit_ladder(k, lad[k], res.ratings.get(k, []), int(direction[k])) for k in lad]
    by = {f.key: f for f in fits}
    upd: dict = {}
    report: list[str] = []
    for f in fits:
        if f.value is None:
            report.append(f"{f.key}: no usable data, keeping {getattr(base, f.key):.3f}")
            continue
        upd[f.key] = f.value
        br = f"[{f.lo if f.lo is not None else '..'} – {f.hi if f.hi is not None else '..'}]"
        report.append(f"{f.key}: {f.value:.3f} ({f.status}, bracket {br})"
                      + (f" — {f.note}" if f.note else ""))

    # tube ladder was printed with a fixed wall gap; shift for the calibrated one
    if "tube_od" in upd and "cavity_gap" in upd:
        tested_gap = float(kit.get("fixed", {}).get("tube_test_gap", 4.8))
        shift = TUBE_WALL_COUPLING * math.sqrt(2.0) * (upd["cavity_gap"] - tested_gap)
        if abs(shift) > 1e-6:
            report.append(f"tube_od: shifted {shift:+.3f} because walls move from the tested "
                          f"gap {tested_gap:.2f} to {upd['cavity_gap']:.3f} (verify with real 2x2 parts)")
        upd["tube_od"] = round(upd["tube_od"] + shift, 4)

    # calipers: separate scale (shrinkage) from constant offset (wall position)
    nom = kit.get("nominal", {})
    pairs = [(float(nom[k]), v) for k, v in res.calipers.items()
             if k in nom and k.endswith(("_length", "_width"))]
    if pairs:
        scale, off = fit_linear(pairs)
        if 0.985 < scale < 1.015:
            upd["xy_scale"] = round(1.0 / scale, 5)
        else:
            report.append(f"xy scale {scale:.4f} looks implausible; ignoring it (check the readings)")
            off = sum(m - n for n, m in pairs) / len(pairs)
        ext = min(max(0.10 + off / 2.0, 0.0), 0.30)
        upd["ext_clearance"] = round(ext, 3)
        report.append(f"exterior: prints {scale * 100 - 100:+.2f}% in scale and {off:+.3f} mm in "
                      f"size -> xy_scale {upd.get('xy_scale', base.xy_scale)}, ext_clearance {ext:.3f}")
    for k_nom, label in (("S_thickness", "plate thickness"), ("S_total_height", "plate + stud height")):
        if k_nom in res.calipers and k_nom in nom:
            dz = res.calipers[k_nom] - float(nom[k_nom])
            flag = "  <-- more than 0.1 off, check first-layer height / Z" if abs(dz) > 0.1 else ""
            report.append(f"{label}: {res.calipers[k_nom]:.2f} vs {float(nom[k_nom]):.2f} ({dz:+.2f}){flag}")
    studs = [(lad["stud_d"][i - 1], res.calipers[f"stud_{i}"]) for i in (1, 5, 9)
             if f"stud_{i}" in res.calipers and len(lad["stud_d"]) >= i]
    if studs:
        bias = sum(m - n for n, m in studs) / len(studs)
        report.append(f"studs print {bias:+.3f} mm vs model on average")
        if "stud_d" in upd:
            report.append(f"chosen stud should measure about {upd['stud_d'] + bias:.2f} mm")

    prof = replace(base, **upd)
    prof.name = name or f"{(res.printer or base.printer).lower().replace(' ', '')}-cal"
    prof.filament = res.filament or base.filament
    prof.calibrated = all(by[k].status in ("ok", "interpolated") for k in ("stud_d", "cavity_gap"))
    prof.history = list(base.history) + [{
        "kit": kit.get("kit"), "ladders": lad,
        "ratings": {k: v for k, v in res.ratings.items()},
        "calipers": res.calipers, "fits": {f.key: [f.value, f.status] for f in fits},
    }]
    if res.notes:
        prof.notes = list(base.notes) + [res.notes]
    return prof, fits, report


def next_ladders(kit: dict, fits: list[LadderFit], fine_step: float = 0.02) -> dict[str, list[float]]:
    """Ladders for the next kit: fine steps around good answers, shifted where out of range."""
    from .coupons import ladder

    out = {}
    for f in fits:
        vals = kit["ladders"][f.key]
        step = abs(vals[1] - vals[0])
        d = int(kit["direction"][f.key])
        if f.status in ("ok", "interpolated") and f.value is not None:
            out[f.key] = ladder(f.value, fine_step, len(vals))
        elif f.status == "all_loose":
            out[f.key] = ladder(f.value + d * step * (len(vals) // 2), step, len(vals))
        elif f.status == "all_tight":
            out[f.key] = ladder(f.value - d * step * (len(vals) // 2), step, len(vals))
        else:
            out[f.key] = vals
    return out


def solve_files(kit_path: str | Path, results_path: str | Path, base: Profile | None = None,
                name: str | None = None):
    kit = json.loads(Path(kit_path).read_text())
    res = parse_results(Path(results_path).read_text())
    return solve(kit, res, base, name)
