"""Slice every unique part of a `calibrick set build` output with PrusaSlicer and flag broken ones.

Bambu Studio has no usable headless mode here, so this uses the PrusaSlicer CLI (same slicing
core lineage) as a stand-in printability check. Each part STL is sliced on its own:
0.4 nozzle, 0.16 mm layers (first layer 0.16), 3 Arachne perimeters, no supports, no skirt,
and **100 % rectilinear infill** so that the extruded volume can be compared directly with
the mesh volume. Per part it records:

* success / return code, slicer warnings and errors, and the mesh report of ``--info``
  (manifold, open edges, repairs, number of shells, volume after PrusaSlicer's repair);
* layer count vs ``round(height / 0.16)``, layers without extrusion, maximum Z;
* XY extent of the extrusions vs the mesh footprint (missing pieces shrink it);
* extruded filament volume and its ratio to the engine's mesh volume.

Tier A parts (verified solids) define the normal filament/mesh ratio band; tier B parts
(overlapping shells the slicer must union) outside that band, or with any hard failure above,
are listed as suspicious.

    python3 -I tools/slice_check.py OUT_DIR [--psroot DIR | --slicer BIN] [--jobs 2]
            [--only ID ...] [--keep-gcode DIR]

OUT_DIR is a `calibrick set build` folder (needs manifest.json and parts.json). Writes
OUT_DIR/slice_check.json and prints a table of suspicious parts.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

LAYER = 0.16
FIL_AREA = math.pi * (1.75 / 2) ** 2          # mm2, 1.75 mm filament
BED = 180.0
SLICE_ARGS = [
    "--layer-height", "0.16", "--first-layer-height", "0.16",
    "--nozzle-diameter", "0.4", "--filament-diameter", "1.75",
    "--perimeters", "3", "--perimeter-generator", "arachne",
    "--fill-density", "100%", "--fill-pattern", "rectilinear",
    "--top-fill-pattern", "rectilinear", "--bottom-fill-pattern", "rectilinear",
    "--skirts", "0", "--brim-width", "0", "--use-relative-e-distances",
    "--bed-shape", f"0x0,{BED:g}x0,{BED:g}x{BED:g},0x{BED:g}", "--center", f"{BED / 2:g},{BED / 2:g}",
    "--loglevel", "2",
]
# lines of slicer output worth keeping (lower case match)
_MSG = re.compile(r"(warning|error|empty layer|repair|open edge|non-manifold|"
                  r"not manifold|consider|bed adhesion|bridging|overhang|floating|"
                  r"collapsing|thin|too small|outside)", re.I)
_PROGRESS = re.compile(r"^\d+ => ")
STABILITY = ("low bed adhesion", "long bridging", "consider enabling", "loose extrusions",
             "collapsing overhang", "detected print stability", "print warning")


def slicer_cmd(args) -> tuple[list[str], dict]:
    env = dict(os.environ)
    if args.psroot:
        root = Path(args.psroot)
        exe = str(root / "usr" / "bin" / "prusa-slicer")
        libs = [str(root / "usr" / "lib" / "x86_64-linux-gnu"), str(root / "usr" / "lib")]
        env["LD_LIBRARY_PATH"] = ":".join(libs + [env.get("LD_LIBRARY_PATH", "")]).rstrip(":")
    else:
        exe = args.slicer
    return [exe], env


def run(cmd, env, timeout):
    t = time.time()
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr, time.time() - t
    except subprocess.TimeoutExpired as e:
        return -9, f"timeout after {timeout}s\n{e.stdout or ''}", time.time() - t


def parse_info(text: str) -> dict:
    keys = ("manifold", "open_edges", "edges_fixed", "facets_removed", "facets_reversed",
            "backwards_edges", "degenerate_facets", "number_of_parts", "volume",
            "size_x", "size_y", "size_z")
    out: dict = {}
    for ln in text.splitlines():
        m = re.match(r"\s*([a-z_]+)\s*=\s*(\S+)", ln)
        if m and m.group(1) in keys:
            v = m.group(2)
            if v in ("yes", "no"):
                out[m.group(1)] = v == "yes"
            else:
                try:
                    out[m.group(1)] = float(v) if "." in v else int(v)
                except ValueError:
                    out[m.group(1)] = v
    return out


def parse_gcode(path: Path) -> dict:
    layers: list[dict] = []
    cur = None
    x = y = 0.0
    xs_lo = ys_lo = math.inf
    xs_hi = ys_hi = -math.inf
    fil_mm = 0.0
    used_cm3 = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith(";LAYER_CHANGE"):
                cur = {"z": None, "e": 0.0}
                layers.append(cur)
                continue
            if ln.startswith(";Z:") and cur is not None:
                cur["z"] = float(ln[3:])
                continue
            if ln.startswith("; filament used [cm3]"):
                used_cm3 = float(ln.split("=")[1])
                continue
            if not ln.startswith("G1"):
                continue
            mx = re.search(r"X([-\d.]+)", ln)
            my = re.search(r"Y([-\d.]+)", ln)
            me = re.search(r"E([-\d.]+)", ln)
            if mx:
                x = float(mx.group(1))
            if my:
                y = float(my.group(1))
            if me and (mx or my):
                e = float(me.group(1))
                if e > 0:
                    fil_mm += e
                    if cur is not None:
                        cur["e"] += e
                    xs_lo, xs_hi = min(xs_lo, x), max(xs_hi, x)
                    ys_lo, ys_hi = min(ys_lo, y), max(ys_hi, y)
    zs = [L["z"] for L in layers if L["z"] is not None]
    empty = [round(L["z"], 3) for L in layers if L["z"] is not None and L["e"] <= 1e-6]
    gaps = [round(b, 3) for a, b in zip(zs, zs[1:]) if b - a > LAYER * 1.5]
    return {
        "layers": len(layers),
        "max_z": round(max(zs), 3) if zs else 0.0,
        "empty_layers": empty,
        "z_gaps_above": gaps,
        "filament_mm3": round(used_cm3 * 1000.0 if used_cm3 is not None else fil_mm * FIL_AREA, 2),
        "extrusion_extent": [round(xs_hi - xs_lo, 3), round(ys_hi - ys_lo, 3)]
        if xs_hi > xs_lo else [0.0, 0.0],
    }


def check_part(pid: str, stl: Path, meta: dict, base_cmd, env, gdir: Path, timeout: int) -> dict:
    r = {"part_id": pid, "tier": meta.get("tier", "?"), "description": meta.get("description", ""),
         "size": meta.get("size"), "engine_volume_mm3": meta.get("volume_mm3"),
         "engine_warnings": meta.get("warnings", [])}
    code, text, _ = run(base_cmd + ["--info", str(stl)], env, 120)
    r["info"] = parse_info(text)
    gpath = gdir / f"{pid}.gcode"
    code, text, dt = run(base_cmd + ["--export-gcode", *SLICE_ARGS, "-o", str(gpath), str(stl)],
                         env, timeout)
    r["returncode"] = code
    r["seconds"] = round(dt, 1)
    msgs = []
    for ln in text.splitlines():
        s = ln.strip()
        if s and not _PROGRESS.match(s) and _MSG.search(s) and s not in msgs:
            msgs.append(s)
    r["stability"] = [m for m in msgs if any(k in m.lower() for k in STABILITY)]
    r["messages"] = [m for m in msgs if m not in r["stability"]]
    r["ok"] = code == 0 and gpath.is_file()
    if r["ok"]:
        r.update(parse_gcode(gpath))
    h = float(r["size"][2]) if r.get("size") else 0.0
    r["expected_layers"] = int(round(h / LAYER)) if h else None
    if r["tier"] == "A":
        try:
            r["solid_components"] = solid_components(stl)
        except Exception as e:  # noqa: BLE001
            r["solid_components"] = []
            r["messages"].append(f"could not decompose mesh: {e}")
    ev = r.get("engine_volume_mm3") or 0.0
    iv = r["info"].get("volume") or 0.0
    fv = r.get("filament_mm3") or 0.0
    r["ratio_fil_engine"] = round(fv / ev, 3) if ev > 0 else None
    r["ratio_fil_slicer_mesh"] = round(fv / iv, 3) if iv > 0 else None
    r["ratio_slicer_engine_vol"] = round(iv / ev, 3) if ev > 0 and iv > 0 else None
    return r


def hard_problems(r: dict) -> list[str]:
    p = []
    if not r["ok"]:
        p.append(f"slicing failed (code {r['returncode']}): " + "; ".join(r["messages"][-2:]))
        return p
    h = float(r["size"][2]) if r.get("size") else 0.0
    if h and abs(r["layers"] - h / LAYER) > 1.0:      # heights need not be layer multiples
        p.append(f"layers {r['layers']} vs expected {h / LAYER:.1f}")
    if r["empty_layers"]:
        p.append(f"{len(r['empty_layers'])} empty layers (first z={r['empty_layers'][0]})")
    if r["z_gaps_above"]:
        p.append(f"Z gaps (missing layers) below z={r['z_gaps_above'][:3]}")
    if r.get("size"):
        ex = r["extrusion_extent"]
        sx, sy = float(r["size"][0]), float(r["size"][1])
        if ex[0] < sx - 1.2 or ex[1] < sy - 1.2:
            p.append(f"extrusions span {ex[0]:.1f} x {ex[1]:.1f} mm, part is {sx:.1f} x {sy:.1f}")
    for m in r["messages"]:
        if re.search(r"error|empty layer", m, re.I):
            p.append(m)
    det = [c for c in r.get("solid_components", []) if c[3] == "detached"]
    if det:
        comps = ", ".join(f"{v:.1f} mm3 z {z0:.2f}-{z1:.2f} gap {g:.2f}" for v, z0, z1, _, g in det[:3])
        p.append(f"{len(det)} detached solid(s): {comps}" + (" ..." if len(det) > 3 else ""))
    return p


def solid_components(stl: Path) -> list[tuple[float, float, float, str, float]]:
    """Connected solids (volume > 0.5 mm3), largest first, as (volume, z0, z1, relation, gap).

    relation of each extra solid to the largest: "overlap" or "touch" (the slicer merges it,
    the engine just did not union it) or "detached" (gap > 0.01 mm: prints loose or floating).
    """
    from calibrick import csg

    v, f = csg.weld(csg.read_stl(stl), tol=1e-4)
    cs = sorted((c for c in csg.from_mesh(v, f).decompose() if c.volume() > 0.5),
                key=lambda c: -c.volume())
    out = []
    for i, c in enumerate(cs):
        bb = c.bounding_box()
        rel, gap = "main", 0.0
        if i:
            if (cs[0] ^ c).volume() > 1e-3:
                rel = "overlap"
            else:
                gap = float(cs[0].min_gap(c, 5.0))
                rel = "touch" if gap <= 0.01 else "detached"
        out.append((round(c.volume(), 2), round(bb[2], 3), round(bb[5], 3), rel, round(gap, 3)))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--psroot", help="root of an extracted prusa-slicer .deb (usr/bin, usr/lib)")
    ap.add_argument("--slicer", default="prusa-slicer", help="slicer executable (if no --psroot)")
    ap.add_argument("--jobs", type=int, default=2)
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--only", nargs="*", help="only these part ids")
    ap.add_argument("--keep-gcode", type=Path, help="keep the G-code in this folder")
    a = ap.parse_args(argv)

    out = a.out_dir
    manifest = json.loads((out / "manifest.json").read_text())
    parts_meta = json.loads((out / "parts.json").read_text())
    stls = {pid: out / rel for pid, rel in manifest["stl"].items()}
    if a.only:
        stls = {k: v for k, v in stls.items() if k in set(a.only)}
    base_cmd, env = slicer_cmd(a)

    tmp = None
    if a.keep_gcode:
        gdir = a.keep_gcode
        gdir.mkdir(parents=True, exist_ok=True)
    else:
        tmp = tempfile.TemporaryDirectory()
        gdir = Path(tmp.name)

    def one(item):
        pid, stl = item
        r = check_part(pid, stl, parts_meta.get(pid, {}), base_cmd, env, gdir, a.timeout)
        if not a.keep_gcode:
            (gdir / f"{pid}.gcode").unlink(missing_ok=True)
        print(f"  {pid:<10} tier {r['tier']}  {'ok' if r['ok'] else 'FAIL':<4} "
              f"{r.get('layers', '-')}/{r['expected_layers']} layers  "
              f"fil/mesh {r['ratio_fil_engine']}  {r['seconds']} s", flush=True)
        return r

    order = sorted(stls.items(), key=lambda kv: kv[0])
    print(f"slicing {len(order)} parts with {a.jobs} jobs ...")
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as ex:
        results = list(ex.map(one, order))
    if tmp:
        tmp.cleanup()

    # tier A defines the normal filament / mesh volume band
    a_ratios = [r["ratio_fil_engine"] for r in results
                if r["tier"] == "A" and r["ok"] and r["ratio_fil_engine"] and not hard_problems(r)]
    band = None
    if len(a_ratios) >= 5:
        med = statistics.median(a_ratios)
        mad = statistics.median(abs(x - med) for x in a_ratios)
        half = max(0.12, 4 * 1.4826 * mad)
        band = (round(med - half, 3), round(med + half, 3))

    for r in results:
        probs = hard_problems(r)
        q = r["ratio_fil_engine"]
        if band and q is not None and not (band[0] <= q <= band[1]):
            probs.append(f"filament/mesh volume {q} outside tier A band {band[0]}-{band[1]}")
        sv = r["ratio_slicer_engine_vol"]
        r["notes"] = []
        merged = [c for c in r.get("solid_components", []) if c[3] in ("overlap", "touch")]
        if merged:
            r["notes"].append(f"{len(merged)} extra solid(s) overlap/touch the body: not unioned "
                              "by the engine, the slicer merges them")
        if sv is not None and abs(sv - 1) > 0.10:      # informational: soups double-count overlaps
            r["notes"].append(f"slicer --info volume is {sv:.2f} x the engine volume")
        r["problems"] = probs
        r["suspicious"] = bool(probs)

    def stats(tier):
        rs = [r for r in results if r["tier"] == tier]
        qs = [r["ratio_fil_engine"] for r in rs if r["ratio_fil_engine"]]
        return {"parts": len(rs), "sliced_ok": sum(r["ok"] for r in rs),
                "suspicious": sum(r["suspicious"] for r in rs),
                "with_open_edges_in_slicer": sum(1 for r in rs if r["info"].get("open_edges")),
                "ratio_fil_engine_median": round(statistics.median(qs), 3) if qs else None,
                "ratio_fil_engine_min": min(qs) if qs else None,
                "ratio_fil_engine_max": max(qs) if qs else None,
                "stability_warnings": sum(1 for r in rs if r["stability"]),
                "multi_solid": sum(1 for r in rs if len(r.get("solid_components", [])) > 1)}

    summary = {
        "slicer": "PrusaSlicer 2.7.2 CLI (stand-in for Bambu Studio)",
        "settings": SLICE_ARGS,
        "tier_A_ratio_band": band,
        "tiers": {t: stats(t) for t in sorted({r["tier"] for r in results})},
        "suspicious": [r["part_id"] for r in results if r["suspicious"]],
        "parts": results,
    }
    dest = out / "slice_check.json"
    dest.write_text(json.dumps(summary, indent=1))

    print()
    for t, s in summary["tiers"].items():
        print(f"tier {t}: {s['parts']} parts, {s['sliced_ok']} sliced, {s['suspicious']} suspicious, "
              f"fil/mesh median {s['ratio_fil_engine_median']} "
              f"[{s['ratio_fil_engine_min']} - {s['ratio_fil_engine_max']}], "
              f"slicer saw open edges in {s['with_open_edges_in_slicer']}, "
              f"multi-solid (not unioned) {s['multi_solid']}")
    print(f"tier A filament/mesh band: {band}")
    sus = [r for r in results if r["suspicious"]]
    if sus:
        print()
        print(f"{'part':<10} {'tier':<4} {'layers':>9} {'fil/mesh':>8} {'open':>5}  problems")
        for r in sorted(sus, key=lambda r: (r["tier"], r["part_id"])):
            lay = f"{r.get('layers', '-')}/{r['expected_layers']}"
            print(f"{r['part_id']:<10} {r['tier']:<4} {lay:>9} {str(r['ratio_fil_engine']):>8} "
                  f"{r['info'].get('open_edges', 0):>5}  " + "; ".join(r["problems"]))
    else:
        print("no suspicious parts")
    print(f"\nwritten {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
