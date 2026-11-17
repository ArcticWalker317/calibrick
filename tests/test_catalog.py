"""Tests for calibrick.catalog against a tiny hand-written Rebrickable fixture (no downloads,
no LDraw library)."""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

from calibrick import catalog
from calibrick.catalog import data
from calibrick.catalog.triage import triage as run_triage
from calibrick.catalog.resolve import DirLibrary, Resolver, decoration_base_guess, suffix_variants

FIXTURE = Path(__file__).parent / "fixtures" / "rb_mini"


class FakeLib:
    def __init__(self, ids):
        self.ids = {i.lower() for i in ids}

    def has_part(self, part_id: str) -> bool:
        return part_id.lower() in self.ids


LIB = FakeLib(["3001", "3024", "3069b", "3070b", "2780", "60478", "99563", "3483", "4073"])


@pytest.fixture()
def rb(tmp_path):
    d = tmp_path / "rb"
    shutil.copytree(FIXTURE, d)
    yield d
    data._close_cached(d)


@pytest.fixture()
def plan(rb):
    return catalog.load_set_plan("1234", lib=LIB, data_dir=rb)


def lot(plan, part_num, color_id):
    hits = [it for it in plan.items if it.part_num == part_num and it.color_id == color_id]
    assert len(hits) == 1, (part_num, color_id, hits)
    return hits[0]


# --------------------------------------------------------------------------- data / sets
def test_sqlite_built_from_csv(rb):
    con = data.open_db(rb)
    assert (rb / data.SQLITE_NAME).exists()
    assert con.execute("SELECT count(*) FROM sets").fetchone()[0] == 3
    # booleans in both spellings, empty optional table created
    assert con.execute("SELECT is_trans FROM colors WHERE id=47").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM elements").fetchone()[0] == 0
    info = data.data_info(rb)
    assert info["source"] == "rebrickable csv" and info["sets"] == 3


def test_sqlite_rebuilt_when_csv_newer(rb):
    import os
    data.open_db(rb)
    with open(rb / "sets.csv", "a", encoding="utf-8") as f:
        f.write("9000-1,Brand New,2026,1,1,https://cdn.rebrickable.com/media/sets/9000-1.jpg\n")
    t = (rb / data.SQLITE_NAME).stat().st_mtime + 10
    os.utime(rb / "sets.csv", (t, t))
    assert [s["set_num"] for s in catalog.find_sets("9000", data_dir=rb)] == ["9000-1"]


@pytest.mark.parametrize("raw,norm", [
    ("21064", "21064-1"), ("21064-1", "21064-1"), (" 21064 - 2 ", "21064-2"), ("10255", "10255-1"),
])
def test_normalize_set_num(raw, norm):
    assert catalog.normalize_set_num(raw) == norm


def test_find_sets(rb):
    by_num = catalog.find_sets("1234", data_dir=rb)
    assert [s["set_num"] for s in by_num] == ["1234-1", "1234-2"]
    assert by_num[0]["theme"] == "Test Theme / Towers"
    assert [s["set_num"] for s in catalog.find_sets("hut", data_dir=rb)] == ["5678-1"]
    assert catalog.find_sets("nothing like this", data_dir=rb) == []


def test_unknown_set(rb):
    with pytest.raises(catalog.SetNotFound):
        catalog.load_set_plan("4242", lib=LIB, data_dir=rb)


def test_latest_inventory_and_spares(plan):
    assert plan.set_num == "1234-1" and plan.inventory_version == 2 and plan.year == 2024
    assert lot(plan, "3001", 4).quantity == 4                  # v1 had 99
    p = lot(plan, "3024", 15)
    assert (p.quantity, p.spares) == (3, 1)                     # spare merged into the lot
    sp = lot(plan, "3024", 0)
    assert (sp.quantity, sp.spares) == (0, 1)                   # spare-only lot kept
    t = lot(plan, "2780", 0)
    assert (t.quantity, t.spares) == (6, 1)
    assert len({(i.part_num, i.color_id) for i in plan.items}) == len(plan.items)


def test_fields_filled(plan):
    p = lot(plan, "3070b", 47)
    assert p.name == "Tile 1 x 1 with Groove" and p.category == "Tiles" and p.material == "Plastic"
    assert p.color_name == "Trans-Clear" and p.color_rgb == "FCFCFC" and p.is_trans


def test_minifig_item(plan):
    f = lot(plan, "fig-000001", 9999)
    assert f.kind == "minifig" and f.decision == "buy" and f.name == "Tower Guard" and f.quantity == 1


# --------------------------------------------------------------------------- resolution
def test_print_resolves_to_base(plan):
    p = lot(plan, "3069bpr0001", 15)
    assert p.ldraw_id == "3069b" and p.resolution == "base"
    assert p.decision == "risky"
    assert any("blank base 3069b" in r for r in p.reasons)
    assert "decoration not reproduced: prints as the plain part" in p.reasons


def test_pattern_suffix_without_relationship(plan):
    p = lot(plan, "3070bpat0002", 0)
    assert p.ldraw_id == "3070b" and p.decision == "risky"


def test_mold_variant_and_suffix(plan):
    m = lot(plan, "6141", 4)
    assert m.ldraw_id == "4073" and m.decision == "print"
    assert any("mold variant 4073" in r for r in m.reasons)
    s = lot(plan, "3070", 19)
    assert s.ldraw_id == "3070b" and s.decision == "print" and s.resolution == "variant"


def test_resolver_details(rb):
    r = Resolver(data.open_db(rb), FakeLib(["3069bpr0001", "3069b"]))
    assert r.resolve("3069bpr0001").ldraw_id == "3069b"        # base preferred over printed file
    r2 = Resolver(data.open_db(rb), FakeLib(["3069bpr0001"]))
    assert r2.resolve("3069bpr0001").ldraw_id == "3069bpr0001"  # printed file as fallback
    assert r.resolve("99999zz").ldraw_id is None
    assert Resolver(data.open_db(rb), None).resolve("3001").how == "assumed"


def test_decoration_regex():
    assert decoration_base_guess("3069bpr0030") == "3069b"
    assert decoration_base_guess("973pr1234c01") == "973"
    assert decoration_base_guess("3626bp01") == "3626b"
    assert decoration_base_guess("3001") is None
    assert decoration_base_guess("2412b") is None
    assert "3069" in suffix_variants("3069b") and "4032a" in suffix_variants("4032")


def test_dir_library_case_insensitive(tmp_path):
    (tmp_path / "parts").mkdir()
    (tmp_path / "parts" / "3001.DAT").write_text("0 Brick  2 x  4\n")
    (tmp_path / "parts" / "3069B.dat").write_text("0 Tile\n")
    lib = DirLibrary(tmp_path)
    assert lib.has_part("3001") and lib.has_part("3069b") and not lib.has_part("3002")


# --------------------------------------------------------------------------- triage
def test_triage_in_plan(plan):
    expect = {
        ("3001", 4): "print", ("99563", 19): "print",
        ("3070b", 47): "risky", ("2780", 0): "risky", ("60478", 19): "risky",
        ("3483", 0): "buy", ("x1", 0): "buy", ("99999zz", 4): "buy",
        ("1234stk01", 9999): "skip",
    }
    for (pn, cid), dec in expect.items():
        assert lot(plan, pn, cid).decision == dec, (pn, lot(plan, pn, cid).reasons)
    assert lot(plan, "99999zz", 4).reasons == ["no LDraw geometry"]
    assert any("cloudy" in r for r in lot(plan, "3070b", 47).reasons)


@pytest.mark.parametrize("kw,decision", [
    (dict(name="Brick 1 x 2", category="Bricks", material="Plastic"), "print"),
    (dict(name="Chain 21 Links", category="Other", material="Metal"), "buy"),
    (dict(name="Plant Leaves 4 x 3", category="Plants & Trees", material="Flexible Plastic"), "buy"),
    (dict(name="Wheel 18mm", category="Wheels and Tyres", material="Plastic"), "risky"),
    (dict(name="Tire 30.4 x 14", category="Wheels and Tyres", material="Plastic"), "buy"),
    (dict(name="Electric 9V Battery Box", category="Electronics", material="Plastic"), "buy"),
    (dict(name="Pneumatic Hose 3mm", category="Pneumatics", material="Plastic"), "buy"),
    (dict(name="Technic Axle Flexible 11", category="Technic Axles", material="Plastic"), "buy"),
    (dict(name="Technic Axle 3", category="Technic Axles", material="Plastic"), "risky"),
    (dict(name="Magnet Holder 2 x 2", category="Magnets and Holders", material="Plastic"), "buy"),
    (dict(name="Minifig Head", category="Minifig Heads", material="Plastic"), "buy"),
    (dict(name="Torso", category="Minifig Upper Body", material="Plastic"), "buy"),
    (dict(name="Hinge Plate 1 x 2", category="Hinges, Arms and Turntables", material="Plastic"), "risky"),
    (dict(name="Plate Special 1 x 1 with Clip", category="Plates Special", material="Plastic"), "risky"),
    (dict(name="Flower Stem", category="Plants & Trees", material="Plastic"), "risky"),
    (dict(name="Sticker Sheet", category="Stickers", material="Cardboard/Paper"), "skip"),
    (dict(name="Brick 1 x 2", category="Bricks", material="Plastic", has_geometry=False), "buy"),
    (dict(name="Brick 1 x 2", category="Bricks", material="Plastic", decorated=True), "risky"),
])
def test_triage_rules(kw, decision):
    assert run_triage(**kw)[0] == decision


# --------------------------------------------------------------------------- outputs
def read_csv(p):
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_buy_list_files(plan, tmp_path):
    paths = catalog.write_buy_list(plan, tmp_path / "out")
    assert [p.name for p in paths] == ["buy_list.csv", "risky_list.csv", "rebrickable_import.csv"]
    buy = read_csv(paths[0])
    assert buy[0] == ["part_num", "color_name", "color_id", "quantity", "name", "reasons", "url"]
    rows = {(r[0], r[2]): r for r in buy[1:]}
    assert rows[("3483", "0")][3] == "4" and rows[("3483", "0")][6] == "https://rebrickable.com/parts/3483/"
    assert ("fig-000001", "9999") in rows and ("1234stk01", "9999") not in rows
    risky = read_csv(paths[1])
    assert {r[0] for r in risky[1:]} == {"3069bpr0001", "3070bpat0002", "3070b", "2780", "60478"}
    imp = read_csv(paths[2])
    assert imp[0] == ["Part", "Color", "Quantity"]
    assert sorted(imp[1:]) == sorted([["3483", "0", "4"], ["x1", "0", "1"], ["99999zz", "4", "1"]])


def test_json_roundtrip_and_summary(plan):
    back = catalog.plan_from_json(catalog.plan_to_json(plan))
    assert back == plan
    text = catalog.summarize(plan)
    assert "1234-1" in text and "#FCFCFC" in text and "99999zz" in text and "risky" in text
