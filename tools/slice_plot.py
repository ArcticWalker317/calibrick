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


if __name__ == "__main__":
    src, out, *zs = sys.argv[1:]
    m = load(src)
    print("status", m.status(), "volume", round(m.volume(), 2), "genus", m.genus())
    plot_slices(m, [float(z) for z in zs] or [0.5], out, Path(src).name)
