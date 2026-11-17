"""Plot horizontal cross-sections of a solid: a quick, exact way to eyeball features.

    python3 -I tools/slice_plot.py part.stl out.png 0.5 1.0 3.0     # z heights in mm
"""
from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from manifold3d import Manifold

from calibrick import csg


def load(path: str | Path) -> Manifold:
    v, f = csg.weld(csg.read_stl(path), tol=1e-4)
    return csg.from_mesh(v, f)


def plot_slices(m: Manifold, zs: list[float], out_png: str | Path, title: str = "",
                width: float = 16.0) -> Path:
    lo, hi = csg.bbox(m)
    aspect = max((hi[1] - lo[1]) / max(hi[0] - lo[0], 1e-6), 0.08)
    fig, axes = plt.subplots(len(zs), 1, figsize=(width, max(2.0, width * aspect) * len(zs)),
                             squeeze=False)
    for ax, z in zip(axes[:, 0], zs):
        for poly in m.slice(z).to_polygons():
            p = np.asarray(poly)
            p = np.vstack([p, p[:1]])
            ax.plot(p[:, 0], p[:, 1], "k-", lw=0.7)
        ax.set_aspect("equal")
        ax.set_xlim(lo[0] - 1, hi[0] + 1)
        ax.set_ylim(lo[1] - 1, hi[1] + 1)
        ax.set_title(f"{title}  z = {z:.2f} mm", fontsize=9)
        ax.grid(True, lw=0.2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    plt.close(fig)
    return Path(out_png)


def soup_segments(tris: np.ndarray, z: float) -> np.ndarray:
    """(K, 2, 2) XY segments where triangles cross the plane z. Works on any triangle soup."""
    d = tris[:, :, 2] - z
    segs = []
    for i, j in ((0, 1), (1, 2), (2, 0)):
        pass
    cross = (d.min(axis=1) < 0) & (d.max(axis=1) > 0)
    t = tris[cross]
    d = d[cross]
    out = np.zeros((len(t), 2, 2))
    n = np.zeros(len(t), dtype=int)
    for i, j in ((0, 1), (1, 2), (2, 0)):
        hit = (d[:, i] * d[:, j]) < 0
        f = np.where(hit, d[:, i] / np.where(hit, d[:, i] - d[:, j], 1.0), 0.0)
        p = t[:, i, :2] + f[:, None] * (t[:, j, :2] - t[:, i, :2])
        idx = np.nonzero(hit & (n < 2))[0]
        out[idx, n[idx]] = p[idx]
        n[idx] += 1
    return out[n == 2]


def plot_soup(tris: np.ndarray, zs: list[float], out_png: str | Path, title: str = "",
              width: float = 16.0) -> Path:
    """Like plot_slices but for meshes that are not valid solids (tier B parts, ladders)."""
    from matplotlib.collections import LineCollection

    pts = tris.reshape(-1, 3)
    lo, hi = pts.min(axis=0), pts.max(axis=0)
    aspect = max((hi[1] - lo[1]) / max(hi[0] - lo[0], 1e-6), 0.08)
    fig, axes = plt.subplots(len(zs), 1, figsize=(width, max(2.0, width * aspect) * len(zs)),
                             squeeze=False)
    for ax, z in zip(axes[:, 0], zs):
        ax.add_collection(LineCollection(soup_segments(tris, z), colors="k", linewidths=0.7))
        ax.set_aspect("equal")
        ax.set_xlim(lo[0] - 1, hi[0] + 1)
        ax.set_ylim(lo[1] - 1, hi[1] + 1)
        ax.set_title(f"{title}  z = {z:.2f} mm", fontsize=9)
        ax.grid(True, lw=0.2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=110)
    plt.close(fig)
    return Path(out_png)


if __name__ == "__main__":
    src, out, *zs = sys.argv[1:]
    zs = [float(z) for z in zs] or [0.5]
    try:
        m = load(src)
        valid = m.status().name == "NoError" and not m.is_empty()
    except Exception:
        valid = False
    if valid:
        print("status", m.status(), "volume", round(m.volume(), 2), "genus", m.genus())
        plot_slices(m, zs, out, Path(src).name)
    else:
        print("not a single valid solid: plotting raw triangle crossings")
        plot_soup(csg.read_stl(src), zs, out, Path(src).name)
