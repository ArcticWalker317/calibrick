"""Parser for a single LDraw file (.dat).

Produces direct triangles (quads split, winding normalised to CCW) and the list of
sub-file references. Nothing here touches the file system layout; see ``library.py``.
"""
from __future__ import annotations

import re
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

# n-d fraction primitives (4-4cyli, 1-4disc, 2-4ring3, 3-16cyli ...) and tori (t04o1429 ...)
_FRACTION_RE = re.compile(r"^(?:\d+-\d+|t\d\d[ioq])")


def is_fraction_primitive(stem: str) -> bool:
    return bool(_FRACTION_RE.match(stem))


@dataclass(slots=True)
class SubRef:
    name: str            # as referenced: lowercase, forward slashes ("s/3001s01.dat", "48/4-4cyli.dat")
    matrix: np.ndarray   # (4,4) float64, LDraw units, child-local -> this file's local
    invert: bool         # preceded by 0 BFC INVERTNEXT
    neg: bool            # determinant of the 3x3 part is negative


@dataclass
class ParsedFile:
    stem: str
    header: dict
    certified: bool                 # 0 BFC CERTIFY present (and no NOCERTIFY)
    tris: np.ndarray                # (K,3,3) float64, file-local, CCW convention
    refs: list[SubRef] = field(default_factory=list)
    n_bad: int = 0                  # malformed geometry lines skipped


def _new_header() -> dict:
    return {
        "description": "",
        "name": "",
        "author": "",
        "ldraw_org": "",
        "ldraw_org_qualifier": "",
        "category": "",
        "keywords": [],
        "license": "",
    }


def _parse_org(rest: str) -> tuple[str, str]:
    w = rest.split()
    if not w:
        return "", ""
    typ = w[0]
    if typ.lower().startswith("unofficial_"):
        typ = typ[len("unofficial_"):]
    qual = []
    for x in w[1:]:
        if x.upper() in ("UPDATE", "ORIGINAL"):
            break
        qual.append(x)
    return typ, " ".join(qual)


def _bulk_floats(txts: list[str], n: int) -> tuple[np.ndarray, np.ndarray | None]:
    """Parse ``len(txts)`` lines of ``n`` numbers each. Returns (array (m,n), keep_index or None)."""
    if not txts:
        return np.zeros((0, n)), None
    arr = None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            arr = np.fromstring(" ".join(txts), dtype=np.float64, sep=" ")
    except Exception:
        arr = None
    if arr is not None and arr.size == n * len(txts):
        return arr.reshape(-1, n), None
    rows, keep = [], []
    for i, s in enumerate(txts):
        tk = s.split()
        if len(tk) < n:
            continue
        try:
            rows.append([float(x) for x in tk[:n]])
        except ValueError:
            continue
        keep.append(i)
    if not rows:
        return np.zeros((0, n)), np.zeros(0, dtype=np.int64)
    return np.asarray(rows, dtype=np.float64), np.asarray(keep, dtype=np.int64)


def parse_text(text: str, stem: str = "", *, header_only: bool = False) -> ParsedFile:
    hdr = _new_header()
    certified = False
    nocert = False
    cw = False
    invert_next = False
    block = 0            # current !TEXMAP block id (0 = not inside START..FALLBACK/END)
    n_blocks = 0
    in_fallback = False
    dropped: set[int] = set()
    t3_txt: list[str] = []
    t3_cw: list[bool] = []
    t3_blk: list[int] = []
    t4_txt: list[str] = []
    t4_cw: list[bool] = []
    t4_blk: list[int] = []
    refs: list[SubRef] = []
    ref_blk: list[int] = []
    bad = 0
    first = True

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        sp = line.split(None, 1)
        t = sp[0]
        rest = sp[1] if len(sp) > 1 else ""
        if t == "0":
            if first:
                first = False
                hdr["description"] = rest.strip()
                continue
            if not rest:
                continue
            up = rest.upper()
            if up[0] == "!":
                if up.startswith("!:"):
                    continue  # texmap-only geometry, invisible to us
                if up.startswith("!TEXMAP"):
                    w = up.split()
                    sub = w[1] if len(w) > 1 else ""
                    if sub == "START":
                        n_blocks += 1
                        block = n_blocks
                        in_fallback = False
                    elif sub == "FALLBACK":
                        if block and not in_fallback:
                            dropped.add(block)   # a fallback exists: use it instead
                        in_fallback = True
                    elif sub == "END":
                        block = 0
                        in_fallback = False
                elif up.startswith("!LDRAW_ORG"):
                    hdr["ldraw_org"], hdr["ldraw_org_qualifier"] = _parse_org(rest[10:])
                elif up.startswith("!CATEGORY"):
                    hdr["category"] = rest[9:].strip()
                elif up.startswith("!KEYWORDS"):
                    hdr["keywords"].extend(k.strip() for k in rest[9:].split(",") if k.strip())
                elif up.startswith("!LICENSE"):
                    hdr["license"] = rest[8:].strip()
                continue
            if up.startswith("BFC"):
                for tok in up.split()[1:]:
                    if tok == "CERTIFY":
                        certified = True
                    elif tok == "NOCERTIFY":
                        nocert = True
                    elif tok == "CW":
                        cw = True
                    elif tok == "CCW":
                        cw = False
                    elif tok == "INVERTNEXT":
                        invert_next = True
                continue
            if up.startswith("NAME:"):
                hdr["name"] = rest[5:].strip()
            elif up.startswith("AUTHOR:"):
                hdr["author"] = rest[7:].strip()
            elif up.startswith("LDRAW_ORG") and not hdr["ldraw_org"]:
                hdr["ldraw_org"], hdr["ldraw_org_qualifier"] = _parse_org(rest[9:])
            continue

        first = False
        if header_only:
            break
        cur_blk = 0 if (in_fallback or block == 0) else block
        if t == "1":
            tok = rest.split()
            inv = invert_next
            invert_next = False
            if len(tok) < 14:
                bad += 1
                continue
            try:
                x, y, z, a, b, c, d, e, f, g, h, i = (float(v) for v in tok[1:13])
            except ValueError:
                bad += 1
                continue
            m = np.array(
                [[a, b, c, x], [d, e, f, y], [g, h, i, z], [0.0, 0.0, 0.0, 1.0]], dtype=np.float64
            )
            det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
            name = " ".join(tok[13:]).lower().replace("\\", "/")
            refs.append(SubRef(name, m, inv, det < 0.0))
            ref_blk.append(cur_blk)
        elif t == "3":
            invert_next = False
            s2 = rest.split(None, 1)
            if len(s2) < 2:
                bad += 1
                continue
            t3_txt.append(s2[1])
            t3_cw.append(cw)
            t3_blk.append(cur_blk)
        elif t == "4":
            invert_next = False
            s2 = rest.split(None, 1)
            if len(s2) < 2:
                bad += 1
                continue
            t4_txt.append(s2[1])
            t4_cw.append(cw)
            t4_blk.append(cur_blk)
        else:
            invert_next = False   # lines (2, 5) and anything unknown

    # ---- triangles -----------------------------------------------------------------
    a3, keep3 = _bulk_floats(t3_txt, 9)
    f3_cw = np.asarray(t3_cw, dtype=bool)
    f3_blk = np.asarray(t3_blk, dtype=np.int64)
    if keep3 is not None:
        bad += len(t3_txt) - len(keep3)
        f3_cw, f3_blk = f3_cw[keep3], f3_blk[keep3]
    a4, keep4 = _bulk_floats(t4_txt, 12)
    f4_cw = np.asarray(t4_cw, dtype=bool)
    f4_blk = np.asarray(t4_blk, dtype=np.int64)
    if keep4 is not None:
        bad += len(t4_txt) - len(keep4)
        f4_cw, f4_blk = f4_cw[keep4], f4_blk[keep4]

    tri3 = a3.reshape(-1, 3, 3)
    q = a4.reshape(-1, 4, 3)
    tri4 = np.stack([q[:, (0, 1, 2)], q[:, (0, 2, 3)]], axis=1).reshape(-1, 3, 3)
    tris = np.concatenate([tri3, tri4], axis=0)
    cwf = np.concatenate([f3_cw, np.repeat(f4_cw, 2)])
    blk = np.concatenate([f3_blk, np.repeat(f4_blk, 2)])
    if cwf.any():
        tris[cwf] = tris[cwf][:, ::-1]
    if dropped:
        tris = tris[~np.isin(blk, list(dropped))]
        refs = [r for r, b in zip(refs, ref_blk) if b not in dropped]
    tris = np.ascontiguousarray(tris)

    return ParsedFile(
        stem=stem,
        header=hdr,
        certified=certified and not nocert,
        tris=tris,
        refs=refs,
        n_bad=bad,
    )


def read_text(path: str | Path) -> str:
    data = Path(path).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    if text[:1] == "﻿":
        text = text[1:]
    return text


def parse_file(path: str | Path, stem: str | None = None, *, header_only: bool = False) -> ParsedFile:
    p = Path(path)
    return parse_text(read_text(p), stem if stem is not None else p.stem.lower(), header_only=header_only)
