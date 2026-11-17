"""Topology repairs that turn nearly-closed LDraw bodies into closed shells.

All functions work on an indexed mesh (verts (N,3), faces (M,3) int) in original, not yet
displaced, coordinates and only ever look at edges that are NOT used by exactly two faces,
so a body that is already closed passes through untouched.

Classes handled
---------------
* open boundary loops: a feature drawn on top of an uncut face (a rib or a hi-res cylinder
  end standing on a flat face, its open end lying inside that face), a missing cap, or the
  joint where a removed stud / tube met a rib. Each loop is capped by triangulating it in
  its best-fit plane (ear clipping), oriented to close the shell it bounds.
"""
from __future__ import annotations

import numpy as np


def edge_counts(f: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """(directed half-edges (3M,2), use count of each half-edge's undirected edge (3M,))."""
    he = np.concatenate([f[:, [0, 1]], f[:, [1, 2]], f[:, [2, 0]]])
    key = np.sort(he, axis=1)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    return he, cnt[inv.reshape(-1)]


def boundary_loops(f: np.ndarray) -> list[list[int]]:
    """Closed loops of boundary half-edges (edges used by one face), split at pinch vertices.

    Each loop lists vertices in the direction the adjacent faces traverse them, so a cap
    must traverse it in reverse. Chains that do not close are dropped.
    """
    he, cnt = edge_counts(f)
    b = he[cnt == 1]
    if not len(b):
        return []
    out_edges: dict[int, list[int]] = {}
    for a, c in b.tolist():
        out_edges.setdefault(a, []).append(c)
    used: set[tuple[int, int]] = set()
    loops: list[list[int]] = []
    for a0, c0 in b.tolist():
        if (a0, c0) in used:
            continue
        path = [a0]
        a, c = a0, c0
        closed = False
        while True:
            used.add((a, c))
            if c == a0:
                closed = True
                break
            path.append(c)
            nxt = [x for x in out_edges.get(c, []) if (c, x) not in used]
            if not nxt:
                break
            a, c = c, nxt[0]
        if not closed:
            continue
        # split at repeated vertices (two loops touching in one vertex)
        stack, seen = [], {}
        for v in path:
            if v in seen:
                k = seen[v]
                sub = stack[k:]
                loops.append(sub)
                for w in sub[1:]:
                    seen.pop(w, None)
                del stack[k + 1:]
            else:
                seen[v] = len(stack)
                stack.append(v)
        loops.append(stack)
    return [lp for lp in loops if len(lp) >= 3]


def _newell(p: np.ndarray) -> np.ndarray:
    q = np.roll(p, -1, axis=0)
    return 0.5 * np.cross(p, q).sum(axis=0)


def _seg_cross(a, b, c, d) -> bool:
    """Proper intersection of 2D segments ab and cd (shared endpoints do not count)."""
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def _simple(q: np.ndarray) -> bool:
    n = len(q)
    if n > 200:
        return False
    for i in range(n):
        a, b = q[i], q[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _seg_cross(a, b, q[j], q[(j + 1) % n]):
                return False
    return True


def ear_clip(q: np.ndarray) -> list[tuple[int, int, int]] | None:
    """Triangulate a simple CCW 2D polygon. Returns index triples, or None on failure."""
    idx = list(range(len(q)))
    tris: list[tuple[int, int, int]] = []
    scale = float(np.ptp(q, axis=0).max()) or 1.0
    eps = 1e-12 * scale * scale

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    guard = 0
    while len(idx) > 3:
        guard += 1
        if guard > 4 * len(q) * len(q):
            return None
        n = len(idx)
        best = None
        for k in range(n):
            i0, i1, i2 = idx[k - 1], idx[k], idx[(k + 1) % n]
            a, b, c = q[i0], q[i1], q[i2]
            ar = cross(a, b, c)
            if ar < -eps:
                continue                                    # reflex
            inside = False
            if ar > eps:
                for j in idx:
                    if j in (i0, i1, i2):
                        continue
                    p = q[j]
                    if cross(a, b, p) >= -eps and cross(b, c, p) >= -eps and cross(c, a, p) >= -eps:
                        if not (np.allclose(p, a) or np.allclose(p, b) or np.allclose(p, c)):
                            inside = True
                            break
            if inside:
                continue
            # prefer the fattest ear (better triangles); collinear ears only as last resort
            score = ar / (np.dot(c - a, c - a) + 1e-30) if ar > eps else -1.0
            if best is None or score > best[0]:
                best = (score, k)
        if best is None:
            return None
        k = best[1]
        n = len(idx)
        tris.append((idx[k - 1], idx[k], idx[(k + 1) % n]))
        del idx[k]
    tris.append((idx[0], idx[1], idx[2]))
    return tris


def triangulate_loop(verts: np.ndarray, loop: list[int]) -> list[tuple[int, int, int]] | None:
    """Cap faces (vertex ids) closing `loop` (which is traversed by its neighbouring faces)."""
    rev = loop[::-1]
    p = verts[rev]
    nv = _newell(p)
    ln = np.linalg.norm(nv)
    if ln < 1e-12:                                        # zero-area sliver: fan is fine
        return [(rev[0], rev[k], rev[k + 1]) for k in range(1, len(rev) - 1)]
    n = nv / ln
    t = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, t)
    u /= np.linalg.norm(u)
    w = np.cross(n, u)
    q = np.stack([p @ u, p @ w], axis=1)                  # CCW about n by construction
    if not _simple(q):
        return None
    tri = ear_clip(q)
    if tri is None:
        return None
    return [(rev[a], rev[b], rev[c]) for a, b, c in tri]


def cap_loops(verts: np.ndarray, f: np.ndarray, *, max_dev: float = 2.5,
              max_size: float | None = None):
    """Cap every closed boundary loop. Returns (cap faces (K,3), number of loops capped,
    number of loops skipped).

    A loop is skipped if it is not simple in its best-fit plane, deviates from that plane by
    more than `max_dev` mm, or (if given) its bounding-box diagonal exceeds `max_size` mm.
    """
    caps: list[tuple[int, int, int]] = []
    n_cap = n_skip = 0
    for lp in boundary_loops(f):
        p = verts[lp]
        size = float(np.linalg.norm(np.ptp(p, axis=0)))
        if max_size is not None and size > max_size:
            n_skip += 1
            continue
        c = p.mean(axis=0)
        nv = _newell(p)
        if np.linalg.norm(nv) > 1e-12:
            nn = nv / np.linalg.norm(nv)
            if float(np.abs((p - c) @ nn).max()) > max_dev:
                n_skip += 1
                continue
        tri = triangulate_loop(verts, lp)
        if tri is None:
            n_skip += 1
            continue
        caps += tri
        n_cap += 1
    return np.array(caps, dtype=f.dtype).reshape(-1, 3), n_cap, n_skip
