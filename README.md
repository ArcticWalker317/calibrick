# calibrick

calibrick turns a LEGO set number into 3D-printable, LEGO-compatible parts that are tuned to your printer.
It looks up the set's parts and works out which ones FDM can print. For each of those it writes an STL calibrated for your printer.
It packs the parts onto one plate per colour and writes a buy list for everything else.
Calibration has two halves: a locked slicer process preset, and a profile of per-feature sizes (studs, walls, tubes, sockets) that is baked into every mesh.
The profile comes from printing a small coupon kit and rating how each station fits genuine bricks.

## Install

```
pip install -e .
calibrick setup            # downloads the LDraw parts library and the Rebrickable catalog CSVs
```

Data goes to `~/.calibrick/` (override with `CALIBRICK_DATA`, or `CALIBRICK_LDRAW` /
`CALIBRICK_REBRICKABLE` for the two libraries). `calibrick setup --refresh` downloads again.

## Calibrate (once per printer + filament + plate)

```
calibrick calibrate kit --printer a1mini --out kit_v1        # coupons + locked process preset
#   import the preset into Bambu Studio, print kit_v1/calibrick_kit_v1_both.stl,
#   rate every station 1-5 against genuine parts, take a few caliper readings
#   (template and details: docs/CALIBRATION.md), save them as results_v1.txt
calibrick calibrate solve results_v1.txt --kit kit_v1/kit.json --name a1mini-pla
calibrick calibrate kit --next-from results_v1.txt --kit kit_v1/kit.json --out kit_v2   # finer ladders
calibrick profile list
calibrick profile show a1mini-pla
```

Repeat print, report and solve with the next kit until the ladders settle. Every part you
generate is only as good as the profile, and that profile is only valid with the process preset it was measured with.

## Build a set

```
calibrick set find "paris"
calibrick set info 21064-1
calibrick set build 21064-1 --profile a1mini-pla --printer a1mini --out paris
calibrick part 3001 3022 --profile a1mini-pla                 # single parts
```

`set build` writes `plates/<colour>/plate_NN.3mf`, `stl/`, `manifest.md`, `buy_list.csv`,
`risky_list.csv`, `rebrickable_import.csv`, the process preset and `report.md`. Options:
`--no-risky`, `--spares`, `--only-color NAME`, `--limit N`. If you leave out `--profile`, the
nominal profile is used and the output is **not calibrated** (the command says so).

## What the labels mean

- **print**: an ordinary part whose fit features are calibrated.
- **risky**: printable, but it relies on something not yet calibrated (clips, bars, hinges,
  wheels), is transparent, or is decorated (it prints plain). It is printed by default; leave it out with `--no-risky`.
- **buy**: no LDraw geometry, not FDM material (rubber, flexible), the engine failed, or the part does not
  fit the printer. These go to the buy list.
- **Tier A**: the generated mesh was verified as one watertight solid.
- **Tier B**: the mesh could not be verified, so it is written as overlapping shells that the slicer
  merges. Check these in the slicer preview. `tools/slice_check.py` slices every part of a build
  and flags the ones that look broken.

## Limitations

Technic holes, pins, axles, bars and clips use uncalibrated starting values. Side studs print
lying down. Print orientation is always studs-up. Some legacy LDraw files come out of the engine
as tier B or broken; broken parts are moved to the buy list. Only Bambu Studio presets are written
(A1 mini, P1P). See `docs/HANDOFF.md` for the full list and open work.

## Credits and trademarks

LEGO is a trademark of the LEGO Group, which does not sponsor, authorize or endorse this
project. Part geometry comes from the [LDraw](https://www.ldraw.org) parts library (CC BY 4.0).
Set inventories come from [Rebrickable](https://rebrickable.com/downloads/).
