#!/usr/bin/env python3
"""Textured mesh (GLB/OBJ) → palette-snapped voxel model in RuneMatch's Spike-3D format.

Why voxelize at all: AI image-to-3D meshes come with messy topology, heavy triangle counts
and smeared UVs. Snapping them to the spike's 0.1-unit lattice with a small palette fixes all
three at once and makes every asset read in one style, whichever model generated it.

Outputs, next to each other:
  <out>.vox.json   the asset: voxel size, palette (sRGB hex + emission), voxels [x,y,z,i].
                   Coordinates follow VoxelModel.cs: voxel (x,y,z) fills [x,x+1)… in voxel
                   units, origin at the bottom centre, Y up.
  <out>.glb        preview mesh, exposed faces only, vertex colour = albedo × 3-neighbour AO
                   (the same AO rule as VoxelModel.EmitInto).
  <out>.png        4-view turntable render, so a model (or a person) can judge the result.

Usage:
  voxelize.py in.glb out/tree --height 2.4 --colors 8
  voxelize.py in.glb out/stone --height 1.2 --palette ember.json --emissive "#7CE8FF=2.0"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image, ImageChops, ImageDraw, ImageFilter
from scipy import ndimage

# ---------------------------------------------------------------- loading + colour sampling


def _texture_of(mesh: trimesh.Trimesh):
    vis = mesh.visual
    if isinstance(vis, trimesh.visual.TextureVisuals) and vis.uv is not None:
        mat = vis.material
        img = getattr(mat, "baseColorTexture", None) or getattr(mat, "image", None)
        if img is not None:
            factor = getattr(mat, "baseColorFactor", None)
            return np.asarray(img.convert("RGB"), dtype=np.float32) / 255.0, (
                np.asarray(factor[:3], dtype=np.float32) / (255.0 if np.max(factor) > 1 else 1.0)
                if factor is not None else np.ones(3, np.float32))
    return None, None


def _flat_colour(mesh: trimesh.Trimesh) -> np.ndarray:
    vis = mesh.visual
    if isinstance(vis, trimesh.visual.ColorVisuals) and vis.kind == "vertex":
        return None  # handled per sample
    mat = getattr(vis, "material", None)
    for attr in ("baseColorFactor", "main_color", "diffuse"):
        c = getattr(mat, attr, None)
        if c is not None:
            c = np.asarray(c, dtype=np.float32)[:3]
            return c / 255.0 if c.max() > 1 else c
    return np.array([0.6, 0.6, 0.6], np.float32)


def sample_coloured_points(path: Path, spacing: float, max_points: int):
    """Dense surface samples with their base colour, from every mesh in the scene."""
    scene = trimesh.load(str(path), force="scene")
    pts_all, col_all = [], []
    meshes = []
    for node in scene.graph.nodes_geometry:
        tf, geom_name = scene.graph[node]
        g = scene.geometry[geom_name]
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        m = g.copy()
        m.apply_transform(tf)
        meshes.append(m)
    if not meshes:
        sys.exit(f"no triangle meshes in {path}")
    return meshes, scene


def sample_mesh(m: trimesh.Trimesh, n: int):
    pts, fidx = trimesh.sample.sample_surface(m, n)
    tex, factor = _texture_of(m)
    if tex is not None:
        tri = m.triangles[fidx]
        bary = trimesh.triangles.points_to_barycentric(tri, pts)
        uv = np.einsum("ij,ijk->ik", bary, m.visual.uv[m.faces[fidx]])
        h, w, _ = tex.shape
        u = np.mod(uv[:, 0], 1.0)
        v = np.mod(uv[:, 1], 1.0)
        px = np.clip((u * (w - 1)).round().astype(int), 0, w - 1)
        py = np.clip(((1.0 - v) * (h - 1)).round().astype(int), 0, h - 1)
        return pts, tex[py, px] * factor
    vis = m.visual
    if isinstance(vis, trimesh.visual.ColorVisuals) and vis.kind == "vertex":
        vc = vis.vertex_colors[:, :3].astype(np.float32) / 255.0
        tri = m.triangles[fidx]
        bary = trimesh.triangles.points_to_barycentric(tri, pts)
        return pts, np.einsum("ij,ijk->ik", bary, vc[m.faces[fidx]])
    return pts, np.tile(_flat_colour(m), (len(pts), 1))


# ---------------------------------------------------------------- voxel grid


def voxelize(meshes, height: float, size: float, max_points: int, fill: bool, min_island: int,
             crop_bottom: float = 0.0):
    combined = trimesh.util.concatenate(meshes)
    lo, hi = combined.bounds
    # SDXL concepts come with plinths whatever the prompt says; TRELLIS.2 models them faithfully
    cut = lo[1] + crop_bottom * (hi[1] - lo[1])
    scale = height / max(hi[1] - cut, 1e-9)

    pts_l, col_l = [], []
    total_area = sum(m.area for m in meshes) * scale * scale
    # ~9 samples per voxel face so thin parts don't punch holes in the shell
    want = int(total_area / (size / 3.0) ** 2)
    budget = min(max(want, 20000), max_points)
    for m in meshes:
        n = max(200, int(budget * (m.area * scale * scale) / max(total_area, 1e-9)))
        p, c = sample_mesh(m, n)
        pts_l.append(p)
        col_l.append(c)
    pts = np.concatenate(pts_l)
    cols = np.clip(np.concatenate(col_l), 0, 1)
    keep = pts[:, 1] >= cut
    pts, cols = pts[keep], cols[keep]
    plo, phi = pts.min(0), pts.max(0)
    centre = np.array([(plo[0] + phi[0]) / 2, cut, (plo[2] + phi[2]) / 2])
    pts = (pts - centre) * scale

    idx = np.floor(pts / size).astype(np.int64)
    idx[:, 1] = np.maximum(idx[:, 1], 0)
    mn = idx.min(0)
    idx -= mn
    dims = idx.max(0) + 1
    flat = np.ravel_multi_index(idx.T, dims)
    uniq, inv = np.unique(flat, return_inverse=True)
    sums = np.zeros((len(uniq), 3))
    np.add.at(sums, inv, cols)
    counts = np.bincount(inv).astype(np.float64)
    vcol = sums / counts[:, None]

    occ = np.zeros(dims, bool)
    occ.flat[uniq] = True
    colour = np.zeros((*dims, 3), np.float32)
    colour.reshape(-1, 3)[uniq] = vcol

    if fill:
        filled = ndimage.binary_fill_holes(occ)
        inner = filled & ~occ
        if inner.any():
            # interior voxels take the colour of the nearest surface voxel (only seen in cutaways)
            _, (ix, iy, iz) = ndimage.distance_transform_edt(~occ, return_indices=True)
            colour[inner] = colour[ix[inner], iy[inner], iz[inner]]
        occ = filled

    if min_island > 0:
        lab, n = ndimage.label(occ, structure=np.ones((3, 3, 3)))
        if n > 1:
            sizes = ndimage.sum(occ, lab, range(1, n + 1))
            keep = np.zeros(n + 1, bool)
            keep[1:] = sizes >= min_island
            keep[1 + int(np.argmax(sizes))] = True
            occ &= keep[lab]

    return occ, colour, mn, scale


# ---------------------------------------------------------------- palette


def hex_to_rgb(h: str) -> np.ndarray:
    h = h.lstrip("#")
    return np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)], np.float32) / 255.0


def rgb_to_hex(c) -> str:
    return "#" + "".join(f"{int(round(float(x) * 255)):02X}" for x in np.clip(c, 0, 1))


def kmeans(cols: np.ndarray, k: int, iters: int = 20, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    k = min(k, len(np.unique(cols.round(3), axis=0)))
    # k-means++ seeding
    cent = [cols[rng.integers(len(cols))]]
    for _ in range(1, k):
        d = np.min(((cols[:, None] - np.array(cent)[None]) ** 2).sum(-1), 1)
        cent.append(cols[rng.choice(len(cols), p=d / d.sum())] if d.sum() > 0 else cols[0])
    cent = np.array(cent)
    for _ in range(iters):
        lab = np.argmin(((cols[:, None] - cent[None]) ** 2).sum(-1), 1)
        for j in range(k):
            if (lab == j).any():
                cent[j] = cols[lab == j].mean(0)
    return cent


def quantize(occ, colour, n_colours: int, palette_file: Path | None, emissive: dict[str, float]):
    cols = colour[occ]
    if palette_file:
        spec = json.loads(palette_file.read_text())
        entries = spec["palette"] if isinstance(spec, dict) else spec
        pal = [(e["hex"] if isinstance(e, dict) else e) for e in entries]
        for e in entries:
            if isinstance(e, dict) and e.get("emission"):
                emissive.setdefault(e["hex"].upper(), float(e["emission"]))
        cent = np.array([hex_to_rgb(h) for h in pal])
    else:
        cent = kmeans(cols, n_colours)
    lab = np.argmin(((cols[:, None] - cent[None]) ** 2).sum(-1), 1)
    used = np.unique(lab)
    remap = {int(o): i for i, o in enumerate(used)}
    hexes = [rgb_to_hex(cent[o]) for o in used]
    # an --emissive colour claims the palette entry nearest to it
    emis = [0.0] * len(hexes)
    for h, strength in emissive.items():
        target = hex_to_rgb(h)
        dist = [float(np.sqrt(((hex_to_rgb(x) - target) ** 2).sum())) for x in hexes]
        j = int(np.argmin(dist))
        if dist[j] > 0.2:  # nothing on the model is close to that glow colour: don't light up wood
            print(f"warning: no palette colour near {h} (closest {hexes[j]}, d={dist[j]:.2f}); emission skipped",
                  file=sys.stderr)
            continue
        emis[j] = strength
    index = np.full(occ.shape, -1, np.int32)
    index[occ] = np.array([remap[int(x)] for x in lab])
    return index, [{"hex": h, "emission": e} for h, e in zip(hexes, emis)]


# ---------------------------------------------------------------- mesh (VoxelModel.EmitInto rules)

NORMALS = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]])


def _tangents(n):
    if n[0] != 0:
        return np.array([0, 0, 1]) * n[0], np.array([0, 1, 0])
    if n[1] != 0:
        return np.array([1, 0, 0]), np.array([0, 0, 1]) * n[1]
    return np.array([0, 1, 0]), np.array([1, 0, 0]) * n[2]


def build_faces(index: np.ndarray, offset: np.ndarray, size: float, palette):
    occ = index >= 0
    pad = np.pad(occ, 1)
    pal = np.array([hex_to_rgb(p["hex"]) for p in palette])
    emis = np.array([p["emission"] for p in palette])
    verts, cols, emi, faces, normals = [], [], [], [], []
    coords = np.argwhere(occ)
    for n in NORMALS:
        nb = pad[tuple((coords + 1 + n).T)]
        exposed = coords[~nb]
        if len(exposed) == 0:
            continue
        u, v = _tangents(n)
        su = np.array([-1, 1, 1, -1])
        sv = np.array([-1, -1, 1, 1])
        centre = exposed + 0.5 + n * 0.5
        base = len(verts) and sum(len(x) for x in verts)
        ci = index[tuple(exposed.T)]
        for k in range(4):
            corner = centre + (u * su[k] + v * sv[k]) * 0.5
            s1 = pad[tuple((exposed + 1 + n + u * su[k]).T)]
            s2 = pad[tuple((exposed + 1 + n + v * sv[k]).T)]
            cc = pad[tuple((exposed + 1 + n + u * su[k] + v * sv[k]).T)]
            level = np.where(s1 & s2, 0, 3 - (s1.astype(int) + s2 + cc))
            ao = 0.55 + 0.15 * level  # 0.55 … 1.0
            verts.append(((corner + offset) * size)[:, None, :])
            cols.append((pal[ci] * ao[:, None])[:, None, :])
            emi.append(emis[ci][:, None])
        stacked_v = np.concatenate(verts[-4:], 1).reshape(-1, 3)
        stacked_c = np.concatenate(cols[-4:], 1).reshape(-1, 3)
        stacked_e = np.concatenate(emi[-4:], 1).reshape(-1)
        verts[-4:] = [stacked_v]
        cols[-4:] = [stacked_c]
        emi[-4:] = [stacked_e]
        m = len(exposed)
        i0 = base + np.arange(m) * 4
        faces.append(np.stack([i0, i0 + 1, i0 + 2, i0, i0 + 2, i0 + 3], 1).reshape(-1, 3))
        normals.append(np.tile(n, (m * 4, 1)))
    V = np.concatenate(verts)
    C = np.concatenate(cols)
    F = np.concatenate(faces)
    N = np.concatenate(normals).astype(np.float32)
    E = np.concatenate(emi)
    # winding: make each triangle face its quad normal
    tri_n = np.cross(V[F[:, 1]] - V[F[:, 0]], V[F[:, 2]] - V[F[:, 0]])
    flip = (tri_n * N[F[:, 0]]).sum(1) < 0
    F[flip] = F[flip][:, ::-1]
    return V, F, C, N, E


# ---------------------------------------------------------------- preview render (no GPU, no Blender)


def _emissive_spill(V, N, C, E, radius: float = 0.22, gain: float = 5.0):
    """Per-vertex light from glowing voxels (a lantern should warm the cloak around it).
    Up to 256 emissive quads act as point lights: lambert × 1/(1+(d/r)²), no shadows."""
    q = np.arange(len(V)).reshape(-1, 4)[:, 0]
    lit = q[E[q] > 0]
    out = np.zeros_like(C)
    if len(lit) == 0:
        return out
    if len(lit) > 256:
        lit = lit[np.linspace(0, len(lit) - 1, 256).astype(int)]
    lp = np.stack([V[i:i + 4].mean(0) for i in lit])
    lc = C[lit] * E[lit][:, None] * (gain / len(lit))
    for s in range(0, len(V), 20000):
        d = lp[None] - V[s:s + 20000, None]                     # [v, l, 3]
        dist = np.linalg.norm(d, axis=-1) + 1e-6
        lam = np.clip((d * N[s:s + 20000, None]).sum(-1) / dist, 0, 1)
        out[s:s + 20000] = (lam / (1 + (dist / radius) ** 2)) @ lc
    return np.minimum(out, 0.9)


def render_views(V, F, C, N, E, out_png: Path, px: int = 420, night: bool = False):
    """4 turntable views. night=True approximates the Ember Vale look: dim blue moonlight, emissive
    voxels at full strength, and a blurred bloom pass added on top."""
    views = []
    light = np.array([0.4, 0.8, 0.45])
    light /= np.linalg.norm(light)
    moon = np.array([0.55, 0.62, 1.0])  # cool tint for lit faces at night
    spill = _emissive_spill(V, N, C, E) if night else None
    for yaw in (35, 125, 215, 305):
        a, p = np.radians(yaw), np.radians(28)
        ry = np.array([[np.cos(a), 0, np.sin(a)], [0, 1, 0], [-np.sin(a), 0, np.cos(a)]])
        rx = np.array([[1, 0, 0], [0, np.cos(p), -np.sin(p)], [0, np.sin(p), np.cos(p)]])
        R = rx @ ry
        P = V @ R.T
        Nr = N @ R.T
        quads = np.arange(len(V)).reshape(-1, 4)  # build_faces emits 4 verts per voxel face, in order
        facing = Nr[quads[:, 0], 2] > 0
        tri = quads[facing]
        depth = P[tri].mean(1)[:, 2]
        order = np.argsort(depth)
        xy = P[:, :2]
        lo, hi = xy.min(0), xy.max(0)
        s = (px * 0.86) / max((hi - lo).max(), 1e-9)
        img = Image.new("RGB", (px, px), (12, 11, 26) if night else (22, 20, 34))
        d = ImageDraw.Draw(img)
        glow = Image.new("RGB", (px, px), (0, 0, 0))
        g = ImageDraw.Draw(glow)
        shade = 0.45 + 0.55 * np.clip(N @ light, 0, 1)
        for t in tri[order]:
            pts = [((xy[i, 0] - lo[0]) * s + px * 0.07, px - ((xy[i, 1] - lo[1]) * s + px * 0.07)) for i in t]
            base = C[t].mean(0)
            e = E[t[0]]
            if e > 0:
                c = base * (1.0 + 0.25 * e)
            elif night:
                c = base * (moon * (0.32 + 0.55 * (shade[t[0]] - 0.45) / 0.55) + spill[t[0]])
            else:
                c = base * shade[t[0]]
            d.polygon(pts, fill=tuple(int(x) for x in np.clip(c, 0, 1) * 255))
            # occluders paint black into the glow layer so bloom respects depth
            g.polygon(pts, fill=tuple(int(x) for x in np.clip(base * min(e, 3.0) / 3.0, 0, 1) * 255) if e > 0
                      else (0, 0, 0))
        if night:
            bloom = glow.filter(ImageFilter.GaussianBlur(px * 0.025))
            wide = glow.filter(ImageFilter.GaussianBlur(px * 0.07))
            img = ImageChops.add(ImageChops.add(img, bloom), wide)
        views.append(img)
    sheet = Image.new("RGB", (px * 4, px))
    for i, im in enumerate(views):
        sheet.paste(im, (i * px, 0))
    sheet.save(out_png)


# ---------------------------------------------------------------- main


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", type=Path)
    ap.add_argument("out", type=Path, help="output stem, e.g. out/tree_red")
    ap.add_argument("--height", type=float, required=True, help="world height in metres (hero ≈ 1.1)")
    ap.add_argument("--size", type=float, default=0.1, help="voxel edge in metres (spike uses 0.1)")
    ap.add_argument("--colors", type=int, default=8, help="palette size when no --palette is given")
    ap.add_argument("--palette", type=Path, help="JSON list of hex (or {hex, emission}) to snap to")
    ap.add_argument("--emissive", action="append", default=[], help='"#RRGGBB=strength", repeatable')
    ap.add_argument("--no-fill", action="store_true", help="keep a hollow shell")
    ap.add_argument("--min-island", type=int, default=4, help="drop floating clumps smaller than this")
    ap.add_argument("--max-points", type=int, default=4_000_000)
    ap.add_argument("--crop-bottom", type=float, default=0.0,
                    help="drop this fraction of the mesh height from the bottom first (cuts off plinths)")
    a = ap.parse_args()

    emissive = {}
    for e in a.emissive:
        h, _, s = e.partition("=")
        emissive[h.upper()] = float(s or 1.0)

    meshes, _ = sample_coloured_points(a.input, a.size / 3, a.max_points)
    occ, colour, mn, scale = voxelize(meshes, a.height, a.size, a.max_points, not a.no_fill, a.min_island,
                                     a.crop_bottom)
    index, palette = quantize(occ, colour, a.colors, a.palette, emissive)

    dims = np.array(index.shape)
    # origin at bottom centre, like VoxelModel stand-ins
    offset = np.array([-(dims[0] // 2), 0, -(dims[2] // 2)])
    coords = np.argwhere(index >= 0)
    V, F, C, N, E = build_faces(index, offset, a.size, palette)

    a.out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "runematch-vox/1",
        "source": a.input.name,
        "size": a.size,
        "dims": dims.tolist(),
        "palette": palette,
        "voxels": np.concatenate([coords + offset, index[tuple(coords.T)][:, None]], 1).tolist(),
    }
    Path(f"{a.out}.vox.json").write_text(json.dumps(doc, separators=(",", ":")))

    mesh = trimesh.Trimesh(V, F, vertex_normals=N, process=False)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=(np.clip(C, 0, 1) * 255).astype(np.uint8))
    mesh.export(f"{a.out}.glb")
    render_views(V, F, C, N, E, Path(f"{a.out}.png"))
    render_views(V, F, C, N, E, Path(f"{a.out}_night.png"), night=True)

    print(json.dumps({
        "out": str(a.out),
        "dims_voxels": dims.tolist(),
        "dims_m": (dims * a.size).round(2).tolist(),
        "voxels": int(len(coords)),
        "triangles": int(len(F)),
        "colours": len(palette),
        "palette": [p["hex"] + (f"*{p['emission']}" if p["emission"] else "") for p in palette],
    }))


if __name__ == "__main__":
    main()
