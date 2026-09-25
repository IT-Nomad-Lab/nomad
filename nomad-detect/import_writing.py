"""Turn samples/WRITING_TASKS.md into calibration samples, and JDE-check each one.

Josias writes between the markers under each task. This copies every filled piece to
samples/human/josias_NN.txt (the prompt text never goes in, only what he wrote) and runs the
style linter on it, so JDE findings show up while the piece is still his to fix.

The findings are advisory. They are for Josias to fix BY HAND. A model that "repairs" a line
turns a human sample into a mixed one, and a mixed human pile is the exact defect this corpus
exists to remove.

  python import_writing.py              # import + JDE report
  python import_writing.py --drop-seed  # also move the Paul Graham / Spolsky stand-ins out of
                                        # human/, once enough of your own pieces are in
"""
import argparse
import pathlib
import re
import shutil

import style

HERE = pathlib.Path(__file__).parent
TASKS = HERE / "samples" / "WRITING_TASKS.md"
HUMAN = HERE / "samples" / "human"
SEED = HERE / "samples" / "human-seed"
TARGET = 20
MIN_WORDS = 150          # the doc's floor; Binoculars itself refuses anything under 48 tokens

SECTION = re.compile(r"^## (\d{2}) · (.+?)$(.*?)(?=^## \d{2} · |\Z)", re.M | re.S)
BODY = re.compile(r"<!-- write below -->(.*?)<!-- end -->", re.S)


def pieces(text: str) -> list[tuple[str, str, str]]:
    out = []
    for num, title, block in SECTION.findall(text):
        m = BODY.search(block)
        body = m.group(1).strip() if m else ""
        if body:
            out.append((num, title.strip(), body))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--drop-seed", action="store_true")
    a = ap.parse_args()

    found = pieces(TASKS.read_text(encoding="utf-8"))
    HUMAN.mkdir(parents=True, exist_ok=True)
    written = {f"josias_{n}.txt" for n, _, _ in found}
    for stale in HUMAN.glob("josias_*.txt"):          # a piece you emptied again leaves the pile
        if stale.name not in written:
            stale.unlink()

    for num, title, body in found:
        (HUMAN / f"josias_{num}.txt").write_text(body + "\n", encoding="utf-8")
        r = style.lint(body)
        words = len(body.split())
        flag = "  ⚠ under 150 words" if words < MIN_WORDS else ""
        print(f"\n{num} · {title}\n   {words} words · JDE score {r['score']} ({r['band']}){flag}")
        seen: dict[str, int] = {}
        for f in r["findings"]:
            if f["severity"] not in ("high", "medium"):
                continue
            seen[f["rule"]] = seen.get(f["rule"], 0) + 1
            if seen[f["rule"]] == 1:                  # first hit per rule, then a count
                print(f"   - line {f['line']} [{f['rule']}] {f['excerpt']}\n     {f['fix']}")
        for rule, c in seen.items():
            if c > 1:
                print(f"   ({rule}: {c - 1} more)")

    n = len(found)
    print(f"\n{n} of {TARGET} written → samples/human/josias_NN.txt")

    if a.drop_seed:
        SEED.mkdir(exist_ok=True)
        moved = [f for f in HUMAN.iterdir() if f.is_file() and not f.name.startswith("josias_")]
        for f in moved:
            shutil.move(str(f), SEED / f.name)
        print(f"moved {len(moved)} stand-in essays to samples/human-seed/")

    if n >= TARGET:
        print("Enough to refit. Drop the stand-ins first if you haven't:\n"
              "  .venv/bin/python import_writing.py --drop-seed\n"
              "  .venv/bin/python calibrate.py --human samples/human --ai samples/ai")


if __name__ == "__main__":
    main()
