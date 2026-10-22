"""Open WebUI sessions (#1113, docs/prd/owui-sessions.md §6).

The NOMAD pipe in Open WebUI (owui/nomad_pipe.py) asks GET /owui/models for its dropdown: one
entry per dispatchable project, then `+ New project`, then `General`. A project marked
`local_only: true` in projects.yaml never leaves the local models. Since §6.5 these entries fill
the Project dropdown, not the model dropdown, and General means no project (any model).

The pipe sends a shared token in X-Nomad-Token. It lives in NOMAD_OWUI_TOKEN or the file
`owui-token` under ~/.config/nomad (mounted at /term-secret in the container), and Open WebUI
keeps a copy in the pipe's valve. With no token configured, every /owui route refuses: an
unwired door must not open.
"""
import os
import secrets

TOKEN_FILES = ("/term-secret/owui-token", os.path.expanduser("~/.config/nomad/owui-token"))

NEW_PROJECT = "new-project"
GENERAL = "general"
PROJECT_PREFIX = "p-"


def token() -> str:
    """The shared token, read per request so a new token works without a restart; '' if none."""
    env = os.environ.get("NOMAD_OWUI_TOKEN", "").strip()
    if env:
        return env
    for cand in TOKEN_FILES:
        try:
            tok = open(cand).read().strip()
        except OSError:
            continue
        if tok:
            return tok
    return ""


def check(given: str | None) -> tuple:
    """(status, message) when a request must be refused, else (0, "")."""
    want = token()
    if not want:
        return 503, ("No Open WebUI token is configured (NOMAD_OWUI_TOKEN or "
                     "~/.config/nomad/owui-token), so /owui is closed.")
    if not given or not secrets.compare_digest(given.strip(), want):
        return 401, "X-Nomad-Token missing or wrong."
    return 0, ""


def models(detail: list | None) -> dict:
    """The dropdown entries. `detail` is the dispatcher's /projects detail list, or None when
    the dispatcher can't be asked: then only `+ New project` and General are offered, and
    `projects_ok` says the list is incomplete, so the pipe doesn't cache it for long."""
    rows = []
    for d in sorted(detail or [], key=lambda d: str(d.get("name") or d.get("id") or "").lower()):
        pid = str(d.get("id") or "")
        if not pid:
            continue
        rows.append({"id": PROJECT_PREFIX + pid, "name": f"NOMAD · {d.get('name') or pid}",
                     "kind": "project", "project": pid,
                     "local_only": d.get("local_only") is True})
    rows.append({"id": NEW_PROJECT, "name": "+ New project", "kind": "new-project",
                 "project": None, "local_only": False})
    rows.append({"id": GENERAL, "name": "General", "kind": "general",
                 "project": None, "local_only": False})
    return {"models": rows, "projects_ok": detail is not None}
