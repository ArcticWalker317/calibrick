"""Printer definitions (bed size, keep-out zones, slicer preset names)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PrinterSpec:
    key: str
    name: str
    bed: tuple[float, float]          # usable X, Y in mm
    max_z: float
    margin: float = 6.0               # keep parts this far from the bed edge
    # rectangles (x0, y0, x1, y1) in bed coordinates that must stay empty
    exclude: tuple[tuple[float, float, float, float], ...] = ()
    bambu_machine: str = ""           # Bambu Studio system machine preset
    bambu_process_base: str = ""      # system process preset the calibrick preset inherits from


PRINTERS: dict[str, PrinterSpec] = {
    "a1mini": PrinterSpec(
        key="a1mini",
        name="Bambu Lab A1 mini",
        bed=(180.0, 180.0),
        max_z=180.0,
        bambu_machine="Bambu Lab A1 mini 0.4 nozzle",
        bambu_process_base="0.16mm Optimal @BBL A1M",
    ),
    "p1p": PrinterSpec(
        key="p1p",
        name="Bambu Lab P1P",
        bed=(256.0, 256.0),
        max_z=250.0,
        exclude=((0.0, 0.0, 18.0, 28.0),),
        bambu_machine="Bambu Lab P1P 0.4 nozzle",
        bambu_process_base="0.16mm Optimal @BBL P1P",
    ),
}


def get_printer(key: str) -> PrinterSpec:
    try:
        return PRINTERS[key.lower()]
    except KeyError:
        raise KeyError(f"unknown printer {key!r}; known: {', '.join(PRINTERS)}") from None
