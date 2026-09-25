"""CLI: run a draft through the detector before it ships.

  python check.py post.md              full check (GPU) — exit 0 pass, 1 revise, 2 fail
  python check.py --lint post.md       style only, no model, instant
  cat draft.txt | python check.py -    read stdin
  python check.py --json post.md       machine-readable, for a git hook or a CI step

The exit code is the point: wire it into a pre-commit hook and a piece of writing cannot land
until it reads human.
"""
import argparse
import json
import sys

EXIT = {"pass": 0, "revise": 1, "fail": 2}
BAR = "─" * 72


def _fmt(res: dict) -> str:
    sty, bino = res["style"], res.get("binoculars", {})
    m = sty["metrics"]
    out = [BAR, f"  VERDICT   {res['verdict'].upper()}  ({res['guidance']})", BAR]
    if bino.get("ok"):
        t = bino["thresholds"]
        cal = "calibrated" if t.get("calibrated") else "UNCALIBRATED defaults"
        out.append(f"  statistical  {bino['score']}  [{bino['band']}]  "
                   f"threshold {t['accuracy']} ({cal})")
        out.append(f"               {bino['tokens']} tokens in {bino['chunks']} chunk(s), "
                   f"worst {bino['most_machine_chunk']}")
    else:
        out.append(f"  statistical  unavailable: {bino.get('error')}")
    out.append(f"  style        {sty['score']}/100  [{sty['band']}]  "
               f"{sty['summary']['total']} findings ({sty['summary']['high']} high)")
    out.append(f"  rhythm       burstiness {m['burstiness']} · sentences {m['mean_sentence_len']}"
               f"±{m['sd_sentence_len']} words · {m['short_sentences']} short")
    out.append(f"  texture      {m['concrete_per_100w']} specifics/100w · "
               f"{m['contractions_per_100w']} contractions/100w · TTR {m['type_token_ratio']}")
    if sty["findings"]:
        out += [BAR, "  FIX THESE"]
        for f in sty["findings"][:12]:
            out.append(f"  L{f['line']:<4} {f['severity']:<6} {f['rule']}")
            out.append(f"        “{f['excerpt'][:64]}”")
            out.append(f"        → {f['fix']}")
        if len(sty["findings"]) > 12:
            out.append(f"  … and {len(sty['findings']) - 12} more")
    out.append(BAR)
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", help="path to the draft, or - for stdin")
    ap.add_argument("--lint", action="store_true", help="style only, skip the GPU models")
    ap.add_argument("--external", action="store_true", help="also call configured paid checkers")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    text = sys.stdin.read() if a.file == "-" else open(a.file, errors="ignore").read()

    import style
    if a.lint:
        sty = style.lint(text)
        res = {"ok": True, "verdict": "pass" if sty["band"] == "human" else
               "revise" if sty["band"] == "borderline" else "fail",
               "guidance": "style only", "style": sty, "binoculars": {"ok": False,
               "error": "skipped (--lint)"}}
    else:
        import binoculars
        from server import _verdict
        sty = style.lint(text)
        bino = binoculars.engine().score(text)
        res = {"ok": True, **_verdict(bino, sty), "style": sty, "binoculars": bino}
        if a.external:
            import external
            res["external"] = external.check(text)

    print(json.dumps(res, indent=2) if a.json else _fmt(res))
    sys.exit(EXIT[res["verdict"]])


if __name__ == "__main__":
    main()
