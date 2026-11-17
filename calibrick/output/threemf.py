"""Minimal 3MF writer: several mesh objects, each instanced any number of times.

A 3MF file is a zip with ``[Content_Types].xml``, ``_rels/.rels`` and ``3D/3dmodel.model``.
Every distinct mesh becomes one ``<object>`` resource; every placement of it becomes one
``<item>`` in ``<build>`` with a transform (rotation about Z around the mesh's own origin,
then translation in X/Y; Z is unchanged). Units are millimetres.

Items are given as mappings or objects with ``name``, ``mesh`` and ``placements``
(optionally ``color_rgb``, e.g. ``"#C91A09"`` or ``"C91A09"``). ``mesh`` is an (M, 3, 3)
array of triangle corners or a path to a binary STL.
"""
from __future__ import annotations

import io
import math
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from ..csg import read_stl, weld

NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
MODEL_PATH = "3D/3dmodel.model"

CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
    ' <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
    ' <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>\n'
    ' <Default Extension="png" ContentType="image/png"/>\n'
    ' <Default Extension="config" ContentType="text/xml"/>\n'
    '</Types>\n'
)

RELS = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
    f' <Relationship Target="/{MODEL_PATH}" Id="rel0" '
    'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>\n'
    '</Relationships>\n'
)


@dataclass
class MeshItem:
    name: str
    mesh: Any                                   # (M,3,3) array or STL path
    placements: list[tuple[float, float, float]] = field(default_factory=list)
    color_rgb: str | None = None


def _get(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def load_tris(mesh: Any) -> np.ndarray:
    """(M, 3, 3) float64 triangle corners from an array or a binary STL path."""
    if isinstance(mesh, (str, Path)):
        return read_stl(mesh)
    tris = np.asarray(mesh, dtype=np.float64)
    if tris.ndim != 3 or tris.shape[1:] != (3, 3):
        raise ValueError(f"mesh must be (M,3,3) triangle corners, got shape {tris.shape}")
    return tris


def placement_matrix(x: float, y: float, rot_deg: float) -> np.ndarray:
    """4x4 column-vector transform: rotate about Z by rot_deg, then translate by (x, y, 0)."""
    a = math.radians(rot_deg)
    c, s = math.cos(a), math.sin(a)
    # snap exact quarter turns so 90 deg rotations are exact
    c, s = round(c, 12) + 0.0, round(s, 12) + 0.0
    m = np.eye(4)
    m[:2, :2] = [[c, -s], [s, c]]
    m[0, 3], m[1, 3] = x, y
    return m


def transform_attr(m: np.ndarray) -> str:
    """3MF transform string (row-vector convention: m00 m01 m02 m10 ... m32)."""
    r = m[:3, :3].T            # 3MF multiplies row vectors: p' = p @ M
    vals = list(r.reshape(-1)) + list(m[:3, 3])
    return " ".join(_num(v) for v in vals)


def _num(v: float) -> str:
    s = f"{float(v):.6f}".rstrip("0").rstrip(".")
    return "0" if s in ("", "-0") else s


def _norm_rgb(rgb: str | None) -> str | None:
    if not rgb:
        return None
    h = rgb.strip().lstrip("#")
    if len(h) != 6:
        return None
    try:
        int(h, 16)
    except ValueError:
        return None
    return "#" + h.upper()


def _mesh_xml(out: io.StringIO, verts: np.ndarray, faces: np.ndarray) -> None:
    out.write("   <mesh>\n    <vertices>\n")
    out.writelines(f'     <vertex x="{_num(a)}" y="{_num(b)}" z="{_num(c)}"/>\n' for a, b, c in verts)
    out.write("    </vertices>\n    <triangles>\n")
    out.writelines(f'     <triangle v1="{a}" v2="{b}" v3="{c}"/>\n' for a, b, c in faces)
    out.write("    </triangles>\n   </mesh>\n")


@dataclass
class _Prepared:
    name: str
    verts: np.ndarray
    faces: np.ndarray
    placements: list[tuple[float, float, float]]
    color: str | None
    object_id: int = 0


def _prepare(items: Iterable[Any]) -> list[_Prepared]:
    prepared = []
    for it in items:
        if isinstance(it, (tuple, list)):
            it = MeshItem(*it)
        name = str(_get(it, "name"))
        tris = load_tris(_get(it, "mesh"))
        verts, faces = weld(tris)
        if len(faces) == 0:
            raise ValueError(f"mesh {name!r} has no triangles")
        pl = [(float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)
              for p in (_get(it, "placements") or [])]
        prepared.append(_Prepared(name, verts, faces, pl, _norm_rgb(_get(it, "color_rgb"))))
    return prepared


def model_xml(prepared: Sequence[_Prepared], *, title: str = "calibrick") -> str:
    out = io.StringIO()
    out.write('<?xml version="1.0" encoding="UTF-8"?>\n')
    out.write(f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS_CORE}">\n')
    out.write(f' <metadata name="Title">{escape(title)}</metadata>\n')
    out.write(' <metadata name="Application">calibrick</metadata>\n')
    out.write(" <resources>\n")
    colors: list[str] = []
    for p in prepared:
        if p.color and p.color not in colors:
            colors.append(p.color)
    next_id = 1
    mat_id = None
    if colors:
        mat_id = next_id
        next_id += 1
        out.write(f'  <basematerials id="{mat_id}">\n')
        for c in colors:
            out.write(f'   <base name={quoteattr(c)} displaycolor="{c}"/>\n')
        out.write("  </basematerials>\n")
    for p in prepared:
        p.object_id = next_id
        next_id += 1
        mat = ""
        if mat_id is not None and p.color:
            mat = f' pid="{mat_id}" pindex="{colors.index(p.color)}"'
        out.write(f'  <object id="{p.object_id}" name={quoteattr(p.name)} type="model"{mat}>\n')
        _mesh_xml(out, p.verts, p.faces)
        out.write("  </object>\n")
    out.write(" </resources>\n <build>\n")
    for p in prepared:
        for x, y, r in p.placements:
            out.write(f'  <item objectid="{p.object_id}" '
                      f'transform="{transform_attr(placement_matrix(x, y, r))}"/>\n')
    out.write(" </build>\n</model>\n")
    return out.getvalue()


def write_3mf(path: str | Path, items: Iterable[Any], *, title: str | None = None) -> Path:
    """Write a multi-object 3MF. Returns the path.

    ``items``: iterable of mappings/objects with ``name``, ``mesh`` and ``placements``
    (list of ``(x, y, rotation_z_degrees)``), or ``(name, mesh, placements)`` tuples.
    Items with no placements are still stored as resources (not printed).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    prepared = _prepare(items)
    xml = model_xml(prepared, title=title or path.stem)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr(MODEL_PATH, xml)
    return path


def read_3mf_items(path: str | Path) -> list[dict]:
    """Small reader for tests/inspection: one dict per build item with the object name,
    vertex count and the 4x4 column-vector transform."""
    import xml.etree.ElementTree as ET

    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read(MODEL_PATH))
    ns = {"m": NS_CORE}
    objs = {}
    for o in root.findall("m:resources/m:object", ns):
        verts = o.findall("m:mesh/m:vertices/m:vertex", ns)
        objs[o.get("id")] = (o.get("name"), len(verts))
    items = []
    for it in root.findall("m:build/m:item", ns):
        vals = [float(v) for v in (it.get("transform") or "1 0 0 0 1 0 0 0 1 0 0 0").split()]
        m = np.eye(4)
        m[:3, :3] = np.array(vals[:9]).reshape(3, 3).T
        m[:3, 3] = vals[9:12]
        name, nv = objs[it.get("objectid")]
        items.append({"objectid": it.get("objectid"), "name": name, "n_vertices": nv, "matrix": m})
    return items
