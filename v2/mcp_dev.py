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
mcp = FastMCP("nomad-dev")


def dispatch_build_impl(project: str, task: str, run_id: str, plan: str = "") -> dict:
    """Dispatch a build task to the Builder (uncommitted edits). Gated; runs only after approval."""
    try:
        r = requests.post(f"{DISPATCH}/dispatch",
                          json={"project": project, "task": task, "mode": "build"}, timeout=900)
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
