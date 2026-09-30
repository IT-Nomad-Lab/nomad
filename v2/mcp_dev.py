"""NOMAD v2 · 3B — the Dev MCP tool: `dev.dispatch_build`.

On approval, dispatches a coding task to the Builder (the v1 dispatcher → headless Claude Code
in the target repo, UNCOMMITTED edits, never commits/pushes). The single gated Execute action of
the dev lane. Returns the build summary + changed files (no NocoDB sink row).
"""
import os
import time

import requests
from mcp.server.fastmcp import FastMCP
from nocodb import _env

_E = _env()
DISPATCH = _E.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090")
def _dispatch_token():
    """Shared secret for the Builder dispatcher (auto-generated on the host at
    ~/.config/nomad/dispatch-token). The dispatcher refuses every route but /health without
    it. Candidates cover the container mounts and the host; '' means the call will 401."""
    env = os.environ.get("NOMAD_DISPATCH_TOKEN")
    if env:
        return env
    for cand in ("/term-secret/dispatch-token", "/host/.config/nomad/dispatch-token",
                 os.path.expanduser("~/.config/nomad/dispatch-token")):
        try:
            return open(cand).read().strip()
        except Exception:
            continue
    return ""


DISPATCH_TOKEN = _dispatch_token()
mcp = FastMCP("nomad-dev")


def dispatch_build_impl(project: str, task: str, run_id: str, plan: str = "",
                        branch: str = "") -> dict:
    """Dispatch a build task to the Builder (uncommitted edits on a feature branch). Gated; runs
    only after approval. The dispatcher picks or creates the branch unless `branch` names one;
    branch="none" is the operator's explicit opt-out."""
    # `plan` is what the operator approved at the gate; the dispatcher puts it in the Builder's
    # prompt. Before #851 only the one-line `task` went, so the approval covered a plan nobody ran.
    body = {"project": project, "task": task, "mode": "build"}
    if plan:
        body["plan"] = plan
    if branch:
        body["branch"] = branch
    try:
        r = requests.post(f"{DISPATCH}/dispatch", json=body,
                          headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=900)
        d = r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"dispatcher unreachable: {e}"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "build failed")}
    return {"ok": True, "summary": (d.get("summary") or "")[:400],
            "changed": d.get("changed", []), "secs": d.get("secs"), "branch": d.get("branch", "")}


def check_project(project: str) -> dict:
    """Ask the dispatcher which registry project `project` names, read-only, before the gate.
    → {ok: True, id, name, subdir, target} | {ok: False, reason, error, candidates} | {ok: None, error} when it could not
    be asked. None is not a pass: the gate says the project is unchecked rather than implying it is
    fine. The dispatcher does the resolving, so the gate and the build can never disagree on it."""
    try:
        r = requests.post(f"{DISPATCH}/resolve", json={"project": project},
                          headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=5)
        d = r.json() if r.status_code == 200 else {}
        d = d if isinstance(d, dict) else {}
    except (requests.RequestException, ValueError) as e:
        return {"ok": None, "error": f"dispatcher unreachable: {str(e)[:120]}"}
    if d.get("ok") is True:
        pid = d.get("id") or project
        return {"ok": True, "id": pid, "name": d.get("name") or "",
                "subdir": d.get("subdir") or "", "target": d.get("target") or pid}
    if d.get("ok") is False:
        return {"ok": False, "error": str(d.get("error") or "unknown project")[:400],
                "reason": "ambiguous" if d.get("reason") == "ambiguous" else "unknown",
                "candidates": [str(c) for c in d.get("candidates") or []][:30]}
    return {"ok": None, "error": f"dispatcher could not resolve projects (HTTP {r.status_code})"}


_IDS = {"at": 0.0, "ids": []}


def project_ids(ttl: int = 300) -> list:
    """The registry ids a build can target, for the Manager's routing prompt. Cached, fail-open:
    an unreachable dispatcher gives [] and the prompt simply goes without the list."""
    if _IDS["at"] and time.monotonic() - _IDS["at"] < ttl:
        return _IDS["ids"]
    try:
        d = requests.get(f"{DISPATCH}/projects", headers={"X-Nomad-Token": DISPATCH_TOKEN},
                         timeout=3).json()
        ids = [str(i) for i in d.get("ids") or []]
    except (requests.RequestException, ValueError, AttributeError):
        ids = []
    # A miss is cached for a minute too, so a dispatcher that is down costs one timeout, not one
    # per routed goal.
    _IDS.update(at=time.monotonic() - (ttl - 60 if not ids else 0), ids=ids)
    return ids


@mcp.tool()
def dispatch_build(project: str, task: str, run_id: str, plan: str = "", branch: str = "") -> dict:
    """Dispatch a coding task to the Builder in `project` (uncommitted edits on a feature branch).
    `project` must be a registry id from the dispatcher's /projects (e.g. "nomad", "civis-poc"),
    never a file name, a description or a guess. `plan` is the approved plan and is handed to the
    Builder. Runs only after the human gate approves; never commits/pushes. Pass `branch` only
    when the operator named one, or "none" when the operator said to skip the feature branch.
    Returns {ok, summary, changed, branch}."""
    return dispatch_build_impl(project, task, run_id, plan, branch)


if __name__ == "__main__":
    mcp.run()
