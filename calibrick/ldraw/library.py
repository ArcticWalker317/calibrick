"""Access to an LDraw parts library on disk (case-insensitive, indexed once)."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .parser import ParsedFile, parse_file

# kind -> sub-directories (relative to the library root), official first
_DIRS: dict[str, tuple[str, ...]] = {
    "parts": ("parts", "unofficial/parts"),
    "s": ("parts/s", "unofficial/parts/s"),
    "p": ("p", "unofficial/p"),
    "p48": ("p/48", "unofficial/p/48"),
    "p8": ("p/8", "unofficial/p/8"),
}
PRIM_KINDS = frozenset({"p", "p48", "p8"})
_MOVED_RE = re.compile(r"^~Moved to\s+(\S+)", re.IGNORECASE)


class LDrawPartNotFound(FileNotFoundError):
    """Raised by ``expand_part`` when ``parts/<id>.dat`` does not exist."""


def normalize_part_id(part_id: str) -> str:
    s = str(part_id).strip().lower().replace("\\", "/")
    if s.endswith(".dat"):
        s = s[:-4]
    if s.startswith("parts/"):
        s = s[6:]
    return s


class LDrawLibrary:
    """One LDraw library root (the folder that contains ``parts/`` and ``p/``)."""

    def __init__(self, root: str | os.PathLike | None = None):
        if root is None:
            from ..config import ldraw_dir

            root = ldraw_dir()
        self.root = Path(root)
        self._index: dict[str, dict[str, str]] | None = None
        self._prim_parsed: dict[str, ParsedFile] = {}     # key -> ParsedFile (p/ files only)
        self._prim_templates: dict = {}                    # (key, hires) -> Template
        self._describe_cache: dict[str, dict] = {}

    # ---- index -----------------------------------------------------------------------
    def _ensure_index(self) -> dict[str, dict[str, str]]:
        if self._index is None:
            idx: dict[str, dict[str, str]] = {}
            for kind, subs in _DIRS.items():
                d: dict[str, str] = {}
                for sub in subs:
                    folder = self.root / sub
                    try:
                        with os.scandir(folder) as it:
                            for e in it:
                                n = e.name
                                if len(n) > 4 and n[-4:].lower() == ".dat":
                                    d.setdefault(n.lower(), e.path)
                    except OSError:
                        continue
                idx[kind] = d
            self._index = idx
        return self._index

    @property
    def available(self) -> bool:
        """True if the library root looks usable (has part files)."""
        return bool(self._ensure_index()["parts"])

    def part_ids(self) -> list[str]:
        """All file stems in ``parts/`` (sorted)."""
        return sorted(n[:-4] for n in self._ensure_index()["parts"])

    def resolve(self, ref: str) -> tuple[str, str, str] | None:
        """Resolve a reference as written in a type-1 line.

        Returns ``(key, kind, path)`` or None. ``kind`` is one of parts / s / p / p48 / p8.
        """
        idx = self._ensure_index()
        name = ref.strip().lower().replace("\\", "/")
        while name.startswith("./"):
            name = name[2:]
        if name.startswith("parts/"):
            name = name[6:]
        elif name.startswith("p/"):
            name = name[2:]
            if not name.startswith(("48/", "8/")):
                p = idx["p"].get(name)
                return ("p/" + name, "p", p) if p else None
        if name.startswith("s/"):
            base = name[2:]
            p = idx["s"].get(base)
            return ("s/" + base, "s", p) if p else None
        if name.startswith("48/"):
            base = name[3:]
            p = idx["p48"].get(base)
            return ("p48/" + base, "p48", p) if p else None
        if name.startswith("8/"):
            base = name[2:]
            p = idx["p8"].get(base)
            return ("p8/" + base, "p8", p) if p else None
        p = idx["parts"].get(name)
        if p:
            return ("parts/" + name, "parts", p)
        p = idx["p"].get(name)
        if p:
            return ("p/" + name, "p", p)
        return None

    def hires_variant(self, key: str, kind: str) -> tuple[str, str, str] | None:
        """The ``p/48`` twin of a ``p/`` file, if there is one."""
        if kind != "p":
            return None
        base = key[2:]
        p = self._ensure_index()["p48"].get(base)
        return ("p48/" + base, "p48", p) if p else None

    # ---- parsing ---------------------------------------------------------------------
    def parsed(self, key: str, kind: str, path: str) -> ParsedFile:
        stem = key.rsplit("/", 1)[-1][:-4]
        if kind in PRIM_KINDS:
            pf = self._prim_parsed.get(key)
            if pf is None:
                pf = parse_file(path, stem)
                self._prim_parsed[key] = pf
            return pf
        return parse_file(path, stem)

    def clear_cache(self) -> None:
        self._prim_parsed.clear()
        self._prim_templates.clear()
        self._describe_cache.clear()

    # ---- public API ------------------------------------------------------------------
    def part_path(self, part_id: str) -> Path | None:
        p = self._ensure_index()["parts"].get(normalize_part_id(part_id) + ".dat")
        return Path(p) if p else None

    def has_part(self, part_id: str) -> bool:
        return (normalize_part_id(part_id) + ".dat") in self._ensure_index()["parts"]

    def describe(self, part_id: str) -> dict:
        """Header-only info: description, category, keywords, ldraw_org (+ a few extras).

        Returns an empty dict if the part does not exist.
        """
        pid = normalize_part_id(part_id)
        hit = self._describe_cache.get(pid)
        if hit is not None:
            return dict(hit)
        path = self.part_path(pid)
        if path is None:
            return {}
        pf = parse_file(path, pid, header_only=True)
        info = header_meta(pf.header)
        self._describe_cache[pid] = info
        return dict(info)


def header_meta(hdr: dict) -> dict:
    """Public meta fields derived from a parsed header."""
    desc = hdr.get("description", "")
    category = hdr.get("category", "")
    if not category:
        words = desc.lstrip("~=_| ").split()
        category = words[0] if words else ""
    out = {
        "description": desc,
        "ldraw_org": hdr.get("ldraw_org", ""),
        "ldraw_org_qualifier": hdr.get("ldraw_org_qualifier", ""),
        "category": category,
        "keywords": list(hdr.get("keywords", [])),
        "license": hdr.get("license", ""),
    }
    m = _MOVED_RE.match(desc)
    if m:
        out["moved_to"] = normalize_part_id(m.group(1))
    return out
