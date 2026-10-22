"""Measure what the detector can and cannot catch, per source model. Run this before you trust it.

A single accuracy number hides the thing that matters: detection gets harder as the writing model
gets better. A pair that catches llama-8b cleanly may be blind to Opus. This script reports the
separation per source, so the limit is on the record instead of in a footnote.

  python evaluate.py                          # current pair, samples/human vs samples/ai
  NOMAD_DETECT_OBSERVER=Qwen/Qwen2.5-3B \
  NOMAD_DETECT_PERFORMER=Qwen/Qwen2.5-3B-Instruct python evaluate.py

Sample files are grouped by filename prefix (fast_, gpt_, opus_), so name them after the model that
wrote them and the per-source table fills itself in.
"""
import pathlib
import statistics
import sys

import binoculars
import style

SAMPLES = pathlib.Path(__file__).with_name("samples")


def auc(pos: list[float], neg: list[float]) -> float:
    """P(a human sample scores above an AI sample). 0.5 = coin flip, 1.0 = perfect separation."""
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def collect(label: str) -> dict[str, list[tuple[float, int]]]:
    eng = binoculars.engine()
    groups: dict[str, list[tuple[float, int]]] = {}
    for f in sorted((SAMPLES / label).glob("*.txt")):
        text = f.read_text(errors="ignore")
        r = eng.score(text)
        if not r.get("ok"):
            print(f"  skipped {f.name}: {r.get('error')}", file=sys.stderr)
            continue
        key = f.name.split("_")[0] if label == "ai" else "human"
        groups.setdefault(key, []).append((r["score"], style.lint(text)["score"]))
    return groups


def main():
    with binoculars.admitted():
        _main()


def _main():
    eng = binoculars.engine()
    human = collect("human").get("human", [])
    ai = collect("ai")
    if not human or not ai:
        print("need samples in samples/human and samples/ai"); return

    hb = [b for b, _ in human]
    hs = [s for _, s in human]
    print(f"\npair: {eng.pair}")
    print(f"{'source':<10} {'n':>3}  {'binoculars':>10} {'AUC':>6}   {'style':>6} {'AUC':>6}")
    print("─" * 52)
    print(f"{'human':<10} {len(hb):>3}  {statistics.fmean(hb):>10.4f} {'—':>6}   "
          f"{statistics.fmean(hs):>6.1f} {'—':>6}")
    for src, rows in sorted(ai.items()):
        b = [x for x, _ in rows]
        s = [x for _, x in rows]
        print(f"{src:<10} {len(b):>3}  {statistics.fmean(b):>10.4f} {auc(hb, b):>6.3f}   "
              f"{statistics.fmean(s):>6.1f} {auc(hs, s):>6.3f}")
    all_ai = [x for rows in ai.values() for x, _ in rows]
    print("─" * 52)
    print(f"{'ALL AI':<10} {len(all_ai):>3}  {statistics.fmean(all_ai):>10.4f} "
          f"{auc(hb, all_ai):>6.3f}")
    print("\nAUC 0.5 = the detector cannot tell that source apart from human writing.")


if __name__ == "__main__":
    main()
