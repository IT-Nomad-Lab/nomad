#!/usr/bin/env python3
"""Finish a voxelized Wanderer (RuneMatch #1000) so it reads like the RVOXEL reference renders.

TRELLIS.2 gets the Wanderer's silhouette, scarf, staff and satchel, but at 0.05 m voxels the hood
opening is ~3 voxels wide and comes back grey-filled, and the robe comes back neutral grey. This
pass works on the .vox.json from voxelize.py:

  1. recolours neutral greys by region: above the scarf → the hood's grey-white ramp,
     below it → the robe's beige-tan ramp (luminance order kept, so the shading stays);
  2. carves the face into the front of the hood: a black void one voxel deep, two white square
     eyes and a small red mouth (wanderer.json: face_visibility, eyes);
  3. makes the warm, bright voxels around the lantern hand emissive orange.

  wanderer_finish.py in.vox.json out/stem --front -z [--lantern +x]

Writes <stem>.vox.json, <stem>.glb, <stem>.png and <stem>_night.png, like voxelize.py.
"""
from __future__ import annotations

import argparse
import colorsys
import json
from pathlib import Path

import numpy as np
import trimesh

from voxelize import build_faces, hex_to_rgb, render_views

HOOD = ["#5E4A3F", "#7D6457", "#9C8272", "#B9A08E"]  # wanderer.json: hood shadow → cream highlight
ROBE = ["#6E563F", "#94775A", "#B79876", "#D4BC9A"]
VOID, EYE, MOUTH, GLOW = "#000000", "#FFFFFF", "#BB2C2C", "#FA8811"

AXES = {"+x": (0, 1), "-x": (0, -1), "+z": (2, 1), "-z": (2, -1)}


def load(path: Path):
    d = json.loads(path.read_text())
    vox = np.array(d["voxels"])
    offset = vox[:, :3].min(0)
    dims = vox[:, :3].max(0) - offset + 1
    index = np.full(dims, -1, int)
    index[tuple((vox[:, :3] - offset).T)] = vox[:, 3]
    return d, index, offset


def hsv(hexes):
    return np.array([colorsys.rgb_to_hsv(*hex_to_rgb(h)) for h in hexes])


def add_colour(palette, hex_, emission=0.0):
    for i, p in enumerate(palette):
        if p["hex"].upper() == hex_.upper() and p["emission"] == emission:
            return i
    palette.append({"hex": hex_.upper(), "emission": emission})
    return len(palette) - 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--front", choices=AXES, required=True, help="the direction the Wanderer faces")
    ap.add_argument("--lantern", choices=["+x", "-x", "+z", "-z"], help="side the lantern hangs on")
    ap.add_argument("--face-w", type=float, default=0.5, help="void width as a fraction of the hood")
    ap.add_argument("--face-h", type=float, default=0.55, help="void height as a fraction of the hood")
    ap.add_argument("--robe-sat", type=float, default=0.28, help="max saturation recoloured as robe")
    ap.add_argument("--robe-min-v", type=float, default=0.22, help="min value recoloured as robe")
    ap.add_argument("--glow-min-v", type=float, default=0.8, help="min value of a lantern-glow voxel")
    ap.add_argument("--lantern-cols", type=int, default=0, help="outer columns on the lantern side left as they are")
    ap.add_argument("--point-rows", type=int, default=0,
                    help="rows the stepped hood point adds above the voxelized top (0 = no point)")
    ap.add_argument("--tail", type=int, default=0, help="length in rows of the scarf tail down the back")
    ap.add_argument("--boots", type=int, default=0, help="rows from the bottom left as they are")
    a = ap.parse_args()

    doc, index, offset = load(a.input)
    palette = [dict(p) for p in doc["palette"]]
    pal_hsv = hsv([p["hex"] for p in palette])
    occ = index >= 0
    H = index.shape[1]

    # Scarf band: the rows where red voxels sit. Everything above its middle is hood.
    red = [i for i, (h, s, v) in enumerate(pal_hsv) if (h < 0.05 or h > 0.92) and s > 0.45]
    ys = np.argwhere(np.isin(index, red))[:, 1]
    scarf_mid = int(np.median(ys)) if len(ys) else int(H * 0.6)
    scarf_top = int(np.percentile(ys, 90)) if len(ys) else int(H * 0.68)

    # 1. Neutral greys → hood or robe ramp, ranked by value.
    neutral = [i for i, (h, s, v) in enumerate(pal_hsv) if s < 0.28 and v > 0.22]
    hood_ids = [add_colour(palette, h) for h in HOOD]
    sub = index[:, scarf_mid:, :]
    order = sorted(neutral, key=lambda i: pal_hsv[i][2])
    for rank, i in enumerate(order):
        sub[sub == i] = hood_ids[min(len(HOOD) - 1, rank * len(HOOD) // max(1, len(order)))]
    # The robe: the reference is light beige all round, but TRELLIS often returns the back and the
    # under-layers near black. Every unsaturated colour between the boots and the scarf joins the
    # ramp; the staff and satchel (saturated browns), the scarf and the boots keep their colours.
    robe = [i for i, (h, s, v) in enumerate(pal_hsv) if s < a.robe_sat and v > a.robe_min_v]
    robe_ids = [add_colour(palette, h) for h in ROBE]
    region = [slice(None), slice(a.boots, scarf_mid), slice(None)]
    if a.lantern and a.lantern_cols:
        lax, lsign = AXES[a.lantern]  # keep the lantern (and its hand) out of the robe ramp
        n = index.shape[lax]
        region[lax] = slice(0, n - a.lantern_cols) if lsign > 0 else slice(a.lantern_cols, n)
    sub = index[tuple(region)]
    order = sorted(robe, key=lambda i: pal_hsv[i][2])
    for rank, i in enumerate(order):
        sub[sub == i] = robe_ids[min(len(ROBE) - 1, rank * len(ROBE) // max(1, len(order)))]

    # 2. Face. Hood = occupied columns above the scarf; void = a box centred on it, on the front.
    axis, sign = AXES[a.front]
    side = 2 - axis if axis in (0, 2) else 0  # the horizontal axis across the face
    hood = occ[:, scarf_top:, :]
    hy = np.argwhere(hood.any(axis=(0, 2)))[:, 0] + scarf_top
    # use the central body span, not the staff: the widest run of the hood rows' silhouette
    across = np.argwhere(occ[:, int(hy.mean()):hy.max() + 1, :].any(axis=(1, axis)))[:, 0]
    lo, hi = int(np.percentile(across, 15)), int(np.percentile(across, 85))
    w = max(3, round((hi - lo + 1) * a.face_w))
    c0 = (lo + hi) // 2 - w // 2
    y_lo, y_hi = scarf_top, hy.max()
    fh = max(3, round((y_hi - y_lo + 1) * a.face_h))
    f0 = y_lo + 1
    void, eye, mouth = (add_colour(palette, VOID), add_colour(palette, EYE), add_colour(palette, MOUTH))

    def cell(xa, y, d):
        c = [0, 0, 0]
        c[1], c[side], c[axis] = y, xa, d
        return tuple(c)

    # The hood front is lumpy, so carve a flat box: the front plane is the median front depth of
    # the face rectangle; everything from one voxel behind it outwards goes, and the layer behind
    # that becomes the black back wall.
    fronts = []
    for y in range(f0, f0 + fh):
        for xa in range(c0, c0 + w):
            line = [slice(None)] * 3
            line[1], line[side] = y, xa
            idx = np.argwhere(occ[tuple(line)])[:, 0]
            if len(idx):
                fronts.append(idx.max() if sign > 0 else idx.min())
    d_front = int(np.median(fronts))
    wall = d_front - 2 * sign
    n = index.shape[axis]
    for y in range(f0, f0 + fh):
        for xa in range(c0, c0 + w):
            if not occ[cell(xa, y, wall - sign)]:
                continue  # no hood behind the wall here: carving would leave a floating panel
            for d in range(n):
                if (d - wall) * sign > 0:
                    index[cell(xa, y, d)] = -1
            index[cell(xa, y, wall)] = void
    # eyes: two single white voxels in the upper half of the void; a small red mouth below them
    ey = f0 + fh - 1 - fh // 3
    row = sorted(c[side] for c in map(list, zip(*np.nonzero(index == void))) if c[1] == ey)
    if len(row) >= 4:
        ex = [row[1], row[-2]]
    elif len(row) >= 2:
        ex = [row[0], row[-1]]
    else:
        ex = row
    for xa in ex:
        index[cell(xa, ey, wall)] = eye
    mouth_x = (ex[0] + ex[-1]) // 2 if ex else c0 + w // 2
    if index[cell(mouth_x, f0 + 1, wall)] == void:
        index[cell(mouth_x, f0 + 1, wall)] = mouth

    # 3. Lantern: warm, bright voxels on the lantern side below the scarf glow.
    if a.lantern:
        lax, lsign = AXES[a.lantern]
        glow = add_colour(palette, GLOW, 2.0)
        warm = [i for i, (h, s, v) in enumerate(hsv([p["hex"] for p in palette]))
                if 0.04 < h < 0.15 and s > 0.4 and v > a.glow_min_v and palette[i]["hex"] not in ROBE + HOOD]
        n = index.shape[lax]
        half = slice(n // 2, n) if lsign > 0 else slice(0, n // 2)
        region = [slice(None)] * 3
        region[lax] = half
        region[1] = slice(0, scarf_mid)
        sub = index[tuple(region)]
        sub[np.isin(sub, warm)] = glow

    # 4. Hood point. TRELLIS rounds the hood off; the reference rises in steps to a point. Above
    # the void, the hood becomes a stepped box pyramid centred on the hood, narrowing every row to
    # a one-voxel tip --point-rows above the old top. The staff and anything else not hood-coloured
    # is kept.
    if a.point_rows:
        index = np.pad(index, ((0, 0), (0, a.point_rows), (0, 0)), constant_values=-1)
        hood_mask = np.isin(index, hood_ids)
        # the hood's own width: its voxels in the rows above the void (the face rows also catch
        # the shoulders), trimmed of strays
        old_top = index.shape[1] - a.point_rows
        pts = np.argwhere(hood_mask[:, f0 + fh:old_top, :].any(axis=1))
        (x0, z0), (x1, z1) = np.percentile(pts, 5, axis=0), np.percentile(pts, 95, axis=0)
        cx, cz = (x0 + x1) / 2, (z0 + z1) / 2
        hx0, hz0 = (x1 - x0) / 2, (z1 - z0) / 2
        base = f0 + fh
        top = index.shape[1] - 1
        X, Z = np.meshgrid(np.arange(index.shape[0]), np.arange(index.shape[2]), indexing="ij")
        for y in range(base, top + 1):
            t = (y - base) / max(1, top - base)  # 0 at the brim, 1 at the tip
            hx = max(0.5, hx0 * (1 - t))
            hz = max(0.5, hz0 * (1 - t))
            inside = (np.abs(X - cx) <= hx) & (np.abs(Z - cz) <= hz)
            layer = index[:, y, :]
            layer[np.isin(layer, hood_ids) & ~inside] = -1
            layer[(layer < 0) & inside] = hood_ids[2]
        # shade the rebuilt hood: step tops get the highlight, the back half the shadow
        for y in range(base, top + 1):
            layer, above = index[:, y, :], (index[:, y + 1, :] if y < top else np.full_like(index[:, y, :], -1))
            hood_here = np.isin(layer, hood_ids)
            back = ((X - cx) if axis == 0 else (Z - cz)) * sign < 0
            layer[hood_here] = hood_ids[2]
            layer[hood_here & back] = hood_ids[1]
            layer[hood_here & (above < 0)] = hood_ids[3]

    # 5. Scarf tail: a two-voxel-wide red strip hangs down the back, just off centre, one voxel
    # proud of the robe, narrowing to one voxel at its end (the reference's back view).
    if a.tail:
        index = np.pad(index, [(1, 1) if ax == axis else (0, 0) for ax in range(3)], constant_values=-1)
        red_id = add_colour(palette, MOUTH)
        body = np.argwhere(index[:, a.boots:scarf_mid, :] >= 0)
        mid = int(np.median(body[:, 0 if side == 0 else 2]))
        for k, y in enumerate(range(scarf_mid, scarf_mid - a.tail, -1)):
            for xa in ([mid + 1, mid + 2] if k < a.tail - 1 else [mid + 1]):
                line = [slice(None)] * 3
                line[1], line[side] = y, xa
                idx = np.argwhere(index[tuple(line)] >= 0)[:, 0]
                if not len(idx):
                    continue
                d = idx.min() if sign > 0 else idx.max()  # the back-most voxel
                index[cell(xa, y, d)] = red_id
                index[cell(xa, y, d - sign)] = red_id

    # drop unused palette entries, write like voxelize.py
    used = sorted(set(index[index >= 0].tolist()))
    remap = {o: n for n, o in enumerate(used)}
    palette = [palette[o] for o in used]
    index = np.vectorize(lambda v: remap.get(v, -1))(index)
    coords = np.argwhere(index >= 0)
    dims = np.array(index.shape)
    off = np.array([-(dims[0] // 2), 0, -(dims[2] // 2)])
    V, F, C, N, E = build_faces(index, off, doc["size"], palette)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    out = dict(doc, source=a.input.name, dims=dims.tolist(), palette=palette,
               voxels=np.concatenate([coords + off, index[tuple(coords.T)][:, None]], 1).tolist())
    Path(f"{a.out}.vox.json").write_text(json.dumps(out, separators=(",", ":")))
    mesh = trimesh.Trimesh(V, F, vertex_normals=N, process=False)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=(np.clip(C, 0, 1) * 255).astype(np.uint8))
    mesh.export(f"{a.out}.glb")
    render_views(V, F, C, N, E, Path(f"{a.out}.png"))
    render_views(V, F, C, N, E, Path(f"{a.out}_night.png"), night=True)
    print(json.dumps({"out": str(a.out), "voxels": int(len(coords)), "face_px": [w, fh],
                      "scarf_rows": [scarf_mid, scarf_top], "palette": [p["hex"] for p in palette]}))


if __name__ == "__main__":
    main()
