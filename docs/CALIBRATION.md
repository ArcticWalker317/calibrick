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

## Not calibrated yet

Technic pin holes, axle holes, pins, bars and clips use starting values in the profile
(`pin_hole_d`, `axle_hole_grow`, `bar_d`, ...). They need their own ladder coupon; see
`docs/HANDOFF.md`.
