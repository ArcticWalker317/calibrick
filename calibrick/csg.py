"""Small solid-modelling helpers on top of manifold3d (all sizes in mm)."""
from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np
from manifold3d import Manifold, Mesh, OpType

# Segments for calibrated round features. Chord error at r = 2.4 mm is ~0.001 mm.
SEG = 96


def box(x0: float, y0: float, z0: float, x1: float, y1: float, z1: float) -> Manifold:
    return Manifold.cube([x1 - x0, y1 - y0, z1 - z0]).translate([x0, y0, z0])


def cyl(cx: float, cy: float, z0: float, z1: float, d: float, seg: int = SEG,
        d_top: float | None = None) -> Manifold:
    """Vertical cylinder (or cone frustum if d_top is given) from z0 to z1."""
    r0 = d / 2.0
    r1 = r0 if d_top is None else d_top / 2.0
    return Manifold.cylinder(z1 - z0, r0, r1, seg).translate([cx, cy, z0])


def ring(cx: float, cy: float, z0: float, z1: float, od: float, id_: float,
         seg: int = SEG) -> Manifold:
    """Vertical tube: outer diameter od, bore id_."""
    return cyl(cx, cy, z0, z1, od, seg) - cyl(cx, cy, z0 - 1.0, z1 + 1.0, id_, seg)


def stud(cx: float, cy: float, z0: float, d: float, h: float, lead: float = 0.12,
         lead_h: float = 0.16, embed: float = 0.3, seg: int = SEG) -> Manifold:
    """Solid stud standing on z0, sunk `embed` below it so unions are clean.

    The top `lead_h` (one layer) is `lead` smaller in radius: a printed lead-in that also
    keeps the slightly over-extruded top layer from acting as the gripping diameter.
    """
    if lead <= 0 or lead_h <= 0 or lead_h >= h:
        return cyl(cx, cy, z0 - embed, z0 + h, d, seg)
    body = cyl(cx, cy, z0 - embed, z0 + h - lead_h, d, seg)
    top = cyl(cx, cy, z0 + h - lead_h - 0.01, z0 + h, d - 2.0 * lead, seg)
    return body + top


def union(parts: Iterable[Manifold]) -> Manifold:
    parts = [p for p in parts if p is not None]
    if not parts:
        return Manifold()
    if len(parts) == 1:
        return parts[0]
    return Manifold.batch_boolean(parts, OpType.Add)


# ---- 3x5 pixel font (enough for labels) ---------------------------------------------------
_FONT = {
    "0": "### # # # # # ###", "1": " #  ##   #   #  ###", "2": "###   # ### #   ###",
    "3": "###   # ###   # ###", "4": "# # # # ###   #   #", "5": "### #   ###   # ###",
    "6": "### #   ### # # ###", "7": "###   #   #   #   #", "8": "### # # ### # # ###",
    "9": "### # # ###   # ###", "A": "### # # ### # # # #", "B": "##  # # ##  # # ## ",
    "C": "### #   #   #   ###", "S": "### #   ###   # ###", "U": "# # # # # # # # ###",
    "-": "        ###        ", "_": "                ###",
}
_FONT["0"] = "### # # # # # # ###"


def _glyph_rows(ch: str) -> list[str]:
    g = _FONT[ch.upper()]
    # glyphs are stored as 5 rows of 3 characters separated by single spaces
    rows = [g[i * 4:i * 4 + 3] for i in range(5)]
    return [r.ljust(3) for r in rows]


def pixel_text(text: str, cx: float, cy: float, z0: float, z1: float, px: float = 1.0,
               underline: bool = False, mirror_x: bool = False) -> Manifold:
    """Blocks spelling `text`, centred on (cx, cy), spanning z0..z1.

    Rows run top (high y) to bottom. `underline` adds a bar below (disambiguates 6/9).
    `mirror_x` flips left/right so the text reads correctly when seen from below.
    """
    cols = len(text) * 3 + (len(text) - 1)
    nrows = 5 + (2 if underline else 0)
    w, h = cols * px, nrows * px
    x_left, y_top = cx - w / 2.0, cy + h / 2.0
    cells: list[tuple[int, int]] = []
    for k, ch in enumerate(text):
        for r, row in enumerate(_glyph_rows(ch)):
            for c, v in enumerate(row):
                if v == "#":
                    cells.append((k * 4 + c, r))
    if underline:
        cells += [(c, 6) for c in range(cols)]
    blocks = []
    for c, r in cells:
        if mirror_x:
            c = cols - 1 - c
        x0 = x_left + c * px
        y1 = y_top - r * px
        # tiny overlap so neighbouring pixels fuse into one solid
        blocks.append(box(x0 - 0.01, y1 - px - 0.01, z0, x0 + px + 0.01, y1 + 0.01, z1))
    return union(blocks)


# ---- mesh conversion / export -----------------------------------------------------------------
def to_tris(m: Manifold) -> np.ndarray:
    """(M, 3, 3) float64 triangle corners."""
    mesh = m.to_mesh()
    v = np.asarray(mesh.vert_properties, dtype=np.float64)[:, :3]
    f = np.asarray(mesh.tri_verts, dtype=np.int64)
    return v[f]


def weld(tris: np.ndarray, tol: float = 1e-5) -> tuple[np.ndarray, np.ndarray]:
    """Merge coincident corners. Returns (vertices (N,3), faces (M,3)); drops degenerate faces."""
    pts = np.asarray(tris, dtype=np.float64).reshape(-1, 3)
    key = np.round(pts / tol).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    verts = pts[first]
    faces = inv.reshape(-1, 3)
    ok = (faces[:, 0] != faces[:, 1]) & (faces[:, 1] != faces[:, 2]) & (faces[:, 0] != faces[:, 2])
    return verts, faces[ok]


def from_mesh(verts: np.ndarray, faces: np.ndarray) -> Manifold:
    """Build a Manifold from an indexed mesh; status() != NoError (or empty) if it is not a valid solid."""
    mesh = Mesh(vert_properties=np.ascontiguousarray(verts, dtype=np.float32),
                tri_verts=np.ascontiguousarray(faces, dtype=np.uint32))
    return Manifold(mesh)


def write_stl(path: str | Path, tris: np.ndarray, name: bytes = b"calibrick") -> Path:
    """Binary STL from (M, 3, 3) triangle corners."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    t = np.asarray(tris, dtype=np.float32)
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 0)
    rec = np.zeros(len(t), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    rec["n"] = n
    rec["v"] = t
    with open(path, "wb") as f:
        f.write(name.ljust(80, b" ")[:80])
        f.write(struct.pack("<I", len(t)))
        f.write(rec.tobytes())
    return path


def read_stl(path: str | Path) -> np.ndarray:
    """(M, 3, 3) triangle corners from a binary STL."""
    data = Path(path).read_bytes()
    n = struct.unpack("<I", data[80:84])[0]
    rec = np.frombuffer(data, dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")],
                        count=n, offset=84)
    return rec["v"].astype(np.float64)


def save(m: Manifold, path: str | Path) -> Path:
    return write_stl(path, to_tris(m))


def bbox(m: Manifold) -> tuple[np.ndarray, np.ndarray]:
    b = m.bounding_box()
    return np.array(b[:3]), np.array(b[3:])
