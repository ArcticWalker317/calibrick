"""Recursive expansion of an LDraw part into a RawMesh (triangles + provenance)."""
from __future__ import annotations

import numpy as np

from .library import PRIM_KINDS, LDrawLibrary, LDrawPartNotFound, header_meta, normalize_part_id
from .parser import is_fraction_primitive
from .types import PrimInstance, RawMesh

# LDraw (x, y, z) with -Y up  ->  part frame in mm with Z up (a proper rotation + 0.4 scale)
FRAME = np.array([[0.4, 0.0, 0.0, 0.0],
                  [0.0, 0.0, 0.4, 0.0],
                  [0.0, -0.4, 0.0, 0.0],
                  [0.0, 0.0, 0.0, 1.0]])
MAX_DEPTH = 40


def expand_part(lib: LDrawLibrary, part_id: str, *, hires: bool = True) -> RawMesh:
    pid = normalize_part_id(part_id)
    root = lib.resolve(pid + ".dat")
    if root is None or root[1] != "parts":
        raise LDrawPartNotFound(pid)

    out_tris: list[np.ndarray] = []
    out_inst: list[np.ndarray] = []
    out_local: list[np.ndarray] = []
    instances: list[PrimInstance] = []
    missing: list[str] = []
    uncertified: list[str] = []
    sem_names: list[str] = []
    sem_mats: list[np.ndarray] = []
    prim_names: list[str] = []
    prim_mats: list[np.ndarray] = []
    n_files = 0
    root_header: dict = {}

    def walk(key, kind, path, M, inv, neg, chain, sem, sem_id, sem_M, prim, prim_id, prim_M,
             rel, is_hires, depth):
        nonlocal n_files, root_header
        pf = lib.parsed(key, kind, path)
        n_files += 1
        if depth == 0:
            root_header = pf.header
        stem = pf.stem
        chain = chain + (stem,)
        in_p = kind in PRIM_KINDS
        if in_p:
            if is_fraction_primitive(stem):
                if prim_id < 0:
                    prim, prim_id, prim_M, rel = stem, len(prim_names), FRAME @ M, np.eye(4)
                    prim_names.append(stem)
                    prim_mats.append(prim_M)
            elif sem_id < 0 and not stem.startswith("stug"):
                sem, sem_id, sem_M = stem, len(sem_names), FRAME @ M
                sem_names.append(stem)
                sem_mats.append(sem_M)
        if len(pf.tris):
            if not pf.certified:
                uncertified.append(stem)
            t = pf.tris
            w = (t @ M[:3, :3].T + M[:3, 3]) @ FRAME[:3, :3].T
            tl = t
            if inv ^ neg:
                w = w[:, ::-1]
                tl = t[:, ::-1]
            if in_p:
                idx = len(instances)
                full = FRAME @ M
                instances.append(PrimInstance(
                    name=stem, hires=bool(is_hires or kind == "p48"), chain=chain, matrix=full,
                    sem=sem, sem_id=sem_id, sem_matrix=sem_M if sem_id >= 0 else full,
                    inverted=bool(inv), prim=prim, prim_id=prim_id,
                    prim_matrix=prim_M if prim_id >= 0 else None,
                    rel_matrix=rel if prim_id >= 0 else None))
                out_inst.append(np.full(len(w), idx, dtype=np.int32))
                out_local.append(np.ascontiguousarray(tl, dtype=np.float32))
            else:
                out_inst.append(np.full(len(w), -1, dtype=np.int32))
                out_local.append(np.zeros(w.shape, dtype=np.float32))
            out_tris.append(w)
        if depth >= MAX_DEPTH:
            return
        for r in pf.refs:
            hit = lib.resolve(r.name)
            if hit is None:
                missing.append(r.name)
                continue
            k2, kind2, path2 = hit
            h2 = is_hires
            if hires and kind2 == "p" and is_fraction_primitive(k2.rsplit("/", 1)[-1][:-4]):
                hv = lib.hires_variant(k2, kind2)
                if hv:
                    k2, kind2, path2 = hv
                    h2 = True
            walk(k2, kind2, path2, M @ r.matrix, inv ^ r.invert, neg ^ r.neg, chain,
                 sem, sem_id, sem_M, prim, prim_id, prim_M,
                 (rel @ r.matrix) if prim_id >= 0 else None, h2, depth + 1)

    walk(root[0], root[1], root[2], np.eye(4), False, False, (), "", -1, None, "", -1, None,
         None, False, 0)

    meta = header_meta(root_header)
    meta.update(bfc_certified=not uncertified, uncertified=sorted(set(uncertified)),
                missing=sorted(set(missing)), n_files=n_files)
    if out_tris:
        tris = np.concatenate(out_tris)
        tri_inst = np.concatenate(out_inst)
        tri_local = np.concatenate(out_local)
    else:
        tris = np.zeros((0, 3, 3))
        tri_inst = np.zeros((0,), dtype=np.int32)
        tri_local = np.zeros((0, 3, 3), dtype=np.float32)
    return RawMesh(
        part_id=pid, tris=tris, tri_inst=tri_inst, tri_local=tri_local, instances=instances,
        meta=meta, sem_names=sem_names,
        sem_matrix=np.array(sem_mats) if sem_mats else np.zeros((0, 4, 4)),
        prim_names=prim_names,
        prim_matrix=np.array(prim_mats) if prim_mats else np.zeros((0, 4, 4)))
