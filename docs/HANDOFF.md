# Handoff: state of calibrick and what to do next

Written for whoever continues this (a person or another model). Read `docs/ARCHITECTURE.md`
for the structure, `docs/CALIBRATION.md` for the physical loop, and the docstring of
`calibrick/engine/core.py` for how geometry is corrected. This file is the status and the
reasoning that is not obvious from the code.

## Where things stand (2026-11-16, evening)

| Piece | State |
|---|---|
| Calibration coupons (`coupons.py`), solver (`calib.py`) | Working. **Kit v1 started printing on the user's A1 mini at 21:50** (Bambu PLA Basic, textured PEI, 2 h 13 min, 28.9 g). `kits/v1/kit.json` records what was printed. Results expected around midnight. |
| Locked Bambu process presets (`slicer/bambu.py`, `presets/`) | **Import into Bambu Studio confirmed by the user** (A1 mini preset; his screenshot shows it selected with 0.16 / 0.16 layers and 31 layers = 4.96 mm). P1P preset untested. |
| LDraw reader (`ldraw/`) | Working: BFC winding, hi-res primitives, provenance per triangle. |
| Geometry engine (`engine/core.py`, `engine/repair.py`) | Working. Paris set 21064: all 122 parts build; 107 (876 of 931 pieces) are verified closed solids (tier A), 15 are overlapping shells (tier B). |
| Catalog (`catalog/`) | Working. Set → lots, colours, print / risky / buy, LDraw id resolution, buy list CSVs. |
| Plates and 3MF (`output/`) | Working. Plain multi-object 3MF; placement verified with PrusaSlicer, not with Bambu Studio. |
| CLI (`cli.py`), README, tests | Working: `calibrick setup / calibrate kit|solve|verify / profile / part / set find|info|build`. 88 tests pass. `setup` downloads are untested (no network in the sandbox). |
| End-to-end Paris build | `calibrick set build 21064-1` → 239 lots, 931 pieces, 24 plates, ~837 g, 9 lots to buy, in about a minute. Nominal profile only, so not a fit you can trust yet. |

Nothing has been test-printed except the coupon kit running tonight. Treat every fit number
as unproven until the coupon results are in a profile and a verification print passes.

Two workers were cut off by usage limits; their partial work is in the tree and tested:
- `engine/repair.py` (`cap_loops`): closes leftover open loops so more bodies become solids
  (76 → 107 tier A). It was mid-way through loops whose outline is not a simple polygon
  (parts 3007, 32828, 3563, 30503, 96874). Parts it touched carry a warning asking for a look
  at the slicer preview, because a cap could in principle fill a real opening.
- `tools/slice_check.py` slices every part with a PrusaSlicer stand-in
  (`/home/claude/work/output/psroot` in the sandbox, not in the repo) and compares filament
  volume with mesh volume. Its last saved result predates the final engine; re-run it.

Print time matters: the locked preset (40 mm/s outer walls, 6 wall loops) took 2 h 13 min for
28.9 g. At that rate the Paris set is roughly 60 hours of printing. The user said time is not
a constraint, but a faster preset is an obvious later experiment (it needs its own calibration).

## The calibration loop: what happens next

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
- **0.16 mm layers, first layer 0.16.** 3.2 mm plates = 20 layers, 9.6 mm bricks = 60,
  1.76 mm studs = 11, 1.92 mm cavity = 12. With a 0.2 first layer none of these land on a
  layer boundary.
- **LDraw is not print geometry.** Known systematic differences that the engine corrects:
  exterior on the grid plane (real: 0.1 mm inside), plate cavity 1.6 deep (a genuine stud is
  1.7-1.8 tall, so sockets are deepened to 1.92), stud 1.6 tall (we print 1.76), tube 6.4 OD
  (real 6.51), wall 1.6 (real 1.5, which falls out of the exterior move).
- **Vertex displacement, not remeshing.** Moving welded vertices keeps LDraw's topology, so
  a mesh that was closed stays closed, and every flat face stays flat.
- **Tier B exists on purpose.** Slicers union overlapping shells, and the fit-critical studs
  and tubes are always our own closed solids, so tier B parts usually print fine. They are
  just not verified. The slicing check is how to find the ones that are actually broken.

## Known limits, in rough priority order

1. **Technic, bar and clip fits are uncalibrated** (`pin_hole_d`, `pin_d`, `axle_hole_grow`,
   `axle_shrink`, `bar_d`, `bar_hole_d`). Build coupon T the same way as S/U: (a) a 1x9
   Technic-beam-like strip, 7.8 mm thick, printed lying flat so the holes are vertical, hole
   diameter 4.80 → 5.20 in 0.05 steps, test with a genuine pin (rate insertion and whether the
   friction ridges hold); (b) a row of 3.18 mm bars standing upright, diameter 3.00 → 3.40,
   test with a genuine clip; (c) a row of vertical holes 3.10 → 3.50 for a genuine bar;
   (d) axle holes are a cross of 4.8 x 1.8 mm arms: ladder the arm width and length offset.
   Parts with horizontal pin holes (Technic bricks) print the hole sideways; expect to need a
   separate, larger value or a teardrop top for those.
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
