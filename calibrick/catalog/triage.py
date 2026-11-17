"""Decide per lot whether to print it, print it with care, buy it or skip it.

All rules live in :data:`RULES`. Each rule is a decision, a reason and a set of conditions
that must all hold:

* ``material`` / ``category`` / ``name`` / ``kind``: regular expressions (case-insensitive,
  ``re.search``) against the item's field; ``not_name`` excludes matching names
* ``flag``: one of the computed flags ``trans``, ``decorated``, ``no_geometry``

The final decision is the strongest one that has a matching rule
(skip > buy > risky > print); the reasons of every matching rule of that strength are kept.
Edit the table to change behaviour; ``{material}`` / ``{category}`` in a reason are filled in.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

STRENGTH = {"skip": 3, "buy": 2, "risky": 1, "print": 0}
FIT = "fit feature not calibrated yet"


@dataclass(frozen=True)
class Rule:
    decision: str
    reason: str
    material: str | None = None
    category: str | None = None
    name: str | None = None
    not_name: str | None = None
    kind: str | None = None
    flag: str | None = None


_MINIFIG_CATS = r"^(Minifigs|Minifig (Heads|Upper Body|Lower Body)|Minidoll (Heads|Upper Body|Lower Body))$"
_TECHNIC_FIT_CATS = r"^Technic (Pins|Axles|Connectors|Bushes)$"
_FIT_NAME = r"\b(bars?|clips?|hinges?|ball|socket|handle|tow ball|bar holder)\b"
_NOT_FIT_NAME = r"gold bar|ingot|bar of soap|chocolate bar"  # 'bar' that is not a 3.2 mm bar
_FLEX_NAME = (r"\b(axle|hose|tube)\b.*\b(flexible|flex|soft)\b"
              r"|\b(flexible|flex|soft)\b.*\b(axle|hose|tube)\b")
_ELECTRIC_NAME = r"power functions|\bmotor\b|battery|\bsensor\b|pneumatic|\belectric|light brick|\bLED\b"

RULES: tuple[Rule, ...] = (
    # ---- skip
    Rule("skip", "sticker sheet: not a part", category=r"^Stickers$"),
    # ---- buy: not plastic, or a kind of part FDM cannot reproduce
    Rule("buy", "{material}: not FDM-printable", material=r"^(?!plastic$).+"),
    Rule("buy", "minifigure: buy", kind=r"^minifig$"),
    Rule("buy", "minifig part: buy (tiny, printed faces / torsos)", category=_MINIFIG_CATS),
    Rule("buy", "tyre: needs rubber", category=r"^Wheels and Tyres$", name=r"\b(tyres?|tires?)\b"),
    Rule("buy", "string / band / reel", category=r"^String, Bands and Reels$"),
    Rule("buy", "flexible hose / soft axle", category=r"^Tubes and Hoses$", not_name=r"\brigid\b"),
    Rule("buy", "flexible hose / soft axle", name=_FLEX_NAME),
    Rule("buy", "electric / power functions / pneumatics", category=r"^(Electronics|Pneumatics)$"),
    Rule("buy", "electric / power functions / pneumatics", name=_ELECTRIC_NAME),
    Rule("buy", "magnet", category=r"^Magnets and Holders$"),
    Rule("buy", "magnet", name=r"\bmagnet"),
    Rule("buy", "no LDraw geometry", flag="no_geometry"),
    # ---- risky: printable, but expect trouble
    Rule("risky", "transparent colour: FDM clear prints cloudy; consider buying", flag="trans"),
    Rule("risky", "decoration not reproduced: prints as the plain part", flag="decorated"),
    Rule("risky", FIT + " (Technic pin / axle / connector)", category=_TECHNIC_FIT_CATS),
    Rule("risky", FIT + " (hinge / turntable)", category=r"^Hinges, Arms and Turntables$"),
    Rule("risky", FIT + " (bar / clip / hinge / ball joint)", name=_FIT_NAME, not_name=_NOT_FIT_NAME),
    Rule("risky", FIT + " (wheel / wheel cover)", category=r"^Wheels and Tyres$"),
    Rule("risky", FIT + " (thin rigid hose)", category=r"^Tubes and Hoses$"),
    Rule("risky", FIT + " (plant / animal)",
         category=r"^(Plants & Trees|Animals / Creatures|Animal / Creature Body Parts)$"),
)

_RX: dict[str, re.Pattern] = {}


def _match(pattern: str | None, value: str) -> bool:
    if pattern is None:
        return True
    rx = _RX.get(pattern)
    if rx is None:
        rx = _RX[pattern] = re.compile(pattern, re.I)
    return rx.search(value or "") is not None


def triage(
    *,
    name: str,
    category: str,
    material: str,
    kind: str = "part",
    is_trans: bool = False,
    decorated: bool = False,
    has_geometry: bool = True,
    rules: tuple[Rule, ...] = RULES,
) -> tuple[str, list[str]]:
    """Return ``(decision, reasons)`` for one lot."""
    flags = {"trans": is_trans, "decorated": decorated, "no_geometry": not has_geometry}
    best = "print"
    reasons: list[str] = []
    for r in rules:
        if r.flag is not None and not flags.get(r.flag, False):
            continue
        if not (_match(r.material, material) and _match(r.category, category)
                and _match(r.name, name) and _match(r.kind, kind)):
            continue
        if r.not_name is not None and _match(r.not_name, name):
            continue
        text = r.reason.format(material=material or "unknown material", category=category)
        if STRENGTH[r.decision] > STRENGTH[best]:
            best, reasons = r.decision, [text]
        elif r.decision == best and text not in reasons:
            reasons.append(text)
    return best, reasons
