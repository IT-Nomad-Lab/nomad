#!/usr/bin/env python3
"""Box-built Blender script (rvoxel_lantern_wanderer.py style) → runematch-vox/1 voxel model.

The script is run against a stand-in `bpy`, so Blender is not needed. Every block(...) call is
recorded as an axis-aligned box with its material, then rasterized onto a voxel lattice
(later blocks paint over earlier ones, the way the author layered them).

Axes: Blender is Z up with the front at -Y. VoxelModel is Y up, and the front goes to +Z.

Usage:
  blend_boxes_to_vox.py rvoxel_lantern_wanderer.py out/wanderer3/b --height 1.4 --size 0.03
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest import mock

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).parent))
from voxelize import build_faces, render_views  # noqa: E402


def linear_to_srgb_hex(c) -> str:
    c = np.clip(np.asarray(c[:3], float), 0, 1)
    s = np.where(c <= 0.0031308, c * 12.92, 1.055 * c ** (1 / 2.4) - 0.055)
    return "#" + "".join(f"{int(round(v * 255)):02X}" for v in s)


def record_blocks(script: Path, skip_parents: set[str]):
    """Run the script with bpy mocked; return (boxes, materials)."""
    src = script.read_text()
    boxes, mats = [], {}

    def cap_mat(name, color, rough=0.82, metallic=0.0, emission=None, strength=0.0):
        mats[name] = {"hex": linear_to_srgb_hex(color), "emission": float(strength) if emission else 0.0}
        return name

    class Empty(dict):  # the script sets .parent/.location and e["target"]
        def __init__(self, name):
            super().__init__()
            self.name, self.parent, self.location = name, None, None

    def cap_empty(name):
        return Empty(name)

    def cap_block(name, x, y, z, sx, sy, sz, material, parent):
        root = parent
        names = set()
        while root is not None:
            names.add(root.name)
            root = getattr(root, "parent", None)
        if not names & skip_parents:
            boxes.append((name, np.array([x, y, z]), np.array([sx, sy, sz]), material))

    # Swap in the capture functions right after the script defines its own helpers.
    src = src.replace("\nclear_scene()\n",
                      "\nclear_scene()\nmat = __cap_mat\nempty = __cap_empty\nblock = __cap_block\n", 1)
    src = src.replace("bpy.ops.wm.save_as_mainfile", "(lambda **k: None)")
    g = {"__name__": "__rvoxel__", "__cap_mat": cap_mat, "__cap_empty": cap_empty, "__cap_block": cap_block}
    fake = {"bpy": mock.MagicMock(), "mathutils": mock.MagicMock()}
    with mock.patch.dict(sys.modules, fake):
        exec(compile(src, str(script), "exec"), g)
    return boxes, mats, float(g.get("VOX", 1.0))


def rasterize(boxes, vox_unit: float, height: float, size: float):
    # Blender (x, y, z) → game (x, z, -y)
    lo = np.array([np.inf] * 3)
    hi = -lo
    game = []
    for name, c, s, m in boxes:
        half = s * vox_unit / 2
        bmin, bmax = c - half, c + half
        gmin = np.array([bmin[0], bmin[2], -bmax[1]])
        gmax = np.array([bmax[0], bmax[2], -bmin[1]])
        game.append((name, gmin, gmax, m))
        lo, hi = np.minimum(lo, gmin), np.maximum(hi, gmax)
    scale = height / (hi[1] - lo[1])
    step = size / scale                       # lattice step in script units
    dims = np.ceil((hi - lo) / step - 1e-6).astype(int)
    grid = np.full(dims, None, dtype=object)
    for name, gmin, gmax, m in game:
        a = np.round((gmin - lo) / step).astype(int)
        b = np.maximum(np.round((gmax - lo) / step).astype(int), a + 1)  # thin parts stay ≥ 1 voxel
        grid[a[0]:b[0], a[1]:b[1], a[2]:b[2]] = m
    return grid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("script", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--height", type=float, default=1.4)
    ap.add_argument("--size", type=float, default=0.03)
    ap.add_argument("--skip", action="append", default=["DISPLAY_GROUND"])
    a = ap.parse_args()

    boxes, mats, vox_unit = record_blocks(a.script, set(a.skip))
    grid = rasterize(boxes, vox_unit, a.height, a.size)
    used = sorted({m for m in grid.ravel() if m is not None})
    palette = [mats[m] for m in used]
    index = np.full(grid.shape, -1, dtype=int)
    for i, m in enumerate(used):
        index[grid == m] = i

    dims = np.array(index.shape)
    offset = np.array([-(dims[0] // 2), 0, -(dims[2] // 2)])
    coords = np.argwhere(index >= 0)
    V, F, C, N, E = build_faces(index, offset, a.size, palette)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    doc = {"format": "runematch-vox/1", "source": a.script.name, "size": a.size, "dims": dims.tolist(),
           "palette": palette,
           "voxels": np.concatenate([coords + offset, index[tuple(coords.T)][:, None]], 1).tolist()}
    Path(f"{a.out}.vox.json").write_text(json.dumps(doc, separators=(",", ":")))
    mesh = trimesh.Trimesh(V, F, vertex_normals=N, process=False)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=(np.clip(C, 0, 1) * 255).astype(np.uint8))
    mesh.export(f"{a.out}.glb")
    render_views(V, F, C, N, E, Path(f"{a.out}.png"))
    render_views(V, F, C, N, E, Path(f"{a.out}_night.png"), night=True)
    print(json.dumps({"out": str(a.out), "boxes": len(boxes), "dims": dims.tolist(),
                      "voxels": int(len(coords)), "palette": {m: mats[m]["hex"] for m in used}}))


if __name__ == "__main__":
    main()
