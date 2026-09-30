#!/usr/bin/env python3
"""Image → textured GLB with TRELLIS.2 (runs in ~/vendor/trellis2/.venv).

  ~/vendor/trellis2/.venv/bin/python trellis_gen.py a.png [b.png …] --out-dir out/ [--res 512|1024|1024_cascade|1536_cascade]

Loads the local weights copy (~/vendor/trellis2/weights), whose pipeline.json uses the MIT BiRefNet
for background removal, not the non-commercial RMBG-2.0.
"""
import os
os.environ.setdefault("ATTN_BACKEND", "xformers")
os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.expanduser("~/vendor/trellis2"))

import torch
from PIL import Image

# xformers turns FlashAttention-3 on for any capability >= 9.0, but its FA3 kernels are Hopper-only
# (sm_90a): on the 5090 (sm_120) the first call dies with "flash_fwd_launch_template.h: invalid
# argument". Fall back to FA2 / cutlass, which both run on Blackwell.
if torch.cuda.get_device_capability() >= (10, 0):
    import xformers.ops.fmha.dispatch as _xdispatch
    _xdispatch._set_use_fa3(False)


from trellis2.pipelines import Trellis2ImageTo3DPipeline
import o_voxel


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+", type=Path, help="in.png [in2.png …]; outputs go to --out-dir")
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--res", default="1024_cascade")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--faces", type=int, default=200_000, help="decimation target")
    ap.add_argument("--texture", type=int, default=2048)
    a = ap.parse_args()
    a.out_dir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    pipe = Trellis2ImageTo3DPipeline.from_pretrained(os.path.expanduser("~/vendor/trellis2/weights"))
    pipe.cuda()
    load_s = time.time() - t0

    for img_path in a.images:
        torch.cuda.reset_peak_memory_stats()
        t1 = time.time()
        mesh = pipe.run(Image.open(img_path), seed=a.seed, pipeline_type=a.res)[0]
        gen_s = time.time() - t1
        mesh.simplify(16777216)
        t2 = time.time()
        glb = o_voxel.postprocess.to_glb(
            vertices=mesh.vertices, faces=mesh.faces, attr_volume=mesh.attrs, coords=mesh.coords,
            attr_layout=mesh.layout, voxel_size=mesh.voxel_size, aabb=[[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
            decimation_target=a.faces, texture_size=a.texture, remesh=True, remesh_band=1, remesh_project=0,
        )
        out = a.out_dir / (img_path.stem + ".glb")
        glb.export(str(out))
        print(json.dumps({
            "image": str(img_path), "glb": str(out), "res": a.res,
            "load_s": round(load_s, 1), "generate_s": round(gen_s, 1), "export_s": round(time.time() - t2, 1),
            "peak_vram_gb": round(torch.cuda.max_memory_allocated() / 2**30, 1),
        }), flush=True)
        load_s = 0.0


if __name__ == "__main__":
    main()
