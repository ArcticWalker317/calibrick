# Handoff: state of calibrick and what to do next

Written for whoever continues this (a person or another model). Read `docs/ARCHITECTURE.md`
for the structure, `docs/CALIBRATION.md` for the physical loop, and the docstring of
`calibrick/engine/core.py` for how geometry is corrected. This file is the status and the
reasoning that is not obvious from the code.

## Where things stand (2026-11-16, evening)

| Piece | State |
|---|---|
| Calibration coupons (`coupons.py`), solver (`calib.py`) | Working in software; **no coupon has printed successfully yet.** Kits v1 and v1b (0.16 mm layers, 0.16 mm first layer; Bambu PLA Basic on textured PEI) both failed in the first layers: small islands let go, then even the solid stud coupon's first layer tore. Cause judged to be the non-stock 0.16 mm first layer. **Kit v2** uses the 0.2 mm scheme and the stock first layer (`kits/v2/kit.json`, preset `calibrick 0.20mm @BBL A1M`). Solve results against the kit.json of the kit he actually printed. |
| Locked Bambu process presets (`slicer/bambu.py`, `presets/`) | **Import into Bambu Studio confirmed by the user** (A1 mini; the 0.16 mm version imported and sliced correctly, the 0.20 mm version uses the same format). P1P preset untested. |
| LDraw reader (`ldraw/`) | Working: BFC winding, hi-res primitives, provenance per triangle. |
| Geometry engine (`engine/core.py`, `engine/repair.py`) | Working. Paris set 21064: all 121 distinct parts build; 100 (856 of 931 pieces) are verified closed solids (tier A), 21 are overlapping shells (tier B). |
| Catalog (`catalog/`) | Working. Set → lots, colours, print / risky / buy, LDraw id resolution, buy list CSVs. |
| Plates and 3MF (`output/`) | Working. Plain multi-object 3MF; placement verified with PrusaSlicer, not with Bambu Studio. |
| CLI (`cli.py`), README, tests | Working: `calibrick setup / calibrate kit|solve|verify / profile / part / set find|info|build`. 88 tests pass. `setup` downloads are untested (no network in the sandbox). |
| End-to-end Paris build | `calibrick set build 21064-1` → 239 lots, 931 pieces, 24 plates, ~837 g, 9 lots to buy, in about a minute. Nominal profile only, so not a fit you can trust yet. |

Nothing has been test-printed except the coupon kit running tonight. Treat every fit number
as unproven until the coupon results are in a profile and a verification print passes.

Two workers were cut off by usage limits; their partial work is in the tree and tested:
- `engine/repair.py`: loop detection and triangulation for closing leftover open loops.
  Capping EVERY loop gave 107 tier A parts but can fill a real opening, so the engine
  (`_cap_safe_loops` in core.py) only caps loops that are small (<= 4 mm) or flat with an
  existing face behind them: 100 tier A. The worker was mid-way through loops whose outline is
  not a simple polygon (parts 3007, 32828, 3563, 30503, 96874). The 21 tier B parts and their
  open / over-shared edge counts are listed by `/home/claude/work/tierb/stats.py`-style sweeps
  (expand_part + build_mesh over the set's ids); biggest by piece count: 3633 fence (17),
  32828 (7), 15068, 30136, 30414, 4569 (6 each).
- `tools/slice_check.py` slices every part with a PrusaSlicer stand-in
  (`/home/claude/work/output/psroot` in the sandbox, not in the repo) and compares filament
  volume with mesh volume. Its last saved result predates the final engine; re-run it.

Slicing check after the last engine fixes (`tools/slice_check.py`, PrusaSlicer stand-in):
all 100 tier A parts slice with extruded volume within 5-15 % of mesh volume, none
suspicious. Of the 21 tier B parts, 16 slice to the right volume and 5 are confirmed bad:
13731 (does not reach the bed in places, slicer refuses), 32828 (only 0.74 of the volume),
91405 (16x16 plate, 0.90), 2654 (1.17), 75937 (slicer errors). Treat those five as "buy or
fix" until repaired; the report does not mark them yet.

Print time matters: the locked preset (40 mm/s outer walls, 6 wall loops) took 2 h 13 min for
28.9 g. At that rate the Paris set is roughly 60 hours of printing. The user said time is not
a constraint, but a faster preset is an obvious later experiment (it needs its own calibration).

## The calibration loop: what happens next

**First-layer adhesion is an open risk for the whole project.** Real plates, tiles and bricks
sit on the bed with the same 1.5 mm wall outlines and free-standing tubes that came loose on
the v1 coupon. If v1b also loses islands on a clean plate, change the first layer in
`slicer/bambu.py` before anything else (hotter bed, slower first layer, or a 0.2 mm first
layer with the Z levels re-planned), then recalibrate. If real parts need help, the same
anchor tabs can be added in the engine: corners of the exterior, and spokes on tubes pointing
between stud positions (see `coupons.underside_coupon`), to be trimmed off after printing.

1. User pastes the report from `docs/CALIBRATION.md` (ratings 1-5 per station, caliper
   readings). Save it as `kits/v1/results.txt` and commit it: it is the only record of the print.
2. `calibrick calibrate solve kits/v1/results.txt --kit kits/v1/kit.json --name a1mini-pla-basic`
   prints the fit per ladder and saves the profile (copy the JSON into `profiles/` in the repo
   too, so it survives the sandbox). Read the report before trusting it:
   - a ladder marked `all_loose` / `all_tight` has no answer yet; send a shifted kit
     (`calibrick calibrate kit --next-from results.txt --kit kit.json`);
   - "studs print X vs model" far from -0.03 or an `ext_clearance` at its 0 or 0.30 limit means
     first-layer flare or a mis-read caliper; ask before building on it.
3. **Verification kit:** `calibrick calibrate verify --profile a1mini-pla-basic --out verify_v1`
   writes one pre-arranged STL of 25 real parts (2x4 bricks, plates, tiles, round 1x1s) plus the
   preset. Send the STL; the five checks are in `docs/CALIBRATION.md`.
4. Expect to tune, in this order: `tube_od` (the only inferred value, see CALIBRATION.md),
   `ext_clearance`, `post_d` (posts under 1xN parts are set to 3.0 mm so they do not grip;
   the coupon has no post), then `stud_h` / `roof_raise` if parts do not seat flush.

Things the coupon may reveal that are fixed in the preset, not the profile:
- **First-layer flare** (cells tight at the entry, exterior wider at the bed): the A1 mini
  stock preset has elephant-foot compensation 0. Set `elefant_foot_compensation` to ~0.1 in
  `slicer/bambu.py`, regenerate the preset, and re-run the kit (a preset change invalidates
  the calibration).
- **Seam bumps on studs**: scarf seam is already on; if studs still rate inconsistent,
  try seam position "back" or random and re-run.

## Design decisions and why

- **Per-feature geometry compensation instead of slicer XY compensation.** One part needs
  opposite corrections (walls in, tubes out), and the slicer has one number each for
  contours and holes. Slicer compensation is locked at 0.
- **Calibrate against genuine parts, by feel, on ladders.** Calipers cannot resolve the
  0.02-0.05 mm that decides clutch; a ladder of nine sizes rated against a genuine brick can.
  Calipers are only used for scale and wall offset.
- **Coupon geometry = part geometry.** The engine re-creates studs with the same
  `csg.stud()` as the coupon (96 segments, one-layer lead-in on top), and coupon cells are
  real plate undersides with free-standing 1.5 mm walls, printed open side down. A ladder
  result is therefore directly the number the engine uses.
- **0.2 mm layers with the stock 0.2 mm first layer.** 3.2 mm plates = 16 layers, 9.6 mm
  bricks = 48, 1.8 mm studs = 9, 2.0 mm plate cavities = 10, so every Z level sits on a layer
  boundary. The first attempt used 0.16 mm layers with a 0.16 mm first layer for the same
  reason; on the user's textured PEI that first layer came out as a thin film and tore, twice.
  Do not go below the stock first-layer height again. If finer layers are ever wanted, 0.1 mm
  with a 0.2 mm first layer also keeps every level on a boundary.
- **LDraw is not print geometry.** Known systematic differences that the engine corrects:
  exterior on the grid plane (real: 0.1 mm inside), plate cavity 1.6 deep (a genuine stud is
  1.7-1.8 tall, so sockets are deepened to 2.0), stud 1.6 tall (we print 1.8), tube 6.4 OD
  (real 6.51), wall 1.6 (real 1.5, which falls out of the exterior move).
- **Vertex displacement, not remeshing.** Moving welded vertices keeps LDraw's topology, so
  a mesh that was closed stays closed, and every flat face stays flat.
- **Tier B exists on purpose.** Slicers union overlapping shells, and the fit-critical studs
  and tubes are always our own closed solids, so tier B parts usually print fine. They are
  just not verified. The slicing check is how to find the ones that are actually broken.

## Known limits, in rough priority order

1. **Technic, bar and clip fits are uncalibrated** (`pin_hole_d`, `pin_d`, `axle_hole_grow`,
   `axle_shrink`, `bar_d`, `bar_hole_d`, `clip_d`). The mechanism to calibrate them exists:
   **part ladders** (`calibrick/ladder.py`, `calibrick calibrate ladder|apply`, see
   CALIBRATION.md) build one real part N times with a field stepped. None has been printed.
   For the Paris set the order is `bar_d` on 60478, then `clip_d` on 15712, then `bar_hole_d`
   on 85861. Still open: axle holes are a cross of flat faces, so `axle_hole_grow` only moves
   their round parts; give the flat faces an offset too. Parts with horizontal pin holes
   (Technic bricks) print the hole sideways and will want a separate, larger value or a
   teardrop top. The 14 "risky" bar/clip lots in the Paris set (3633 fence, 60478, 15712,
   32828, 44861, 4081b ...) stay risky until these ladders are done.
2. **Side studs** (brackets, headlight bricks, SNOT bricks) are synthesised but print lying
   down, so they come out slightly flat and rough. Options: per-part orientation choice, or a
   separate `side_stud_d`.
3. **Only the global bottom plane counts as a stud cavity.** Parts with cavities at other
   heights or facing sideways get no cavity-wall offset and no ceiling raise there.
4. **Print orientation is always as-built.** Inverted slopes, arches and some brackets want a
   different orientation or supports; `needs_support` is a rough heuristic (downward faces
   wider than 1 mm that cannot be bridged over 14 mm).
5. **Hand-modelled curves are not offset** (only flat faces and primitive circles are), and
   stud variants that are cut by a curved body (stud10, stud15) keep LDraw geometry.
6. **Decorated parts print blank; transparent and flexible parts are flagged**, not solved.
7. **Colour → filament mapping** is not built: plates are grouped by LEGO colour only.
8. **Bambu-native project 3MF** (named objects on plates with the preset embedded) is not
   built; plates are plain 3MF plus an importable preset.
9. `setup` downloads (LDraw complete.zip, Rebrickable CSVs) are written but could not be run
   in the sandbox.
   Also: parts 5264, 5091, 5092 and 7307 of the Paris set have no geometry in the mirror used
   here. They are recent moulds; check the official library and LDraw's unofficial parts
   (`<ldraw>/unofficial/parts` is already searched by the reader) before buying them.
10. Performance: the 16x16 plate takes ~18 s (per-vertex Python loop in the displacement solve
    and the zipper); fine for now, vectorise if it matters.

## Working in the dev sandbox

- LDraw library: `git clone --depth 1 https://github.com/pybricks/ldraw` (a mirror; ldraw.org
  itself is blocked) → `CALIBRICK_LDRAW=<clone>`.
- Rebrickable: the `rb.db` SQLite from github.com/ojuuji/rb.db releases → put it in the
  directory named by `CALIBRICK_REBRICKABLE`.
- Bambu reference profiles: github.com/bambulab/BambuStudio, `resources/profiles/BBL`.
- `python3 -I -m pytest tests -q`; `tools/slice_plot.py part.stl out.png 1.0 4.0` draws exact
  cross-sections, the fastest way to check a dimension.
- The engine is fast enough to sweep: `expand_part` + `build_mesh` over a set's id list, count
  tiers, read the warnings.

## Ideas worth trying later

- F1 / Speed Champions sets (the user's real goal): lots of curved slopes, brackets, clips and
  wheels. Needs items 1, 2 and 4 above first; tyres and windscreens stay on the buy list.
- A local web UI for calibration entry and set browsing (the CLI is the engine room).
- Sending plates to the printers directly (Bambu LAN mode) once output is trusted.
- Per-colour quick-check coupon (3 stations around the calibrated value) when changing spools.
