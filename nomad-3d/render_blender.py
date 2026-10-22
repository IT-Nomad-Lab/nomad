#!/usr/bin/env python3
"""Cinematic turnaround of a runematch-vox/1 model in Blender Cycles (nomad-pm #1000).

The voxel faces come from voxelize.build_faces, so the mesh is the same one the game gets. The
lantern's emissive voxels light the scene for real. On top of that, a warm point light sits in
the lantern, with a cool dim fill and a rim light that are linked to the character only, so the floor gets
the lantern alone. Dark background.

Usage: render_blender.py model.vox.json out.png [--px 400] [--samples 96] [--views front,back,…]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from render6 import TURNAROUND, load  # noqa: E402

BLENDER = Path.home() / "bin/blender"

SCENE = r'''
import bpy, math, sys
import numpy as np
from mathutils import Vector
a = sys.argv[sys.argv.index("--") + 1:]
d = np.load(a[0]); out, px, samples = a[1], int(a[2]), int(a[3]); yaws = [float(v) for v in a[4].split(",")]
V, F, C, E = d["V"], d["F"], d["C"], d["E"]
bpy.ops.wm.read_factory_settings(use_empty=True)
sc = bpy.context.scene
me = bpy.data.meshes.new("vox")
me.from_pydata(V.tolist(), [], F.tolist())
col = me.color_attributes.new("col", "FLOAT_COLOR", "POINT")
col.data.foreach_set("color", np.concatenate([np.clip(C[:, :3], 0, 1) ** 2.2, np.ones((len(C), 1))], 1).ravel().astype(np.float32))
ecol = me.color_attributes.new("ecol", "FLOAT_COLOR", "POINT")
lin = np.clip(C[:, :3], 0, 1) ** 2.2
sat = lin                                                                # the palette's own orange
ecol.data.foreach_set("color", np.concatenate([sat, np.ones((len(C), 1))], 1).ravel().astype(np.float32))
emi = me.attributes.new("emi", "FLOAT", "POINT")
emi.data.foreach_set("value", E.astype(np.float32))
ob = bpy.data.objects.new("vox", me); sc.collection.objects.link(ob)
mat = bpy.data.materials.new("vox"); mat.use_nodes = True; nt = mat.node_tree
bs = nt.nodes["Principled BSDF"]
ca = nt.nodes.new("ShaderNodeAttribute"); ca.attribute_name = "col"
ea = nt.nodes.new("ShaderNodeAttribute"); ea.attribute_name = "emi"
# emissive voxels get a black diffuse base, so nearby lights can't wash their colour out
mix = nt.nodes.new("ShaderNodeMix"); mix.data_type = "RGBA"; mix.clamp_factor = True
mix.inputs["B"].default_value = (0, 0, 0, 1)
nt.links.new(ca.outputs["Color"], mix.inputs["A"])
eca = nt.nodes.new("ShaderNodeAttribute"); eca.attribute_name = "ecol"
nt.links.new(eca.outputs["Color"], bs.inputs["Emission Color"])
mul = nt.nodes.new("ShaderNodeMath"); mul.operation = "MULTIPLY"; mul.inputs[1].default_value = 1.0
nt.links.new(ea.outputs["Fac"], mix.inputs["Factor"]); nt.links.new(mix.outputs["Result"], bs.inputs["Base Color"])
nt.links.new(ea.outputs["Fac"], mul.inputs[0]); nt.links.new(mul.outputs[0], bs.inputs["Emission Strength"])
bs.inputs["Roughness"].default_value = 0.85
ob.data.materials.append(mat)
lo, hi = V.min(0), V.max(0); h = hi[1] - lo[1]
em = E > 0
lan = V[em].mean(0) if em.any() else np.array([0.15, 0.4, 0.05])
# Y-up model -> Blender Z-up: (x, y, z) -> (x, -z, y)
ob.rotation_euler = (math.pi / 2, 0, 0)
def bl(p): return Vector((p[0], -p[2], p[1]))
# floor and world
bpy.ops.mesh.primitive_plane_add(size=60, location=(0, 0, lo[1]))
fm = bpy.data.materials.new("floor"); fm.use_nodes = True
fm.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value = (0.008, 0.007, 0.008, 1)
fm.node_tree.nodes["Principled BSDF"].inputs["Roughness"].default_value = 0.75
# fade the floor into the background colour with distance, so there is no hard horizon
BG = (0.004, 0.004, 0.006, 1)
fn = fm.node_tree.nodes; fl = fm.node_tree.links
geo = fn.new("ShaderNodeNewGeometry")
ln = fn.new("ShaderNodeVectorMath"); ln.operation = "LENGTH"
mr = fn.new("ShaderNodeMapRange"); mr.inputs["From Min"].default_value = h * 1.2; mr.inputs["From Max"].default_value = h * 4.0
bg = fn.new("ShaderNodeEmission"); bg.inputs["Color"].default_value = BG; bg.inputs["Strength"].default_value = 1.0
mx = fn.new("ShaderNodeMixShader")
fl.new(geo.outputs["Position"], ln.inputs[0]); fl.new(ln.outputs["Value"], mr.inputs["Value"])
fl.new(mr.outputs["Result"], mx.inputs["Fac"])
fl.new(fn["Principled BSDF"].outputs["BSDF"], mx.inputs[1]); fl.new(bg.outputs["Emission"], mx.inputs[2])
fl.new(mx.outputs["Shader"], fn["Material Output"].inputs["Surface"])
bpy.context.object.data.materials.append(fm)
w = bpy.data.worlds.new("w"); sc.world = w; w.use_nodes = True
w.node_tree.nodes["Background"].inputs["Color"].default_value = BG
def light(kind, loc, energy, color, size=0.5, rot=None):
    l = bpy.data.lights.new(kind, kind); l.energy = energy; l.color = color
    if kind in ("POINT", "SPOT"): l.shadow_soft_size = size
    if kind == "AREA": l.size = size
    o = bpy.data.objects.new(kind, l); o.location = loc; sc.collection.objects.link(o)
    if rot is not None:
        o.rotation_euler = (Vector((0, 0, h * 0.6)) - o.location).to_track_quat("-Z", "Y").to_euler()
    return o
# rig parented to an empty so lights turn with the character relative to camera
rig = bpy.data.objects.new("rig", None); sc.collection.objects.link(rig)
ob.parent = rig
# the lantern body is solid, so a light at its centre is shadowed: put the warm lights just
# outside the glass, one on the body side (lights the robe) and one in front (lights the floor)
elo, ehi = V[em].min(0), V[em].max(0)
gap = 0.06
for p, e in (((elo[0] - gap, lan[1], lan[2]), 9), ((lan[0], lan[1], ehi[2] + gap), 7),
             ((ehi[0] + gap, lan[1], lan[2]), 4), ((lan[0], lan[1], elo[2] - gap), 5)):
    l = light("POINT", bl(np.array(p)), e, (1.0, 0.4, 0.08), 0.03); l.parent = rig
cam = bpy.data.cameras.new("cam"); cam.lens = 85
co = bpy.data.objects.new("cam", cam); sc.collection.objects.link(co); sc.camera = co
dist = h * 4.0                  # lights are placed relative to this; the camera is closer
cam_dist, cam_z, aim_z = h * 2.9, h * 0.62, h * 0.52   # character fills ~80% of the frame height
co.location = (0, -cam_dist, cam_z)
co.rotation_euler = (math.pi / 2 - math.atan2(cam_z - aim_z, cam_dist), 0, 0)
# the key, fill and rim light only the character (Cycles light linking): the floor gets the
# lantern alone, a warm pool fading to black, as in the reference sheet
char = bpy.data.collections.new("character"); sc.collection.children.link(char); char.objects.link(ob)
for args in (((dist * 0.5, -dist * 0.6, h * 1.8), 110, (0.75, 0.82, 1.0), 1.2),    # cool key
             ((-dist * 0.6, -dist * 0.5, h * 1.2), 25, (0.6, 0.65, 0.8), 2.0),     # soft fill
             ((-dist * 0.3, dist * 0.8, h * 1.6), 60, (1.0, 0.75, 0.5), 0.6)):     # warm rim from behind
    light("AREA", *args, True).light_linking.receiver_collection = char
sc.render.engine = "CYCLES"; sc.cycles.samples = samples; sc.cycles.use_denoising = True
prefs = bpy.context.preferences.addons["cycles"].preferences
for dev in ("OPTIX", "CUDA"):
    try:
        prefs.compute_device_type = dev; prefs.get_devices()
        if any(x.type == dev for x in prefs.devices):
            for x in prefs.devices: x.use = x.type == dev
            sc.cycles.device = "GPU"; break
    except Exception: pass
sc.view_settings.view_transform = "Standard"; sc.view_settings.look = "None"; sc.view_settings.exposure = 0.15
sc.render.resolution_x, sc.render.resolution_y = px, int(px * 1.36)
# bloom: bright lantern glass and eyes bleed light, as in the reference sheet
sc.use_nodes = True
ct = sc.node_tree
rl = ct.nodes.new("CompositorNodeRLayers")
comp = ct.nodes.get("Composite") or ct.nodes.new("CompositorNodeComposite")
gl = ct.nodes.new("CompositorNodeGlare")
for k, v in (("glare_type", "BLOOM"), ("quality", "HIGH"), ("threshold", 0.9), ("size", 8), ("mix", 0.0)):
    try:
        setattr(gl, k, v)
    except Exception:
        if k == "glare_type": gl.glare_type = "FOG_GLOW"
for k, v in (("Threshold", 1.0), ("Size", 0.5), ("Strength", 0.5)):
    if k in gl.inputs: gl.inputs[k].default_value = v
ct.links.new(rl.outputs["Image"], gl.inputs["Image"])
ct.links.new(gl.outputs["Image"], comp.inputs["Image"])
print("GLARE", gl.glare_type)
for i, yaw in enumerate(yaws):
    rig.rotation_euler = (0, 0, math.radians(yaw))
    sc.render.filepath = f"{out}_{i}.png"
    bpy.ops.render.render(write_still=True)
print("DEVICE", sc.cycles.device)
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--px", type=int, default=400)
    ap.add_argument("--samples", type=int, default=96)
    ap.add_argument("--views", default=",".join(TURNAROUND))
    a = ap.parse_args()
    from PIL import Image
    V, F, C, N, E = load(a.model)
    names = a.views.split(",")
    with tempfile.TemporaryDirectory() as td:
        npz, py = Path(td) / "m.npz", Path(td) / "scene.py"
        np.savez(npz, V=V, F=F, C=C, E=E)
        py.write_text(SCENE)
        yaws = ",".join(str(TURNAROUND[n]) for n in names)
        r = subprocess.run([str(BLENDER), "-b", "--factory-startup", "-P", str(py), "--",
                            str(npz), str(Path(td) / "v"), str(a.px), str(a.samples), yaws],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(r.stdout[-3000:] + r.stderr[-3000:])
        print([l for l in r.stdout.splitlines() if l.startswith(("DEVICE", "GLARE"))])
        ims = [Image.open(Path(td) / f"v_{i}.png").convert("RGB") for i in range(len(names))]
    sheet = Image.new("RGB", (sum(i.width for i in ims), ims[0].height))
    x = 0
    for im in ims:
        sheet.paste(im, (x, 0)); x += im.width
    sheet.save(a.out)


if __name__ == "__main__":
    main()
