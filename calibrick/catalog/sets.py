"""Set lookup and set plans (inventory -> lots with LDraw ids and triage decisions)."""
from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import data
from .model import PlanItem, SetPlan
from .resolve import Resolver, default_library
from .triage import triage

_SETNUM = re.compile(r"^\s*([0-9A-Za-z.]+?)\s*(?:-\s*(\d+))?\s*$")
NO_COLOR_ID = 9999


class SetNotFound(LookupError):
    pass


def normalize_set_num(s: str) -> str:
    """'21064' -> '21064-1'; '21064-2' stays; surrounding / inner blanks are dropped."""
    m = _SETNUM.match(str(s))
    if not m:
        return str(s).strip()
    base, ver = m.groups()
    return f"{base}-{ver or 1}"


def _looks_like_set_num(q: str) -> bool:
    return bool(re.match(r"^\s*\d[0-9A-Za-z.]*\s*(-\s*\d+)?\s*$", q))


def _theme_name(con: sqlite3.Connection, theme_id) -> str:
    if theme_id is None or not data.has_table(con, "themes"):
        return ""
    names: list[str] = []
    seen = set()
    while theme_id is not None and theme_id not in seen:
        seen.add(theme_id)
        row = con.execute("SELECT name, parent_id FROM themes WHERE id=?", (theme_id,)).fetchone()
        if not row:
            break
        names.append(row["name"])
        theme_id = row["parent_id"]
    return " / ".join(reversed(names))


def _set_dict(con, row) -> dict:
    return {
        "set_num": row["set_num"],
        "name": row["name"],
        "year": row["year"],
        "num_parts": row["num_parts"],
        "theme": _theme_name(con, row["theme_id"]),
        "img_url": row["img_url"] or "",
    }


def find_sets(query: str, limit: int = 20, *, data_dir: Path | None = None) -> list[dict]:
    """Sets matching a set number ('21064', '21064-1') or a name substring ('paris')."""
    con = data.open_db(data_dir)
    q = (query or "").strip()
    if not q:
        return []
    rows: list = []
    seen: set[str] = set()

    def add(rs):
        for r in rs:
            if r["set_num"] not in seen and len(rows) < limit:
                seen.add(r["set_num"])
                rows.append(r)

    if _looks_like_set_num(q):
        sn = normalize_set_num(q)
        add(con.execute("SELECT * FROM sets WHERE set_num=?", (sn,)))
        add(con.execute("SELECT * FROM sets WHERE set_num=? COLLATE NOCASE", (sn,)))
        if "-" not in q:  # all versions of a bare number: 21064-1, 21064-2 ...
            base = re.sub(r"\s+", "", q)
            add(con.execute(
                "SELECT * FROM sets WHERE set_num LIKE ? ESCAPE '\\' ORDER BY set_num",
                (base.replace("_", "\\_").replace("%", "\\%") + "-%",),
            ))
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    add(con.execute(
        "SELECT * FROM sets WHERE name LIKE ? ESCAPE '\\' ORDER BY year DESC, num_parts DESC LIMIT ?",
        (like, limit),
    ))
    return [_set_dict(con, r) for r in rows]


def _latest_inventory(con, set_num: str) -> tuple[int, int] | None:
    row = con.execute(
        "SELECT id, version FROM inventories WHERE set_num=? ORDER BY version DESC LIMIT 1", (set_num,)
    ).fetchone()
    return (row["id"], row["version"]) if row else None


def _collect(con, inv_id: int, mult: int, parts: dict, figs: dict, depth: int = 0) -> None:
    """Accumulate part lots and minifigs of one inventory (and its sub-sets) into dicts."""
    for r in con.execute(
        "SELECT part_num, color_id, quantity, is_spare, img_url FROM inventory_parts WHERE inventory_id=?",
        (inv_id,),
    ):
        lot = parts[(r["part_num"], r["color_id"])]
        lot["spares" if r["is_spare"] else "quantity"] += r["quantity"] * mult
        if r["img_url"] and not lot.get("img_url"):
            lot["img_url"] = r["img_url"]
    if data.has_table(con, "inventory_minifigs"):
        for r in con.execute("SELECT fig_num, quantity FROM inventory_minifigs WHERE inventory_id=?", (inv_id,)):
            figs[r["fig_num"]] += r["quantity"] * mult
    if depth < 3 and data.has_table(con, "inventory_sets"):
        for r in con.execute("SELECT set_num, quantity FROM inventory_sets WHERE inventory_id=?", (inv_id,)):
            sub = _latest_inventory(con, r["set_num"])
            if sub:
                _collect(con, sub[0], mult * r["quantity"], parts, figs, depth + 1)


def load_set_plan(set_num: str, *, lib=None, data_dir: Path | None = None) -> SetPlan:
    """Build the plan for one set from its latest inventory version.

    ``lib`` is anything with ``has_part(id) -> bool`` (normally a
    ``calibrick.ldraw.LDrawLibrary``). Without it, ``<CALIBRICK_LDRAW>/parts`` is scanned;
    if there is no library at all, geometry is assumed to exist and a warning is added.
    """
    con = data.open_db(data_dir)
    sn = normalize_set_num(set_num)
    srow = con.execute("SELECT * FROM sets WHERE set_num=?", (sn,)).fetchone() or con.execute(
        "SELECT * FROM sets WHERE set_num=? COLLATE NOCASE", (sn,)
    ).fetchone()
    if srow is None:
        hint = find_sets(set_num, 5, data_dir=data_dir)
        extra = ("; did you mean " + ", ".join(f"{h['set_num']} ({h['name']})" for h in hint)) if hint else ""
        raise SetNotFound(f"set {sn} is not in the Rebrickable catalog{extra}")
    sn = srow["set_num"]

    warnings: list[str] = []
    if lib is None:
        lib = default_library()
        if lib is None:
            warnings.append("no LDraw library found: geometry assumed to exist for every part")
    resolver = Resolver(con, lib)

    inv = _latest_inventory(con, sn)
    parts: dict = defaultdict(lambda: {"quantity": 0, "spares": 0, "img_url": ""})
    figs: dict = defaultdict(int)
    if inv is None:
        warnings.append(f"set {sn} has no inventory in the catalog")
        inv = (0, 0)
    else:
        _collect(con, inv[0], 1, parts, figs)

    colors = {r["id"]: r for r in con.execute("SELECT id, name, rgb, is_trans FROM colors")}
    cats = {r["id"]: r["name"] for r in con.execute("SELECT id, name FROM part_categories")}
    part_info: dict[str, sqlite3.Row] = {}
    for pn in {k[0] for k in parts}:
        part_info[pn] = con.execute(
            "SELECT name, part_cat_id, part_material FROM parts WHERE part_num=?", (pn,)
        ).fetchone()

    items: list[PlanItem] = []
    for (pn, cid), lot in parts.items():
        p = part_info.get(pn)
        name = p["name"] if p else pn
        category = cats.get(p["part_cat_id"], "") if p else ""
        material = (p["part_material"] or "") if p else ""
        c = colors.get(cid)
        res = resolver.resolve(pn, name)
        decision, reasons = triage(
            name=name, category=category, material=material, kind="part",
            is_trans=bool(c["is_trans"]) if c else False,
            decorated=res.decorated, has_geometry=res.ldraw_id is not None,
        )
        items.append(PlanItem(
            part_num=pn,
            ldraw_id=res.ldraw_id,
            name=name,
            category=category,
            material=material,
            color_id=cid,
            color_name=c["name"] if c else str(cid),
            color_rgb=(c["rgb"] or "").upper() if c else "",
            is_trans=bool(c["is_trans"]) if c else False,
            quantity=lot["quantity"],
            spares=lot["spares"],
            decision=decision,
            reasons=list(res.notes) + reasons,
            kind="part",
            resolution=res.how,
            img_url=lot["img_url"] or "",
        ))

    nc = colors.get(NO_COLOR_ID)
    for fig, qty in figs.items():
        row = None
        if data.has_table(con, "minifigs"):
            row = con.execute("SELECT name, img_url FROM minifigs WHERE fig_num=?", (fig,)).fetchone()
        name = row["name"] if row else fig
        decision, reasons = triage(name=name, category="Minifigs", material="", kind="minifig")
        items.append(PlanItem(
            part_num=fig, ldraw_id=None, name=name, category="Minifigs", material="",
            color_id=NO_COLOR_ID, color_name=nc["name"] if nc else "[No Color/Any Color]",
            color_rgb=(nc["rgb"] or "").upper() if nc else "", is_trans=False,
            quantity=qty, spares=0, decision=decision, reasons=reasons,
            kind="minifig", resolution="none", img_url=(row["img_url"] if row else "") or "",
        ))

    order = {"print": 0, "risky": 1, "buy": 2, "skip": 3}
    items.sort(key=lambda it: (order.get(it.decision, 9), -it.quantity, it.part_num, it.color_id))

    stamp = ""
    try:
        stamp = data.data_info(data_dir)["timestamp"]
    except Exception:  # noqa: BLE001 - informational only
        pass
    return SetPlan(
        set_num=sn,
        name=srow["name"],
        year=srow["year"],
        num_parts=srow["num_parts"],
        items=items,
        theme=_theme_name(con, srow["theme_id"]),
        img_url=srow["img_url"] or "",
        inventory_version=inv[1],
        data_timestamp=stamp,
        warnings=warnings,
    )
