"""Command line for calibrick.

    calibrick setup [--refresh]
    calibrick calibrate kit [--printer a1mini] [--base PROFILE] [--out DIR] [--step 0.05]
                            [--next-from RESULTS --kit KIT_JSON]
    calibrick calibrate solve RESULTS.txt --kit kit.json [--name NAME] [--base PROFILE]
    calibrick profile list | show NAME
    calibrick part ID [ID ...] [--profile NAME] [--out DIR]
    calibrick set find QUERY | info SET
    calibrick set build SET [--profile NAME] [--printer a1mini] [--out DIR] [--no-risky]
                            [--spares] [--only-color NAME] [--limit N]

The modules do the work; this file only wires them together and prints.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import zipfile
from collections import Counter, defaultdict
from dataclasses import asdict, replace
from pathlib import Path

from . import config
from .printers import PRINTERS, get_printer
from .profile import Profile, load_profile

LDRAW_URL = "https://library.ldraw.org/library/updates/complete.zip"

NOMINAL_WARNING = (
    "WARNING: using the nominal profile. These parts are NOT calibrated for your printer;\n"
    "         fits will be off. Run `calibrick calibrate kit`, print it, then\n"
    "         `calibrick calibrate solve` and pass --profile NAME."
)


class CLIError(Exception):
    pass


# ------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------
def _profile_from_arg(arg: str | None) -> Profile:
    try:
        return load_profile(arg)
    except FileNotFoundError as e:
        raise CLIError(str(e)) from None


def _announce_profile(prof: Profile, explicit: bool, printer_key: str | None = None) -> list[str]:
    """Print (and return) the lines that say which profile is used and whether it is calibrated."""
    lines = []
    if not explicit:
        lines.append(NOMINAL_WARNING)
    else:
        lines.append(f"profile: {prof.name} (geometry key {prof.geometry_key()})")
        if not prof.calibrated:
            lines.append(f"WARNING: profile {prof.name!r} is not marked calibrated; "
                         "output is NOT calibrated.")
    if printer_key and prof.printer and prof.printer != printer_key:
        lines.append(f"WARNING: profile {prof.name!r} was made for printer {prof.printer!r}, "
                     f"building for {printer_key!r}.")
    for ln in lines:
        print(ln)
    return lines


def _next_tag(tag: str | None) -> str:
    m = re.fullmatch(r"v(\d+)", str(tag or ""))
    return f"v{int(m.group(1)) + 1}" if m else "v2"


def _fmt_size(size) -> str:
    return " x ".join(f"{v:.2f}" for v in size)


def size_problem(result, lib) -> str | None:
    """Compare the engine's output size with the raw LDraw part; None if plausible.

    Calibration moves faces by tenths of a millimetre, so the print-frame bounding box must
    match the LDraw bounding box (compared as sorted extents, orientation may change) to
    within a couple of millimetres. A larger difference means the engine produced garbage.
    """
    from .ldraw import expand_part

    if not result.ok:
        return None
    try:
        t = expand_part(lib, result.part_id).tris.reshape(-1, 3)
    except Exception:  # noqa: BLE001
        return None
    raw = sorted(float(v) for v in (t.max(axis=0) - t.min(axis=0)))
    got = sorted(float(v) for v in result.size)
    if any(abs(g - r) > max(2.0, 0.1 * r) for g, r in zip(got, raw)):
        return (f"engine output is {_fmt_size(result.size)} mm but the LDraw part is "
                f"{_fmt_size(raw)} mm (sorted extents): broken mesh")
    return None


def _fmt_feats(feats: dict) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(feats.items())) or "-"


# ------------------------------------------------------------------------------------------
# setup
# ------------------------------------------------------------------------------------------
def install_ldraw_zip(zip_path: Path, dest: Path) -> int:
    """Unpack the LDraw complete.zip so that ``dest/parts`` exists.

    The official zip has one top-level ``ldraw/`` folder; that prefix is stripped. A zip
    without it is unpacked as is. Returns the number of files written.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    n = 0
    with zipfile.ZipFile(zip_path) as zf:
        names = [i.filename for i in zf.infolist() if not i.is_dir()]
        tops = {n.replace("\\", "/").split("/", 1)[0].lower() for n in names}
        strip = tops == {"ldraw"}
        for info in zf.infolist():
            if info.is_dir():
                continue
            rel = info.filename.replace("\\", "/")
            if strip:
                rel = rel.split("/", 1)[1] if "/" in rel else ""
            if not rel:
                continue
            target = (dest / rel).resolve()
            if root not in target.parents:
                raise CLIError(f"refusing unsafe path in zip: {info.filename!r}")
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            n += 1
    if not (dest / "parts").is_dir():
        raise CLIError(f"{zip_path} did not contain an LDraw library (no parts/ folder)")
    return n


def _download(url: str, path: Path) -> None:
    import requests

    with requests.get(url, stream=True, timeout=300,
                      headers={"User-Agent": "calibrick/0.1 (personal hobby tool)"}) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)


def cmd_setup(a) -> int:
    ld = config.ldraw_dir()
    if (ld / "parts").is_dir() and not a.refresh:
        print(f"LDraw library: already present at {ld}")
    else:
        print(f"LDraw library: downloading {LDRAW_URL}")
        with tempfile.TemporaryDirectory() as td:
            zp = Path(td) / "complete.zip"
            try:
                _download(LDRAW_URL, zp)
            except Exception as e:  # noqa: BLE001
                raise CLIError(f"could not download the LDraw library ({e}). Download "
                               f"complete.zip by hand and unzip it so that {ld}/parts exists.")
            n = install_ldraw_zip(zp, ld)
        print(f"LDraw library: {n} files in {ld}")
    from . import catalog

    try:
        d = catalog.ensure_data(refresh=a.refresh, log=print)
    except catalog.DataError as e:
        raise CLIError(str(e)) from None
    print(f"Rebrickable data: {d}")
    print(f"profiles: {config.profiles_dir()}")
    print(f"cache: {config.cache_dir()}")
    return 0


# ------------------------------------------------------------------------------------------
# calibrate
# ------------------------------------------------------------------------------------------
def _print_solve(report: list[str]) -> None:
    for ln in report:
        print("  " + ln)


def cmd_calibrate_kit(a) -> int:
    from . import calib, coupons
    from .slicer.bambu import write_process_preset

    printer = get_printer(a.printer)
    base = _profile_from_arg(a.base)
    base.printer = printer.key if not a.base else base.printer
    if a.next_from or a.kit:
        if not (a.next_from and a.kit):
            raise CLIError("--next-from and --kit go together")
        kit = json.loads(Path(a.kit).read_text())
        res = calib.parse_results(Path(a.next_from).read_text())
        prof, fits, report = calib.solve(kit, res, base)
        print(f"solved {a.next_from} against {a.kit}:")
        _print_solve(report)
        ladders = calib.next_ladders(kit, fits)
        tag = _next_tag(kit.get("kit"))
        coupon_prof = prof
    else:
        ladders = coupons.default_ladders(a.step)
        tag = "v1"
        coupon_prof = base
    out = Path(a.out) if a.out else Path.cwd() / f"calibrick_kit_{tag}"
    kit = coupons.build_kit(coupon_prof, out, ladders=ladders, tag=tag)
    preset = write_process_preset(coupon_prof, printer, out)
    print(f"kit {tag} written to {out}")
    for k, f in kit["files"].items():
        print(f"  {k:<11} {out / f}")
    print(f"  kit.json    {out / 'kit.json'}")
    print(f"  preset      {preset}")
    print("ladders (station 1 -> 9):")
    for k, v in kit["ladders"].items():
        print(f"  {k:<11} {' '.join(f'{x:.3f}' for x in v)}")
    print("Print *_both.stl with the preset above, rate every station, then run "
          "`calibrick calibrate solve RESULTS.txt --kit kit.json` (see docs/CALIBRATION.md).")
    return 0


def cmd_calibrate_solve(a) -> int:
    from . import calib

    base = _profile_from_arg(a.base) if a.base else None
    kit = json.loads(Path(a.kit).read_text())
    prof, fits, report = calib.solve_files(a.kit, a.results, base, a.name)
    print(f"calibration from {a.results} (kit {kit.get('kit')}):")
    _print_solve(report)
    path = prof.save(config.profiles_dir() / f"{prof.name}.json")
    print(f"profile {prof.name!r} saved to {path} "
          f"({'calibrated' if prof.calibrated else 'NOT fully calibrated: studs or walls missing'})")
    print("suggested next ladders (station 1 -> 9):")
    for k, v in calib.next_ladders(kit, fits).items():
        print(f"  {k:<11} {' '.join(f'{x:.3f}' for x in v)}")
    print(f"next kit: calibrick calibrate kit --next-from {a.results} --kit {a.kit}")
    print(f"or check the fit on real parts: calibrick calibrate verify --profile {prof.name}")
    return 0


def cmd_calibrate_ladder(a) -> int:
    from . import ladder
    from .slicer.bambu import write_process_preset

    prof = _profile_from_arg(a.profile)
    out = Path(a.out) if a.out else Path.cwd() / f"calibrick_ladder_{a.part}_{a.field}"
    values = ladder.default_values(a.field, prof, a.center, a.step, a.n)
    info = ladder.build_part_ladder(a.part, a.field, values, prof, out)
    preset = write_process_preset(prof, get_printer(prof.printer), out)
    print(f"ladder for {a.field} on part {a.part} ({info['description']}) written to {out}")
    print(f"  print    {out / info['file']}   ({info['footprint'][0]} x {info['footprint'][1]} mm)")
    print(f"  preset   {preset}")
    print(f"  values   {' '.join(f'{v:.3f}' for v in values)}   (copy 1 -> {len(values)}, numbered tabs)")
    if not info["responds"]:
        print(f"WARNING: stepping {a.field} did not change this part's geometry; pick another part or field.")
    print("Rate each copy 1-5 against a genuine mating part, then run\n"
          f"  calibrick calibrate apply {out / ('ladder_' + a.part + '_' + a.field + '.json')} "
          f"\"<ratings>\" --profile {prof.name}")
    return 0


def cmd_calibrate_apply(a) -> int:
    from . import ladder

    info = json.loads(Path(a.ladder_json).read_text())
    prof = _profile_from_arg(a.profile)
    new, fit = ladder.apply_ladder(info, a.ratings, prof)
    if new is prof:
        print(f"{info['field']}: no answer ({fit.status}). {fit.note}")
        return 1
    path = new.save(config.profiles_dir() / f"{new.name}.json")
    print(f"{info['field']}: {getattr(prof, info['field']):.3f} -> {fit.value:.3f} ({fit.status}); saved {path}")
    return 0


def cmd_calibrate_verify(a) -> int:
    from .slicer.bambu import write_process_preset
    from .verify import build_verification_kit

    prof = _profile_from_arg(a.profile)
    out = Path(a.out) if a.out else Path.cwd() / f"calibrick_verify_{prof.name}"
    s = build_verification_kit(prof, out)
    preset = write_process_preset(prof, get_printer(prof.printer), out)
    if not prof.calibrated:
        print("WARNING: this profile is not calibrated; the parts show nominal starting values.")
    print(f"verification kit for profile {prof.name!r} written to {out}")
    print(f"  print    {out / s['file']}   ({s['footprint'][0]} x {s['footprint'][1]} mm, pre-arranged)")
    print(f"  preset   {preset}")
    for p in s["parts"]:
        flag = "" if p["ok"] and p["tier"] == "A" else f"   <-- tier {p['tier']}"
        print(f"  {p['part']:<7} x{p['copies']}  {p['why']}{flag}")
    print("Print it with the same preset, filament and plate as the coupons, then run the five "
          "checks under 'Verification kit checks' in docs/CALIBRATION.md.")
    return 0


# ------------------------------------------------------------------------------------------
# profile
# ------------------------------------------------------------------------------------------
def cmd_profile_list(a) -> int:
    d = config.profiles_dir()
    files = sorted(d.glob("*.json"))
    if not files:
        print(f"no profiles in {d}")
        return 0
    print(f"profiles in {d}:")
    for f in files:
        try:
            p = Profile.load(f)
        except Exception as e:  # noqa: BLE001
            print(f"  {f.stem:<20} unreadable ({e})")
            continue
        print(f"  {f.stem:<20} printer {p.printer:<8} "
              f"{'calibrated' if p.calibrated else 'uncalibrated':<12} {p.filament}")
    return 0


def cmd_profile_show(a) -> int:
    p = _profile_from_arg(a.name)
    print(p.to_json())
    print(f"geometry key: {p.geometry_key()}")
    return 0


# ------------------------------------------------------------------------------------------
# part
# ------------------------------------------------------------------------------------------
def cmd_part(a) -> int:
    from .engine import build_part
    from .ldraw import LDrawLibrary

    prof = _profile_from_arg(a.profile)
    _announce_profile(prof, bool(a.profile))
    out = Path(a.out) if a.out else config.cache_dir() / prof.geometry_key()
    lib = LDrawLibrary()
    bad = 0
    for pid in a.ids:
        try:
            r = build_part(pid, prof, out, lib=lib)
        except Exception as e:  # noqa: BLE001
            print(f"{pid}: FAILED {type(e).__name__}: {e}")
            bad += 1
            continue
        if not r.ok:
            print(f"{r.part_id}: FAILED " + "; ".join(r.warnings))
            bad += 1
            continue
        prob = size_problem(r, lib)
        if prob:
            print(f"{r.part_id}: BROKEN {prob}")
            bad += 1
        print(f"{r.part_id}: tier {r.tier}  {_fmt_size(r.size)} mm  {r.volume_mm3:.0f} mm3  "
              f"{r.description}")
        print(f"  features: {_fmt_feats(r.features)}")
        if r.needs_support:
            print("  needs support")
        for w in r.warnings:
            print(f"  warning: {w}")
        print(f"  {r.mesh_path}")
    return 1 if bad else 0


# ------------------------------------------------------------------------------------------
# set
# ------------------------------------------------------------------------------------------
def cmd_set_find(a) -> int:
    from . import catalog

    rows = catalog.find_sets(a.query, limit=a.limit)
    if not rows:
        print("no sets found")
        return 1
    for r in rows:
        print(f"{r['set_num']:<12} {r['year']:<5} {r['num_parts']:>5} parts  {r['name']}"
              + (f"  [{r['theme']}]" if r.get("theme") else ""))
    return 0


def cmd_set_info(a) -> int:
    from . import catalog

    print(catalog.summarize(catalog.load_set_plan(a.set)))
    return 0


def build_set(set_num: str, prof: Profile, printer, out: Path, *, include_risky=True,
              spares=False, only_color: str | None = None, limit: int | None = None,
              profile_lines: list[str] | None = None, log=print) -> dict:
    """The whole `set build` pipeline. Returns a summary dict (also written to report.md)."""
    from . import catalog
    from .engine import build_part
    from .ldraw import LDrawLibrary
    from .output import PrintJob, build_plates
    from .slicer.bambu import write_process_preset

    plan = catalog.load_set_plan(set_num)
    out.mkdir(parents=True, exist_ok=True)
    wanted = ("print", "risky") if include_risky else ("print",)
    items = [it for it in plan.items if it.decision in wanted and it.ldraw_id
             and it.quantity + (it.spares if spares else 0) > 0]
    if only_color:
        oc = only_color.strip().lower()
        items = [it for it in items if it.color_name.lower() == oc]
        if not items:
            raise CLIError(f"no printable parts in colour {only_color!r}; colours: "
                           + ", ".join(sorted({i.color_name for i in plan.items})))
    usage = Counter()
    for it in items:
        usage[it.ldraw_id] += it.quantity + (it.spares if spares else 0)
    ids = [pid for pid, _ in sorted(usage.items(), key=lambda kv: (-kv[1], kv[0]))]
    if limit:
        ids = ids[:limit]
        items = [it for it in items if it.ldraw_id in set(ids)]

    cache = config.cache_dir() / prof.geometry_key()
    lib = LDrawLibrary()
    results = {}
    log(f"building {len(ids)} unique parts into {cache}")
    for i, pid in enumerate(ids, 1):
        try:
            r = build_part(pid, prof, cache, lib=lib)
        except Exception as e:  # noqa: BLE001
            from .engine import PartResult
            r = PartResult(part_id=pid, ok=False, warnings=[f"{type(e).__name__}: {e}"])
        prob = size_problem(r, lib)
        if prob:
            r = replace(r, ok=False, warnings=list(r.warnings) + [prob])
        results[pid] = r
        if i % 25 == 0 or i == len(ids):
            log(f"  {i}/{len(ids)}")

    # failed parts go to the buy list
    failed = {pid: r for pid, r in results.items() if not r.ok}
    for it in items:
        if it.ldraw_id in failed:
            it.decision = "buy"
            it.reasons = list(it.reasons) + [
                "geometry engine failed: " + ("; ".join(failed[it.ldraw_id].warnings) or "unknown")]
    items = [it for it in items if it.ldraw_id not in failed]

    # one job per (part, colour)
    agg: dict[tuple[str, int], list] = {}
    for it in items:
        k = (it.ldraw_id, it.color_id)
        q = it.quantity + (it.spares if spares else 0)
        if k in agg:
            agg[k][0] += q
        else:
            agg[k] = [q, it.color_name, it.color_rgb]
    jobs = [PrintJob(results[pid], q, cid, cn, rgb)
            for (pid, cid), (q, cn, rgb) in sorted(agg.items())]
    plates = build_plates(jobs, printer, out)
    manifest = json.loads((out / "manifest.json").read_text())

    # parts the packer could not place (too big for the bed) also go to the buy list
    skipped = manifest.get("skipped", [])
    skip_key = {(s["part_id"], s["color_id"]): s["reason"] for s in skipped}
    for it in items:
        reason = skip_key.get((it.ldraw_id, it.color_id))
        if reason:
            it.decision = "buy"
            it.reasons = list(it.reasons) + [f"not printable on {printer.name}: {reason}"]

    buy_files = catalog.write_buy_list(plan, out)
    preset = write_process_preset(prof, printer, out)
    prof.save(out / "profile.json")
    (out / "plan.json").write_text(catalog.plan_to_json(plan))
    (out / "parts.json").write_text(json.dumps(
        {pid: {**{k: v for k, v in asdict(r).items() if k != "mesh_path"},
               "mesh_path": str(r.mesh_path) if r.mesh_path else None}
         for pid, r in sorted(results.items())}, indent=1))

    summary = _set_report(plan, prof, printer, out, results, items, plates, manifest,
                          include_risky=include_risky, spares=spares, only_color=only_color,
                          limit=limit, preset=preset, buy_files=buy_files,
                          profile_lines=profile_lines or [])
    return summary


def _set_report(plan, prof, printer, out, results, items, plates, manifest, *, include_risky,
                spares, only_color, limit, preset, buy_files, profile_lines) -> dict:
    printed = [it for it in items if it.decision in ("print", "risky")]
    pieces_by_pid = Counter()
    for it in printed:
        pieces_by_pid[it.ldraw_id] += it.quantity + (it.spares if spares else 0)
    built = {pid: r for pid, r in results.items() if r.ok}
    tier_parts = Counter(results[pid].tier for pid in pieces_by_pid)
    tier_pieces = Counter()
    for pid, n in pieces_by_pid.items():
        tier_pieces[results[pid].tier] += n
    failed = {pid: r for pid, r in results.items() if not r.ok}
    dec = Counter()
    dec_pcs = Counter()
    for it in plan.items:
        dec[it.decision] += 1
        dec_pcs[it.decision] += it.quantity
    by_colour = defaultdict(lambda: {"plates": 0, "objects": 0, "g": 0.0})
    for pl in plates:
        c = by_colour[pl.color_name]
        c["plates"] += 1
        c["objects"] += pl.object_count
        c["g"] += pl.filament_g
    total_g = round(sum(pl.filament_g for pl in plates), 1)

    L = [f"# {plan.set_num} {plan.name} ({plan.year})", ""]
    if profile_lines:
        L += ["```", *profile_lines, "```", ""]
    L += [f"- profile: `{prof.name}` (geometry key `{prof.geometry_key()}`, "
          f"{'calibrated' if prof.calibrated else '**not calibrated**'})",
          f"- printer: {printer.name}; process preset: `{preset.name}`",
          f"- options: risky {'included' if include_risky else 'excluded'}, spares "
          f"{'included' if spares else 'excluded'}"
          + (f", only colour {only_color}" if only_color else "")
          + (f", limited to {limit} unique parts" if limit else ""),
          f"- printed: {len(printed)} lots, {sum(pieces_by_pid.values())} pieces, "
          f"{len(pieces_by_pid)} unique parts; {len(plates)} plates, {total_g} g "
          "(solid-volume estimate)", ""]
    L += ["## Decisions (after the build)", "", "| decision | lots | pieces |", "|---|---|---|"]
    for d in ("print", "risky", "buy", "skip"):
        if dec[d]:
            L.append(f"| {d} | {dec[d]} | {dec_pcs[d]} |")
    L += ["", "## Engine tiers", "", "| tier | unique parts | pieces |", "|---|---|---|"]
    for t in sorted(set(tier_parts) | set(tier_pieces)):
        L.append(f"| {t} | {tier_parts[t]} | {tier_pieces[t]} |")
    L += ["", "Tier A: verified watertight solid. Tier B: overlapping shells the slicer has "
          "to union (not verified).", ""]
    L += ["## Plates per colour", "", "| colour | plates | objects | g |", "|---|---|---|---|"]
    for name, c in sorted(by_colour.items()):
        L.append(f"| {name} | {c['plates']} | {c['objects']} | {c['g']:.1f} |")
    L.append(f"| **total** | {len(plates)} | {sum(c['objects'] for c in by_colour.values())} "
             f"| {total_g} |")
    sup = sorted(pid for pid, r in built.items() if r.needs_support and pid in pieces_by_pid)
    L += ["", "## Needs support", ""]
    L += [f"- {pid} {built[pid].description}" for pid in sup] or ["none"]
    L += ["", "## Warnings", ""]
    warn = [(pid, r) for pid, r in sorted(built.items()) if r.warnings and pid in pieces_by_pid]
    L += [f"- {pid} (tier {r.tier}) {r.description}: " + "; ".join(r.warnings)
          for pid, r in warn] or ["none"]
    L += ["", "## Failed in the engine (moved to the buy list)", ""]
    L += [f"- {pid} {r.description}: " + "; ".join(r.warnings)
          for pid, r in sorted(failed.items())] or ["none"]
    if manifest.get("skipped"):
        L += ["", "## Not placed on a plate (moved to the buy list)", ""]
        L += [f"- {s['part_id']} {s['color_name']} x{s['quantity']}: {s['reason']}"
              for s in manifest["skipped"]]
    L += ["", "## Files", "", "- plates: `plates/<colour>/plate_NN.3mf`, part STLs: `stl/`",
          "- `manifest.md` / `manifest.json`: plate contents and filament",
          *[f"- `{p.name}`" for p in buy_files],
          f"- `{preset.name}`: import into Bambu Studio (File > Import > Import Configs...)",
          "- `profile.json`, `plan.json`, `parts.json`: the profile, set plan and per-part "
          "engine results used", ""]
    (out / "report.md").write_text("\n".join(L), encoding="utf-8")
    return {
        "lots_printed": len(printed), "pieces_printed": sum(pieces_by_pid.values()),
        "unique_parts": len(pieces_by_pid), "tiers_parts": dict(tier_parts),
        "tiers_pieces": dict(tier_pieces), "plates": len(plates),
        "by_colour": {k: dict(v) for k, v in by_colour.items()}, "total_g": total_g,
        "failed": sorted(failed), "skipped": manifest.get("skipped", []),
        "decisions": dict(dec), "needs_support": sup,
    }


def cmd_set_build(a) -> int:
    printer = get_printer(a.printer)
    prof = _profile_from_arg(a.profile)
    lines = _announce_profile(prof, bool(a.profile), printer.key)
    from . import catalog

    set_num = catalog.normalize_set_num(a.set)
    out = Path(a.out) if a.out else Path.cwd() / set_num
    s = build_set(set_num, prof, printer, out, include_risky=a.risky, spares=a.spares,
                  only_color=a.only_color, limit=a.limit, profile_lines=lines)
    print(f"printed: {s['lots_printed']} lots, {s['pieces_printed']} pieces, "
          f"{s['unique_parts']} unique parts")
    print("tiers (unique parts): " + ", ".join(f"{k} {v}" for k, v in sorted(s["tiers_parts"].items())))
    for name, c in sorted(s["by_colour"].items()):
        print(f"  {name:<22} {c['plates']:>2} plates {c['objects']:>4} objects {c['g']:>7.1f} g")
    print(f"total: {s['plates']} plates, {s['total_g']} g")
    if s["failed"]:
        print("engine failures (moved to buy list): " + ", ".join(s["failed"]))
    if s["skipped"]:
        print(f"{len(s['skipped'])} jobs did not fit the printer (moved to buy list)")
    print(f"buy: {s['decisions'].get('buy', 0)} lots; see {out / 'buy_list.csv'}")
    print(f"report: {out / 'report.md'}")
    if not prof.calibrated:
        print("REMINDER: these parts were generated with an uncalibrated profile.")
    return 0


# ------------------------------------------------------------------------------------------
# parser
# ------------------------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="calibrick",
                                 description="Printer-calibrated STLs for LEGO-compatible parts and sets.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    printers = sorted(PRINTERS)

    p = sub.add_parser("setup", help="download the LDraw library and Rebrickable data")
    p.add_argument("--refresh", action="store_true", help="download again even if present")
    p.set_defaults(func=cmd_setup)

    cal = sub.add_parser("calibrate", help="calibration kit and solver").add_subparsers(
        dest="sub", required=True)
    p = cal.add_parser("kit", help="write the coupon kit and the locked process preset")
    p.add_argument("--printer", default="a1mini", choices=printers)
    p.add_argument("--base", help="profile to start from (name or path)")
    p.add_argument("--out", help="output folder (default ./calibrick_kit_<tag>)")
    p.add_argument("--step", type=float, default=0.05, help="ladder step in mm (first kit)")
    p.add_argument("--next-from", metavar="RESULTS", help="results of the previous kit")
    p.add_argument("--kit", metavar="KIT_JSON", help="kit.json of the previous kit")
    p.set_defaults(func=cmd_calibrate_kit)
    p = cal.add_parser("solve", help="turn kit results into a profile")
    p.add_argument("results")
    p.add_argument("--kit", required=True, metavar="KIT_JSON")
    p.add_argument("--name", help="profile name (default <printer>-cal)")
    p.add_argument("--base", help="profile to start from (name or path)")
    p.set_defaults(func=cmd_calibrate_solve)
    p = cal.add_parser("ladder", help="one real part built N times with one profile field stepped")
    p.add_argument("--part", required=True, help="LDraw part id, e.g. 15712")
    p.add_argument("--field", required=True, help="profile field to step, e.g. clip_d, bar_d, pin_hole_d")
    p.add_argument("--center", type=float, help="middle value (default: the profile's current value)")
    p.add_argument("--step", type=float, default=0.05)
    p.add_argument("--n", type=int, default=9, help="number of copies")
    p.add_argument("--profile", help="profile name or path")
    p.add_argument("--out", help="output folder")
    p.set_defaults(func=cmd_calibrate_ladder)
    p = cal.add_parser("apply", help="set a profile field from the ratings of a part ladder")
    p.add_argument("ladder_json")
    p.add_argument("ratings", help='quoted ratings, station 1 first, e.g. "5 4 3 3 2 1 1 1 1"')
    p.add_argument("--profile", required=True, help="profile name or path to update")
    p.set_defaults(func=cmd_calibrate_apply)
    p = cal.add_parser("verify", help="real parts built with a profile, to confirm the fit")
    p.add_argument("--profile", help="profile name or path (default: nominal, uncalibrated)")
    p.add_argument("--out", help="output folder (default ./calibrick_verify_<profile>)")
    p.set_defaults(func=cmd_calibrate_verify)

    pr = sub.add_parser("profile", help="list or show profiles").add_subparsers(
        dest="sub", required=True)
    pr.add_parser("list").set_defaults(func=cmd_profile_list)
    p = pr.add_parser("show")
    p.add_argument("name")
    p.set_defaults(func=cmd_profile_show)

    p = sub.add_parser("part", help="build calibrated STLs for LDraw part ids")
    p.add_argument("ids", nargs="+")
    p.add_argument("--profile")
    p.add_argument("--out")
    p.set_defaults(func=cmd_part)

    st = sub.add_parser("set", help="find, inspect and build sets").add_subparsers(
        dest="sub", required=True)
    p = st.add_parser("find")
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_set_find)
    p = st.add_parser("info")
    p.add_argument("set")
    p.set_defaults(func=cmd_set_info)
    p = st.add_parser("build")
    p.add_argument("set")
    p.add_argument("--profile", help="profile name or path (default: nominal, uncalibrated)")
    p.add_argument("--printer", default="a1mini", choices=printers)
    p.add_argument("--out", help="output folder (default ./<set number>)")
    p.add_argument("--include-risky", dest="risky", action="store_true", default=True,
                   help="print risky parts too (default)")
    p.add_argument("--no-risky", dest="risky", action="store_false",
                   help="leave risky parts out (they stay on risky_list.csv)")
    p.add_argument("--spares", dest="spares", action="store_true", default=False,
                   help="also print the spare pieces the real set includes")
    p.add_argument("--no-spares", dest="spares", action="store_false", help="(default)")
    p.add_argument("--only-color", metavar="NAME", help="only this colour (Rebrickable name)")
    p.add_argument("--limit", type=int, metavar="N",
                   help="only the N most-used unique parts (quick tests)")
    p.set_defaults(func=cmd_set_build)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return int(a.func(a) or 0)
    except CLIError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        from . import catalog

        if isinstance(e, (catalog.DataError, catalog.SetNotFound, KeyError)):
            print(f"error: {e}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    sys.exit(main())
