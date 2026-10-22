#!/usr/bin/env python3
"""Lantern Wanderer, built voxel by voxel (nomad-pm #1000).

AI image-to-3D gave a round blob hood and no back detail, so this script builds the character
from shapes measured off the operator's turnaround sheet. The proportions and part list come
from the operator's rvoxel_lantern_wanderer.py blockout, and the colour ramps from
tools/rvoxel/characters/wanderer.json and the sheet.

Shapes are written in design units (D = 3 cm, 47 rows to the hood tip = 1.41 m) and
rasterized at --size, which must divide 3 cm: 0.03 → 1 voxel per unit, 0.01 → 3. The hood keeps
its terraces at every size. Silhouettes, hems, the strap and the fabric streaks are resolved
at the fine size.

Y is up, the front faces +Z, and the lantern hand is +X. That's the same frame as voxelize.py
and VoxelModel.cs.

Outputs <out>.vox.json (runematch-vox/1, plus a "parts" map for the 6-part rig), .glb, .png
and _night.png.

Usage: wanderer_build.py out/wanderer3/wanderer [--size 0.03|0.015|0.01]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import trimesh

sys.path.insert(0, str(Path(__file__).parent))
from voxelize import build_faces, render_views  # noqa: E402

DESIGN = 0.03

# ramps run dark → light
RAMPS = {
    "hood":    ["#4F3A30", "#735849", "#987A66", "#BD9F86"],
    "robe":    ["#4C382E", "#7A5E4C", "#A68468", "#CBAE92"],
    "scarf":   ["#7A1712", "#A4231B", "#C8322A", "#E0503A"],
    "boots":   ["#241C1A", "#382E2E", "#4E403C"],
    "gloves":  ["#241C1C", "#34292B", "#4A3B3A"],
    "wood":    ["#3E2414", "#5A3420", "#6D3E24", "#8A5634"],
    "leather": ["#4A2A16", "#6A3E22", "#8A5530", "#A86C3E"],
    "metal":   ["#2A2628", "#3E383A", "#575052"],
    "under":   ["#2A2220", "#3A2F2B", "#4A3D37"],
}
FLAT = {  # name: (hex, emission)
    "inner": ("#2A2220", 0.0),
    "void":  ("#0A0708", 0.0),
    "eye":   ("#FFFFFF", 1.5),
    "gold":  ("#D9A441", 0.0),
    "glow":  ("#FA8811", 2.0),
    "core":  ("#FFD873", 3.0),
}
BASE = {"hood": 2, "robe": 2, "scarf": 2, "boots": 1, "gloves": 1, "wood": 2, "leather": 2, "metal": 1, "under": 1}
MATS = list(RAMPS) + list(FLAT)
PARTS = ["head", "torso", "arm_l", "arm_r", "leg_l", "leg_r", "lantern", "staff"]   # the last two are hand gear

# design-unit bounds: x ∈ [-20, 21), y ∈ [0, 47), z ∈ [-13, 14)
DX0, DX1, DY1, DZ0, DZ1 = -20, 21, 47, -13, 14


def noise(*a) -> np.ndarray:
    """Deterministic per-cell noise in [0, 1); arguments broadcast like numpy arrays."""
    h = np.uint64(2166136261)
    for v in a:
        h = ((h ^ (np.asarray(v).astype(np.int64).astype(np.uint64) & np.uint64(0xFFFFFFFF)))
             * np.uint64(16777619)) & np.uint64(0xFFFFFFFF)
    h ^= h >> np.uint64(13)
    h = (h * np.uint64(1274126177)) & np.uint64(0xFFFFFFFF)
    return (h & np.uint64(0xFFFF)).astype(np.float64) / 65536.0


class Model:
    """Fine grid. Every public method takes design units (floats allowed)."""

    def __init__(self, s: int):
        self.s = s
        self.shape = ((DX1 - DX0) * s, DY1 * s, (DZ1 - DZ0) * s)
        self.mat = np.full(self.shape, -1, dtype=np.int16)
        self.part = np.full(self.shape, -1, dtype=np.int8)
        self.lvl = np.zeros(self.shape, dtype=np.int8)
        # design-unit centre of every fine cell, per axis
        self.cx = (np.arange(self.shape[0]) + 0.5) / s + DX0
        self.cy = (np.arange(self.shape[1]) + 0.5) / s
        self.cz = (np.arange(self.shape[2]) + 0.5) / s + DZ0

    # -- index helpers
    def fx(self, x):
        return int(round((x - DX0) * self.s))

    def fy(self, y):
        return int(round(y * self.s))

    def fz(self, z):
        return int(round((z - DZ0) * self.s))

    def put(self, sl, mat, part, lvl=0, mask=None):
        if mask is None:
            self.mat[sl], self.part[sl], self.lvl[sl] = MATS.index(mat), PARTS.index(part), lvl
        else:
            self.mat[sl][mask] = MATS.index(mat)
            self.part[sl][mask] = PARTS.index(part)
            self.lvl[sl][mask] = lvl

    def box(self, x0, x1, y0, y1, z0, z1, mat, part, lvl=0):
        """Inclusive design cells [x0..x1] × [y0..y1] × [z0..z1]."""
        sl = (slice(max(0, self.fx(x0)), self.fx(x1 + 1)), slice(max(0, self.fy(y0)), self.fy(y1 + 1)),
              slice(max(0, self.fz(z0)), self.fz(z1 + 1)))
        self.put(sl, mat, part, lvl)

    def section(self, y0, y1, a, b, mat, part, xc=0.0, zc=0.0, p=3.0):
        """Superellipse cross-section on design rows [y0, y1). a, b, xc, zc may be callables of y."""
        for j in range(self.fy(y0), self.fy(y1)):
            y = self.cy[j]
            A, B = [f(y) if callable(f) else f for f in (a, b)]
            XC, ZC = [f(y) if callable(f) else f for f in (xc, zc)]
            u = np.abs(self.cx - XC)[:, None] / max(A, 1e-3)
            v = np.abs(self.cz - ZC)[None, :] / max(B, 1e-3)
            mask = u ** p + v ** p <= 1.0
            self.put((slice(None), j, slice(None)), mat, part, mask=mask)

    def front(self) -> np.ndarray:
        """Front-most occupied fine z per (x, y); -1 where empty."""
        occ = self.mat >= 0
        last = occ.shape[2] - 1 - np.argmax(occ[:, :, ::-1], axis=2)
        return np.where(occ.any(2), last, -1)

    def back(self) -> np.ndarray:
        occ = self.mat >= 0
        return np.where(occ.any(2), np.argmax(occ, axis=2), -1)


def steps(d: dict, y):
    """Step table: d maps a design row to a value; rows in between take the row below's value."""
    keys = np.array(sorted(d))
    vals = np.array([d[k] for k in keys], dtype=float)
    return vals[np.clip(np.searchsorted(keys, np.floor(y), side="right") - 1, 0, len(keys) - 1)]


def within(q, lo, hi):
    """Design cells [lo, hi) by centre (q is a quantized centre, lo/hi whole units)."""
    return (q > lo) & (q < hi)


def build(s: int) -> Model:
    m = Model(s)
    X, Y, Z = m.cx[:, None, None], m.cy[None, :, None], m.cz[None, None, :]
    # centre of the design cell each fine voxel sits in: forms are built in whole design blocks
    QX, QY, QZ = np.floor(X) + 0.5, np.floor(Y) + 0.5, np.floor(Z) + 0.5

    def fill(mask, mat, part, lvl=0):
        mask = np.broadcast_to(mask, m.shape)
        m.mat[mask], m.part[mask], m.lvl[mask] = MATS.index(mat), PARTS.index(part), lvl

    def carve(mask):
        mask = np.broadcast_to(mask, m.shape)
        m.mat[mask], m.part[mask] = -1, -1

    def blk(x0, x1, y0, y1, z0, z1):
        return within(QX, x0, x1) & within(QY, y0, y1) & within(QZ, z0, z1)

    def octagon(x, z, a, b, c):
        return (np.abs(x) < a) & (np.abs(z) < b) & (np.abs(x) + np.abs(z) < a + b - c)

    def is_(name):
        return m.mat == MATS.index(name)

    # ---- boots and legs: short, chunky, toes forward, a clear gap between them
    for x0, part in ((-7, "leg_r"), (2, "leg_l")):
        fill(blk(x0, x0 + 5, 0, 3, -3, 4) & ~(within(QY, 2, 3) & within(QZ, 3, 4)), "boots", part)
        fill(blk(x0, x0 + 5, 0, 1, -3, 4), "boots", part, -1)                       # sole
        fill(blk(x0 + (1 if x0 < 0 else 0), x0 + (5 if x0 < 0 else 4), 3, 8, -2, 2), "under", part)

    # ---- robe: dark under-robe, then the beige outer robe in panels
    ra = {4: 9.0, 7: 8.5, 10: 8.0, 13: 7.5, 17: 7.0, 20: 7.0}       # half-width of the outer robe
    rb = {4: 6.5, 9: 6.0, 15: 5.5, 20: 5.5}                 # half-depth
    A, B = steps(ra, QY), steps(rb, QY)
    fill(within(QY, 4, 20) & octagon(QX, QZ, A - 0.5, B - 0.5, 3), "under", "torso")
    outer = within(QY, 5, 25) & octagon(QX, QZ, A + 1, B + 1, 3)
    # ragged hem: 16 teeth around the body, each a chunky step 0-3 blocks above the base
    teeth = np.array([1, 0, 2, 1, 0, 3, 1, 0, 2, 0, 1, 3, 0, 1, 2, 0])
    th = (np.arctan2(QX, QZ) + np.pi) / (2 * np.pi) * len(teeth)
    hem = 5 + teeth[np.floor(th).astype(int) % len(teeth)] + ((th % 1) > 0.55)
    outer &= QY > hem
    # front split: an inverted V from row 15 to the hem shows the under-robe and legs
    split = (QZ > 0) & (np.abs(QX - 0.5) < np.maximum(0, (15 - QY) * 0.5)) & (QZ > B - 2.5)
    outer &= ~split
    fill(outer & (QX < 0), "robe", "torso", 0)
    fill(outer & (QX >= 0), "robe", "torso", 1)
    fill(outer & (QZ < -2), "robe", "torso", -1)
    # big folds: vertical grooves one block deep, different on each side
    carve(is_("robe") & within(QX, -5, -4) & within(QY, 7, 20) & (QZ > B))
    carve(is_("robe") & within(QX, 4, 5) & within(QY, 10, 22) & (QZ > B))
    carve(is_("robe") & within(QX, -3, -2) & within(QY, 8, 21) & (QZ < -B))
    carve(is_("robe") & within(QX, 3, 4) & within(QY, 6, 17) & (QZ < -B))

    # ---- arms: big shoulder masses, bent sleeves, oversized fists, gaps to the torso
    for sd, part in ((-1, "arm_r"), (1, "arm_l")):
        def bx(x0, x1, y0, y1, z0, z1):  # mirrored box, x given for the +X side
            return blk(x0, x1, y0, y1, z0, z1) if sd > 0 else blk(-x1, -x0, y0, y1, z0, z1)
        sx = sd * QX
        shoulder = bx(6, 11, 21, 26, -3, 3) & ~((sx > 10) & (QY > 24.5))
        fill(shoulder, "robe", part, 0)
        fill(bx(8, 13, 19, 23, -2, 3), "robe", part, 0)                  # forearm, outward
        fill(bx(8, 13, 22, 23, -2, 3), "robe", part, 1)                  # lit top of the sleeve
        fill(bx(9, 14, 18, 20, -2, 4), "robe", part, -1)                 # cuff, shadowed underside
        fill(bx(10, 15, 16, 20, 0, 5), "gloves", part)                   # fist
        fill(bx(10, 15, 19, 20, 0, 5), "gloves", part, 1)                # knuckles catch the light

    # ---- staff, gripped in the -X fist, crook curling outward
    fill(blk(-13, -11, 0, 39, 1, 3), "wood", "staff")
    fill(blk(-13, -11, 26, 27, 1, 3) | blk(-14, -10, 30, 31, 0, 4), "wood", "staff", 1)   # knots
    fill(blk(-15, -11, 39, 41, 1, 3), "wood", "staff", 1)
    fill(blk(-16, -14, 36, 39, 1, 3), "wood", "staff", 0)
    fill(blk(-16, -15, 35, 36, 1, 3), "wood", "staff", -1)
    fill(blk(-14, -10, 16, 20, 0, 4) & ~blk(-13, -11, 16, 20, 1, 3), "gloves", "arm_r")  # wrap
    fill(blk(-13, -11, 16, 20, 1, 3) & (Z > 3 - 1 / s), "gloves", "arm_r")

    # ---- lantern hanging from the +X fist, clear of the robe
    fill(blk(13, 15, 15, 16, 1, 3), "metal", "lantern")                     # handle
    fill(blk(11, 17, 13, 15, -2, 6) & ~(within(QY, 14, 15) & ~blk(12, 16, 14, 15, -1, 5)), "metal", "lantern", 1)
    fill(blk(10, 18, 6, 7, -3, 7), "metal", "lantern")                     # base
    body = blk(11, 17, 7, 13, -2, 6)
    fill(body, "core", "lantern")
    ex = (X < 12 + 1 / s) | (X > 16 - 1 / s)
    ez = (Z < -1 + 1 / s) | (Z > 5 - 1 / s)
    u = np.where(ex, np.abs(Z - 2), np.abs(X - 14))
    d = np.abs(u) + np.abs(Y - 10) * 0.6
    pane = body & (ex ^ ez)
    fill(body & ex & ez, "metal", "lantern")
    fill(pane & ~((d <= 1.6) & (d > 0.8)), "glow", "lantern")
    fill(pane & (d <= 1.6) & (d > 0.8), "core", "lantern")

    # ---- satchel on the -X hip, strap from the +X shoulder
    zs = 7  # robe front at the hip is z 6.5-7
    fill(blk(-9, -2, 9, 15, zs - 2, zs + 1), "leather", "torso", 0)
    fill(blk(-9, -2, 12, 15, zs - 2, zs + 1) & (Z < zs + 1 + 1 / s), "leather", "torso", 1)  # flap
    fill(blk(-6, -5, 11, 12, zs, zs + 1) & (Z < zs + 1 + 1 / s), "gold", "torso")
    line = np.abs((QX - 7) - (QY - 24) * 1.1) < 1.0
    front_surface = (QZ > 0) & (Z > np.floor(B + 1) - 1)
    fill(line & within(QY, 13, 24) & front_surface & (np.abs(Z - (B + 1)) < 1 + 1 / s), "leather", "torso", 0)
    back_surface = (QZ < 0) & (Z < -np.floor(B + 1) + 1)
    fill(line & within(QY, 12, 24) & back_surface & (np.abs(-Z - (B + 1)) < 1 + 1 / s), "leather", "torso", -1)

    # ---- hood: chamfered shell built from a few big cloth planes, brow forward, blunt peak to +X
    # each side and the back step at different rows, so the steps never line up into rings
    wR = {26: 11, 28: 10.5, 30: 10, 32: 9.5, 33: 9, 35: 8.5, 36: 8, 37: 7, 38: 6.5, 39: 5.5, 40: 4.5, 41: 3.5, 42: 2.5, 43: 1}
    wL = {26: 11, 28: 10, 29: 9.5, 31: 9, 33: 8.5, 34: 8, 36: 7, 37: 6.5, 38: 5.5, 39: 4.5, 40: 3.5, 41: 2.5, 42: 1.5, 43: 0}
    zf = {26: 7, 28: 6.5, 35: 7.5, 37: 8.5, 39: 6.5, 40: 5.5, 41: 4, 42: 2.5, 43: 0}
    # the back is three cloth panels, each stepping at its own rows, so no step line runs
    # all the way across: a raised spine panel (off-centre to +X, under the peak) between two
    # side panels that fall away at different rates
    zbL = {26: -8.5, 28: -8, 30: -7.5, 33: -6.5, 35: -6, 37: -5, 39: -4.5, 41: -3.5, 42: -3, 43: -2.5}
    zbC = {26: -9, 31: -8.5, 34: -8, 36: -7, 38: -6, 40: -5, 42: -4, 43: -3}
    zbR = {26: -8.5, 29: -7.5, 32: -7, 34: -6.5, 36: -5.5, 39: -4.5, 41: -3.5, 43: -2.5}
    spine = (QX > -1) & (QX < 3)
    WL, WR, ZF = (steps(t, QY) for t in (wL, wR, zf))
    ZB = np.where(spine, steps(zbC, QY), np.where(QX < 0, steps(zbL, QY), steps(zbR, QY)))
    dl, dr, df, db = QX + WL, WR - QX, ZF - QZ, QZ - ZB
    hood = (within(QY, 26, 44) & (dl > 0) & (dr > 0) & (df > 0) & (db > 0)
            & (np.minimum(dl, dr) + df > 2.5) & (np.minimum(dl, dr) + db > 3.5))
    # value per plane: lit +X/front planes, darker -X/back planes, one crease between back panels
    fill(hood, "hood", "head", 0)
    fill(hood & (QX < -0.5) & (QZ < 0), "hood", "head", -1)
    fill(hood & (QX > 0.5) & (QZ > -2), "hood", "head", 1)
    fill(hood & within(QY, 37, 39) & (QZ > 4), "hood", "head", 1)            # brow
    # overlapping back planes: the +X panel sits one fine voxel proud of the -X panel
    # the spine panel overlaps the -X panel: one fine voxel proud along its back and its -X edge
    sp = hood & spine & (db < 1) & within(QY, 27, 42)
    fill(np.roll(sp, -1, axis=2) & (m.mat < 0), "hood", "head", 0)
    edge = hood & spine & (QX < 0) & (QZ < -2) & within(QY, 28, 42)
    fill(np.roll(edge, -1, axis=0) & (m.mat < 0), "hood", "head", 1)
    # the hood's lower edge hangs over the back of the scarf as one pointed flap, off-centre
    flap = within(QY, 24, 29) & (np.abs(QX - 1) < (QY - 23) * 1.6) & within(QZ, -11, -9)
    fill(flap, "hood", "head", 0)
    fill(flap & (QX < 1), "hood", "head", -1)
    # a fold plane laid over the -X side, offset one fine voxel
    side = hood & (dl < 1) & within(QY, 29, 36) & within(QZ, -6, 1)
    fill(np.roll(side, -1, axis=0) & (m.mat < 0), "hood", "head", -1)

    # face opening: an inverted trapezoid, 3 blocks deep, dark walls, black back plane
    hw = np.where(QY < 37, 3.5 + (QY - 28) * (1 / 4), np.where(QY < 38, 4, 2.5))
    mouth_of = within(QY, 28, 38) & (np.abs(QX) < hw)
    carve(hood & mouth_of & (QZ > 2))
    wall = hood & within(QY, 27, 40) & (np.abs(QX) < hw + 1) & (QZ > 1) & (QZ < 6)
    fill(wall & is_("hood"), "inner", "head")
    fill(mouth_of & within(QZ, 0, 2) & (m.mat >= 0), "void", "head")
    for x0 in (-4 - 1 / 3, 2 + 2 / 3):                                          # eyes, slightly tall
        eye = (X > x0) & (X < x0 + 5 / 3) & (Y > 31 + 1 / 3) & (Y < 33 + 1 / 3) & (Z > 2) & (Z < 2 + 1 / s)
        fill(eye, "eye", "head")

    # ---- scarf: a draped wrap, not a drum. The top edge rises and falls by sector and covers the
    # bottom of the face opening; the drape steps down over the shoulders with a ragged lower edge;
    # a big stepped V hangs down the chest and one notched tail hangs down the back
    sec = np.floor((np.arctan2(QX, QZ) + np.pi) / (2 * np.pi) * 12).astype(int) % 12   # 0 = back, 6 = front
    top_t = np.array([29, 29, 30, 29, 30, 30, 30, 30, 29, 30, 29, 30])[sec]
    bot_t = np.array([21, 22, 21, 22, 22, 21, 21, 22, 22, 21, 22, 21])[sec]
    wrap = (QY >= 25) & (QY < top_t) & octagon(QX - 0.5, QZ - 0.5, 11.5, 9.5, 3.5)
    out = np.maximum(np.abs(QX - 0.5) - 9, 0)                                  # past the neck, over the shoulder
    drape = ((QY >= bot_t + out * 0.8) & (QY < 25 - out * 0.9)
             & octagon(QX - 0.5, QZ - 0.5, 12.5, 10, 4))
    fill(drape, "scarf", "torso", 0)
    fill(drape & (QY < bot_t + out * 0.8 + 1), "scarf", "torso", -1)           # shadowed underside row
    fill(wrap, "scarf", "torso", 0)
    fill(wrap & (QY >= top_t - 1) & (QZ > -2), "scarf", "torso", 1)            # lit top lip toward the light
    hv = np.floor((QY - 12) * 0.6)
    vee = within(QY, 12, 22) & (np.abs(QX - 0.5) < hv) & within(QZ, 0, np.where(QY > 18, 8.5, 7.5))
    fill(vee, "scarf", "torso", 0)
    fill(vee & (QX > 0.5), "scarf", "torso", 1)                                # +X half faces the lantern
    fill(vee & (np.abs(QX - 0.5 + (QY - 17) * 0.4) < 0.8), "scarf", "torso", -1)  # one diagonal fold
    # slanted fold values around the wrap: broad bands, so it reads as cloth, not corrugation
    th = (np.arctan2(QX, QZ) + np.pi) / (2 * np.pi) * 8 + (QY - 22) * 0.45
    band = np.array([0, 0, -1, 0, 1, 1, 0, -1])[np.floor(th).astype(int) % 8]
    roll = is_("scarf") & (wrap | drape) & (QY < top_t - 1)
    m.lvl[roll] = np.clip(np.broadcast_to(band, m.shape)[roll] + m.lvl[roll], -2, 1)
    # back tail, off-centre to -X, wide, with a notched end
    tw = np.where(QY > 14, 2.5, 2.0)
    tail = within(QY, 10, 23) & (np.abs(QX + 3) < tw) & within(QZ, -8, -5) & ~(within(QY, 10, 12) & within(QX, -3.5, -2.5))
    fill(tail, "scarf", "torso", 0)
    fill(tail & (QX < -3.5), "scarf", "torso", -1)
    fill(tail & (QY < 13), "scarf", "torso", -1)

    # ---- value grouping: a lit top layer on every step of cloth, wood and leather
    occ = m.mat >= 0
    top = occ & ~np.concatenate([occ[:, 1:, :], np.zeros_like(occ[:, :1, :])], axis=1)
    lit = np.isin(m.mat, [MATS.index(n) for n in ("robe", "scarf", "wood", "leather", "boots")])
    m.lvl = m.lvl.astype(int) + (top & lit)
    return m


def export(m: Model, out: Path, size: float, px: int):
    palette, lut = [], {}
    for name, ramp in RAMPS.items():
        for j, h in enumerate(ramp):
            lut[(MATS.index(name), j)] = len(palette)
            palette.append({"hex": h, "emission": 0.0})
    for name, (h, e) in FLAT.items():
        lut[(MATS.index(name), 0)] = len(palette)
        palette.append({"hex": h, "emission": e})

    index = np.full(m.shape, -1, dtype=np.int32)
    for mi, name in enumerate(MATS):
        sel = m.mat == mi
        if not sel.any():
            continue
        if name in RAMPS:
            j = np.clip(BASE[name] + m.lvl[sel], 0, len(RAMPS[name]) - 1)
            index[sel] = np.array([lut[(mi, v)] for v in range(len(RAMPS[name]))])[j]
        else:
            index[sel] = lut[(mi, 0)]

    occ = np.argwhere(index >= 0)
    lo, hi = occ.min(0), occ.max(0) + 1
    index = index[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    parts = m.part[lo[0]:hi[0], lo[1]:hi[1], lo[2]:hi[2]]
    used = np.unique(index[index >= 0])
    remap = np.full(len(palette), -1)
    remap[used] = np.arange(len(used))
    index = np.where(index >= 0, remap[np.maximum(index, 0)], -1)
    palette = [palette[o] for o in used]

    dims = np.array(index.shape)
    # bottom-centre origin on the body axis (staff and lantern stick out sideways)
    offset = np.array([lo[0] + DX0 * m.s, 0, lo[2] + DZ0 * m.s])
    coords = np.argwhere(index >= 0)
    V, F, C, N, E = build_faces(index, offset, size, palette)
    out.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "format": "runematch-vox/1",
        "source": "wanderer_build.py",
        "size": size,
        "dims": dims.tolist(),
        "palette": palette,
        "voxels": np.concatenate([coords + offset, index[tuple(coords.T)][:, None]], 1).tolist(),
        "part_names": PARTS,
        "parts": parts[tuple(coords.T)].tolist(),
    }
    Path(f"{out}.vox.json").write_text(json.dumps(doc, separators=(",", ":")))
    mesh = trimesh.Trimesh(V, F, vertex_normals=N, process=False)
    mesh.visual = trimesh.visual.ColorVisuals(mesh, vertex_colors=(np.clip(C, 0, 1) * 255).astype(np.uint8))
    mesh.export(f"{out}.glb")
    render_views(V, F, C, N, E, Path(f"{out}.png"), px=px)
    render_views(V, F, C, N, E, Path(f"{out}_night.png"), px=px, night=True)
    pc = np.bincount(parts[tuple(coords.T)], minlength=len(PARTS))
    print(json.dumps({"out": str(out), "size": size, "dims": dims.tolist(), "dims_m": (dims * size).round(2).tolist(),
                      "voxels": int(len(coords)), "faces": int(len(F) // 2), "colours": len(palette),
                      "parts": dict(zip(PARTS, pc.tolist()))}))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out", type=Path)
    ap.add_argument("--size", type=float, default=0.03, help="voxel edge in metres; must divide 0.03")
    ap.add_argument("--px", type=int, default=0, help="render size per view (default scales with detail)")
    a = ap.parse_args()
    s = round(DESIGN / a.size)
    if s < 1 or abs(s * a.size - DESIGN) > 1e-6:
        sys.exit("--size must divide 0.03 (0.03, 0.015, 0.01, 0.0075 …)")
    export(build(s), a.out, a.size, a.px or min(900, 420 + 160 * (s - 1)))


if __name__ == "__main__":
    main()
