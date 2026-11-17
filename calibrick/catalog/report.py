"""Compact text summary of a SetPlan for the CLI."""
from __future__ import annotations

from collections import defaultdict

from .model import DECISIONS, SetPlan


def summarize(plan: SetPlan, *, top: int = 15) -> str:
    L: list[str] = []
    L.append(f"{plan.set_num}  {plan.name} ({plan.year})"
             + (f"  [{plan.theme}]" if plan.theme else ""))
    parts = [it for it in plan.items if it.kind == "part"]
    figs = [it for it in plan.items if it.kind == "minifig"]
    L.append(f"catalog parts {plan.num_parts}; inventory v{plan.inventory_version}: "
             f"{len(parts)} lots, {sum(i.quantity for i in parts)} pieces "
             f"(+{sum(i.spares for i in parts)} spares), "
             f"{len({i.color_id for i in parts})} colours, {sum(i.quantity for i in figs)} minifigs"
             + (f"; data {plan.data_timestamp}" if plan.data_timestamp else ""))
    for w in plan.warnings:
        L.append(f"warning: {w}")

    L.append("")
    L.append("decision   lots  pieces")
    for d in DECISIONS:
        its = plan.by_decision(d)
        if its:
            L.append(f"{d:<8} {len(its):>6} {sum(i.quantity for i in its):>7}")

    L.append("")
    L.append("by colour (print + risky)   lots  pieces")
    by_col: dict = defaultdict(lambda: [0, 0, "", False])
    for it in plan.items:
        if it.decision in ("print", "risky"):
            e = by_col[it.color_name]
            e[0] += 1
            e[1] += it.quantity
            e[2], e[3] = it.color_rgb, it.is_trans
    for name, (lots, pcs, rgb, trans) in sorted(by_col.items(), key=lambda kv: -kv[1][1]):
        label = f"{name}{' (trans)' if trans else ''}"
        L.append(f"  #{rgb:<6} {label:<20} {lots:>4} {pcs:>7}")

    L.append("")
    L.append(f"largest lots")
    for it in sorted(plan.items, key=lambda i: -i.quantity)[:top]:
        L.append(f"  {it.quantity:>4} x {it.part_num:<12} {it.color_name:<18} {it.decision:<5} {it.name[:50]}")

    unresolved = [it for it in parts if it.ldraw_id is None]
    if unresolved:
        L.append("")
        L.append("no LDraw geometry")
        for it in unresolved:
            L.append(f"  {it.part_num:<12} {it.color_name:<18} x{it.quantity:<4} {it.name[:50]}")
    return "\n".join(L)
