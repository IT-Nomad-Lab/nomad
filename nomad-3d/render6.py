#!/usr/bin/env python3
"""Render a runematch-vox/1 model from the 6 turnaround angles (front, front-right, right, back,
left, front-left), so it can be compared one-to-one with the reference sheet.

Usage: render6.py model.vox.json out.png [--px 360] [--pitch 10] [--night] [--yaws a,b,…]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from voxelize import build_faces, render_views  # noqa: E402

# yaw that shows each named side to the camera (front faces +Z, lantern hand +X)
TURNAROUND = {"front": 0, "front_right": 45, "right": 90, "back": 180, "left": -90, "front_left": -45}


def load(path: Path):
    doc = json.loads(path.read_text())
    vox = np.array(doc["voxels"])
    lo = vox[:, :3].min(0)
    index = np.full(vox[:, :3].max(0) - lo + 1, -1, dtype=int)
    index[tuple((vox[:, :3] - lo).T)] = vox[:, 3]
    return build_faces(index, lo, doc["size"], doc["palette"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--px", type=int, default=360)
    ap.add_argument("--pitch", type=float, default=10)
    ap.add_argument("--night", action="store_true")
    ap.add_argument("--yaws", default=None)
    a = ap.parse_args()
    yaws = [float(v) for v in a.yaws.split(",")] if a.yaws else list(TURNAROUND.values())
    render_views(*load(a.model), a.out, px=a.px, night=a.night, yaws=yaws, pitch=a.pitch)


if __name__ == "__main__":
    main()
