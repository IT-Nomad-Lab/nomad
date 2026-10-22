"""Tagging a project with a type without changing its configuration (#1393, PRD 6.5, A7).

Operator rule: tagging keeps the existing configuration. A tag is planned in three steps:

  1. before = the project compiled as it is now (its current type, its legacy-<id> policy, the
     policies its projects.yaml entry attaches);
  2. after = the same with the new type's policies;
  3. every MCP server the new type would add is held back by a deny in legacy-<id>
     (`legacy-hold-mcp-<channel>`), so the after side compiles to the before side.

What the type would add or remove is listed as proposed changes. The tag saves with every add
held; `accept` then lifts one hold at a time, so the operator takes each change on its own. A
change that no hold can stop (a server the type denies, a setting or permission rule that
moves) makes the plan refuse: `TagPlan.ok` is false and `save` raises.

Skill grants are not compiled to any enforcement point yet, so they are not part of the diff.

Writes only the repo: policies/legacy/legacy-<id>.yaml and the entry's `type:` line in
projects.yaml. Live grants (mcp-grants.json, the gateway) are left alone: a saved tag compiles
to what is live today, and an accepted change shows up in `policy-compile.py --check` until it
is deployed.
"""
import dataclasses
import difflib
import os
import re

import yaml

from nomad_policy import compiler, legacy, schema

LEGACY_DIR = os.path.join(schema.POLICIES_DIR, "legacy")   # policies/legacy/legacy-<id>.yaml
HOLD = "legacy-hold-mcp-"                                  # + channel


@dataclasses.dataclass(frozen=True)
class Change:
    """One thing the type would change: an MCP server added to or removed from one channel."""
    channel: str
    server: str
    op: str             # add | remove

    @property
    def id(self):
        return f"mcp:{self.channel}:{'+' if self.op == 'add' else '-'}{self.server}"


@dataclasses.dataclass(frozen=True)
class TagPlan:
    project: str
    type: str
    legacy: dict                # the legacy-<id> policy to save, holds included
    before: dict                # compiled fields now
    after: dict                 # compiled fields with the tag and the holds
    proposed: tuple             # Changes the type would make, none of them applied
    refused: tuple              # why the tag cannot be saved; empty = it can

    @property
    def ok(self):
        return not self.refused

    def diff(self):
        """The before/after diff as unified-diff lines; empty when the tag keeps everything."""
        return list(difflib.unified_diff(compiler.render(self.before).splitlines(),
                                         compiler.render(self.after).splitlines(),
                                         f"{self.project} now", f"{self.project} as {self.type}",
                                         lineterm="", n=2))


def legacy_path(pid, root=LEGACY_DIR):
    return os.path.join(root, f"legacy-{pid}.yaml")


def current_legacy(baseline, root=LEGACY_DIR):
    """The project's saved legacy-<id> policy (raw), else the one built from its baseline."""
    path = legacy_path(baseline["id"], root)
    if os.path.exists(path):
        with open(path) as f:
            return yaml.safe_load(f)
    return legacy.legacy_policy(baseline)


def holds(raw):
    """{channel: set of servers} a legacy policy holds back."""
    return {s["when"]["channel"][0]: set(s["match"]) for s in raw.get("statements") or []
            if s.get("sid", "").startswith(HOLD)}


def with_holds(raw, held):
    """`raw` with its hold statements replaced by `held` ({channel: servers})."""
    out = dict(raw)
    keep = [s for s in raw.get("statements") or [] if not s.get("sid", "").startswith(HOLD)]
    keep += [{"sid": HOLD + c, "effect": "deny", "kind": "mcp", "match": sorted(names),
              "when": {"channel": [c]}} for c, names in sorted(held.items()) if names]
    out.pop("statements", None)
    if keep:
        out["statements"] = keep
    return out


def _compile(project, project_type, raw, baseline, policies, registry, universe):
    layers = compiler.layers_for(project_type, policies,
                                 (schema.validate(raw, registry),) + tuple(policies[x] for x in project.policies))
    got, _ = compiler.compile_project(project, layers, universe, baseline["mcp"])
    return got


def _servers(fields):
    mcp = fields["mcp"]
    return {"gateway": set(mcp["gateway"]["servers"] or []), "local": set(mcp["local_model_set"] or []),
            "repo": set(mcp["repo_mcp_json"] or []) - {legacy.GATEWAY_ENTRY}}


def plan(project, new_type, baseline, policies, registry, universe, root=LEGACY_DIR):
    """The TagPlan for giving `project` (autopilot.projects.Project) the type `new_type`."""
    raw = current_legacy(baseline, root)
    before = _compile(project, project.type, raw, baseline, policies, registry, universe)
    bare = with_holds(raw, {})
    open_ = _compile(project, new_type, bare, baseline, policies, registry, universe)
    was, now = _servers(before), _servers(open_)
    proposed = tuple(Change(c, s, "add") for c in sorted(now) for s in sorted(now[c] - was[c])) + \
        tuple(Change(c, s, "remove") for c in sorted(was) for s in sorted(was[c] - now[c]))
    held = {c: now[c] - was[c] for c in now}
    new = with_holds(raw, held)
    after = _compile(project, new_type, new, baseline, policies, registry, universe)
    refused = tuple(f"{ch.id}: the type removes it and no hold can keep it"
                    for ch in proposed if ch.op == "remove")
    if after != before and not refused:
        refused = ("the type changes settings or permission rules that no hold can keep",)
    return TagPlan(project.id, new_type, new, before, after, proposed, refused)


def held_ids(raw):
    """The change ids a legacy policy holds back, sorted."""
    return sorted(Change(c, s, "add").id for c, names in holds(raw).items() for s in names)


def lift(raw, change_id):
    """`raw` with the hold on `change_id` lifted. Removes are not held, so only adds can be."""
    held = holds(raw)
    hit = [(c, s) for c, names in held.items() for s in names if Change(c, s, "add").id == change_id]
    if not hit:
        raise ValueError(f"{raw.get('id')}: no held change {change_id!r}; held: "
                         + (", ".join(held_ids(raw)) or "none"))
    c, s = hit[0]
    held[c].discard(s)
    return with_holds(raw, held)


def accept(pid, change_id, root=LEGACY_DIR):
    """Lift one hold from a saved legacy-<id> policy: the type's add `change_id` takes effect.
    Returns the new raw policy (written)."""
    path = legacy_path(pid, root)
    with open(path) as f:
        raw = yaml.safe_load(f)
    new = lift(raw, change_id)
    _write_policy(path, new)
    return new


def policy_text(raw):
    """The file text of a legacy-<id> policy, header included."""
    return (f"# Written by nomad_policy/tag.py (#1393): {raw['id']}, the snapshot that keeps this\n"
            "# project's configuration through its tag. legacy-hold-* statements hold back what\n"
            "# the type would add; nomad_policy.tag.accept lifts them one at a time.\n"
            + yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))


def _write_policy(path, raw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(policy_text(raw))


def set_type(text, pid, new_type):
    """projects.yaml text with `type: new_type` on the entry `pid` (replaced or added after its
    id line). Comments and every other line stay as they are."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"\s*- id: {re.escape(pid)}\s*(#.*)?$", l)), None)
    if start is None:
        raise ValueError(f"projects.yaml: no entry {pid!r}")
    indent = " " * (len(lines[start]) - len(lines[start].lstrip()) + 2)
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(indent)):
        end += 1
    for i in range(start + 1, end):
        if re.match(rf"{indent}type:", lines[i]):
            lines[i] = f"{indent}type: {new_type}\n"
            return "".join(lines)
    lines.insert(start + 1, f"{indent}type: {new_type}\n")
    return "".join(lines)


def save(p, registry_file, root=LEGACY_DIR):
    """Write a TagPlan: the legacy-<id> policy and the entry's `type:`. Refuses a plan that is
    not ok, or whose before/after diff is not empty."""
    if not p.ok:
        raise ValueError(f"{p.project}: tag refused: " + "; ".join(p.refused))
    if p.diff():
        raise ValueError(f"{p.project}: tag refused: the before/after diff is not empty")
    _write_policy(legacy_path(p.project, root), p.legacy)
    with open(registry_file) as f:
        text = f.read()
    with open(registry_file, "w") as f:
        f.write(set_type(text, p.project, p.type))
