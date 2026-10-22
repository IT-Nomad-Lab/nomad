"""Shadow probe for `gate_network: none` (#1531, docs/prd/merge-gate-network.md R5).

    uv run --no-project --with pyyaml python -m autopilot.gates probe <project> [--keep]

Runs the project's pre-merge gate once, as the merge queue would but with gate_network none, in
a scratch detached worktree of the default branch. Prints pass/fail per step and the log folder.
It changes no config and no merge state: the wrappers and logs go to a fresh folder under the
state dir, and the worktree is removed afterwards (--keep leaves it). Exit 0 when the gate
passes, 1 when it fails, 2 on a usage or setup error.
"""
import dataclasses
import os
import subprocess
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from autopilot import merge as merge_mod  # noqa: E402
from autopilot import permissions  # noqa: E402
from autopilot import projects as projects_mod  # noqa: E402
from autopilot.worker import default_branch  # noqa: E402

USAGE = "usage: python3 -m autopilot.gates probe <project> [--keep]"


class ProbeError(Exception):
    pass


def _git(repo, *args):
    p = subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise ProbeError(f"git {' '.join(args)} failed in {repo}: {p.stderr.strip()[:300]}")
    return p.stdout.strip()


def probe(project, state_dir=None, runner=None, keep=False, out=print):
    """Run `project`'s pre-merge gate with gate_network none. Returns the merge.Gate."""
    ap = project.autopilot
    if ap is None or not ap.pre_merge_gate:
        raise ProbeError(f"{project.id}: no autopilot pre-merge gate to probe")
    repo = project.repo
    default = default_branch(repo)
    tip = _git(repo, "rev-parse", "--verify", f"refs/heads/{default}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    root = os.path.join(state_dir or merge_mod.STATE_DIR, "probe", project.id, stamp)
    os.makedirs(ap.worktree_root, exist_ok=True)
    wt = os.path.join(ap.worktree_root, f"probe-gate-network-{stamp}")
    gp = dataclasses.replace(project, autopilot=dataclasses.replace(ap, gate_network="none"))
    ws = permissions.write_wrappers(gp, root)          # its own copies: the live ones stay as is
    _git(repo, "worktree", "add", "-q", "--detach", wt, tip)
    try:
        out(f"{project.id}: probing the pre-merge gate with gate_network none on "
            f"{default} {tip[:9]} in {wt}")
        gate = merge_mod.GateRunner(gp, ws, os.path.join(root, "logs"),
                                    runner or merge_mod.run_command).run(wt)
    finally:
        if keep:
            out(f"kept the worktree: {wt}")
        else:
            _git(repo, "worktree", "remove", "--force", wt)
    out(merge_mod.format_table(gate))
    for s in gate.steps:
        for r in s.real:
            out(f"- {r}")
    out(f"logs: {os.path.join(root, 'logs')}")
    out(f"{project.id}: {'PASS' if gate.ok else 'FAIL'} with gate_network none. No config changed.")
    return gate


def main(argv):
    args = argv[1:]
    keep = "--keep" in args
    args = [a for a in args if a != "--keep"]
    if len(args) != 2 or args[0] != "probe":
        print(USAGE, file=sys.stderr)
        return 2
    try:
        project = projects_mod.hub_get(args[1])
        if project is None:
            raise ProbeError(f"no project {args[1]!r} in projects.yaml")
        gate = probe(project, keep=keep)
    except (ProbeError, projects_mod.ConfigError) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    return 0 if gate.ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
