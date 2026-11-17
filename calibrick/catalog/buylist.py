"""Buy list, risky list and a Rebrickable part-list import file for a SetPlan."""
from __future__ import annotations

import csv
from pathlib import Path

from .model import PlanItem, SetPlan

COLUMNS = ("part_num", "color_name", "color_id", "quantity", "name", "reasons", "url")


def rebrickable_url(item: PlanItem) -> str:
    if item.kind == "minifig":
        return f"https://rebrickable.com/minifigs/{item.part_num}/"
    return f"https://rebrickable.com/parts/{item.part_num}/"


def _write_list(path: Path, items: list[PlanItem]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLUMNS)
        for it in items:
            w.writerow([it.part_num, it.color_name, it.color_id, it.quantity, it.name,
                        "; ".join(it.reasons), rebrickable_url(it)])


def write_buy_list(plan: SetPlan, out_dir: Path) -> list[Path]:
    """Write ``buy_list.csv``, ``risky_list.csv`` and ``rebrickable_import.csv``.

    Quantities are what the build needs (spares not included). The import file uses
    Rebrickable's part-list CSV format (``Part,Color,Quantity`` with Rebrickable colour ids);
    minifigures cannot go into a Rebrickable part list and are only in ``buy_list.csv``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = lambda it: (-it.quantity, it.part_num, it.color_id)  # noqa: E731
    buy = sorted((it for it in plan.items if it.decision == "buy" and it.quantity > 0), key=key)
    risky = sorted((it for it in plan.items if it.decision == "risky" and it.quantity > 0), key=key)

    p_buy, p_risky, p_imp = out / "buy_list.csv", out / "risky_list.csv", out / "rebrickable_import.csv"
    _write_list(p_buy, buy)
    _write_list(p_risky, risky)

    merged: dict[tuple[str, int], int] = {}
    for it in buy:
        if it.kind == "part":
            merged[(it.part_num, it.color_id)] = merged.get((it.part_num, it.color_id), 0) + it.quantity
    with open(p_imp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(("Part", "Color", "Quantity"))
        for (pn, cid), q in merged.items():
            w.writerow((pn, cid, q))
    return [p_buy, p_risky, p_imp]
