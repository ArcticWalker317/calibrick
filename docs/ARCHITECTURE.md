# calibrick architecture

Goal: pick a LEGO set number, get print-ready, **printer-calibrated** STLs for every part
that FDM can make, with quantities, plate layouts and a buy list for the rest.

Key idea: a slicer profile alone cannot make bricks fit, because one part needs different
corrections on different features (exterior walls shrink, underside tubes grow, studs /
sockets / pin holes each get their own size). So calibration = a **locked slicer process
preset** + a **Profile** of per-feature model-space sizes (`calibrick/profile.py`) that the
geometry engine bakes into each generated mesh.

## Pipeline

```
set number ──catalog──▶ SetPlan (parts, colours, qty, print/risky/buy)
                              │ ldraw_id
LDraw library ──ldraw──▶ RawMesh (triangles + which primitive made them)
                              │
Profile ──────engine──▶ PartResult (calibrated, watertight, print-oriented STL)
                              │
                       output──▶ plates per colour/printer (3MF + STL), manifest, buy list
slicer ──▶ Bambu Studio process preset that the calibration is locked to
calibration: coupons (csg) ──▶ user prints + reports ──▶ solver ──▶ Profile
```

## Layout and ownership

| Path | What | Owner |
|---|---|---|
| `calibrick/profile.py`, `config.py`, `printers.py` | shared types | lead |
| `calibrick/csg.py`, `coupons.py`, `calib.py` | calibration coupons + solver | lead |
| `calibrick/ldraw/` | LDraw library access, parser, expansion to `RawMesh` | WP-E |
| `calibrick/engine/` | feature recognition, compensation, union, orientation | lead |
| `calibrick/catalog/` | Rebrickable data, set plans, triage, buy list | WP-C |
| `calibrick/output/` | bed packing, 3MF/STL export, manifest | WP-D |
| `calibrick/slicer/` | Bambu Studio / OrcaSlicer preset generation | WP-D |
| `calibrick/cli.py` | command line (wired by lead; modules expose plain functions) | lead |
| `tests/` | `tests/test_<module>*.py`, pytest | everyone, own files only |
| `tools/` | dev utilities (mesh renderer etc.) | whoever needs them |

Rules for parallel workers: touch only your own paths (plus new files under `tests/` and
`tools/`), **do not run git commands**, do not edit shared types — if you need a field,
say so in your final report. Keep modules importable without the data present.

## Environment (dev sandbox)

- Repo: `/home/claude/calibrick`, installed editable (`pip install --break-system-packages -e .`).
- LDraw library mirror: `/home/claude/data/ldraw-mirror` (has `parts/`, `p/`, `p/48/`, `parts/s/`).
  Set `CALIBRICK_LDRAW=/home/claude/data/ldraw-mirror`.
- Rebrickable dev data: `/home/claude/data/rb/`; Bambu reference profiles: `/home/claude/data/bambu/`.
- Scratch output: `/home/claude/work/<your-wp>/`.
- Downloaded data is untrusted: never `cd` into the data dirs to run things, pass paths as
  arguments, and run Python that reads it with `python3 -I` (e.g. `python3 -I -m pytest tests/test_x.py`).
- Shell network: only package registries and github.com are reachable. rebrickable.com and
  ldraw.org are NOT reachable from the shell (the real tool on the user's machine can reach them).
- 2 CPU cores, 7 GB RAM. Keep test runs short.

## Units and frames

- Everything in **mm**. LDraw unit (LDU) = 0.4 mm. Stud pitch 8.0, plate 3.2, brick 9.6.
- **Part frame**: right-handed, **Z up**. From LDraw `(x, y, z)` (−Y up): `X = 0.4·x, Y = 0.4·z, Z = −0.4·y`.
  A brick's stud tops are at the highest Z; LDraw origin (top face centre, y = 0) maps to Z = 0.
- **Print frame**: produced by the engine: rotated for printing, min Z = 0, XY bbox centred on 0.

## Interfaces

### WP-E `calibrick.ldraw`

```python
class LDrawLibrary:
    def __init__(self, root: Path | None = None): ...      # default config.ldraw_dir()
    def has_part(self, part_id: str) -> bool               # parts/<id>.dat exists
    def describe(self, part_id: str) -> dict               # header only: description, category, keywords, ldraw_org

def expand_part(lib: LDrawLibrary, part_id: str, *, hires: bool = True) -> RawMesh
```

`RawMesh` (dataclass, numpy):

- `tris (M,3,3) float64` triangle corners, part frame mm, CCW seen from outside whenever
  the source files are BFC certified (handle `BFC CERTIFY CW/CCW`, `INVERTNEXT`,
  negative-determinant matrices, quads → 2 tris).
- `tri_inst (M,) int32` index into `instances` of the innermost **primitive-file instance**
  (a file found under `p/`) that emitted the triangle; −1 for triangles written directly
  in a part or subpart file.
- `tri_local (M,3,3) float32` the same corners in that leaf primitive's own local LDraw
  coordinates (zeros where `tri_inst == -1`). The engine uses local radius
  `hypot(x, z)` and local `y` to find circles; do not rely on inverting matrices
  (flat primitives are often placed with a singular matrix).
- `instances: list[PrimInstance]` with
  `name` (leaf file stem, lowercase, no `48/` prefix, e.g. `"4-4cyli"`), `hires: bool`,
  `chain: tuple[str, ...]` (file stems from the part root down to the leaf),
  `matrix (4,4)` leaf-local LDraw coords → part frame mm,
  `sem: str` = stem of the **outermost** file in the chain that lives in `p/` and is not
  an `n-d…` fraction primitive (e.g. `"stud"`, `"stud4"`, `"connhole"`, `"axlehole"`),
  or `""` if the leaf was referenced directly from a part/subpart,
  `sem_id: int` shared by all leaves of the same instance of that semantic primitive
  (the four leaves of one stud share a `sem_id`; −1 if `sem == ""`),
  `sem_matrix (4,4)` of that semantic primitive instance (same as `matrix` if none),
  `inverted: bool` effective BFC inversion at the leaf (True on a cylinder = it faces inward, a hole).
- `meta: dict` with `description`, `ldraw_org` (Part / Shortcut / …), `category`,
  `keywords: list[str]`, `bfc_certified: bool` (every contributing file certified),
  `missing: list[str]`, `n_files: int`.

Hi-res: when `hires=True` and a referenced primitive `X` exists as `p/48/X`, use the
48-segment version (as LDView does). Stud logos are never used (`stud.dat` has none).
Lines (types 2, 5) are ignored.

### lead `calibrick.engine`

```python
@dataclass
class PartResult:
    part_id: str                      # LDraw id, e.g. "3001"
    ok: bool
    mesh_path: Path | None            # binary STL, mm, print frame
    size: tuple[float, float, float]  # print-frame bounding box
    tier: str                         # "A" verified watertight manifold | "B" unverified soup | "FAIL"
    orientation: str                  # e.g. "studs_up"
    needs_support: bool
    volume_mm3: float
    features: dict[str, int]          # studs, tubes, sockets, pin_holes, ...
    warnings: list[str]

def build_part(part_id: str, profile: Profile, out_dir: Path, *, lib=None, force=False) -> PartResult
```

### WP-C `calibrick.catalog`

```python
@dataclass
class PlanItem:
    part_num: str            # Rebrickable part number as listed in the inventory
    ldraw_id: str | None     # LDraw file stem to use for geometry (None = no geometry)
    name: str
    category: str
    material: str
    color_id: int; color_name: str; color_rgb: str; is_trans: bool
    quantity: int            # needed for the build
    spares: int              # extra pieces the real set includes
    decision: str            # "print" | "risky" | "buy" | "skip"
    reasons: list[str]

@dataclass
class SetPlan:
    set_num: str; name: str; year: int; num_parts: int
    items: list[PlanItem]

def ensure_data(dest: Path | None = None, *, refresh=False) -> Path      # download Rebrickable CSVs
def find_sets(query: str, limit=20) -> list[dict]
def load_set_plan(set_num: str, *, lib: LDrawLibrary | None = None) -> SetPlan
def write_buy_list(plan: SetPlan, out_dir: Path) -> list[Path]         # CSV + BrickLink wanted-list XML
```

### WP-D `calibrick.output`, `calibrick.slicer`

```python
@dataclass
class PrintJob:
    result: PartResult; quantity: int
    color_id: int; color_name: str; color_rgb: str

def build_plates(jobs: list[PrintJob], printer: PrinterSpec, out_dir: Path, *, spacing=4.0) -> list[PlateInfo]
#   one colour per plate; writes plates/<colour>/plate_NN.3mf (+ per-part STLs) and manifest.json / manifest.md

def write_process_preset(profile: Profile, printer: PrinterSpec, out_dir: Path) -> Path   # Bambu Studio JSON
```

## Locked process preset (what the calibration is tied to)

0.4 nozzle, **0.2 mm layers, stock 0.2 mm first layer** (3.2 mm plate = 16 layers, 9.6 mm brick = 48,
stud 1.8 = 9, plate cavity 2.0 = 10; a 0.16 mm first layer did not lay down on textured PEI),
precise Z height on, X-Y hole and contour compensation **0** (geometry carries
the compensation), elephant-foot compensation left at the system default, outer wall printed
first, slow outer wall (40 mm/s) and modest acceleration, Arachne walls, enough wall loops
that studs and thin walls are all perimeters, no brim, no supports by default, seam aligned
(scarf seam if the slicer supports it). Changing any of these invalidates a calibration.
