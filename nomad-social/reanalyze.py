"""Re-run vision analysis on a finished batch, from the files already on disk.

No Instagram request is made: metadata, comments, frames and transcripts are read back from the
batch folder. Only items whose analysis failed are retried (pass --all to redo every item), then
report.md / items.json / metrics.csv are rewritten.

Written 2026-09-22 after Gemini's free tier (20 requests/day) ran out 16 items into an 80-link
batch. Pick the model with NOMAD_SOCIAL_VISION_MODELS, e.g. `private` when the GPU is free:

    NOMAD_SOCIAL_VISION_MODELS=private .venv/bin/python reanalyze.py output/<batch> \
        --focus "..." --name "..."
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import analyze
import fetch
import pipeline

FRAME = re.compile(r"media_(\d+)_f\d+_([\d.]+)s\.jpg$")


def visuals(rec: dict) -> list[dict]:
    """Same order and labels media.process() produced."""
    out = []
    frames = sorted((Path(rec["dir"]) / "frames").glob("*.jpg"))
    for m in sorted(rec["media"], key=lambda m: m["index"]):
        if m["type"] == "image":
            out.append({"path": m["path"], "label": f"slide {m['index'] + 1} (image)"})
            continue
        for f in frames:
            g = FRAME.search(f.name)
            if g and int(g.group(1)) == m["index"]:
                out.append({"path": str(f),
                            "label": f"slide {m['index'] + 1} video @ {float(g.group(2)):.1f}s"})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("batch")
    ap.add_argument("--focus", default="")
    ap.add_argument("--name", default="")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    root = Path(a.batch).resolve()
    results = json.loads((root / "items.json").read_text())
    todo = [r for r in results if r.get("media") and (a.all or r["analysis"].get("error"))]
    print(f"{len(todo)} of {len(results)} to analyse with {analyze.VISION_MODELS}", flush=True)

    for n, rec in enumerate(todo, 1):
        t0 = time.time()
        item = fetch.Item(url=rec["url"], kind=rec["kind"], id=rec["id"], dir=Path(rec["dir"]),
                          meta=rec["meta"], media=rec["media"], comments=rec["comments"],
                          errors=rec["errors"])
        processed = {"visuals": visuals(rec), "transcripts": rec.get("transcripts") or [],
                     "videos": rec.get("videos") or []}
        rec["analysis"] = analyze.analyse_item(item, processed)
        (item.dir / "analysis.json").write_text(json.dumps(rec["analysis"], indent=1,
                                                           ensure_ascii=False))
        err = rec["analysis"].get("error")
        print(f"[{n}/{len(todo)}] {'FAIL ' + err[:120] if err else 'ok'} "
              f"{rec['url']} ({time.time() - t0:.0f}s)", flush=True)
        # checkpoint, so a crash halfway keeps what is done
        (root / "items.json").write_text(json.dumps(results, indent=1, ensure_ascii=False,
                                                    default=str))

    res = pipeline.write(root, results, a.focus, a.name, say=print)
    print(json.dumps({k: res[k] for k in ("report", "total", "analysed")}, indent=1))


if __name__ == "__main__":
    main()
