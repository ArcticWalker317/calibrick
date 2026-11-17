"""Small checks for calibrick.slicer.bambu (the generated process preset)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from calibrick.printers import PRINTERS
from calibrick.profile import Profile
from calibrick.slicer.bambu import write_process_preset

REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("key", sorted(PRINTERS))
def test_preset_json(tmp_path, key):
    printer = PRINTERS[key]
    path = write_process_preset(Profile(), printer, tmp_path)
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["type"] == "process"
    assert doc["inherits"] == printer.bambu_process_base
    assert doc["print_settings_id"] == doc["name"] == path.stem
    assert doc["layer_height"] == "0.16"
    assert doc["initial_layer_print_height"] == "0.16"
    for k in ("xy_hole_compensation", "xy_contour_compensation"):
        assert float(doc[k]) == 0.0
    # every value is a string or a list of strings
    for k, v in doc.items():
        assert isinstance(v, str) or (isinstance(v, list) and all(isinstance(x, str) for x in v)), k
    # shipped preset matches the generator
    shipped = REPO / "presets" / path.name
    if shipped.is_file():
        assert json.loads(shipped.read_text(encoding="utf-8")) == doc
