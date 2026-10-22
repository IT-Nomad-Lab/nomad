#!/usr/bin/env python3
"""Compile each project's effective policy and compare it to its baseline (#1392).

nomad_policy/compiler.py does the work. Every project gets its type's policies plus its
legacy-<project> policy (policies/legacy/legacy-<id>.yaml once tagged, else built from
policies/baseline/<id>.json), and the output must be byte
identical to the baseline's mcp, autopilot and autopilot_permissions fields. Read-only: it
writes nothing and changes no grant.

  policy-compile.py [project ...]          print the compiled fields and the gaps
  policy-compile.py --check [project ...]  diff against the baselines; exit 1 on any difference

Run: uv run --no-project --with pyyaml python scripts/policy-compile.py --check
"""
import argparse
import difflib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NOMAD = os.path.dirname(HERE)
sys.path.insert(0, NOMAD)

from autopilot import projects  # noqa: E402
from nomad_policy import compiler, schema, tag  # noqa: E402
from nomad_projects import profiles  # noqa: E402


def compile_all(ids=(), registry=None, baseline=compiler.BASELINE):
    """{id: (compiled, baseline fields, gaps)} for the projects named (all when none)."""
    f = registry or os.path.join(NOMAD, "projects.yaml")
    reg = schema.Registry.live()
    policies = profiles.load_policies(reg)
    universe = compiler.Universe.live()
    out = {}
    for p in projects.load(f, base=os.path.expanduser(projects.HUB_BASE)):
        if ids and p.id not in ids:
            continue
        with open(os.path.join(baseline, p.id + ".json")) as fh:
            base = json.load(fh)
        layers = compiler.legacy_layers(base, p.type, policies, reg,
                                        tuple(policies[x] for x in p.policies), tag.current_legacy(base))
        got, gaps = compiler.compile_project(p, layers, universe, base["mcp"])
        out[p.id] = (got, compiler.baseline_fields(base), gaps)
    missing = set(ids) - set(out)
    if missing:
        raise SystemExit(f"no such project: {', '.join(sorted(missing))}")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="diff against the baselines")
    ap.add_argument("projects", nargs="*")
    a = ap.parse_args(argv)
    res = compile_all(a.projects)
    if not a.check:
        for pid, (got, _, gaps) in res.items():
            print(f"## {pid}")
            print(compiler.render(got), end="")
            for g in gaps:
                print("gap:", g)
        return 0
    bad = 0
    for pid, (got, want, _) in res.items():
        diff = list(difflib.unified_diff(compiler.render(want).splitlines(), compiler.render(got).splitlines(),
                                         f"baseline/{pid}.json", f"compiled/{pid}", lineterm="", n=2))
        if diff:
            bad += 1
            print("\n".join(diff))
    bridge = compiler.check_bridge_hook()
    for g in bridge:
        print(g, file=sys.stderr)
    if bad:
        print(f"{bad} of {len(res)} projects compile differently from the baseline", file=sys.stderr)
        return 1
    if bridge:
        return 1
    print(f"ok: {len(res)} projects compile identically to the baseline; the agent-bridge hook matches its policy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
