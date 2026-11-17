"""Geometry engine: LDraw RawMesh + calibration Profile -> printable, calibrated solid.

Why this exists
---------------
LDraw draws parts idealised for rendering: exterior walls sit exactly on the 8 mm grid
(real parts are 0.1 mm smaller per side), walls are 1.6 mm, underside tubes 6.4 mm OD
(real 6.51), studs 4.8 x 1.6, and plate cavities are only 1.6 mm deep (a genuine 1.7-1.8 mm
stud would bottom out). FDM adds its own per-feature errors. One XY compensation number in
the slicer cannot fix this, because different features of one part need different and
opposite corrections. So the engine corrects per feature, in the model.

How
---
1. Expand the part (calibrick.ldraw) and weld it into an indexed mesh.
2. Find **circles**: every vertex emitted by an axisymmetric LDraw primitive (cyli, disc,
   ring, ndis, con ...) lies on a circle about that primitive's axis. Circles carry radius,
   axis, whether the surface faces outward (boss) or inward (hole), and which semantic
   primitive (stud, stud4, peghole ...) they came from.
3. **Re-synthesise** the fit-critical free-standing features as our own closed, high-resolution
   solids at calibrated sizes, and delete their LDraw triangles:
   studs (solid / hollow), underside tubes, underside posts. These use the same csg.stud()
   as the calibration coupon, so coupon results transfer exactly.
4. **Displace** the remaining body vertices: a small least-squares solve per vertex so that
   - exterior side faces move in by `ext_clearance`,
   - cavity walls (enclosed side faces that reach the bottom) move by the calibrated wall offset,
   - shallow cavity ceilings move up (`roof_raise`) so genuine studs seat fully,
   - circles left in the body (sockets, pin holes, bars, round exteriors) change radius,
   - every other flat face stays in its plane.
   Moving shared vertices keeps the mesh exactly as watertight as LDraw made it.
5. Union body + features with manifold3d when the body is a valid solid (tier A). Otherwise
   emit the pieces as overlapping shells, which slicers union themselves (tier B).

Known limits (see docs/HANDOFF.md): only the global bottom plane is treated as the stud
cavity; side studs are synthesised but print lying down; curved hand-modelled surfaces are
not offset; Technic fits use uncalibrated starting values; print orientation is always
"as built" (studs up).
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from manifold3d import Manifold
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components
from scipy.spatial import cKDTree

from .. import csg
from . import repair
from ..ldraw import LDrawLibrary, RawMesh, expand_part
from ..profile import LDU, Profile

ENGINE_VERSION = 6     # bump whenever generated geometry can change (invalidates the part cache)
WELD_TOL = 1e-3          # mm
MIN_ROOF = 0.9           # mm of roof that must remain above a raised cavity ceiling
MAX_SHIFT = 0.6          # mm; no calibration offset legitimately moves a vertex further
PLATE_CAVITY = 1.6       # LDraw depth of a plate-height cavity

_PRIM_RE = re.compile(r"^\d+-\d+([a-z]+?)(\d*)$")


def prim_info(stem: str):
    """(curved, radii) for axisymmetric fraction primitives (unit = primitive-local), else None."""
    m = _PRIM_RE.match(stem)
    if not m:
        return None
    base, num = m.group(1), m.group(2)
    if base.startswith("cyl"):
        return True, (1.0,)
    if base in ("disc", "ndis", "chrd", "tang"):
        return False, (1.0,)
    if base in ("ring", "rin", "ri") and num:
        n = float(int(num))
        return False, (n, n + 1.0)
    if base in ("con", "co") and num:
        n = float(int(num))
        return True, (n, n + 1.0)
    return None


@dataclass
class PartResult:
    part_id: str
    ok: bool
    mesh_path: Path | None = None
    size: tuple[float, float, float] = (0.0, 0.0, 0.0)
    tier: str = "FAIL"                 # "A" verified solid | "B" overlapping shells | "FAIL"
    orientation: str = "studs_up"
    needs_support: bool = False
    volume_mm3: float = 0.0
    features: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    description: str = ""


@dataclass
class _Circle:
    center: np.ndarray
    axis: np.ndarray
    radius: float
    votes: int = 0                    # >0 outward-facing surface (boss), <0 inward (hole)
    flat: bool = False                # touched by a flat cap primitive (disc/ring/ndis)
    hires: bool = True
    partial: bool = False             # made (at least partly) from arc primitives, not full circles
    sem_ids: set = field(default_factory=set)
    prim_ids: set = field(default_factory=set)
    group: int = -1                   # coaxial group id
    t: float = 0.0                    # position along the axis


# ------------------------------------------------------------------------------------------
# small numeric helpers
# ------------------------------------------------------------------------------------------
def _unit(v):
    n = np.linalg.norm(v)
    return v / n if n > 1e-12 else v


def _canon(a):
    """Sign-normalise a direction so parallel axes compare equal."""
    a = _unit(np.asarray(a, dtype=np.float64))
    k = int(np.argmax(np.abs(a)))
    return -a if a[k] < 0 else a


def weld(tris: np.ndarray, tol: float = WELD_TOL):
    """Cluster coincident corners. Returns (verts (N,3), vidx (M,3))."""
    pts = tris.reshape(-1, 3)
    key = np.round(pts / 1e-6).astype(np.int64)
    uniq, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    up = uniq.astype(np.float64) * 1e-6
    n = len(up)
    pairs = cKDTree(up).query_pairs(tol, output_type="ndarray")
    if len(pairs):
        g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
        nc, lab = connected_components(g, directed=False)
        cent = np.zeros((nc, 3))
        np.add.at(cent, lab, up)
        cent /= np.bincount(lab, minlength=nc)[:, None]
    else:
        lab, cent = np.arange(n), up
    return cent, lab[inv].reshape(-1, 3)


def ray_dist(orig: np.ndarray, dirs: np.ndarray, tris: np.ndarray, chunk: int = 48) -> np.ndarray:
    """Distance to the first triangle hit for each ray (inf if none). Moller-Trumbore."""
    out = np.full(len(orig), np.inf)
    if not len(tris) or not len(orig):
        return out
    v0 = tris[:, 0]
    e1 = tris[:, 1] - v0
    e2 = tris[:, 2] - v0
    for s in range(0, len(orig), chunk):
        o = orig[s:s + chunk, None, :]
        d = dirs[s:s + chunk, None, :]
        p = np.cross(d, e2[None])
        det = np.einsum("rtk,tk->rt", p, e1)
        ok = np.abs(det) > 1e-12
        inv = np.where(ok, 1.0 / np.where(ok, det, 1.0), 0.0)
        tv = o - v0[None]
        u = np.einsum("rtk,rtk->rt", tv, p) * inv
        q = np.cross(tv, e1[None])
        v = np.einsum("rtk,rtk->rt", np.broadcast_to(d, q.shape), q) * inv
        t = np.einsum("rtk,tk->rt", q, e2) * inv
        hit = ok & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9) & (t > 1e-4)
        out[s:s + chunk] = np.where(hit, t, np.inf).min(axis=1)
    return out


def _edge_use(f: np.ndarray, nv: int) -> np.ndarray:
    """(M,3) number of faces using each face edge (edge i joins corner i and i+1)."""
    e = np.sort(np.stack([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]], axis=1), axis=2).reshape(-1, 2)
    key = e[:, 0].astype(np.int64) * nv + e[:, 1]
    _, inv, cnt = np.unique(key, return_inverse=True, return_counts=True)
    return cnt[inv.reshape(-1)].reshape(-1, 3)


def zipper(verts: np.ndarray, f: np.ndarray, f_curved: np.ndarray, vc: dict, passes: int = 6):
    """Repair T-junctions: split open edges at open vertices that lie on them.

    LDraw allows an edge of one face to run past vertices of its neighbours, and hi-res
    (48-segment) primitives meet hand-written 16-segment neighbours along chords. Both leave
    the mesh open. A vertex counts as "on" an open edge within 3 microns, or within 6 % of
    the edge length when edge ends and vertex all sit on the same primitive circle (arc
    versus chord). Returns (f, f_curved, number of splits).
    """
    n_split = 0
    for _ in range(passes):
        cnt = _edge_use(f, len(verts))
        bf, be = np.nonzero(cnt == 1)
        if not len(bf):
            break
        bverts = np.unique(np.concatenate([f[bf, be], f[bf, (be + 1) % 3]]))
        tree = cKDTree(verts[bverts])
        split: dict[int, tuple[int, list[int]]] = {}
        for fi, ei in zip(bf.tolist(), be.tolist()):
            if fi in split:
                continue
            a, b = int(f[fi, ei]), int(f[fi, (ei + 1) % 3])
            pa, pb = verts[a], verts[b]
            ab = pb - pa
            ln = float(np.linalg.norm(ab))
            if ln < 1e-5:
                continue
            arc = vc.get(a, set()) & vc.get(b, set())
            hits = []
            for ci in tree.query_ball_point((pa + pb) / 2, ln / 2 + 0.06 * ln + 3e-3):
                v = int(bverts[ci])
                if v == a or v == b:
                    continue
                t = float((verts[v] - pa) @ ab) / (ln * ln)
                if t * ln < 2e-3 or (1 - t) * ln < 2e-3:
                    continue
                tol = 0.06 * ln if (arc and arc & vc.get(v, set())) else 3e-3
                if np.linalg.norm(verts[v] - (pa + t * ab)) <= tol:
                    hits.append((t, v))
            if hits:
                split[fi] = (ei, [v for _, v in sorted(hits)])
        if not split:
            break
        keep = np.ones(len(f), dtype=bool)
        new_f, new_c = [], []
        for fi, (ei, vs) in split.items():
            a, b, c = int(f[fi, ei]), int(f[fi, (ei + 1) % 3]), int(f[fi, (ei + 2) % 3])
            chain = [a] + vs + [b]
            for k in range(len(chain) - 1):
                new_f.append((chain[k], chain[k + 1], c))
                new_c.append(bool(f_curved[fi]))
            keep[fi] = False
            n_split += len(vs)
        f = np.vstack([f[keep], np.array(new_f, dtype=f.dtype)])
        f_curved = np.concatenate([f_curved[keep], np.array(new_c, dtype=bool)])
    return f, f_curved, n_split


def drop_flat_open(verts: np.ndarray, f: np.ndarray, f_curved: np.ndarray):
    """Remove connected components that are open, perfectly flat sheets (they enclose nothing)."""
    if not len(f):
        return f, f_curved, 0
    nv = len(verts)
    g = coo_matrix((np.ones(len(f) * 2), (np.concatenate([f[:, 0], f[:, 1]]),
                                          np.concatenate([f[:, 1], f[:, 2]]))), shape=(nv, nv))
    nc, vlab = connected_components(g, directed=False)
    flab = vlab[f[:, 0]]
    is_open = np.zeros(nc, dtype=bool)
    np.logical_or.at(is_open, flab, (_edge_use(f, nv) == 1).any(axis=1))
    n = np.cross(verts[f[:, 1]] - verts[f[:, 0]], verts[f[:, 2]] - verts[f[:, 0]])
    ln = np.linalg.norm(n, axis=1, keepdims=True)
    n = np.divide(n, ln, out=np.zeros_like(n), where=ln > 1e-12)
    first = np.full(nc, -1, dtype=np.int64)
    first[flab[::-1]] = np.arange(len(f))[::-1]
    dev = np.zeros(nc)
    np.maximum.at(dev, flab, 1.0 - np.abs(np.einsum("ij,ij->i", n, n[first[flab]])))
    drop = is_open & (dev < 1e-6)
    keep = ~drop[flab]
    return f[keep], f_curved[keep], int(drop.sum())


def _cap_safe_loops(verts: np.ndarray, f: np.ndarray, small: float = 4.0):
    """Cap open boundary loops, but only where a cap cannot fill a real opening.

    A loop is capped if it is small (bounding-box diagonal <= `small` mm: seams, slivers,
    joints of removed features) or if it is flat and an existing face of the body already
    lies across it (a rib or cylinder end drawn standing on an uncut face). Large loops over
    empty space are left open: they may be real openings, and the part then stays tier B.
    Returns (cap faces, loops capped, loops left open).
    """
    tris = verts[f]
    caps: list[tuple[int, int, int]] = []
    n_cap = n_open = 0
    for lp in repair.boundary_loops(f):
        p = verts[lp]
        c = p.mean(axis=0)
        safe = float(np.linalg.norm(np.ptp(p, axis=0))) <= small
        if not safe:
            nv = repair._newell(p)
            ln = float(np.linalg.norm(nv))
            if ln > 1e-12:
                nn = nv / ln
                if float(np.abs((p - c) @ nn).max()) < 0.05:
                    d = ray_dist(np.array([c - 0.02 * nn, c + 0.02 * nn]), np.array([nn, -nn]), tris)
                    safe = bool((d < 0.06).any())
        tri = repair.triangulate_loop(verts, lp) if safe else None
        if tri is None:
            n_open += 1
            continue
        caps += tri
        n_cap += 1
    return np.array(caps, dtype=f.dtype).reshape(-1, 3), n_cap, n_open


def _frame(axis: np.ndarray, origin: np.ndarray) -> np.ndarray:
    """3x4 transform taking local +Z to `axis` and the local origin to `origin`."""
    a = _unit(axis)
    t = np.array([1.0, 0.0, 0.0]) if abs(a[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = _unit(np.cross(t, a))
    v = np.cross(a, u)
    return np.column_stack([u, v, a, origin])


# ------------------------------------------------------------------------------------------
# circles
# ------------------------------------------------------------------------------------------
def find_circles(raw: RawMesh):
    """Tag triangle corners that lie on primitive circles.

    Returns (circles, corner_circle (M,3) int, tri_curved (M,) bool).
    """
    m = raw.n_tris
    corner_circle = np.full((m, 3), -1, dtype=np.int64)
    tri_curved = np.zeros(m, dtype=bool)
    circles: list[_Circle] = []
    index: dict = {}
    groups: dict = {}
    if not raw.instances:
        return circles, corner_circle, tri_curved
    order = np.argsort(raw.tri_inst, kind="stable")
    sorted_inst = raw.tri_inst[order]
    for ii, inst in enumerate(raw.instances):
        if inst.prim_id < 0 or inst.rel_matrix is None:
            continue
        info = prim_info(inst.prim)
        if info is None:
            continue
        curved, radii = info
        lo, hi = np.searchsorted(sorted_inst, [ii, ii + 1])
        sel = order[lo:hi]
        if not len(sel):
            continue
        if curved:
            tri_curved[sel] = True
        pm = inst.prim_matrix
        xcol, ycol, zcol = pm[:3, 0], pm[:3, 1], pm[:3, 2]
        sx, sz = np.linalg.norm(xcol), np.linalg.norm(zcol)
        if sx < 1e-9 or abs(sx - sz) > 1e-3 * sx or abs(xcol @ zcol) > 1e-3 * sx * sz:
            continue                                        # squashed: an ellipse, not a circle
        a = ycol if np.linalg.norm(ycol) > 1e-9 else np.cross(zcol, xcol)
        a = _canon(a)
        loc = raw.tri_local[sel].astype(np.float64).reshape(-1, 3)
        p = loc @ inst.rel_matrix[:3, :3].T + inst.rel_matrix[:3, 3]
        rho = np.hypot(p[:, 0], p[:, 2])
        yv = np.round(p[:, 1], 3)
        flat_idx = (sel[:, None] * 3 + np.arange(3)[None]).reshape(-1)
        for r in radii:
            on = np.abs(rho - r) < 0.02
            if not on.any():
                continue
            for y in np.unique(yv[on]):
                c = pm[:3, 1] * y + pm[:3, 3]
                rad = r * sx
                key = (tuple(np.round(c / 2e-3).astype(np.int64)),
                       tuple(np.round(a, 3)), int(round(rad / 2e-3)))
                cid = index.get(key)
                if cid is None:
                    cid = len(circles)
                    index[key] = cid
                    foot = c - (c @ a) * a
                    gkey = (tuple(np.round(a, 3)), tuple(np.round(foot / 5e-3).astype(np.int64)))
                    gid = groups.setdefault(gkey, len(groups))
                    circles.append(_Circle(center=c, axis=a, radius=rad, group=gid, t=float(c @ a)))
                ci = circles[cid]
                msk = on & (yv == y)
                corner_circle.reshape(-1)[flat_idx[msk]] = cid
                if curved:
                    ci.votes += (-1 if inst.inverted else 1) * int(msk.sum())
                else:
                    ci.flat = True
                ci.hires = ci.hires and inst.hires
                ci.partial = ci.partial or not inst.prim.startswith("4-4")
                ci.sem_ids.add(inst.sem_id)
                ci.prim_ids.add(inst.prim_id)
    return circles, corner_circle, tri_curved


def circle_rule(c: _Circle, sems: set[str], prof: Profile) -> tuple[str, float]:
    """(feature kind, radial change in mm) for a circle that stays in the body."""
    r = c.radius
    rl = r / LDU
    outer, inner = c.votes > 0, c.votes < 0

    def near(x):
        return abs(rl - x) < 0.06

    def has(*prefixes):
        return any(s.startswith(prefixes) for s in sems)

    kind, target = "", None
    if inner:
        if has("connhol", "peghol", "npeghol", "beamhol") and near(6):
            kind, target = "pin_hole", prof.pin_hole_d / 2
        elif has("axlehol", "axleho", "axl2hol", "axl3hol"):
            kind, target = "axle_hole", r + prof.axle_hole_grow
        elif near(6):
            kind, target = "socket", prof.socket_d / 2
        elif near(4):
            if has("clip") or c.partial:        # an arc, not a full bore: a C-clip
                kind, target = "clip", prof.clip_d / 2
            else:
                kind, target = "bar_hole", prof.bar_hole_d / 2
        else:
            kind, target = "hole", r + prof.hole_grow
        if not c.hires:                         # 16-gon hole: make the flats, not the corners, fit
            target = target / math.cos(math.pi / 16)
    elif outer:
        if has("connect", "confric") and near(6):
            kind, target = "pin", prof.pin_d / 2
        elif has("axle") and not has("axlehol", "axleho"):
            kind, target = "axle", r - prof.axle_shrink
        elif near(6):
            if abs(c.axis[2]) > 0.99 or has("stud"):
                kind, target = "stud_like", prof.stud_d / 2
            else:
                kind, target = "pin_like", prof.pin_d / 2
        elif near(8) and has("stud"):
            kind, target = "tube_like", prof.tube_od / 2
        elif near(4):
            kind, target = ("post_like", prof.post_d / 2) if has("stud") else ("bar", prof.bar_d / 2)
        elif rl >= 9:
            kind, target = "round_exterior", r - prof.ext_clearance
        else:
            kind, target = "boss", r - prof.boss_shrink
    if target is None:
        return "", 0.0
    return kind, float(target - r)


# ------------------------------------------------------------------------------------------
# main build
# ------------------------------------------------------------------------------------------
@dataclass
class BuildOutput:
    tris: np.ndarray
    tier: str
    features: dict[str, int]
    warnings: list[str]
    needs_support: bool
    volume: float


def build_mesh(raw: RawMesh, prof: Profile) -> BuildOutput:
    warns: list[str] = []
    feats: dict[str, int] = {}

    def count(k, n=1):
        feats[k] = feats.get(k, 0) + n

    if raw.n_tris == 0:
        raise ValueError("part has no geometry")
    if raw.meta.get("missing"):
        warns.append("missing LDraw files: " + ", ".join(raw.meta["missing"][:5]))
    if not raw.meta.get("bfc_certified", True):
        warns.append("not BFC certified: face directions may be unreliable")

    verts, vidx = weld(raw.tris)
    circles, corner_circle, tri_curved = find_circles(raw)
    m = raw.n_tris
    removed = np.zeros(m, dtype=bool)
    z_bot = float(verts[:, 2].min())
    sem_of_inst = np.array([i.sem_id for i in raw.instances] or [-1])
    prim_of_inst = np.array([i.prim_id for i in raw.instances] or [-1])
    tri_sem = np.where(raw.tri_inst >= 0, sem_of_inst[np.maximum(raw.tri_inst, 0)], -1)
    tri_prim = np.where(raw.tri_inst >= 0, prim_of_inst[np.maximum(raw.tri_inst, 0)], -1)

    # ---- 3. re-synthesise studs, tubes, posts ------------------------------------------------
    solids: list[Manifold] = []
    pockets: list[tuple[float, float]] = []            # XY of stud sockets that open at the bottom
    synth_gids: set[int] = set()                       # coaxial groups of every synthesised feature
    synth_groups: dict[int, tuple[str, set]] = {}      # coaxial group -> (kind, radii LDU)
    by_sem: dict[int, list[_Circle]] = {}
    for c in circles:
        for s in c.sem_ids:
            if s >= 0:
                by_sem.setdefault(s, []).append(c)
    sem_prims: dict[int, set] = {}
    for inst in raw.instances:
        if inst.sem_id >= 0 and inst.prim_id >= 0:
            sem_prims.setdefault(inst.sem_id, set()).add(inst.prim)
    for sid, cs in by_sem.items():
        name = raw.sem_names[sid]
        if not name.startswith("stud"):
            continue
        if any(not p.startswith("4-4") for p in sem_prims.get(sid, ())):
            continue                                   # cut / partial stud variant: leave to the body
        if len({c.group for c in cs}) != 1:
            continue
        outer = {round(c.radius / LDU, 1) for c in cs if c.votes > 0}
        inner = {round(c.radius / LDU, 1) for c in cs if c.votes < 0}
        if outer == {6.0} and inner <= {4.0}:
            kind = "stud"
        elif outer == {8.0} and inner == {6.0}:
            kind = "tube"
        elif outer == {4.0} and not inner:
            kind = "post"
        else:
            continue
        a = cs[0].axis
        ts = [c.t for c in cs]
        t0, t1 = min(ts), max(ts)
        if t1 - t0 < 0.2:
            continue
        flat_ts = {round(c.t, 3) for c in cs if c.flat}
        lo_flat, hi_flat = round(t0, 3) in flat_ts, round(t1, 3) in flat_ts
        vertical = abs(a[2]) > 0.99
        if lo_flat != hi_flat:
            cap_hi = hi_flat                           # the capped end is the tip / free end
        elif vertical:
            up_is_hi = a[2] > 0
            cap_hi = up_is_hi if kind == "stud" else not up_is_hi
        else:
            continue
        foot = cs[0].center - cs[0].t * a
        tip = foot + (t1 if cap_hi else t0) * a
        base = foot + (t0 if cap_hi else t1) * a
        length = t1 - t0
        if kind == "stud":
            hollow = bool(inner)
            sol = csg.stud(0, 0, 0, prof.stud_d, prof.stud_h, prof.stud_chamfer, prof.layer_height)
            if hollow:
                sol = sol - csg.cyl(0, 0, 0.0, prof.stud_h + 1.0, prof.bar_hole_d)
            sol = sol.transform(_frame(tip - base, base))
            count("hollow_studs" if hollow else "studs")
            if not vertical:
                count("side_studs")
        else:
            embed = 0.3 + prof.roof_raise
            if kind == "tube":
                sol = csg.ring(0, 0, 0, length + embed, prof.tube_od, prof.socket_d)
                count("tubes")
            else:
                sol = csg.cyl(0, 0, 0, length + embed, prof.post_d)
                count("posts")
            sol = sol.transform(_frame(base - tip, tip))
            synth_groups[cs[0].group] = (kind, outer | inner)
            if kind == "tube" and vertical and tip[2] <= z_bot + 0.05:
                pockets.append((float(tip[0]), float(tip[1])))
        synth_gids.add(cs[0].group)
        solids.append(sol)
        removed |= tri_sem == sid
    # generic cylinders that merely extend a synthesised tube/post (older part files)
    if synth_groups:
        prim_circles: dict[int, list[_Circle]] = {}
        for c in circles:
            for p in c.prim_ids:
                prim_circles.setdefault(p, []).append(c)
        generic_prims = set(np.unique(tri_prim[(tri_sem < 0) & (tri_prim >= 0)]).tolist())
        for p in generic_prims:
            cs = prim_circles.get(p, [])
            if not cs or len({c.group for c in cs}) != 1 or cs[0].group not in synth_groups:
                continue
            _, radii = synth_groups[cs[0].group]
            info = prim_info(raw.prim_names[p])
            if info and info[0] and {round(c.radius / LDU, 1) for c in cs} <= radii:
                removed |= tri_prim == p
                warns.append("tube/post extension cylinder dropped (legacy part file): check tube length")

    # ---- body mesh -----------------------------------------------------------------------------
    vc_all: dict[int, set] = {}                        # welded vertex -> circle ids
    flat_cc = corner_circle.reshape(-1)
    tagged = np.nonzero(flat_cc >= 0)[0]
    for v, cidv in zip(vidx.reshape(-1)[tagged].tolist(), flat_cc[tagged].tolist()):
        vc_all.setdefault(v, set()).add(cidv)
    all_tris = raw.tris                                # ray obstacles incl. removed features

    def dedupe(f, f_curved):
        good = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
        f, f_curved = f[good], f_curved[good]
        if len(f):
            _, first = np.unique(np.sort(f, axis=1), axis=0, return_index=True)
            first.sort()
            f, f_curved = f[first], f_curved[first]
        return f, f_curved

    keep = ~removed
    f, f_curved = dedupe(vidx[keep], tri_curved[keep])
    if len(f):
        f, f_curved, n_split = zipper(verts, f, f_curved, vc_all)
        if n_split:
            count("t_junctions_fixed", n_split)
            f, f_curved = dedupe(f, f_curved)
        f, f_curved, n_drop = drop_flat_open(verts, f, f_curved)
        # close holes left where a synthesised feature was cut away from the body
        synth_circ = {i for i, c in enumerate(circles) if c.group in synth_gids}
        if synth_circ and len(f):
            bf_, be_ = np.nonzero(_edge_use(f, len(verts)) == 1)
            centers: dict[int, int] = {}
            extra, caps = [], []
            for fi, ei in zip(bf_.tolist(), be_.tolist()):
                a, b = int(f[fi, ei]), int(f[fi, (ei + 1) % 3])
                common = vc_all.get(a, set()) & vc_all.get(b, set()) & synth_circ
                if not common:
                    continue
                cid = min(common)
                if cid not in centers:
                    centers[cid] = len(verts) + len(extra)
                    extra.append(circles[cid].center)
                caps.append((b, a, centers[cid]))
            if caps:
                verts = np.vstack([verts, np.array(extra)])
                f = np.vstack([f, np.array(caps, dtype=f.dtype)])
                f_curved = np.concatenate([f_curved, np.zeros(len(caps), dtype=bool)])
                count("capped_loops", len(centers))
    # close remaining open loops (features drawn on uncut faces, missing caps, joints of
    # removed features). Caps carry no displacement constraints and are no ray obstacles,
    # so the dimensions of everything LDraw drew are exactly as without them.
    f_cap = np.zeros(len(f), dtype=bool)
    if len(f) and (_edge_use(f, len(verts)) == 1).any():
        capf, n_cl, n_left_open = _cap_safe_loops(verts, f)
        if len(capf):
            f = np.vstack([f, capf])
            f_curved = np.concatenate([f_curved, np.ones(len(capf), dtype=bool)])
            f_cap = np.concatenate([f_cap, np.ones(len(capf), dtype=bool)])
            count("repaired_loops", n_cl)
            warns.append(f"{n_cl} small or face-backed open loop(s) in the LDraw mesh were closed automatically")
    new_verts = verts.copy()
    if len(f):
        p0, p1, p2 = verts[f[:, 0]], verts[f[:, 1]], verts[f[:, 2]]
        nrm = np.cross(p1 - p0, p2 - p0)
        area = 0.5 * np.linalg.norm(nrm, axis=1)
        okf = area > 1e-9
        f, f_curved, f_cap, nrm, area = f[okf], f_curved[okf], f_cap[okf], nrm[okf], area[okf]
        nrm = nrm / (2 * area[:, None])
        cen = verts[f].mean(axis=1)
        body_tris = verts[f[~f_cap]]

        # ---- 4a. planar patches and their classification --------------------------------------
        planar = ~f_curved
        pid = np.full(len(f), -1, dtype=np.int64)
        delta = np.zeros(len(f))                      # how far each face moves AGAINST its normal
        if planar.any():
            pf = np.nonzero(planar)[0]
            pkey = np.concatenate([np.round(nrm[pf], 3),
                                   np.round(np.einsum("ij,ij->i", nrm[pf], cen[pf]) / 2e-3)[:, None]],
                                  axis=1)
            _, plane_id = np.unique(pkey, axis=0, return_inverse=True)
            plane_id = plane_id.reshape(-1)
            e = np.sort(np.stack([f[pf][:, [0, 1]], f[pf][:, [1, 2]], f[pf][:, [2, 0]]], axis=1),
                        axis=2).reshape(-1, 2)
            ef = np.repeat(np.arange(len(pf)), 3)
            ek = e[:, 0] * (len(verts) + 1) + e[:, 1]
            o = np.argsort(ek, kind="stable")
            same = (ek[o][1:] == ek[o][:-1]) & (plane_id[ef[o][1:]] == plane_id[ef[o][:-1]])
            a_, b_ = ef[o][1:][same], ef[o][:-1][same]
            g = coo_matrix((np.ones(len(a_)), (a_, b_)), shape=(len(pf), len(pf)))
            npatch, lab = connected_components(g, directed=False)
            pid[pf] = lab
            p_area = np.bincount(lab, weights=area[pf], minlength=npatch)
            vz = verts[f[pf]][:, :, 2]
            p_zmin = np.full(npatch, np.inf)
            p_zmax = np.full(npatch, -np.inf)
            np.minimum.at(p_zmin, lab, vz.min(axis=1))
            np.maximum.at(p_zmax, lab, vz.max(axis=1))
            # representative face per patch = its largest
            rep = np.full(npatch, -1, dtype=np.int64)
            big = np.argsort(area[pf])
            rep[lab[big]] = pf[big]
            p_n = nrm[rep]
            p_delta = np.zeros(npatch)
            lateral = (np.abs(p_n[:, 2]) < 0.7) & (p_area > 0.3)
            li = np.nonzero(lateral)[0]
            if len(li):
                # up to 3 sample rays per patch (largest faces)
                samp_face, samp_patch = [], []
                o2 = np.lexsort((-area[pf], lab))
                rank = np.arange(len(pf)) - np.searchsorted(lab[o2], lab[o2])
                chosen = o2[(rank < 3) & lateral[lab[o2]]]
                samp_face = pf[chosen]
                samp_patch = lab[chosen]
                d = ray_dist(cen[samp_face] + 1e-3 * nrm[samp_face], nrm[samp_face], body_tris)
                esc = np.bincount(samp_patch, weights=np.isinf(d).astype(float), minlength=npatch)
                tot = np.bincount(samp_patch, minlength=npatch)
                exterior = lateral & (esc >= 0.5 * np.maximum(tot, 1))
                enclosed = lateral & ~exterior
                h = np.sqrt(np.maximum(1 - p_n[:, 2] ** 2, 0))
                p_delta[exterior] = prof.ext_clearance * h[exterior]
                wall = enclosed & (p_zmin <= z_bot + 0.05) & (p_zmax - p_zmin >= 1.0)
                p_delta[wall] = prof.cavity_wall_offset
                count("exterior_faces", int(exterior.sum()))
                count("cavity_walls", int(wall.sum()))
            # shallow cavity ceilings
            ceil = (p_n[:, 2] < -0.99) & (p_zmin > z_bot + 1.0) & (p_zmin < z_bot + PLATE_CAVITY + prof.roof_raise - 0.02) & (p_area > 0.5)
            ci = np.nonzero(ceil)[0]
            if len(ci) and prof.roof_raise > 0:
                c0 = cen[rep[ci]]
                dirs4 = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]], dtype=float)
                o4 = np.repeat(c0 - np.array([0, 0, 0.3]), 4, axis=0)
                d4 = ray_dist(o4, np.tile(dirs4, (len(ci), 1)), all_tris).reshape(-1, 4)
                boxed = (d4 < 40.0).sum(axis=1) >= 3
                up = ray_dist(c0 + np.array([0, 0, 1e-3]), np.tile([0.0, 0.0, 1.0], (len(ci), 1)), body_tris)
                depth = p_zmin[ci] - z_bot
                want = (PLATE_CAVITY + prof.roof_raise) - depth
                room = np.where(np.isinf(up), 0.0, up - MIN_ROOF)
                raise_by = np.where(boxed, np.clip(np.minimum(want, room), 0.0, None), 0.0)
                p_delta[ci] = raise_by
                count("raised_ceilings", int((raise_by > 0.01).sum()))
                if (boxed & (raise_by < want - 0.01)).any():
                    warns.append("cavity roof too thin to deepen fully: genuine studs may not seat completely")
            delta[pf] = p_delta[lab]

        # ---- 4b. per-vertex displacement solve --------------------------------------------------
        sem_names_of = lambda c: {raw.sem_names[s] for s in c.sem_ids if s >= 0}
        circ_dr = np.zeros(len(circles))
        for i, c in enumerate(circles):
            kind, dr = circle_rule(c, sem_names_of(c), prof)
            circ_dr[i] = dr
        n_conflict = 0
        cons: dict[int, list] = {}
        sock_xy: set[tuple[float, float]] = set()
        active: set[int] = set()
        used_kinds: dict[str, set] = {}
        for fi in np.nonzero(planar)[0]:
            n_ = nrm[fi]
            b_ = -delta[fi]
            for v in f[fi]:
                cons.setdefault(int(v), []).append((n_, b_))
                if b_ != 0.0:
                    active.add(int(v))
        vc = {v: vc_all[v] for v in np.unique(f).tolist() if v in vc_all}
        for v, cids in vc.items():
            for cidv in cids:
                c = circles[cidv]
                rel = verts[v] - c.center
                rad = rel - (rel @ c.axis) * c.axis
                ln = np.linalg.norm(rad)
                if ln < 1e-6:
                    continue
                cons.setdefault(v, []).append((rad / ln, circ_dr[cidv]))
                k, _ = circle_rule(c, sem_names_of(c), prof)
                if k == "socket" and abs(c.axis[2]) > 0.99 and verts[v][2] <= z_bot + 0.05:
                    sock_xy.add((round(float(c.center[0]), 2), round(float(c.center[1]), 2)))
                if circ_dr[cidv] != 0.0:
                    active.add(v)
                    used_kinds.setdefault(k, set()).add(c.group)
        for k, gs in used_kinds.items():
            count(k + "s", len(gs))
        for v in active:
            rows = cons[v]
            # merge duplicate constraints (same direction and value)
            uniq = {}
            for n_, b_ in rows:
                uniq[(tuple(np.round(n_, 3)), round(float(b_), 5))] = (n_, b_)
            a_mat = np.array([u[0] for u in uniq.values()])
            b_vec = np.array([u[1] for u in uniq.values()])
            # damped least squares: nearly parallel constraints that disagree must not fling
            # the vertex away (plain lstsq amplifies them by 1/sin(angle))
            atb = a_mat.T @ b_vec
            d_ = np.linalg.solve(a_mat.T @ a_mat + 1e-4 * np.eye(3), atb)
            if np.linalg.norm(d_) > MAX_SHIFT:
                d_ = np.linalg.solve(a_mat.T @ a_mat + 0.05 * np.eye(3), atb)
                ln_ = float(np.linalg.norm(d_))
                if ln_ > MAX_SHIFT:
                    d_ = d_ * (MAX_SHIFT / ln_)
                n_conflict += 1
            new_verts[v] = verts[v] + d_
        if n_conflict:
            warns.append(f"{n_conflict} vertices had conflicting offset constraints and were moved "
                         "conservatively: look at that part in the slicer preview")

    # ---- 5. assemble ----------------------------------------------------------------------------
    good_parts: list[Manifold] = list(solids)
    soup: list[np.ndarray] = []
    tier = "A"
    if len(f):
        used = np.unique(f)
        remap = np.full(len(new_verts), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        bv, bf = new_verts[used], remap[f]
        body = csg.from_mesh(bv, bf)
        if body.status().name == "NoError" and not body.is_empty() and body.volume() > 0:
            good_parts.append(body)
        else:
            g = coo_matrix((np.ones(len(bf) * 2), (np.concatenate([bf[:, 0], bf[:, 1]]),
                                                    np.concatenate([bf[:, 1], bf[:, 2]]))),
                           shape=(len(bv), len(bv)))
            nc, vlab = connected_components(g, directed=False)
            flab = vlab[bf[:, 0]]
            n_bad = 0
            for k in range(nc):
                fk = bf[flab == k]
                uk = np.unique(fk)
                rm = np.full(len(bv), -1, dtype=np.int64)
                rm[uk] = np.arange(len(uk))
                piece = csg.from_mesh(bv[uk], rm[fk])
                if piece.status().name == "NoError" and not piece.is_empty() and piece.volume() > 0:
                    good_parts.append(piece)
                else:
                    soup.append(bv[fk])
                    n_bad += 1
            if n_bad:
                tier = "B"
                use = _edge_use(bf, len(bv))
                warns.append(f"{n_bad} of {nc} body shells are not closed solids ({int((use == 1).sum())} open "
                             f"edges, {int((use > 2).sum())} over-shared): exported as overlapping shells for "
                             "the slicer to union (inspect the sliced preview)")
    solid = csg.union(good_parts) if good_parts else Manifold()
    # make every bottom-opening stud socket deep enough for a genuine stud
    if len(f):
        pockets += [xy for xy in sock_xy if all(abs(xy[0] - p[0]) + abs(xy[1] - p[1]) > 0.1 for p in pockets)]
    if pockets and prof.roof_raise > 0:
        if tier == "A" and not solid.is_empty():
            depth = PLATE_CAVITY + prof.roof_raise
            st = csg.to_tris(solid)
            o = np.array([[x, y, z_bot + depth + 1e-3] for x, y in pockets])
            up = ray_dist(o, np.tile([0.0, 0.0, 1.0], (len(o), 1)), st)
            cuts = []
            for (x, y), t in zip(pockets, up):
                dpt = depth if t >= MIN_ROOF else depth - (MIN_ROOF - t)
                if dpt > PLATE_CAVITY + 0.01:
                    cuts.append(csg.cyl(x, y, z_bot - 1.0, z_bot + dpt, prof.socket_d))
            if cuts:
                solid = solid - csg.union(cuts)
                count("deepened_sockets", len(cuts))
        else:
            warns.append("stud sockets could not be deepened (body is not a closed solid)")
    tris_out = [csg.to_tris(solid)] if not solid.is_empty() else []
    tris_out += soup
    if not tris_out:
        raise ValueError("nothing left to export")
    tris = np.concatenate(tris_out)
    volume = float(solid.volume()) if not solid.is_empty() else 0.0
    if soup:
        st = np.concatenate(soup)
        volume += float(np.einsum("ij,ij->i", st[:, 0], np.cross(st[:, 1], st[:, 2])).sum() / 6.0)

    # ---- 6. print frame --------------------------------------------------------------------------
    lo, hi = tris.reshape(-1, 3).min(axis=0), tris.reshape(-1, 3).max(axis=0)
    tris = tris - np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
    if prof.xy_scale != 1.0 or prof.z_scale != 1.0:
        tris = tris * np.array([prof.xy_scale, prof.xy_scale, prof.z_scale])
    needs_support = _needs_support(tris)
    if feats.get("side_studs"):
        warns.append("has side studs: they print lying down, expect a rougher / tighter fit on them")
    return BuildOutput(tris, tier, feats, warns, needs_support, volume)


def _needs_support(tris: np.ndarray, max_bridge: float = 14.0) -> bool:
    """True if some downward face is neither on the bed nor bridgeable between two walls."""
    n = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    area = 0.5 * np.linalg.norm(n, axis=1)
    ok = area > 1e-9
    nz = np.zeros(len(tris))
    nz[ok] = n[ok, 2] / (2 * area[ok])
    cen = tris.mean(axis=1)
    edge = np.stack([tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 1], tris[:, 0] - tris[:, 2]], axis=1)
    longest = np.linalg.norm(edge, axis=2).max(axis=1)
    width = np.divide(2 * area, longest, out=np.zeros_like(area), where=longest > 1e-9)
    # ledges narrower than ~1 mm (tile grooves, chamfers) print fine unsupported
    cand = np.nonzero((nz < -0.6) & (cen[:, 2] > 0.3) & (area > 0.4) & (width > 1.0))[0]
    if not len(cand):
        return False
    cand = cand[np.argsort(-area[cand])[:(24 if len(tris) > 40000 else 80)]]
    # a face is bridgeable if, in ANY of 8 directions, something supports it on both sides
    # (big plates have clear channels between tubes along X and Y but not diagonally)
    ang = np.arange(8) * (np.pi / 8)
    half = np.stack([np.cos(ang), np.sin(ang), np.zeros(8)], axis=1)
    dirs = np.concatenate([half, -half])
    o = np.repeat(cen[cand] - np.array([0, 0, 0.08]), 16, axis=0)
    d = ray_dist(o, np.tile(dirs, (len(cand), 1)), tris).reshape(-1, 16)
    bridged = ((d[:, :8] < max_bridge) & (d[:, 8:] < max_bridge)).any(axis=1)
    return bool((~bridged).any())


def build_part(part_id: str, profile: Profile, out_dir: str | Path, *,
               lib: LDrawLibrary | None = None, force: bool = False) -> PartResult:
    """Generate (or reuse) the calibrated STL for one LDraw part id."""
    from ..ldraw import normalize_part_id

    pid = normalize_part_id(part_id)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stl, side = out / f"{pid}.stl", out / f"{pid}.json"
    stamp = {"engine": ENGINE_VERSION, "profile_key": profile.geometry_key()}
    if not force and stl.exists() and side.exists():
        try:
            d = json.loads(side.read_text())
            if all(d.get(k) == v for k, v in stamp.items()):
                r = d["result"]
                r["mesh_path"] = stl
                r["size"] = tuple(r["size"])
                return PartResult(**r)
        except Exception:
            pass
    lib = lib or LDrawLibrary()
    desc = ""
    try:
        raw = expand_part(lib, pid)
        desc = raw.meta.get("description", "")
        b = build_mesh(raw, profile)
    except Exception as e:  # noqa: BLE001 - one bad part must not stop a whole set
        return PartResult(part_id=pid, ok=False, warnings=[f"{type(e).__name__}: {e}"], description=desc)
    csg.write_stl(stl, b.tris)
    pts = b.tris.reshape(-1, 3)
    size = tuple(float(x) for x in (pts.max(axis=0) - pts.min(axis=0)).round(3))
    res = PartResult(part_id=pid, ok=True, mesh_path=stl, size=size, tier=b.tier,
                     needs_support=b.needs_support, volume_mm3=round(b.volume, 2),
                     features=b.features, warnings=b.warnings, description=desc)
    d = asdict(res)
    d["mesh_path"] = stl.name
    side.write_text(json.dumps({**stamp, "result": d}, indent=1))
    return res
