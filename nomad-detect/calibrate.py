"""Fit the detector's thresholds to YOUR model pair and YOUR writing.

A Binoculars score is only meaningful against a threshold, and the published number (0.9015) was
fitted to the Falcon-7B pair on the paper's corpus. Swap the models or change the domain and the
line moves. Running uncalibrated is the single easiest way to get a confident wrong answer, so this
script exists and the service reports `calibrated: false` until you run it.

Feed it two piles of text:

  human   your own writing. Real posts, real emails, real docs. Twenty is enough to be useful.
  ai      the same kind of thing produced by the models you actually write with.

It scores every sample, then fits two lines:

  accuracy   the threshold with the best overall hit rate. Day-to-day default.
  low_fpr    the stricter line where at most 1% of YOUR writing gets called machine. Use this one
             when a false accusation costs something.

  python calibrate.py --human samples/human --ai samples/ai
  python calibrate.py --report            # show the current thresholds and the fit that made them
"""
import argparse
import datetime
import json
import pathlib
from typing import Any

import binoculars
import style

THRESHOLDS = binoculars.THRESHOLDS
FPR_TARGET = 0.01


FPR_CAP = 0.25


def _fit_thresholds(human: list[float], ai: list[float]) -> dict[str, float]:
    """Predict 'ai' when score < threshold, so sweeping every observed score finds the best split.

    The sweep is CAPPED at FPR_CAP. Unconstrained, a class of AI samples the pair cannot detect
    (frontier models scoring above human writing) drags the best-accuracy threshold up past the
    human mean, and the detector starts calling your own writing machine-made. A detector that
    cries wolf on the operator's real work gets ignored within a week, which makes it worse than
    no detector. Better to miss the models we know we miss, and say so.
    """
    candidates = sorted(set(human + ai))
    best_t, best_acc = candidates[0], -1.0
    for t in candidates:
        fpr = sum(1 for s in human if s < t) / len(human)
        if fpr > FPR_CAP:
            continue
        correct = sum(1 for s in ai if s < t) + sum(1 for s in human if s >= t)
        acc = correct / (len(ai) + len(human))
        if acc > best_acc:
            best_acc, best_t = acc, t
    # low-FPR line: the highest threshold that still misflags at most FPR_TARGET of human samples
    hs = sorted(human)
    allowed = int(len(hs) * FPR_TARGET)
    low_fpr = hs[allowed] if allowed < len(hs) else hs[0]
    tpr = sum(1 for s in ai if s < low_fpr) / max(len(ai), 1)
    return {"accuracy": round(best_t, 4), "low_fpr": round(min(low_fpr, best_t), 4),
            "fit_accuracy": round(best_acc, 4), "tpr_at_low_fpr": round(tpr, 4),
            "fpr_at_accuracy": round(sum(1 for s in human if s < best_t) / len(human), 3),
            "tpr_at_accuracy": round(sum(1 for s in ai if s < best_t) / max(len(ai), 1), 3)}


def auc(human: list[float], ai: list[float]) -> float:
    """P(a human sample outscores an AI sample). 0.5 means the detector is blind to that source."""
    if not human or not ai:
        return float("nan")
    wins = sum((h > a) + 0.5 * (h == a) for h in human for a in ai)
    return round(wins / (len(human) * len(ai)), 3)


def fit(samples: list[dict[str, Any]], save: bool = True) -> dict[str, Any]:
    with binoculars.admitted():
        return _fit(samples, save)


def _fit(samples: list[dict[str, Any]], save: bool) -> dict[str, Any]:
    eng = binoculars.engine()
    scored, skipped = {"human": [], "ai": []}, []
    by_source: dict[str, list[float]] = {}
    # The style linter is scored on the same corpus, because the verdict caveat tells callers to
    # fall back on it for sources Binoculars misses. That advice is only true where it is measured.
    sty_human: list[float] = []
    sty_by_source: dict[str, list[float]] = {}
    for s in samples:
        ss = style.lint(s["text"])["score"]
        if s["label"] == "human":
            sty_human.append(ss)
        elif s.get("source"):
            sty_by_source.setdefault(s["source"], []).append(ss)
        r = eng.score(s["text"])
        if r.get("ok"):
            scored[s["label"]].append(r["score"])
            if s["label"] == "ai" and s.get("source"):
                by_source.setdefault(s["source"], []).append(r["score"])
        else:
            skipped.append(r.get("error"))
    if len(scored["human"]) < 2 or len(scored["ai"]) < 2:
        return {"ok": False, "error": "need at least 2 scorable samples per label",
                "scored": {k: len(v) for k, v in scored.items()}, "skipped": skipped}

    t = _fit_thresholds(scored["human"], scored["ai"])
    entry = {**t, "n_samples": len(scored["human"]) + len(scored["ai"]),
             "n_human": len(scored["human"]), "n_ai": len(scored["ai"]),
             "human_mean": round(sum(scored["human"]) / len(scored["human"]), 4),
             "ai_mean": round(sum(scored["ai"]) / len(scored["ai"]), 4),
             "fitted": datetime.date.today().isoformat()}
    if by_source:
        # Per-source separation. This is the honest part: it names the models this pair cannot
        # tell from human writing, and the service repeats it on every verdict.
        entry["auc_by_source"] = {k: auc(scored["human"], v) for k, v in sorted(by_source.items())}
        entry["blind_to"] = [k for k, a in entry["auc_by_source"].items() if a < 0.65]
        if sty_human and sty_by_source:
            # Same measure on the style channel (higher score = more human, so the direction of
            # auc() is unchanged). `blind_to_both` is the set nothing here can catch: text that
            # was written to pass the very linter being used to judge it cannot be judged by it.
            entry["style_auc_by_source"] = {k: auc(sty_human, v)
                                            for k, v in sorted(sty_by_source.items())}
            entry["blind_to_both"] = sorted(
                k for k in entry["blind_to"] if entry["style_auc_by_source"].get(k, 1.0) < 0.65)
    if save:
        data = {}
        if THRESHOLDS.exists():
            try:
                data = json.loads(THRESHOLDS.read_text())
            except Exception:
                data = {}
        data[eng.pair] = entry
        THRESHOLDS.write_text(json.dumps(data, indent=2) + "\n")
    return {"ok": True, "pair": eng.pair, "saved": save, **entry,
            "separation": round(entry["human_mean"] - entry["ai_mean"], 4), "skipped": skipped}


def _load_dir(d: str) -> list[tuple[str, str]]:
    """(text, source) pairs. Source comes from the filename prefix before the first underscore,
    so naming a file gpt_03.txt is all it takes to get a per-model breakdown."""
    p = pathlib.Path(d)
    return [(f.read_text(errors="ignore"), f.name.split("_")[0]) for f in sorted(p.rglob("*"))
            if f.is_file() and f.suffix.lower() in (".txt", ".md")]


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--human", help="directory of human-written .txt/.md samples")
    ap.add_argument("--ai", help="directory of model-written .txt/.md samples")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--report", action="store_true", help="print current thresholds and exit")
    a = ap.parse_args()

    if a.report or not (a.human and a.ai):
        print(json.dumps(binoculars.thresholds(), indent=2))
        if THRESHOLDS.exists():
            print(THRESHOLDS.read_text())
        return

    samples = ([{"text": t, "label": "human"} for t, _ in _load_dir(a.human)] +
               [{"text": t, "label": "ai", "source": src} for t, src in _load_dir(a.ai)])
    print(f"scoring {len(samples)} samples through {binoculars.OBSERVER} | "
          f"{binoculars.PERFORMER} …")
    print(json.dumps(fit(samples, save=not a.no_save), indent=2))


if __name__ == "__main__":
    main()
