"""Engineering-crew tools — the hands of the dev team.

The engineers don't free-hand code in an LLM loop; they DISPATCH precise tasks to
Claude Code running inside the target repo (the proven dispatcher pipeline) and reason
over the returned diff and test output. Edits land UNCOMMITTED for human review —
commit/push stay human-gated, same guardrail as the interactive Builder.
"""
import os

import requests
from crewai.tools import tool

DISPATCH = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090")
TIMEOUT = int(os.environ.get("NOMAD_DEV_TIMEOUT", "900"))


def _dispatch(project, task, mode):
    try:
        r = requests.post(f"{DISPATCH}/dispatch",
                          json={"project": project, "task": task, "mode": mode},
                          timeout=TIMEOUT)
        return r.json()
    except requests.RequestException as e:
        return {"ok": False, "error": f"dispatcher unreachable: {e}"}


@tool("dispatch_plan")
def dispatch_plan(project: str, task: str) -> str:
    """Produce a READ-ONLY implementation plan for a task inside a repo (no edits).
    Use this first to scope a change. `project` is a NOMAD project name; `task` is a
    clear, specific instruction. Returns the plan."""
    res = _dispatch(project, task, "plan")
    if not res.get("ok"):
        return f"PLAN FAILED: {res.get('error')}"
    return f"PLAN ({res['project']}, {res.get('secs')}s):\n{res.get('summary', '')}"


@tool("dispatch_build")
def dispatch_build(project: str, task: str) -> str:
    """Implement a task by editing files inside a repo via Claude Code (UNCOMMITTED).
    `project` is a NOMAD project name; `task` must be a precise, self-contained
    instruction (the engineer's spec). Returns a summary plus the list of changed files
    and a diffstat. NEVER commits or pushes — the human reviews the working tree."""
    res = _dispatch(project, task, "build")
    if not res.get("ok"):
        return f"BUILD FAILED: {res.get('error')}"
    changed = "\n".join(res.get("changed", [])) or "(no file changes)"
    return (f"BUILT ({res['project']}, {res.get('secs')}s):\n{res.get('summary', '')}\n\n"
            f"Changed files:\n{changed}\n\n{res.get('diffstat', '')}")


@tool("list_dev_projects")
def list_dev_projects() -> str:
    """List the repos the dev team can build in (NOMAD-marked projects)."""
    try:
        r = requests.get(f"{DISPATCH}/projects", timeout=20)
        return "Buildable projects: " + ", ".join(r.json().get("projects", []))
    except requests.RequestException as e:
        return f"Could not reach dispatcher: {e}"


@tool("run_check")
def run_check(project: str, command: str) -> str:
    """Run a verification command INSIDE a repo and return its real exit code + output.
    This is how QA PROVES a change works — use it to run things like:
      • `git diff --stat` / `git diff`  → prove the change is additive-only
      • `python3 -m py_compile <files>` → prove the code parses
      • `pytest -q` or the project's test command → prove tests pass
      • `python3 -c "<assertions>"`     → prove specific behavior
    Sandboxed: destructive/irreversible commands (rm, git commit/push/checkout, docker,
    sudo, kill…) and non-local network are BLOCKED — it only RUNS checks, never mutates.
    Report the actual rc and output as your evidence; do not claim a pass you didn't run."""
    try:
        r = requests.post(f"{DISPATCH}/verify",
                          json={"project": project, "command": command}, timeout=180)
        res = r.json()
    except requests.RequestException as e:
        return f"CHECK ERROR: dispatcher unreachable: {e}"
    if not res.get("ok"):
        return f"CHECK REFUSED: {res.get('error')}"
    out = (res.get("stdout") or "").strip() or "(no stdout)"
    err = (res.get("stderr") or "").strip()
    tail = f"\nstderr:\n{err}" if err else ""
    verdict = "PASS (rc=0)" if res.get("rc") == 0 else f"FAIL (rc={res.get('rc')})"
    return f"CHECK {verdict} in {res.get('secs')}s:\n{out}{tail}"
