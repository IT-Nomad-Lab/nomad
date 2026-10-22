#!/usr/bin/env python3
"""Merge claude-config/ into a Claude Code settings.json (claude-parity scope 6, #1053).

  sync_settings.py [--host wsl|windows] [--settings PATH] [--dry-run]

baseline.json plus the host's overlay (wsl.json or windows.json) each hold:

  "set"     a settings fragment. Objects merge key by key, lists gain the missing items in
            order, anything else is overwritten. Keys and items it doesn't name are kept.
  "retire"  substrings. Every hook command and permission rule containing one is removed:
            these are the old install.sh / ntfy.ps1 entries the nomad-operator plugin replaces.
            Retiring waits until that plugin is installed, so a host is never left with no gate.

What was set last time is kept in nomad-sync.json next to settings.json. An item dropped from
claude-config/ is removed from settings.json on the next run, and only if the last run set it.

It writes only when something changed, and copies the old file to settings.json.bak-<time>
first, so a second run changes nothing (G11). --dry-run prints the diff with env and token
values masked. Running it against a real settings.json is the operator's step, not an agent's.
"""
import argparse
import copy
import datetime
import difflib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "claude-hooks"))
from winpath import to_wsl  # noqa: E402

PLUGIN = "nomad-operator@nomad-marketplace"
STATE = "nomad-sync.json"
SECRET_KEY = re.compile(r"(?i)token|secret|password|passwd|api[_-]?key|auth")


# ── loading ──

def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def config(host):
    """The merged "set" fragment and the "retire" list for one host."""
    base = load_json(os.path.join(HERE, "baseline.json"))
    over = load_json(os.path.join(HERE, f"{host}.json"))
    if base is None or over is None:
        raise SystemExit(f"sync-settings: claude-config/baseline.json or {host}.json is missing")
    want = apply_set(copy.deepcopy(base.get("set") or {}), over.get("set") or {})
    return want, list(base.get("retire") or []) + list(over.get("retire") or [])


def default_host():
    if os.environ.get("NOMAD_HOST") == "windows":
        return "windows"
    return "wsl"


# ── merge ──

def apply_set(dst, src):
    """Merge `src` into `dst` in place: objects key by key, lists by missing item."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            apply_set(dst[k], v)
        elif isinstance(v, list) and isinstance(dst.get(k), list):
            dst[k].extend(x for x in v if x not in dst[k])
        else:
            dst[k] = copy.deepcopy(v)
    return dst


def unset(dst, old, new):
    """Remove from `dst` what `old` (the last run's fragment) set and `new` no longer does."""
    for k, v in old.items():
        if k not in dst:
            continue
        nv = new.get(k) if isinstance(new, dict) else None
        if isinstance(v, dict) and isinstance(dst[k], dict):
            unset(dst[k], v, nv if isinstance(nv, dict) else {})
            if not dst[k] and nv is None:
                del dst[k]
        elif isinstance(v, list) and isinstance(dst[k], list):
            keep = nv if isinstance(nv, list) else []
            dst[k] = [x for x in dst[k] if x not in v or x in keep]
            if not dst[k] and nv is None:
                del dst[k]
        elif nv is None and dst[k] == v:
            del dst[k]


def retire(settings, needles):
    """Drop hook commands and permission rules that contain any of `needles`."""
    if not needles:
        return
    hit = lambda s: isinstance(s, str) and any(n in s for n in needles)  # noqa: E731
    hooks = settings.get("hooks")
    if isinstance(hooks, dict):
        for event in list(hooks):
            groups = []
            for g in hooks[event] if isinstance(hooks[event], list) else []:
                if isinstance(g, dict) and isinstance(g.get("hooks"), list):
                    g = dict(g, hooks=[h for h in g["hooks"]
                                       if not (isinstance(h, dict) and hit(h.get("command")))])
                    if not g["hooks"]:
                        continue
                groups.append(g)
            if groups:
                hooks[event] = groups
            else:
                del hooks[event]
        if not hooks:
            del settings["hooks"]
    perms = settings.get("permissions")
    if isinstance(perms, dict):
        for kind in ("allow", "ask", "deny"):
            if isinstance(perms.get(kind), list):
                perms[kind] = [r for r in perms[kind] if not hit(r)]


def plugin_installed(settings_dir):
    data = load_json(os.path.join(settings_dir, "plugins", "installed_plugins.json"), {})
    plugins = data.get("plugins", data) if isinstance(data, dict) else {}
    return isinstance(plugins, dict) and bool(plugins.get(PLUGIN))


def sync(current, want, needles, last, installed):
    """The new settings and a list of notes for the operator."""
    out = copy.deepcopy(current)
    unset(out, last, want)
    apply_set(out, want)
    notes = []
    if needles and installed:
        retire(out, needles)
    elif needles and has_any(current, needles):
        notes.append(f"kept the old hook entries: {PLUGIN} isn't installed yet, and removing them "
                     f"now would leave this host without them. Run again after the plugin install.")
    return out, notes


def has_any(settings, needles):
    text = json.dumps({k: settings.get(k) for k in ("hooks", "permissions")})
    return any(json.dumps(n)[1:-1] in text for n in needles)


# ── output ──

def masked(settings):
    """A copy safe to print: env values and anything keyed like a secret replaced."""
    def walk(o, key=""):
        if isinstance(o, dict):
            return {k: ("<masked>" if key == "env" or (SECRET_KEY.search(k) and not
                                                       isinstance(v, (dict, list))) else walk(v, k))
                    for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v, key) for v in o]
        return o
    return walk(settings)


def dump(settings):
    return json.dumps(settings, indent=2, ensure_ascii=False) + "\n"


def write(path, text):
    tmp = path + ".nomad-sync.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main(argv=None):
    p = argparse.ArgumentParser(prog="sync-settings", description=(__doc__ or "").split("\n")[0])
    p.add_argument("--host", choices=("wsl", "windows"), default=default_host())
    p.add_argument("--settings", help="settings.json to update (default ~/.claude/settings.json; "
                                      "required for windows, e.g. /mnt/c/Users/<you>/.claude/settings.json)")
    p.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    a = p.parse_args(argv)
    if a.settings:
        # From Git Bash this is C:/Users/... or /c/Users/...; it is read here, in WSL.
        path = os.path.abspath(os.path.expanduser(to_wsl(a.settings, a.host == "windows")))
    elif a.host == "wsl":
        path = os.path.expanduser("~/.claude/settings.json")
    else:
        p.error("--settings is required for --host windows (the Windows home isn't visible from here)")
    d = os.path.dirname(path)
    state_path = os.path.join(d, STATE)

    want, needles = config(a.host)
    current = load_json(path, {})
    if not isinstance(current, dict):
        raise SystemExit(f"sync-settings: {path} is not a JSON object")
    last = load_json(state_path, {}).get("set") or {}
    new, notes = sync(current, want, needles, last, plugin_installed(d))

    before, after = dump(current), dump(new)
    if a.dry_run:
        diff = difflib.unified_diff(dump(masked(current)).splitlines(True),
                                    dump(masked(new)).splitlines(True), path, path + " (synced)")
        sys.stdout.writelines(diff)
    elif before != after:
        if os.path.exists(path):
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            with open(path, encoding="utf-8") as f, \
                 open(f"{path}.bak-{stamp}", "w", encoding="utf-8", newline="\n") as b:
                b.write(f.read())
            print(f"backed up to {path}.bak-{stamp}")
        os.makedirs(d, exist_ok=True)
        write(path, after)
        print(f"updated {path} ({a.host})")
    else:
        print(f"no change: {path} ({a.host})")
    if not a.dry_run and load_json(state_path, {}).get("set") != want:
        write(state_path, dump({"host": a.host, "set": want}))
    for n in notes:
        print("note: " + n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
