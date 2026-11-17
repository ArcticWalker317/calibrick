"""Plan data types shared with the rest of calibrick (see docs/ARCHITECTURE.md, WP-C)."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field, fields

DECISIONS = ("print", "risky", "buy", "skip")


@dataclass
class PlanItem:
    part_num: str            # Rebrickable part number as listed in the inventory
    ldraw_id: str | None     # LDraw file stem to use for geometry (None = no geometry)
    name: str
    category: str
    material: str
    color_id: int
    color_name: str
    color_rgb: str           # six hex digits, no '#'
    is_trans: bool
    quantity: int            # needed for the build
    spares: int              # extra pieces the real set includes
    decision: str            # "print" | "risky" | "buy" | "skip"
    reasons: list[str] = field(default_factory=list)
    # ---- extras beyond the agreed interface (all optional) ----
    kind: str = "part"       # "part" | "minifig"
    resolution: str = ""     # how ldraw_id was found: exact | base | variant | assumed | none
    img_url: str = ""

    @property
    def printable(self) -> bool:
        return self.decision in ("print", "risky") and bool(self.ldraw_id)


@dataclass
class SetPlan:
    set_num: str
    name: str
    year: int
    num_parts: int
    items: list[PlanItem] = field(default_factory=list)
    # ---- extras beyond the agreed interface (all optional) ----
    theme: str = ""
    img_url: str = ""
    inventory_version: int = 0
    data_timestamp: str = ""          # ISO time of the catalog data used
    warnings: list[str] = field(default_factory=list)

    def by_decision(self, decision: str) -> list[PlanItem]:
        return [it for it in self.items if it.decision == decision]

    def pieces(self, decision: str | None = None, *, spares: bool = False) -> int:
        return sum(
            it.quantity + (it.spares if spares else 0)
            for it in self.items
            if decision is None or it.decision == decision
        )

    def printable_items(self) -> list[PlanItem]:
        return [it for it in self.items if it.printable]

    def ldraw_usage(self, *, spares: bool = False) -> list[tuple[str, int]]:
        """Unique LDraw ids of everything to be printed, most-used first, with piece counts."""
        c: Counter[str] = Counter()
        for it in self.printable_items():
            c[it.ldraw_id] += it.quantity + (it.spares if spares else 0)
        return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def plan_to_json(plan: SetPlan, *, indent: int | None = 2) -> str:
    return json.dumps(asdict(plan), indent=indent)


def plan_from_json(data: str | dict) -> SetPlan:
    d = json.loads(data) if isinstance(data, str) else dict(data)
    item_fields = {f.name for f in fields(PlanItem)}
    plan_fields = {f.name for f in fields(SetPlan)}
    items = [PlanItem(**{k: v for k, v in it.items() if k in item_fields}) for it in d.get("items", [])]
    kw = {k: v for k, v in d.items() if k in plan_fields and k != "items"}
    return SetPlan(items=items, **kw)
