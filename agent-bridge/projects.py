"""Project id → working directory, from the registry (projects.yaml) and nothing else.

General chat, or no project, is ~/nomad. An id the registry doesn't list is refused, so a chat can't
point the agent at an arbitrary folder. `local_only` projects are refused too: their chats never
leave the machine (docs/prd/owui-sessions.md, #1119) and this agent runs on Claude.
"""
import os
import sys

HOME = os.path.expanduser("~")
HUB = os.path.join(HOME, "nomad")


def _registry():
    sys.path.insert(0, os.path.join(HUB, "dispatcher"))
    try:
        import projects_registry as pr
    finally:
        sys.path.pop(0)
    f = pr.registry_file([HOME], os.environ.get("NOMAD_REGISTRY", ""))
    if not f:
        raise RuntimeError("projects.yaml not found")
    return pr.read_entries(f)


def resolve(project: str, entries=None) -> tuple[str, str]:
    """(project id, absolute cwd). Raises ValueError with a sentence for the chat."""
    pid = (project or "").strip()
    if pid.startswith("p-"):
        pid = pid[2:]
    if pid in ("", "general", "new-project"):
        return "general", HUB
    rows = entries if entries is not None else _registry()
    row = next((r for r in rows if r["id"] == pid or pid in (r.get("aliases") or [])), None)
    if not row:
        raise ValueError(f"project {pid!r} is not in the registry, so the agent has no folder for it")
    if row.get("local_only") is True:
        raise ValueError(f"{row['id']} is a local-only project: its chats never go to a cloud model, "
                         "and the agent runs on Claude. Use a local model there.")
    path = row["path"]
    if not os.path.isdir(path):
        raise ValueError(f"{row['id']}: folder {path} does not exist")
    return row["id"], path
