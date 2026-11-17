"""Map a Rebrickable part number to the LDraw file that supplies its geometry.

Rebrickable numbering mostly follows LDraw, with three systematic differences:
decorated parts (``3069bpr0030`` has no LDraw twin, but its blank base ``3069b`` does),
mold variants / alternates (``3070a`` vs ``3070b``) and letter suffixes (``3070b`` vs ``3070``).

Order tried by :meth:`Resolver.resolve`:

1. decorated parts (``P`` print / ``T`` pattern relationship, child = decorated part, or a
   decoration suffix such as ``pr0001`` / ``pat0001`` / ``p01``): the blank base, itself
   resolved with steps 2-4; the decorated file only if no base resolves. The blank base is
   preferred even when LDraw has the printed file: same solid, no coplanar decoration
   polygons, and it shares the cached mesh with the plain part.
2. the exact id
3. mold variants (``M``) and alternates (``A``), both directions, up to two hops
4. letter-suffix variants (3069b <-> 3069 <-> 3069a ...)
"""
from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .. import config

# decorated-part suffixes: Rebrickable "pr0001"/"pat0001" (optionally followed by an
# assembly suffix such as c01) and LDraw-style "p01" / "pb01" / "p0a"
_DECOR_RB = re.compile(r"^(?P<base>\d[0-9a-z]*?)(?:pr|pat)\d{2,}[0-9a-z]*$", re.I)
_DECOR_LD = re.compile(r"^(?P<base>\d+[a-z]?)p[0-9a-z]{2,3}$", re.I)
_DECOR_NAME = re.compile(r"\b(print(ed)?|pattern|decorat\w*|sticker)\b", re.I)
_DIGITS_LETTER = re.compile(r"^(\d+)([a-z])$")
_DIGITS = re.compile(r"^\d+$")


@dataclass
class Resolution:
    ldraw_id: str | None
    how: str                 # exact | base | variant | assumed | none
    decorated: bool = False
    notes: list[str] = field(default_factory=list)


class DirLibrary:
    """Minimal stand-in for calibrick.ldraw.LDrawLibrary: only knows which parts exist.

    Looks for ``<root>/parts/<id>.dat`` case-insensitively, from one cached directory listing.
    """

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self._stems: set[str] | None = None

    def _scan(self) -> set[str]:
        if self._stems is None:
            stems: set[str] = set()
            try:
                with os.scandir(self.root / "parts") as it:
                    for e in it:
                        n = e.name.lower()
                        if n.endswith(".dat"):
                            stems.add(n[:-4])
            except OSError:
                pass
            self._stems = stems
        return self._stems

    def available(self) -> bool:
        return bool(self._scan())

    def has_part(self, part_id: str) -> bool:
        return part_id.lower() in self._scan()


_DIR_LIBS: dict[str, DirLibrary] = {}


def default_library() -> DirLibrary | None:
    """Directory-scan library for ``config.ldraw_dir()``, or None if it has no parts."""
    root = str(Path(config.ldraw_dir()).resolve())
    lib = _DIR_LIBS.get(root)
    if lib is None:
        lib = _DIR_LIBS[root] = DirLibrary(root)
    return lib if lib.available() else None


def decoration_base_guess(part_num: str) -> str | None:
    """Blank-base part number implied by the part number alone (None if not decorated)."""
    for rx in (_DECOR_RB, _DECOR_LD):
        m = rx.match(part_num)
        if m:
            return m.group("base")
    return None


def suffix_variants(part_num: str) -> list[str]:
    """Near-identical parts that differ only by a mold letter (3069b <-> 3069 <-> 3069a)."""
    pn = part_num.lower()
    out: list[str] = []
    m = _DIGITS_LETTER.match(pn)
    if m:
        digits, letter = m.groups()
        out.append(digits)
        out += [digits + c for c in "abcdef" if c != letter]
    elif _DIGITS.match(pn):
        out += [pn + c for c in "abcd"]
    return out


_REL_LABEL = {"M": "mold variant", "A": "alternate part"}


class Resolver:
    """Resolve part numbers against the catalog relationships and an LDraw library.

    ``lib`` needs only ``has_part(id) -> bool``. ``lib=None`` means no library could be
    found: nothing can be verified, so every part is assumed to exist under its own (or
    its blank base) number and ``how`` is ``"assumed"``.
    """

    def __init__(self, conn: sqlite3.Connection, lib=None):
        self.conn = conn
        self.lib = lib
        self._has: dict[str, bool] = {}
        self._cache: dict[str, Resolution] = {}

    # -- library ---------------------------------------------------------------------
    def has(self, pid: str) -> bool:
        pid = pid.lower()
        if pid not in self._has:
            try:
                self._has[pid] = bool(self.lib.has_part(pid)) if self.lib is not None else True
            except Exception:  # noqa: BLE001 - a broken library lookup counts as "missing"
                self._has[pid] = False
        return self._has[pid]

    # -- relationships ---------------------------------------------------------------
    def _rel(self, pn: str, types: tuple[str, ...], *, up: bool) -> list[tuple[str, str]]:
        q = ",".join("?" for _ in types)
        src, dst = ("child_part_num", "parent_part_num") if up else ("parent_part_num", "child_part_num")
        rows = self.conn.execute(
            f"SELECT {dst}, rel_type FROM part_relationships WHERE {src}=? AND rel_type IN ({q})"
            f" ORDER BY {dst}",
            (pn, *types),
        ).fetchall()
        return [(r[0], r[1]) for r in rows]

    def decoration_bases(self, pn: str) -> list[str]:
        """Blank ancestors of a printed / patterned part (P/T parents), nearest first."""
        out: list[str] = []
        frontier = [pn]
        for _ in range(3):
            nxt: list[str] = []
            for p in frontier:
                for par, _t in self._rel(p, ("P", "T"), up=True):
                    if par not in out and par != pn:
                        out.append(par)
                        nxt.append(par)
            frontier = nxt
            if not frontier:
                break
        return out

    def mold_variants(self, pn: str) -> list[tuple[str, str]]:
        """(part, rel_type) of mold variants and alternates within two hops, nearest first."""
        seen = {pn}
        order: list[tuple[str, str]] = []
        frontier = [pn]
        for _ in range(2):
            nxt: list[str] = []
            for p in frontier:
                for q, t in self._rel(p, ("M", "A"), up=True) + self._rel(p, ("M", "A"), up=False):
                    if q not in seen:
                        seen.add(q)
                        order.append((q, t))
                        nxt.append(q)
            frontier = nxt
        # mold variants are the same shape; alternates may differ a little -> try M first
        return sorted(order, key=lambda qt: qt[1] != "M")

    # -- resolution ------------------------------------------------------------------
    def _plain(self, pn: str) -> tuple[str, str, str] | None:
        """(ldraw_id, how, note) for an undecorated part number, or None."""
        if self.has(pn):
            return pn.lower(), "exact", ""
        for cand, t in self.mold_variants(pn):
            if decoration_base_guess(cand) is None and self.has(cand):
                return cand.lower(), "variant", f"geometry from {_REL_LABEL[t]} {cand}"
        for cand in suffix_variants(pn):
            if self.has(cand):
                return cand.lower(), "variant", f"geometry from similar part {cand} (mold letter differs)"
        return None

    def resolve(self, part_num: str, name: str = "") -> Resolution:
        """Resolve one part; ``name`` (the catalog name) helps recognise decorated parts."""
        if part_num in self._cache:
            return self._cache[part_num]
        res = self._resolve(part_num, name)
        self._cache[part_num] = res
        return res

    def _resolve(self, pn: str, name: str) -> Resolution:
        assumed = self.lib is None
        bases = self.decoration_bases(pn)
        guess = decoration_base_guess(pn)
        # a suffix that merely looks like a decoration code only counts when the name says
        # so, or when the part itself has no geometry anyway
        if guess and not bases and not _DECOR_NAME.search(name or "") and self.has(pn):
            guess = None
        decorated = bool(bases) or guess is not None

        if decorated:
            cands = list(bases)
            if guess and guess not in cands:
                cands.append(guess)
            for base in cands:
                hit = self._plain(base)
                if hit:
                    lid, _how, note = hit
                    notes = [f"decorated part: geometry from blank base {base}"]
                    if note:
                        notes.append(note)
                    return Resolution(lid, "assumed" if assumed else "base", True, notes)
            hit = None if assumed else self._plain(pn)
            if hit:
                lid, how, note = hit
                return Resolution(lid, how, True, [note] if note else [])
            return Resolution(None, "none", True, [])

        hit = self._plain(pn)
        if hit:
            lid, how, note = hit
            return Resolution(lid, "assumed" if assumed else how, False, [note] if note else [])
        return Resolution(None, "none", False, [])


def resolve_ldraw_id(part_num: str, conn: sqlite3.Connection, lib=None, name: str = "") -> Resolution:
    """One-off convenience wrapper around :class:`Resolver`."""
    return Resolver(conn, lib).resolve(part_num, name)
