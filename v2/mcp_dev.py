"""NOMAD v2 · 3B — the Dev MCP tool: `dev.dispatch_build`.

On approval, dispatches a coding task to the Builder (the v1 dispatcher → headless Claude Code
in the target repo, UNCOMMITTED edits, never commits/pushes). The single gated Execute action of
the dev lane. Returns the build summary + changed files (no NocoDB sink row).
"""
import os

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


def dispatch_build_impl(project: str, task: str, run_id: str, plan: str = "") -> dict:
    """Dispatch a build task to the Builder (uncommitted edits). Gated; runs only after approval."""
    try:
        r = requests.post(f"{DISPATCH}/dispatch",
                          json={"project": project, "task": task, "mode": "build"},
                          headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=900)
        d = r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"dispatcher unreachable: {e}"}
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "build failed")}
    return {"ok": True, "summary": (d.get("summary") or "")[:400],
            "changed": d.get("changed", []), "secs": d.get("secs")}


@mcp.tool()
def dispatch_build(project: str, task: str, run_id: str, plan: str = "") -> dict:
    """Dispatch a coding task to the Builder in `project` (uncommitted edits). Runs only after the
    human gate approves; never commits/pushes. Returns {ok, summary, changed}."""
    return dispatch_build_impl(project, task, run_id, plan)


if __name__ == "__main__":
    mcp.run()
