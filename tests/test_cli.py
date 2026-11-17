"""Command line: argument wiring, setup unzip logic, calibration loop, set build smoke test."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from calibrick import cli, config
from calibrick.profile import Profile

DOC = Path(__file__).resolve().parents[1] / "docs" / "CALIBRATION.md"
LDRAW_OK = (config.ldraw_dir() / "parts").is_dir()
RB_OK = any((config.rebrickable_dir() / n).exists() for n in ("rb.db", "rebrickable.sqlite", "sets.csv"))


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Profiles and cache go to a temp dir; the LDraw / Rebrickable locations are kept."""
    monkeypatch.setenv("CALIBRICK_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("CALIBRICK_PROFILES", str(tmp_path / "profiles"))
    monkeypatch.setenv("CALIBRICK_CACHE", str(tmp_path / "cache"))
    (tmp_path / "profiles").mkdir()
    (tmp_path / "cache").mkdir()
    return tmp_path


def run(capsys, *args):
    code = cli.main(list(args))
    return code, capsys.readouterr().out


# ---- setup: unzip ---------------------------------------------------------------------------
def make_zip(path: Path, names: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for n, body in names.items():
            zf.writestr(n, body)
    return path


def test_install_ldraw_zip_strips_top_folder(tmp_path):
    z = make_zip(tmp_path / "complete.zip", {
        "ldraw/LDConfig.ldr": "0 config",
        "ldraw/parts/3001.dat": "0 Brick  2 x  4",
        "ldraw/parts/s/3001s01.dat": "0 sub",
        "ldraw/p/stud.dat": "0 Stud",
        "ldraw/p/48/4-4cyli.dat": "0 cyl",
    })
    dest = tmp_path / "lib"
    n = cli.install_ldraw_zip(z, dest)
    assert n == 5
    assert (dest / "parts" / "3001.dat").read_text() == "0 Brick  2 x  4"
    assert (dest / "parts" / "s" / "3001s01.dat").is_file()
    assert (dest / "p" / "48" / "4-4cyli.dat").is_file()
    assert (dest / "LDConfig.ldr").is_file()
    assert not (dest / "ldraw").exists()


def test_install_ldraw_zip_without_top_folder(tmp_path):
    z = make_zip(tmp_path / "flat.zip", {"parts/3024.dat": "0 x", "p/stud.dat": "0 y"})
    cli.install_ldraw_zip(z, tmp_path / "lib")
    assert (tmp_path / "lib" / "parts" / "3024.dat").is_file()


def test_install_ldraw_zip_rejects_bad_zips(tmp_path):
    z = make_zip(tmp_path / "evil.zip", {"ldraw/parts/a.dat": "0", "ldraw/../../evil.txt": "x"})
    with pytest.raises(cli.CLIError):
        cli.install_ldraw_zip(z, tmp_path / "lib")
    assert not (tmp_path / "evil.txt").exists()
    z = make_zip(tmp_path / "noparts.zip", {"ldraw/readme.txt": "x"})
    with pytest.raises(cli.CLIError):
        cli.install_ldraw_zip(z, tmp_path / "lib2")


# ---- parser ---------------------------------------------------------------------------------
def test_set_build_defaults():
    a = cli.build_parser().parse_args(["set", "build", "21064-1"])
    assert a.risky is True and a.spares is False and a.printer == "a1mini"
    assert a.profile is None and a.limit is None and a.only_color is None
    a = cli.build_parser().parse_args(["set", "build", "21064", "--no-risky", "--spares",
                                       "--only-color", "Tan", "--limit", "3", "--printer", "p1p"])
    assert (a.risky, a.spares, a.only_color, a.limit, a.printer) == (False, True, "Tan", 3, "p1p")


def test_unknown_profile_is_an_error(capsys):
    code = cli.main(["profile", "show", "does-not-exist"])
    assert code == 2
    assert "not found" in capsys.readouterr().err


# ---- calibration loop -----------------------------------------------------------------------
def results_text() -> str:
    import re

    blocks = re.findall(r"```\n(.*?)```", DOC.read_text(), re.S)
    return next(b for b in blocks if b.startswith("printer:"))


def test_calibration_loop(tmp_path, capsys):
    kit_dir = tmp_path / "kit1"
    code, out = run(capsys, "calibrate", "kit", "--out", str(kit_dir), "--step", "0.04")
    assert code == 0
    kit = json.loads((kit_dir / "kit.json").read_text())
    assert kit["kit"] == "v1"
    assert kit["ladders"]["stud_d"][0] == pytest.approx(4.90 - 4 * 0.04)
    assert (kit_dir / "calibrick_kit_v1_both.stl").is_file()
    assert list(kit_dir.glob("calibrick 0.16mm @BBL A1M.json"))

    res = tmp_path / "results.txt"
    res.write_text(results_text())
    code, out = run(capsys, "calibrate", "solve", str(res), "--kit", str(kit_dir / "kit.json"),
                    "--name", "mine")
    assert code == 0
    assert "stud_d:" in out and "suggested next ladders" in out
    saved = config.profiles_dir() / "mine.json"
    prof = Profile.load(saved)
    assert prof.name == "mine" and prof.calibrated

    code, out = run(capsys, "profile", "list")
    assert "mine" in out and "calibrated" in out
    code, out = run(capsys, "profile", "show", "mine")
    assert '"name": "mine"' in out and prof.geometry_key() in out

    kit2 = tmp_path / "kit2"
    code, out = run(capsys, "calibrate", "kit", "--next-from", str(res),
                    "--kit", str(kit_dir / "kit.json"), "--out", str(kit2))
    assert code == 0
    k2 = json.loads((kit2 / "kit.json").read_text())
    assert k2["kit"] == "v2"
    steps = {round(b - a, 4) for a, b in zip(k2["ladders"]["stud_d"], k2["ladders"]["stud_d"][1:])}
    assert steps == {0.02}
    assert (kit2 / "calibrick_kit_v2_both.stl").is_file()


def test_next_from_needs_kit(tmp_path, capsys):
    code = cli.main(["calibrate", "kit", "--next-from", "x.txt", "--out", str(tmp_path / "k")])
    assert code == 2


def test_next_tag():
    assert cli._next_tag("v1") == "v2"
    assert cli._next_tag("v9") == "v10"
    assert cli._next_tag(None) == "v2"


# ---- parts and sets (need data) -------------------------------------------------------------
@pytest.mark.skipif(not LDRAW_OK, reason="LDraw library not available")
def test_part_command(tmp_path, capsys):
    code, out = run(capsys, "part", "3022", "3024", "--out", str(tmp_path / "parts"))
    assert code == 0
    assert "NOT calibrated" in out
    assert "3022: tier A  15.80 x 15.80 x 4.96 mm" in out
    assert (tmp_path / "parts" / "3024.stl").is_file()


@pytest.mark.skipif(not (LDRAW_OK and RB_OK), reason="LDraw library or Rebrickable data missing")
def test_set_build_smoke(tmp_path, capsys):
    out_dir = tmp_path / "paris"
    code, out = run(capsys, "set", "build", "21064-1", "--out", str(out_dir), "--limit", "4",
                    "--only-color", "Tan")
    assert code == 0
    assert "NOT calibrated" in out
    for name in ("report.md", "manifest.json", "buy_list.csv", "profile.json", "plan.json",
                 "calibrick 0.16mm @BBL A1M.json"):
        assert (out_dir / name).is_file(), name
    rep = (out_dir / "report.md").read_text()
    assert "Paris" in rep and "## Engine tiers" in rep and "not calibrated" in rep
    man = json.loads((out_dir / "manifest.json").read_text())
    assert man["plates"] and all(p["color_name"] == "Tan" for p in man["plates"])
    # parts were cached under the profile's geometry key
    assert list((config.cache_dir() / Profile().geometry_key()).glob("*.stl"))


@pytest.mark.skipif(not RB_OK, reason="Rebrickable data missing")
def test_set_find_and_info(capsys):
    code, out = run(capsys, "set", "find", "21064")
    assert code == 0 and "21064-1" in out
    code, out = run(capsys, "set", "info", "21064-1")
    assert code == 0 and "Paris" in out
