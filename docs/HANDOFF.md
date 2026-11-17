# Handoff: state of calibrick and what to do next

Written for whoever continues this (a person or another model). Read `docs/ARCHITECTURE.md`
for the structure, `docs/CALIBRATION.md` for the physical loop, and the docstring of
`calibrick/engine/core.py` for how geometry is corrected. This file is the status and the
reasoning that is not obvious from the code.

## Where things stand (2026-11-16, evening)

| Piece | State |
|---|---|
| Calibration coupons (`coupons.py`), solver (`calib.py`) | Working. Kit v1 sent to the user; he is printing it on the **A1 mini**. `kits/v1/kit.json` records what was printed. |
| Locked Bambu process presets (`slicer/bambu.py`, `presets/`) | Generated from the real Bambu profile definitions. **Import into Bambu Studio not verified** (no slicer here); manual fallback table in `presets/README.md`. |
| LDraw reader (`ldraw/`) | Working: BFC winding, hi-res primitives, provenance per triangle. |
| Geometry engine (`engine/core.py`) | Working. Paris set 21064: 76 of 122 parts (759 of 931 pieces) verified closed solids (tier A); the rest exported as overlapping shells (tier B). No failures. |
| Catalog (`catalog/`) | Working, 38 tests. Set → lots, colours, print / risky / buy, LDraw id resolution, buy list CSVs. |
| Plates and 3MF (`output/`) | Working, tests pass. Plain multi-object 3MF; placement verified with PrusaSlicer, not with Bambu Studio. |
| CLI, README, engine tests, end-to-end run, slicing check | Being written by a parallel worker at the time of writing. |

Nothing has been test-printed except by the user tonight. Treat every fit number as
unproven until his coupon results are in a profile and a verification print passes.

## The calibration loop: what happens next

1. User pastes the report from `docs/CALIBRATION.md` (ratings 1-5 per station, caliper readings).
2. `calib.solve(kit, parse_results(text))` → `Profile` (save as `a1mini-<filament>.json`) plus a
   report. `calib.next_ladders()` gives a finer kit (0.02 steps) around each answer.
3. **Verification kit** (not built yet): real parts from the engine with the new profile:
   3001 (brick 2x4), 3022 (plate 2x2), 3023b (plate 1x2), 3024 (plate 1x1), 6141 (round 1x1),
   3070b (tile 1x1), 87079 (tile 2x4). Checks: genuine on printed, printed on genuine, printed
   on printed, and several plates side by side on a genuine baseplate (exterior clearance).
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
