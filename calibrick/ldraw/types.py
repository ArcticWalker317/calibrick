"""Data types returned by the LDraw expander (see docs/ARCHITECTURE.md, WP-E)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class PrimInstance:
    """One instance of a primitive file (a file under ``p/``) that emitted triangles.

    Fields required by the architecture doc
    ---------------------------------------
    name        leaf file stem, lowercase, no ``48/`` prefix (e.g. ``"4-4cyli"``; with
                hi-res primitives the leaf is usually a quarter piece such as ``"1-4cyli"``)
    hires       the leaf file came from ``p/48``
    chain       file stems from the part root down to the leaf
    matrix      (4,4) leaf-local LDraw coordinates -> part frame mm
    sem         outermost non-fraction, non-stud-group primitive in the chain
                (``"stud"``, ``"stud4"``, ``"connhole"``, ``"box5"`` ...), ``""`` if none
    sem_id      shared by all leaves of the same instance of ``sem`` (-1 if none)
    sem_matrix  (4,4) of that semantic primitive instance (== ``matrix`` if none)
    inverted    parity of ``BFC INVERTNEXT`` along the chain (True on a cylinder = hole)

    Extras
    ------
    prim        outermost *fraction* primitive in the chain (``"4-4cyli"`` when the leaf is
                the hi-res quarter ``"1-4cyli"``); ``""`` if the leaf is not a fraction primitive
    prim_id     shared by all leaves of the same instance of ``prim`` (-1 if none). Use this
                to treat the four hi-res quarters of one cylinder as one object.
    prim_matrix (4,4) of that fraction primitive instance -> part frame mm (== ``matrix`` if none)
    rel_matrix  (4,4) leaf-local -> ``prim``-local, pure LDraw units (identity if none).
                ``rel_matrix @ [x, y, z, 1]`` puts a ``tri_local`` corner into the frame of
                the whole fraction primitive (same axis: local Y, same radii).
    """

    name: str
    hires: bool
    chain: tuple[str, ...]
    matrix: np.ndarray
    sem: str
    sem_id: int
    sem_matrix: np.ndarray
    inverted: bool
    prim: str = ""
    prim_id: int = -1
    prim_matrix: np.ndarray | None = None
    rel_matrix: np.ndarray | None = None


def _empty(shape, dtype):
    return np.zeros(shape, dtype=dtype)


@dataclass
class RawMesh:
    """Triangle soup of one LDraw part plus provenance of every triangle.

    ``tris``       (M,3,3) float64, part frame mm (Z up), CCW seen from outside when the
                   source files are BFC certified.
    ``tri_inst``   (M,) int32 index into ``instances`` (innermost primitive-file instance
                   that emitted the triangle), -1 for triangles written in a part/subpart.
    ``tri_local``  (M,3,3) float32 the same corners in the leaf primitive's local LDraw
                   coordinates (zeros where ``tri_inst == -1``).
    ``instances``  list[PrimInstance]
    ``meta``       description, ldraw_org, category, keywords, bfc_certified, missing,
                   n_files (+ extras: ldraw_org_qualifier, license, moved_to, uncertified)

    Extras: vectorised copies of the per-instance data so the engine does not have to
    loop over dataclasses:
    ``inst_matrix (L,4,4)``, ``inst_sem_id (L,)``, ``inst_prim_id (L,)``,
    ``inst_inverted (L,)``, ``sem_names`` / ``sem_matrix (S,4,4)`` indexed by sem_id,
    ``prim_names`` / ``prim_matrix (P,4,4)`` indexed by prim_id.
    """

    part_id: str
    tris: np.ndarray
    tri_inst: np.ndarray
    tri_local: np.ndarray
    instances: list[PrimInstance]
    meta: dict
    inst_matrix: np.ndarray = field(default_factory=lambda: _empty((0, 4, 4), np.float64))
    inst_sem_id: np.ndarray = field(default_factory=lambda: _empty((0,), np.int32))
    inst_prim_id: np.ndarray = field(default_factory=lambda: _empty((0,), np.int32))
    inst_inverted: np.ndarray = field(default_factory=lambda: _empty((0,), bool))
    sem_names: list[str] = field(default_factory=list)
    sem_matrix: np.ndarray = field(default_factory=lambda: _empty((0, 4, 4), np.float64))
    prim_names: list[str] = field(default_factory=list)
    prim_matrix: np.ndarray = field(default_factory=lambda: _empty((0, 4, 4), np.float64))

    # ---- small conveniences ---------------------------------------------------------
    @property
    def n_tris(self) -> int:
        return int(self.tris.shape[0])

    def bbox(self) -> tuple[np.ndarray, np.ndarray]:
        """(min_xyz, max_xyz) in mm; zeros for an empty mesh."""
        if not len(self.tris):
            return np.zeros(3), np.zeros(3)
        pts = self.tris.reshape(-1, 3)
        return pts.min(axis=0), pts.max(axis=0)

    def areas(self) -> np.ndarray:
        """(M,) triangle areas in mm^2 (0 for degenerate triangles)."""
        t = self.tris
        if not len(t):
            return np.zeros(0)
        return 0.5 * np.linalg.norm(np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0]), axis=1)

    def signed_volume(self) -> float:
        """Divergence-theorem volume of the soup (exact only for closed, outward-wound shells)."""
        t = self.tris
        if not len(t):
            return 0.0
        return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)
