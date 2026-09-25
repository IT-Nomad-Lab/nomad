"""Run a batch without the service.

  .venv/bin/python cli.py links.txt --focus "what hooks work in travel reels"
  .venv/bin/python cli.py https://www.instagram.com/reel/XXXX/ https://www.instagram.com/p/YYYY/

Arguments are URLs or files with one URL per line (# comments allowed). Prints the report path.
"""
import argparse
import json
import sys
from pathlib import Path

import pipeline


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="URLs and/or files of URLs")
    ap.add_argument("--focus", default="", help="what the report should answer")
    ap.add_argument("--name", default="", help="batch name (folder + report title)")
    a = ap.parse_args()
    urls = []
    for x in a.inputs:
        p = Path(x)
        urls += p.read_text().splitlines() if p.is_file() else [x]
    res = pipeline.run(urls, a.focus, a.name, progress=lambda s: print(s, file=sys.stderr))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
