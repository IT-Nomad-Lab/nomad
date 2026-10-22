#!/usr/bin/env python3
"""Snapshot every project's effective configuration: the "must stay identical" baseline (#1389).

PRD docs/prd/project-types-and-inline-asks.md section 6.5: tagging a project with a type must
leave its configuration unchanged, and the policy compiler must reproduce today's output. This
script writes one deterministic, sorted JSON file per project to `policies/baseline/<id>.json`
(plus `_global.json` for what is not per project). Read-only against live systems.

  config-snapshot.py            write the baselines (also removes files of projects that are gone)
  config-snapshot.py --check    compare live state to the committed baselines; print a diff and
                                exit 1 when they differ, 0 when identical

Fields of `<project>.json`, and where each comes from:
  id, path, repo, group, aliases, dispatch, local_only
        projects.yaml entry, read by autopilot/projects.py `load` (the registry loader, with
        entry paths resolved against the home folder like the hub does). `repo` = realpath.
  autopilot
        the validated `autopilot:` block with every default applied (autopilot.projects.
        Autopilot): test suites, gates, cap, model, max_budget_usd, deny_edit_globs, lanes...
        null when the project has no block (the autopilot never runs it).
  mcp.gateway
        mcp-gateway/grant.py logic: the gateway key ~/.config/nomad/mcp-keys/<project> and the
        servers granted to it in the LiteLLM gateway (GET /key/info, read-only). Only key
        PRESENCE and server names are recorded, never the key. `servers` is null when the
        gateway is unreachable (then --check skips this field and says so).
  mcp.local_model_set
        ~/.config/nomad/mcp-grants.json (read by agent-bridge/backends.py for local-model runs):
        `default` + the project's own list. Claude and Codex are not limited by it. null = no file.
  mcp.repo_mcp_json
        server names in <repo>/.mcp.json (names only; null = no file).
  autopilot_permissions
        autopilot/permissions.py `build_settings` with no grants, for a placeholder worktree
        `<worktree_root>/baseline` and the real state dir: the rendered Claude settings
        (permissions allow/deny/ask/additionalDirectories) and, per wrapper, its name, rules,
        command and a sha256 of the script text. Or {"error": ...} when the entry is refused.
        null when the project has no autopilot block.
  agent_bridge
        agent-bridge/projects.py `resolve`: the folder a chat for this project gets, or the
        refusal message (local-only project / missing folder).

`_global.json`: the autopilot permission template and the agent-bridge policy_hook rules (which
commands are denied or asked). They are not per project.

Not captured: per-run grants (`grant: <action> <arg>` lines on tasks, ap-permission-grants.md),
which exist only while a run is live; the gateway key values; OWUI and Claude Code user
settings; the guard.py hook (global, not per-project config); anything under ~/.claude.

Tests: uv run --no-project --with pytest --with pyyaml python -m pytest scripts/test_config_snapshot.py -q
Run:   uv run --no-project --with pyyaml python scripts/config-snapshot.py [--check]
"""
import argparse
import dataclasses
import difflib
import hashlib
import importlib.util
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NOMAD = os.path.dirname(HERE)
sys.path.insert(0, NOMAD)

from autopilot import permissions, projects  # noqa: E402

BASELINE = os.path.join(NOMAD, "policies", "baseline")
HOME = os.path.expanduser("~")
GRANTS_FILE = os.path.join(HOME, ".config", "nomad", "mcp-grants.json")
KEYDIR = os.path.join(HOME, ".config", "nomad", "mcp-keys")
GLOBAL = "_global"
# Fields --check ignores when the live source was unreachable (the fresh value is null).
SKIPPABLE = (("mcp", "gateway", "servers"),)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(NOMAD, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def plain(x):
    """JSON-ready, deterministic: dataclasses to dicts, sets to sorted lists, tuples to lists."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type):
        return {f.name: plain(getattr(x, f.name)) for f in dataclasses.fields(x)}
    if isinstance(x, dict):
        return {str(k): plain(v) for k, v in x.items()}
    if isinstance(x, (set, frozenset)):
        return sorted(plain(v) for v in x)
    if isinstance(x, (list, tuple)):
        return [plain(v) for v in x]
    return x


# ---- live sources (each is replaceable in tests) ------------------------------------------------

def gateway_servers(project):
    """(key_present, sorted server names, or None if the gateway can't be asked). No key value
    leaves this function."""
    path = os.path.join(KEYDIR, project)
    if not os.path.exists(path):
        return False, []
    try:
        g = _load("nomad_mcp_grant", "mcp-gateway/grant.py")
        key = open(path).read().strip()
        ids = {s["server_id"]: n for n, s in g.servers().items()}
        return True, (sorted(ids.get(i, i) for i in g.current(key)) if key else [])
    except Exception:
        return True, None


def local_grants(path=GRANTS_FILE):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def repo_mcp_names(repo):
    try:
        with open(os.path.join(repo, ".mcp.json")) as f:
            return sorted((json.load(f).get("mcpServers") or {}).keys())
    except (OSError, ValueError, AttributeError):
        return None


class Sources:
    """Everything the snapshot reads outside the registry; tests pass their own."""
    def __init__(self, gateway=gateway_servers, grants=local_grants, mcp_names=repo_mcp_names,
                 state_dir=permissions.STATE_DIR, default_branch=None):
        self.gateway, self.grants, self.mcp_names = gateway, grants, mcp_names
        self.state_dir, self.default_branch = state_dir, default_branch


def render_permissions(p, src, template=None):
    """`template`: a permission template other than autopilot/permissions.template.json (the
    policy compiler passes the one it builds, nomad_policy/compiler.py)."""
    if p.autopilot is None:
        return None
    wt = os.path.join(p.autopilot.worktree_root, "baseline")
    try:
        s, _, _ = permissions.build_settings_with_grants(
            p, wt, src.state_dir, template=template, default_branch=src.default_branch)
        ws = permissions.wrappers(p, src.state_dir)
    except Exception as e:  # ConfigError, ValueError: a refusal is part of the state
        return {"error": f"{type(e).__name__}: {e}"}
    return {"worktree": wt, "settings": s, "wrappers": [
        {"name": w.name, "path": w.path, "rules": w.rules, "takes_filter": w.takes_filter,
         "command": plain(w.command),
         "script_sha256": hashlib.sha256(w.script.encode()).hexdigest()}
        for w in ws]}


def bridge_view(pid, entries):
    ab = _load("nomad_bridge_projects", "agent-bridge/projects.py")
    try:
        _, cwd = ab.resolve(pid, entries)
        return {"cwd": cwd}
    except ValueError as e:
        return {"refused": str(e)}


def project_snapshot(p, entry, entries, src):
    g = src.grants()
    key_present, servers = src.gateway(p.id)
    local = None if g is None else sorted(
        set(g.get("default") or []) | set((g.get("projects") or {}).get(p.id) or []))
    return plain({
        "id": p.id, "path": p.path, "repo": p.repo, "group": p.group, "aliases": p.aliases,
        "dispatch": p.dispatch, "local_only": entry.get("local_only") is True,
        "autopilot": p.autopilot,
        "mcp": {"gateway": {"key_present": key_present, "servers": servers},
                "local_model_set": local, "repo_mcp_json": src.mcp_names(p.repo)},
        "autopilot_permissions": render_permissions(p, src),
        "agent_bridge": bridge_view(p.id, entries),
    })


def global_snapshot():
    hook = _load("nomad_policy_hook", "agent-bridge/policy_hook.py")
    return plain({
        "autopilot_permission_template": permissions.load_template(),
        "agent_bridge_policy_hook": {
            "deny_patterns": {"stash": hook.STASH.pattern, "commit in shared checkout": hook.COMMIT.pattern,
                              "force push": hook.FORCE.pattern},
            "ask_patterns": [[why, rx.pattern, rx.flags] for rx, why in hook.ASK]},
    })


def snapshot(registry=None, src=None, base=None):
    """{file stem: dict} for every project plus _global."""
    src = src or Sources()
    f = registry or os.path.join(NOMAD, "projects.yaml")
    base = base or os.path.expanduser(projects.HUB_BASE)
    entries = projects.projects_registry.read_entries(f, base)
    rows = {e["id"]: e for e in entries}
    out = {p.id: project_snapshot(p, rows[p.id], entries, src)
           for p in projects.load(f, base=base)}
    out[GLOBAL] = global_snapshot()
    return out


def render(d):
    return json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write(snap, outdir=BASELINE):
    os.makedirs(outdir, exist_ok=True)
    keep = {k + ".json" for k in snap}
    for fn in os.listdir(outdir):
        if fn.endswith(".json") and fn not in keep:
            os.remove(os.path.join(outdir, fn))
    for k, d in snap.items():
        with open(os.path.join(outdir, k + ".json"), "w") as f:
            f.write(render(d))


def _get(d, path):
    for k in path:
        d = d.get(k) if isinstance(d, dict) else None
    return d


def _mask(name, committed, live):
    """When a source could not be asked, take the committed value so it does not count."""
    notes = []
    for path in SKIPPABLE:
        if _get(live, path) is None and _get(committed, path) is not None:
            d = live
            for k in path[:-1]:
                d = d[k]
            d[path[-1]] = _get(committed, path)
            notes.append(f"{name}: {'.'.join(path)} skipped (source unreachable)")
    return notes


def check(snap, outdir=BASELINE):
    """(diff lines, notes). An empty diff = identical."""
    diff, notes = [], []
    have = ({fn[:-5] for fn in os.listdir(outdir) if fn.endswith(".json")}
            if os.path.isdir(outdir) else set())
    for k in sorted(set(snap) | have):
        if k not in have:
            diff.append(f"+ {k}: no committed baseline")
            continue
        if k not in snap:
            diff.append(f"- {k}: committed baseline but no such project now")
            continue
        with open(os.path.join(outdir, k + ".json")) as f:
            committed = json.load(f)
        live = json.loads(render(snap[k]))
        notes += _mask(k, committed, live)
        diff += difflib.unified_diff(render(committed).splitlines(), render(live).splitlines(),
                                     f"baseline/{k}.json", f"live/{k}.json", lineterm="", n=2)
    return diff, notes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="compare live state to the baselines")
    ap.add_argument("--registry", help="projects.yaml (default: this checkout's)")
    ap.add_argument("--out", default=BASELINE, help="baseline folder")
    a = ap.parse_args(argv)
    snap = snapshot(a.registry)
    if not a.check:
        write(snap, a.out)
        print(f"wrote {len(snap)} baseline files to {a.out}")
        return 0
    diff, notes = check(snap, a.out)
    for n in notes:
        print("note:", n)
    if diff:
        print("\n".join(diff))
        print(f"config differs from the committed baseline ({a.out})", file=sys.stderr)
        return 1
    print(f"ok: {len(snap) - 1} projects identical to the baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
