#!/usr/bin/env python3
"""Tag a project with a type, keeping its configuration (#1393, nomad_policy/tag.py).

Dry run by default: prints the before/after diff (empty when the tag keeps everything), the
changes the type proposes and why a tag would be refused. Nothing is written without --save.

  policy-tag.py <project> <type>                  plan the tag (dry run)
  policy-tag.py <project> <type> --save           write legacy-<id>.yaml and the type: line
  policy-tag.py <project> --accept <change-id>    lift one hold, e.g. mcp:gateway:+figma

Writes only the repo (policies/legacy/, projects.yaml), never live grants. Exit 1 on refusal.

Run: uv run --no-project --with pyyaml --with requests python scripts/policy-tag.py nomad-crm website
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
NOMAD = os.path.dirname(HERE)
sys.path.insert(0, NOMAD)

from autopilot import projects  # noqa: E402
from nomad_policy import compiler, schema, tag  # noqa: E402
from nomad_projects import profiles  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("project")
    ap.add_argument("type", nargs="?")
    ap.add_argument("--save", action="store_true", help="write the tag (refused unless the diff is empty)")
    ap.add_argument("--accept", metavar="CHANGE", help="lift one held change from a saved tag")
    a = ap.parse_args(argv)
    if a.accept:
        tag.accept(a.project, a.accept)
        print(f"{a.project}: accepted {a.accept}; policy-compile.py --check shows it until it is deployed")
        return 0
    if not a.type:
        ap.error("a type is needed unless --accept is given")
    registry_file = os.path.join(NOMAD, "projects.yaml")
    p = next((x for x in projects.load(registry_file, base=os.path.expanduser(projects.HUB_BASE))
              if x.id == a.project), None)
    if p is None:
        raise SystemExit(f"no such project: {a.project}")
    with open(os.path.join(compiler.BASELINE, p.id + ".json")) as fh:
        base = json.load(fh)
    reg = schema.Registry.live()
    plan = tag.plan(p, a.type, base, profiles.load_policies(reg), reg, compiler.Universe.live())
    diff = plan.diff()
    print("\n".join(diff) if diff else f"{p.id} as {a.type}: empty diff, the configuration stays as it is")
    for ch in plan.proposed:
        print(f"proposed: {ch.id}" + (" (held; accept with --accept)" if ch.op == "add" else ""))
    for why in plan.refused:
        print(f"refused: {why}", file=sys.stderr)
    if not plan.ok:
        return 1
    if a.save:
        tag.save(plan, registry_file)
        print(f"saved: {tag.legacy_path(p.id)} and type: {a.type} in projects.yaml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
