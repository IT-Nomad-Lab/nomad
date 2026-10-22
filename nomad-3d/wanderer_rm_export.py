#!/usr/bin/env python3
"""Approved Wanderer build (wanderer_build.py, 1 cm) → RuneMatch's six rig parts and two hand gear files.

RuneMatch (#1000) wants:
  heroes/wanderer/{head,torso,arm_l,arm_r,leg_l,leg_r}.vox.json   each part's origin at its rig joint
  gear/lantern.vox.json, gear/staff_wanderer.vox.json              bottom-centre origin
all at 0.05 m, the catalogue row size.

Steps:
  1. mirror X: the builder holds the lantern in +X, the game's Hand_L (the lantern) sits at -X
  2. resample 1 cm → 5 cm per part on one shared lattice (blocks floor-divided about the origin):
     a block is solid at >= 1/3 fill, its colour the block's modal palette entry; a block holding
     enough glow is glow (the lantern panes), and each eye collapses to one block at its centroid,
     since a 5 x 6 x 1 cm eye never fills a third of a 5 cm block; in the head, a block holding
     enough face-opening voxels (inner walls, void) is dark, so the opening stays a recess
  3. parts: subtract the rig joint (from RuneMatch's own rig(), so they can never disagree)
     gear: shift to bottom-centre

Usage:
  wanderer_rm_export.py out/wanderer3/we10.vox.json <RuneMatch checkout> [--preview out/wanderer3/rm05]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

K = 5            # 1 cm → 5 cm
SIZE = 0.05
FILL = 1 / 3
GLOW_MIN = 12    # emissive voxels that make a block emissive even under the fill rule
DARK_MIN = 20    # face-opening voxels (inner walls, void) that make a head block dark
DARK_HEX = {"#2A2220", "#0A0708"}
BODY = {"head": "neck", "torso": "hips", "arm_l": "shoulder_l", "arm_r": "shoulder_r",
        "leg_l": "hip_l", "leg_r": "hip_r"}
GEAR = {"lantern": "gear/lantern.vox.json", "staff": "gear/staff_wanderer.vox.json"}
EYE_HEX = "#FFFFFF"


def resample(xyz: np.ndarray, col: np.ndarray, emissive: np.ndarray, eye: np.ndarray, dark: np.ndarray) -> dict:
    """{(bx, by, bz): palette index} for one part."""
    blocks = np.floor_divide(xyz, K)
    out = {}
    keys, inv = np.unique(blocks, axis=0, return_inverse=True)
    inv = inv.ravel()
    for k in range(len(keys)):
        sel = inv == k
        c = col[sel]
        e = emissive[c] & ~eye[c]
        d = dark[c]
        if e.sum() >= GLOW_MIN:
            out[tuple(int(q) for q in keys[k])] = int(np.bincount(c[e]).argmax())
        elif d.sum() >= DARK_MIN:                 # the face opening stays a dark recess
            out[tuple(int(q) for q in keys[k])] = int(np.bincount(c[d]).argmax())
        elif sel.sum() >= FILL * K ** 3:
            body = c[~eye[c]]
            if len(body):
                out[tuple(int(q) for q in keys[k])] = int(np.bincount(body).argmax())
    return out


def eyes(xyz, col, eye):
    """Each eye (one per x side) becomes one block at its centroid."""
    sel = eye[col]
    out = {}
    for side in (xyz[sel, 0] < 0, xyz[sel, 0] >= 0):
        if side.any():
            p = xyz[sel][side].mean(0)
            out[tuple(int(q) for q in np.floor_divide(np.round(p).astype(int), K))] = int(col[sel][side][0])
    return out


def write(path: Path, blocks: dict, palette: list, source: str, shift=(0, 0, 0)) -> dict:
    used = sorted({c for c in blocks.values()})
    remap = {c: i for i, c in enumerate(used)}
    vox = sorted([x - shift[0], y - shift[1], z - shift[2], remap[c]] for (x, y, z), c in blocks.items())
    a = np.array([v[:3] for v in vox])
    dims = (a.max(0) - a.min(0) + 1).tolist()
    doc = {"format": "runematch-vox/1", "source": source, "size": SIZE, "dims": dims,
           "palette": [palette[c] for c in used], "voxels": vox}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, separators=(",", ":")))
    return doc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", type=Path)
    ap.add_argument("repo", type=Path)
    ap.add_argument("--preview", type=Path, help="also write the assembled 5 cm figure, for rendering")
    a = ap.parse_args()

    sys.path.insert(0, str(a.repo / "scripts"))
    import gen_voxel_placeholders as gvp  # noqa: E402
    import voxel_catalogue as vc  # noqa: E402

    d = json.loads(a.model.read_text())
    assert abs(d["size"] * K - SIZE) < 1e-9, d["size"]
    v = np.array(d["voxels"])
    xyz, col = v[:, :3].copy(), v[:, 3]
    xyz[:, 0] = -xyz[:, 0] - 1                                    # mirror X (step 1)
    part = np.array(d["parts"])
    names = d["part_names"]
    palette = d["palette"]
    emissive = np.array([p["emission"] > 0 for p in palette])
    eye = np.array([p["hex"].upper() == EYE_HEX and p["emission"] > 0 for p in palette])
    src = "nomad-3d:wanderer_build.py(%s)|mirrored|resampled:%g" % (a.model.stem, SIZE)

    joints, _, _, _ = gvp.rig(vc.HERO)
    models = a.repo / "Assets/Resources/World3D/Models"
    figure, tris = {}, {}
    for pn in list(BODY) + list(GEAR):
        sel = part == names.index(pn)
        dark = np.array([pn == "head" and p["hex"].upper() in DARK_HEX for p in palette])
        blocks = resample(xyz[sel], col[sel], emissive, eye, dark)
        if pn == "head":
            for (ex, ey, ez), c in eyes(xyz[sel], col[sel], eye).items():
                blocks[(ex, ey, ez)] = c
                for z in range(ez + 1, ez + 8):          # keep the line of sight from the front open
                    blocks.pop((ex, ey, z), None)
        figure.update(blocks)
        if pn in BODY:
            j = joints[BODY[pn]]
            shift = tuple(int(round(c / SIZE)) for c in j)
            rel = "heroes/wanderer/%s.vox.json" % pn
        else:
            b = np.array(list(blocks))
            lo, hi = b.min(0), b.max(0)
            X, Z = hi[0] - lo[0] + 1, hi[2] - lo[2] + 1
            shift = (int(lo[0] + X // 2), int(lo[1]), int(lo[2] + Z // 2))
            rel = GEAR[pn]
        doc = write(models / rel, blocks, palette, src, shift)
        keyed = {(x, y, z): (doc["palette"][c]["hex"].lower(), doc["palette"][c]["emission"])
                 for x, y, z, c in doc["voxels"]}
        tris[rel] = gvp.greedy_tris(keyed)
        print("%-34s dims %-14s voxels %5d tris %5d" % (rel, doc["dims"], len(doc["voxels"]), tris[rel]))
    print("body + lantern + staff tris:", sum(tris.values()))
    if a.preview:
        # the figure exactly as built (gear where the builder put it), mirrored back so +X is the lantern
        # again and the preview reads like the reference sheet
        vox = [[-x - 1, y, z, c] for (x, y, z), c in figure.items()]
        b = np.array([q[:3] for q in vox])
        doc = {"format": "runematch-vox/1", "source": src, "size": SIZE,
               "dims": (b.max(0) - b.min(0) + 1).tolist(), "palette": palette, "voxels": vox}
        Path(f"{a.preview}.vox.json").write_text(json.dumps(doc, separators=(",", ":")))


if __name__ == "__main__":
    main()
