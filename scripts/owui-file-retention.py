#!/usr/bin/env python3
"""Delete old files the NOMAD pipe uploaded into Open WebUI (#1356, docs/prd/owui-inline-files.md §8).

    scripts/owui-file-retention.py                 # dry run: list what would go, delete nothing
    scripts/owui-file-retention.py --days 14       # other cutoff (default 30, or $OWUI_FILE_RETENTION_DAYS)
    scripts/owui-file-retention.py --apply         # delete via DELETE /api/v1/files/{id}
    scripts/owui-file-retention.py --also ID       # add a specific unmarked (pre-0.6.1) file by id

Which files: only those whose meta.data carries `nomad_pipe_upload: true`, the marker
owui/nomad_pipe.py (0.6.1+) sets in `upload_file`. An operator's own upload never has it, so it is
never selected. Files the pipe uploaded before the marker existed have none; they are listed under
"unmarked" and left alone unless named with --also (the operator reads the list and picks).

Deleting is red tier: the default is a dry run, and --apply is the operator's call. Open WebUI's
DELETE removes the DB row, the stored blob and the vector collection, and cascades the chat_file
links. A chat message that already shows the file keeps its markdown link/embed, which then 404s;
that is the point of a retention window.

Mirrors agent-bridge/inbox.py (30 days). Refuses to run against an Open WebUI version other than
the pipe's required_open_webui_version. Admin key, never printed: $OWUI_ADMIN_KEY or
~/.config/nomad/owui-admin-key.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIPE = ROOT / "owui" / "nomad_pipe.py"
BASE = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
KEY_FILE = Path.home() / ".config" / "nomad" / "owui-admin-key"
MARKER = "nomad_pipe_upload"   # keep equal to UPLOAD_MARKER in owui/nomad_pipe.py
PAGE_SIZE = 50                 # Open WebUI's files list page size


def keep_days() -> int:
    return int(os.environ.get("OWUI_FILE_RETENTION_DAYS") or 30)


def is_pipe_upload(f: dict) -> bool:
    return (((f.get("meta") or {}).get("data") or {}).get(MARKER)) is True


def size_of(f: dict) -> int:
    return int((f.get("meta") or {}).get("size") or 0)


def select(files: list, days: float, now: float, also=()) -> tuple[list, list]:
    """(candidates, unmarked_old). Candidates: marked files older than `days`, plus any `also` id.
    unmarked_old: unmarked files past the cutoff, reported but never selected."""
    cutoff = now - days * 86400
    also = set(also)
    cand, unmarked = [], []
    for f in files:
        old = (f.get("created_at") or now) < cutoff
        if f.get("id") in also or (is_pipe_upload(f) and old):
            cand.append(f)
        elif old and not is_pipe_upload(f):
            unmarked.append(f)
    return cand, unmarked


def row(f: dict, now: float) -> str:
    m = f.get("meta") or {}
    age = (now - (f.get("created_at") or now)) / 86400
    return (f"{f.get('id')}  {str(f.get('filename') or m.get('name') or '?')[:40]:40}  "
            f"owner={str(f.get('user_id') or '?')[:8]}  age={age:.1f}d  size={size_of(f)}")


def report(label: str, files: list, now: float) -> str:
    head = f"{label}: {len(files)} file(s), {sum(size_of(f) for f in files)} bytes"
    return "\n".join([head] + ["  " + row(f, now) for f in files])


class Api:
    def __init__(self, key: str):
        self.key = key

    def __call__(self, method: str, path: str):
        req = urllib.request.Request(BASE + path, method=method)
        req.add_header("Authorization", f"Bearer {self.key}")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Open WebUI {method} {path}: HTTP {e.code}") from None

    def files(self) -> list:
        out, page = [], 1
        while True:
            res = self("GET", f"/api/v1/files/?content=false&page={page}") or {}
            items = res.get("items") or []
            out += items
            if len(items) < PAGE_SIZE or len(out) >= (res.get("total") or 0):
                return out
            page += 1

    def delete(self, fid: str):
        return self("DELETE", f"/api/v1/files/{fid}")


def pinned_version() -> str:
    m = re.search(r"^required_open_webui_version:\s*(\S+)", PIPE.read_text(), re.M)
    return m.group(1) if m else ""


def main(argv=None, api=None, now=None, have=None) -> int:
    ap = argparse.ArgumentParser(description="Retention for files the NOMAD pipe uploaded into Open WebUI.")
    ap.add_argument("--days", type=float, default=None,
                    help="keep this many days (default 30 or $OWUI_FILE_RETENTION_DAYS)")
    ap.add_argument("--apply", action="store_true", help="really delete (default: dry run)")
    ap.add_argument("--also", action="append", default=[], metavar="ID", help="also delete this unmarked file")
    args = ap.parse_args(argv)
    days = args.days if args.days is not None else keep_days()
    now = now or time.time()

    if have is None:
        with urllib.request.urlopen(BASE + "/api/version", timeout=10) as r:
            have = json.loads(r.read().decode()).get("version", "")
    want = pinned_version()
    if have != want:
        print(f"Open WebUI is {have}, the pipe is pinned to {want}: check the files API, then bump "
              "required_open_webui_version.", file=sys.stderr)
        return 2
    if api is None:
        key = os.environ.get("OWUI_ADMIN_KEY", "").strip() or (
            KEY_FILE.read_text().strip() if KEY_FILE.is_file() else "")
        if not key:
            print(f"No Open WebUI admin key: set OWUI_ADMIN_KEY or save one to {KEY_FILE}.", file=sys.stderr)
            return 2
        api = Api(key)

    files = api.files()
    ids = {f.get("id") for f in files}
    missing = [i for i in args.also if i not in ids]
    if missing:
        print(f"--also: no such file: {', '.join(missing)}", file=sys.stderr)
        return 2
    cand, unmarked = select(files, days, now, args.also)
    total = sum(size_of(f) for f in cand)
    print(f"Open WebUI {have} · {len(files)} file(s) · cutoff {days:g} days · "
          f"{'APPLY' if args.apply else 'dry run, nothing deleted'}")
    print(report("to delete", cand, now))
    print(report("unmarked, older than the cutoff, NOT touched (use --also ID)", unmarked, now))
    if not args.apply:
        print(f"total {len(cand)} file(s), {total} bytes would be deleted; run with --apply to do it")
        return 0
    for f in cand:
        api.delete(f["id"])
    print(f"deleted {len(cand)} file(s), {total} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
