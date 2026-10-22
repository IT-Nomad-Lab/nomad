"""Where nomad/projects.yaml lives and how its entries resolve. No side effects on import.

Shared by the dispatcher (dispatch.py) and the autopilot (autopilot/projects.py) so there is
one rule for finding the registry and turning an entry's `path` into an absolute folder:
- the file is `nomad/projects.yaml` under the first root that has one, unless an explicit
  override path is given (NOMAD_REGISTRY);
- entry paths are relative to the folder that holds nomad/, so the same file works on the
  host (~) and in the console container (/host).
"""
import os

DEFAULT_TYPE = "generic"    # an entry without `type:` (profiles/generic.yaml: today's behaviour)


def entry_type(entry):
    """The project type an entry names (`type:` in projects.yaml), "generic" when it has none.
    No profile check here (the dispatcher must keep working on a registry it cannot validate);
    autopilot/projects.py refuses an unknown type."""
    t = entry.get("type")
    return str(t).strip() if t is not None and str(t).strip() else DEFAULT_TYPE


def registry_file(roots, override=""):
    """The registry path, or None. `override` wins when set (and must exist)."""
    if override:
        return override if os.path.isfile(override) else None
    for root in roots:
        f = os.path.join(root, "nomad", "projects.yaml")
        if os.path.isfile(f):
            return f
    return None


def registry_base(f):
    """The folder entry paths are relative to: the parent of the folder holding the file."""
    return os.path.dirname(os.path.dirname(os.path.abspath(f)))


def read_entries(f, base=None):
    """The raw `projects:` list of `f`, keeping only mappings with an id and a path, each with
    `path` made absolute against `base` (default: registry_base(f)). Raises on an unreadable or
    unparseable file."""
    import yaml
    with open(f, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{f}: top level must be a mapping with a `projects:` list")
    base = base or registry_base(f)
    out = []
    for e in data.get("projects") or []:
        if not isinstance(e, dict) or not e.get("id") or not e.get("path"):
            continue
        out.append({**e, "id": str(e["id"]), "path": os.path.join(base, str(e["path"]))})
    return out
