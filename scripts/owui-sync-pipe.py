#!/usr/bin/env python3
"""Install or update NOMAD's Open WebUI functions from the repo copies (#1118, #1138).

    scripts/owui-sync-pipe.py            # sync both functions and the hidden model entries
    scripts/owui-sync-pipe.py --check    # say what would change, change nothing

The repo copies are the source of truth (docs/prd/owui-sessions.md §6.1, §6.5):
- owui/nomad_pipe.py as function `nomad` (NOMAD Auto). An OWUI function is one file, so the line
  `# @@ASK_CARD@@` in it is replaced with owui/ask_card.py's source (cut before its samples), which
  defines build_card for the ask cards (#1362, the only ask path since #1364). The repo copy loads
  the sibling file instead;
- owui/nomad_filter.py as function `nomad_project`, a global filter.

The script:
- refuses to run against an Open WebUI version other than the pinned one (REQUIRED, from each
  function's front matter), since the function API moves between releases;
- creates each function, or updates it when the content differs;
- makes sure it's active, and the filter global;
- sets the valves: ENGINE_URL, and NOMAD_TOKEN from the engine's owui-token file;
- hides every `nomad.*` entry but NOMAD Auto from the model dropdown (a model record with
  meta.hidden), since the Project dropdown lists them instead. Run it again after adding a
  project by hand; M3's setup runs it.

The Project dropdown itself (owui/nomad_ui.js) is a read-only mount, not synced here.

Credentials, never printed: an Open WebUI admin API key from $OWUI_ADMIN_KEY or
~/.config/nomad/owui-admin-key (Settings → Account → API keys), and the shared engine token from
$NOMAD_OWUI_TOKEN or ~/.config/nomad/owui-token.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FUNCTIONS = (("nomad", ROOT / "owui" / "nomad_pipe.py", False),          # id, source, global
             ("nomad_project", ROOT / "owui" / "nomad_filter.py", True))
AUTO = "nomad.auto"
VISIBLE = {AUTO, "nomad.local"}
BASE = os.environ.get("OWUI_URL", "http://127.0.0.1:3000").rstrip("/")
ENGINE_URL = os.environ.get("NOMAD_OWUI_ENGINE_URL", "http://nomad-v2-engine:8099")
CONFIG = Path.home() / ".config" / "nomad"
CARD_MARKER = "# @@ASK_CARD@@\n"
CARD_SOURCE = ROOT / "owui" / "ask_card.py"


def bundle(content: str, card: str | None = None) -> str:
    """The text installed in Open WebUI: `content` with the card marker line replaced by the card
    builder's source. Content without the marker is returned unchanged. The samples at the end of
    ask_card.py are test data and stay out."""
    if CARD_MARKER not in content:
        return content
    src = card if card is not None else CARD_SOURCE.read_text()
    src = src.split("\nSAMPLE_Q = ", 1)[0].rstrip() + "\n"
    return content.replace(CARD_MARKER, "# --- owui/ask_card.py, inlined by scripts/owui-sync-pipe.py ---\n" + src
                           + "# --- end of ask_card.py ---\n", 1)


def front_matter(text: str) -> dict:
    """The `key: value` lines of the pipe's leading docstring."""
    m = re.match(r'\s*"""(.*?)"""', text, re.S)
    out = {}
    for line in (m.group(1) if m else "").splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip():
            out[k.strip()] = v.strip()
    return out


def secret(env: str, name: str) -> str:
    v = os.environ.get(env, "").strip()
    if not v and (CONFIG / name).is_file():
        v = (CONFIG / name).read_text().strip()
    return v


def plan(installed: dict | None, content: str, is_global: bool = False) -> list:
    """What has to happen, given the installed function (or None)."""
    if installed is None:
        return ["create", "activate"] + (["global"] if is_global else []) + ["valves"]
    steps = []
    if installed.get("content") != content:
        steps.append("update")
    if not installed.get("is_active"):
        steps.append("activate")
    if is_global and not installed.get("is_global"):
        steps.append("global")
    return steps + ["valves"]


def hide_plan(listed: list, records: list) -> list:
    """[(action, form)] so that every listed `nomad.*` entry but NOMAD Auto and NOMAD Local has a hidden model
    record, and NOMAD Auto isn't hidden. `listed` is /api/models, `records` /api/v1/models/all."""
    have = {r["id"]: r for r in records}
    out = []
    for m in listed:
        mid = m.get("id") or ""
        if not mid.startswith("nomad."):
            continue
        want = mid not in VISIBLE
        rec = have.get(mid)
        if rec is None:
            if want:
                out.append(("create", {"id": mid, "name": m.get("name") or mid,
                                       "base_model_id": None, "meta": {"hidden": True},
                                       "params": {}}))
        elif bool((rec.get("meta") or {}).get("hidden")) != want:
            out.append(("update", {**{k: rec.get(k) for k in ("id", "name", "base_model_id",
                                                             "params", "is_active")},
                                   "meta": {**(rec.get("meta") or {}), "hidden": want}}))
    return out


class Api:
    def __init__(self, key: str):
        self.key = key

    def __call__(self, method: str, path: str, body=None):
        req = urllib.request.Request(BASE + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise SystemExit(f"Open WebUI {method} {path}: HTTP {e.code}") from None

    def function(self, fid: str) -> dict | None:
        """The installed function, or None. Listed first: a missing id answers 401, the same as
        a bad key, so asking for it directly can't tell the two apart."""
        if fid not in {f.get("id") for f in self("GET", "/api/v1/functions/") or []}:
            return None
        return self("GET", f"/api/v1/functions/id/{fid}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Install or update the NOMAD pipe in Open WebUI.")
    ap.add_argument("--check", action="store_true", help="report only")
    args = ap.parse_args()

    sources = {fid: bundle(path.read_text()) for fid, path, _ in FUNCTIONS}
    with urllib.request.urlopen(BASE + "/api/version", timeout=10) as r:
        have = json.loads(r.read().decode()).get("version", "")
    for fid, content in sources.items():
        want = front_matter(content).get("required_open_webui_version", "")
        if have != want:
            print(f"Open WebUI is {have}, {fid} is pinned to {want}: check the function API, "
                  "then bump required_open_webui_version.", file=sys.stderr)
            return 2

    key = secret("OWUI_ADMIN_KEY", "owui-admin-key")
    token = secret("NOMAD_OWUI_TOKEN", "owui-token")
    if not key:
        print(f"No Open WebUI admin key: create one (Settings → Account → API keys) and save it "
              f"to {CONFIG / 'owui-admin-key'} (chmod 600).", file=sys.stderr)
        return 2
    if not token and not args.check:
        print(f"No engine token at {CONFIG / 'owui-token'}.", file=sys.stderr)
        return 2

    api = Api(key)
    ok = True
    for fid, _, is_global in FUNCTIONS:
        ok = sync_function(api, fid, sources[fid], is_global, token, have, args.check) and ok

    hides = hide_plan((api("GET", "/api/models") or {}).get("data") or [],
                      api("GET", "/api/v1/models/all") or [])
    print(f"model dropdown: {len(hides)} entr{'y' if len(hides) == 1 else 'ies'} to hide or show")
    if not args.check:
        for action, form in hides:
            api("POST", "/api/v1/models/create" if action == "create"
                else "/api/v1/models/model/update", form)
    if args.check:
        return 0
    print("synced" if ok else "NOT synced: an installed copy differs or is inactive")
    return 0 if ok else 1


def sync_function(api, fid: str, content: str, is_global: bool, token: str, have: str,
                  check: bool) -> bool:
    fm = front_matter(content)
    installed = api.function(fid)
    steps = plan(installed, content, is_global)
    old = front_matter((installed or {}).get("content") or "").get("version", "none")
    print(f"Open WebUI {have} · {fid} {old} → {fm.get('version')} · steps: {', '.join(steps)}")
    if check:
        return True
    form = {"id": fid, "name": fm.get("title", fid), "content": content,
            "meta": {"description": fm.get("description", "")}}
    if "create" in steps:
        api("POST", "/api/v1/functions/create", form)
    if "update" in steps:
        api("POST", f"/api/v1/functions/id/{fid}/update", form)
    state = api.function(fid) or {}
    if "activate" in steps and not state.get("is_active"):
        api("POST", f"/api/v1/functions/id/{fid}/toggle")
    if is_global and not state.get("is_global"):
        api("POST", f"/api/v1/functions/id/{fid}/toggle/global")
    # keep valves the operator set by hand (e.g. AGENT_ENABLED); only these two are managed
    kept = api("GET", f"/api/v1/functions/id/{fid}/valves") or {}
    api("POST", f"/api/v1/functions/id/{fid}/valves/update",
        {**kept, "ENGINE_URL": ENGINE_URL, "NOMAD_TOKEN": token})
    final = api.function(fid) or {}
    return bool(final.get("content") == content and final.get("is_active")
                and (final.get("is_global") or not is_global))


if __name__ == "__main__":
    sys.exit(main())
