"""Bambu Studio (and OrcaSlicer) process-preset generation.

A calibration is only valid for the exact process it was measured with, so calibrick ships
its own *locked* process preset. The preset is written as a Bambu Studio "user preset"
JSON that inherits from the stock ``<layer>mm ... @BBL <printer>`` system preset and
overrides only what matters for dimensional accuracy.

Format notes (checked against Bambu Studio master, profile version 02.09.x):

* The importer (File > Import > Import Configs...) needs ``name``, a parseable ``version``,
  ``print_settings_id`` (this is what marks the file as a *process* preset) and an
  ``inherits`` value that names a preset the application already has.
* Every value is a string. Speeds and accelerations are stored once per extruder
  *variant* of the machine (the A1 mini has one, the P1P has two: standard and high-flow
  hotend), so those keys are lists of strings whose length matches the base preset's
  ``print_extruder_variant``.
* ``wall_sequence`` is the current key for the wall order; ``wall_infill_order`` is the
  legacy spelling that the loader still maps onto it. Both are written.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..printers import PrinterSpec
from ..profile import Profile

PRESET_VERSION = "2.0.0.0"

# Stock process presets by layer height (same names for every Bambu printer).
_BASE_LABEL_BY_LAYER: dict[float, str] = {
    0.08: "0.08mm Extra Fine",
    0.12: "0.12mm Fine",
    0.16: "0.16mm Optimal",
    0.20: "0.20mm Standard",
    0.24: "0.24mm Draft",
    0.28: "0.28mm Extra Draft",
}


@dataclass(frozen=True)
class BambuPrinterInfo:
    suffix: str                       # the "@BBL <suffix>" part of preset names
    variants: tuple[str, ...]         # print_extruder_variant of the stock process presets
    extruder_ids: tuple[str, ...]     # print_extruder_id of the stock process presets
    elephant_foot: str                # elefant_foot_compensation of the stock 0.16 mm preset


# Taken from resources/profiles/BBL/{process,machine} in the Bambu Studio repository.
BAMBU_PRINTERS: dict[str, BambuPrinterInfo] = {
    "a1mini": BambuPrinterInfo("A1M", ("Direct Drive Standard",), ("1",), "0"),
    "p1p": BambuPrinterInfo(
        "P1P", ("Direct Drive Standard", "Direct Drive High Flow"), ("1", "1"), "0.15"
    ),
}

# Keys Bambu Studio stores once per extruder variant (lists in the JSON).
PER_VARIANT_KEYS = frozenset(
    {
        "outer_wall_speed",
        "inner_wall_speed",
        "small_perimeter_speed",
        "small_perimeter_threshold",
        "top_surface_speed",
        "initial_layer_speed",
        "initial_layer_infill_speed",
        "gap_infill_speed",
        "default_acceleration",
        "outer_wall_acceleration",
    }
)


def _fmt(x: float) -> str:
    """Format a number the way Bambu Studio writes them (no trailing zeros)."""
    s = f"{x:.4f}".rstrip("0").rstrip(".")
    return s or "0"


def bambu_info(printer: PrinterSpec) -> BambuPrinterInfo:
    try:
        return BAMBU_PRINTERS[printer.key]
    except KeyError:
        raise KeyError(
            f"no Bambu preset data for printer {printer.key!r}; known: {', '.join(BAMBU_PRINTERS)}"
        ) from None


def base_preset_name(profile: Profile, printer: PrinterSpec) -> str:
    """Stock process preset the calibrick preset inherits from."""
    info = bambu_info(printer)
    lh = round(float(profile.layer_height), 2)
    if abs(lh - 0.16) < 1e-9 and printer.bambu_process_base:
        return printer.bambu_process_base
    nearest = min(_BASE_LABEL_BY_LAYER, key=lambda k: abs(k - lh))
    return f"{_BASE_LABEL_BY_LAYER[nearest]} @BBL {info.suffix}"


def preset_name(profile: Profile, printer: PrinterSpec) -> str:
    return f"calibrick {float(profile.layer_height):.2f}mm @BBL {bambu_info(printer).suffix}"


def locked_settings(
    profile: Profile,
    printer: PrinterSpec,
    *,
    scarf_seam: bool = True,
    elephant_foot: float | None = None,
) -> dict[str, str]:
    """The locked process settings as plain ``key -> string`` (not yet expanded per variant).

    ``elephant_foot=None`` keeps the stock value of the printer's 0.16 mm preset, written
    explicitly so a later change of the stock presets cannot shift a calibration.
    """
    info = bambu_info(printer)
    lh = _fmt(float(profile.layer_height))
    efc = info.elephant_foot if elephant_foot is None else _fmt(float(elephant_foot))
    s: dict[str, str] = {
        # --- layers: every brick height is a whole number of layers ------------------
        "layer_height": lh,
        "initial_layer_print_height": lh,
        "precise_z_height": "1",
        # --- size compensation lives in the geometry, not in the slicer --------------
        "xy_hole_compensation": "0",
        "xy_contour_compensation": "0",
        "enable_circle_compensation": "0",
        "elefant_foot_compensation": efc,
        # --- walls -------------------------------------------------------------------
        "wall_generator": "arachne",
        "wall_sequence": "outer wall/inner wall",
        "wall_infill_order": "outer wall/inner wall/infill",   # legacy spelling of the above
        "wall_loops": "6",
        "top_shell_layers": "6",
        "bottom_shell_layers": "5",
        "sparse_infill_density": "25%",
        "sparse_infill_pattern": "gyroid",
        # --- seam ----------------------------------------------------------------------
        "seam_position": "aligned",
        # --- speeds (mm/s) and accelerations (mm/s^2) --------------------------------
        "outer_wall_speed": "40",
        "inner_wall_speed": "80",
        "small_perimeter_speed": "50%",        # of the outer wall speed -> 20 mm/s
        "small_perimeter_threshold": "4",      # loops with radius <= 4 mm: studs, tubes, sockets
        "top_surface_speed": "60",
        "initial_layer_speed": "25",
        "initial_layer_infill_speed": "40",
        "gap_infill_speed": "80",
        "default_acceleration": "4000",
        "outer_wall_acceleration": "2000",
        # --- nothing that changes the footprint ---------------------------------------
        "brim_type": "no_brim",
        "enable_support": "0",
    }
    if scarf_seam:
        s.update(
            {
                "override_filament_scarf_seam_setting": "1",
                "seam_slope_type": "external",       # scarf on contours, plain seam in holes
                "seam_slope_conditional": "1",       # only where a corner cannot hide the seam
                "seam_slope_entire_loop": "0",
                "seam_slope_inner_walls": "1",
                "seam_slope_steps": "10",
                "seam_slope_start_height": "10%",
                "seam_slope_gap": "0",
                "seam_slope_min_length": "10",
            }
        )
    else:
        s.update({"override_filament_scarf_seam_setting": "1", "seam_slope_type": "none"})
    return s


def process_preset(profile: Profile, printer: PrinterSpec, **options) -> dict:
    """Build the complete preset document (ready for ``json.dump``)."""
    info = bambu_info(printer)
    name = preset_name(profile, printer)
    n = len(info.variants)
    doc: dict = {
        "type": "process",
        "name": name,
        "inherits": base_preset_name(profile, printer),
        "from": "User",
        "is_custom_defined": "0",
        "print_settings_id": name,
        "version": PRESET_VERSION,
        "description": (
            "Locked process for calibrick calibration and parts. Changing any value "
            "invalidates the calibration profile that was measured with it."
        ),
        "print_extruder_id": list(info.extruder_ids),
        "print_extruder_variant": list(info.variants),
    }
    for key, value in locked_settings(profile, printer, **options).items():
        doc[key] = [value] * n if key in PER_VARIANT_KEYS else value
    return doc


def write_process_preset(profile: Profile, printer: PrinterSpec, out_dir: Path, **options) -> Path:
    """Write the Bambu Studio process preset JSON and return its path.

    Import it with File > Import > Import Configs... while the matching printer is
    selected. Keyword options: ``scarf_seam`` (default True), ``elephant_foot`` (mm,
    default: stock value for the printer).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    doc = process_preset(profile, printer, **options)
    path = out_dir / f"{doc['name']}.json"
    path.write_text(json.dumps(doc, indent=4) + "\n", encoding="utf-8")
    return path
