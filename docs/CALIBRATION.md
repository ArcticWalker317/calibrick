# Calibration walkthrough

A calibration is valid for one **printer + filament + plate type + the locked process preset**.
Do it once per combination; re-check with one coupon when you change filament brand or colour.

## 1. Print the kit

1. In Bambu Studio select the printer, then **File > Import > Import Configs...** and pick
   `presets/calibrick 0.16mm @BBL A1M.json` (see `presets/README.md` for the manual fallback).
2. Process: `calibrick 0.16mm @BBL A1M`. Filament: your normal preset for the spool, untouched.
3. Import `calibrick_kit_v1_both.stl`. Do not scale, rotate or flip it: it is already lying
   the right way (the underside coupon prints open side down, like real parts).
4. No supports, no brim. Leave flow dynamics calibration and bed levelling on.
5. Let the print cool to room temperature before testing.

The kit is two flat coupons with stations numbered 1 to 9:

| Coupon | Lane | What steps along it | Station 1 → 9 | Test with a genuine… |
|---|---|---|---|---|
| S (studs on top) | – | stud diameter | 4.70 → 5.10 mm | 2x2 brick or plate pressed on |
| U (hollow side) | A, 1x2 cells | gap between the walls | 4.70 → 5.10 mm | 1x2 plate/brick pressed in |
| U | B, 2x2 cells with a tube | tube diameter | 6.30 → 6.70 mm | 2x2 plate/brick pressed in |
| U | C, rings | ring bore | 4.75 → 5.15 mm | one stud (any 1-wide part) pressed in |

For coupon U, lay it roof-down on the table and press the genuine part's studs into each
cell. The numbers are engraved on both faces; the bar under each digit is its bottom.
Use the **same genuine part** for a whole lane, ideally a newer one that still clutches well.

## 2. Rate every station

| Rating | Meaning |
|---|---|
| 1 | loose: no real grip, falls off |
| 2 | holds, but lighter than a genuine connection |
| 3 | like genuine |
| 4 | tighter than genuine, but seats fully and comes apart by hand |
| 5 | too tight: will not seat fully, or needs real force |
| - | not tested |

## 3. Measure with calipers (light pressure, a couple of readings each)

- S length (nominal 143.80), S width (23.80), S thickness of the plate only (3.20),
  S total height plate + stud (4.96)
- one stud across its diameter at stations 1, 5 and 9 (model 4.70 / 4.90 / 5.10)
- U length (150.20) and the outside width of one lane-B cell (15.80)

## 4. Report

Paste this back (or save it as a text file for `calibrick calibrate solve`):

```
printer: A1 mini
filament: brand / type / colour
plate: textured PEI
S: 1 1 2 3 3 4 5 5 5
A: 5 5 4 3 3 2 1 1 1
B: 1 1 2 3 4 5 5 5 5
C: 5 4 3 3 2 1 1 1 1
S length: 143.71
S width: 23.74
S thickness: 3.18
S total height: 4.93
stud 1: 4.66
stud 5: 4.87
stud 9: 5.06
U length: 150.10
U cell B: 15.76
notes: anything odd (first-layer flare, stringing, a station that felt different)
```

## How the numbers are used

- Each lane gives the model-space size whose fit is "like genuine" (centre of the 3s, or
  interpolated between a 2 and a 4). That value goes straight into the profile:
  `stud_d`, `cavity_gap`, `tube_od`, `socket_d`.
- Lane B was printed with nominal 1.5 mm walls (gap 4.8). If lane A says the walls should
  sit elsewhere, the tube is shifted by `0.7 * sqrt(2) * (cavity_gap - 4.8)`: an estimate of
  how much of the lost or gained wall grip the tube has to take over. This is the one
  inferred number in the profile, so the verification kit re-checks 2-wide parts directly.
- Caliper lengths separate **scale** (shrinkage, matters on long plates) from **offset**
  (how fat walls print). Scale becomes `xy_scale`; offset becomes `ext_clearance`
  (0.10 mm per side is the real-brick clearance, plus half the measured oversize).
- Height readings are reported, not compensated: layers are quantised, so a plate that is
  more than 0.1 mm off means first-layer squish or Z needs attention at the printer.

## Second pass

`calib.next_ladders()` builds the next kit: 0.02 mm steps around every answer that was
found, or a shifted ladder where every station was too loose or too tight. After that the
verification kit is real parts generated with the profile (2x4 brick, 2x2 and 1x2 plates,
1x1 round plate) checked against genuine parts and against each other.

### Verification kit checks

`calibrick.verify.build_verification_kit(profile, out_dir)` writes one pre-arranged STL of
real parts. Print it with the same preset, filament and plate, then check:

1. **Genuine on printed:** a genuine 2x2 on the printed 2x4 brick and 2x2 plate studs.
2. **Printed on genuine:** every printed part pressed onto a genuine plate. Tiles and plates
   must seat flush with no gap underneath (a gap means sockets are too shallow or too tight).
3. **Printed on printed:** stack the two 2x4 bricks; the pair should measure 19.2 mm without
   studs, and pull apart like genuine bricks.
4. **Side by side:** printed 1x1 plates and tiles packed next to each other on a genuine
   plate must drop in without pushing each other (exterior clearance), with a hairline gap.
5. **Pitch:** the 1x4 plate and 2x4 tile must seat on four studs in a row without rocking
   (XY scale).

Report which check failed and in which direction (loose / tight / gap); each maps to one
profile field: 1 → `stud_d`, 2 → `cavity_gap` (1-wide) or `tube_od` (2-wide) or `socket_d`
(round), a gap under seated parts → `roof_raise`, 4 → `ext_clearance`, 5 → `xy_scale`.

## Bars, clips, Technic holes: part ladders

The coupon kit covers studs, walls, tubes and sockets. Every other fit is calibrated on a
real part: the same part is built several times with one profile field stepped, each copy
tied to a numbered tab by a thin strip you snap off afterwards.

```
calibrick calibrate ladder --part 60478 --field bar_d      --profile NAME   # handle; test with a genuine clip
calibrick calibrate ladder --part 15712 --field clip_d     --profile NAME   # clip; test with a genuine bar
calibrick calibrate ladder --part 85861 --field bar_hole_d --profile NAME   # open stud; test with a genuine bar
calibrick calibrate ladder --part 3700  --field pin_hole_d --profile NAME   # Technic hole; test with a genuine pin
calibrick calibrate apply ladder_15712_clip_d.json "5 5 4 3 3 2 1 1 1" --profile NAME
```

Same 1-5 ratings, copy 1 first. Do bars before clips: once `bar_d` is right, printed bars and
handles can stand in for genuine ones. Any field in `calibrick/ladder.py: DIRECTION` can be
laddered on any part that has the feature; the command warns if stepping the field does not
change that part. Until a field has been laddered it holds a reasonable FDM starting value,
and the set report marks parts that depend on it as "risky".

Not covered yet: axle holes and axles (the cross is flat-sided, not a circle, so only its
round parts move), and holes that print sideways (Technic bricks) versus upright (beams)
share one `pin_hole_d`.
