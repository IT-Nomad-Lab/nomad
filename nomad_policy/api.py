"""The cockpit's policy operations (#1396, PRD docs/prd/project-types-and-inline-asks.md 6.5, A8).

Read: list, get, effective access per project (each item with the statement that decided it),
the simulator (project + kind + name -> allow, ask or deny), and the tag dry run
(nomad_policy/tag.py). Write: save a policy, attach or detach one to a type or a project.

Every write lands on a branch of the nomad repo (`policy/<slug>`), never on master. The branch
is made in a throwaway worktree, so the shared checkout is not touched, and the result carries
the diff against master. Merging stays a yellow action (docs/SDLC.md).

Guardrails. A guardrail is a policy with role guardrail, or any file in policies/ itself
(the guardrails and the worker base, which every project gets). `save` refuses them; only
`save_guardrail` writes them, and it needs `confirm` equal to the policy id. The dispatcher
calls it only for a request carrying the cockpit token (dispatcher/dispatch.py), and the
hooks deny agents the token and the route (#1396).

Refused everywhere: `legacy-*` policies, which belong to the tag operation, and attaching a
guardrail (it applies to every project already).

The dispatcher runs this on the host: the engine container has neither the repo nor git.
"""
import datetime
import difflib
import json
import os
import re
import subprocess
import tempfile

import yaml

from nomad_policy import compiler, schema, tag
from nomad_policy import evaluator as ev

MASTER = os.environ.get("NOMAD_HUB_BRANCH", "master")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_YAML = 64 * 1024


class ApiError(ValueError):
    """A refused request. `status` is the HTTP code the route answers with."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.status = status


# ---- the repo ----------------------------------------------------------------------------------

def _paths(root):
    pol = os.path.join(root, "policies")
    return {"policies": pol, "types": os.path.join(pol, "types"), "legacy": os.path.join(pol, "legacy"),
            "profiles": os.path.join(root, "profiles"), "projects": os.path.join(root, "projects.yaml"),
            "baseline": os.path.join(pol, "baseline")}


def _load(root, registry):
    """{id: (Policy, path relative to root)} for policies/ and policies/types/."""
    p = _paths(root)
    out = {}
    for d in (p["policies"], p["types"]):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith(".yaml"):
                    path = os.path.join(d, f)
                    out[f[:-5]] = (schema.load_file(path, registry), os.path.relpath(path, root))
    return out


def _guardrail(policy, rel):
    """A guardrail by role, or by living in policies/ itself."""
    return (policy is not None and policy.role == "guardrail") or (rel or "").count("/") == 1


def _profiles(root):
    d = _paths(root)["profiles"]
    out = {}
    for f in sorted(os.listdir(d)):
        if f.endswith(".yaml"):
            with open(os.path.join(d, f)) as fh:
                out[f[:-5]] = yaml.safe_load(fh) or {}
    return out


def _projects_raw(root):
    with open(_paths(root)["projects"]) as f:
        return {e["id"]: e for e in (yaml.safe_load(f) or {}).get("projects") or [] if isinstance(e, dict)}


def _attachments(root, pid):
    types = sorted(t for t, d in _profiles(root).items() if pid in (d.get("policies") or []))
    projs = sorted(i for i, e in _projects_raw(root).items() if pid in (e.get("policies") or []))
    return {"types": types, "projects": projs}


# ---- read --------------------------------------------------------------------------------------

def list_policies(root=schema.NOMAD, registry=None):
    reg = registry or schema.Registry.live()
    out = []
    for pid, (p, rel) in _load(root, reg).items():
        out.append({"id": pid, "role": p.role, "description": p.description, "file": rel,
                    "guardrail": _guardrail(p, rel), "statements": len(p.statements),
                    "settings": sorted(p.settings), "attached": _attachments(root, pid)})
    # What the cockpit's editor offers (#1397): the attach targets and the schema's choices.
    return {"ok": True, "policies": out, "types": sorted(_profiles(root)),
            "projects": sorted(_projects_raw(root)),
            "schema": {"roles": list(schema.ROLES), "effects": list(schema.EFFECTS),
                       "kinds": list(schema.KINDS), "when_keys": list(schema.WHEN_KEYS),
                       "setting_keys": list(schema.SETTING_KEYS), "channels": list(schema.CHANNELS),
                       "tiers": list(schema.TIERS)}}


def get_policy(pid, root=schema.NOMAD, registry=None):
    reg = registry or schema.Registry.live()
    pols = _load(root, reg)
    if pid not in pols:
        raise ApiError(f"no policy {pid!r}", 404)
    p, rel = pols[pid]
    with open(os.path.join(root, rel)) as f:
        text = f.read()
    out = {"ok": True, "id": pid, "role": p.role, "file": rel, "guardrail": _guardrail(p, rel),
           "yaml": text, "policy": yaml.safe_load(text), "attached": _attachments(root, pid)}
    out["pending"] = _pending(root, f"policy/{pid}", rel)
    return out


def _pending(root, branch, rel):
    """The unmerged edit on `branch`: {branch, ahead, yaml}, or None. The editor continues from it,
    so a second save does not drop the first one."""
    if _base_ref(root, branch) != branch:
        return None
    ahead = _git(root, "rev-list", "--count", f"{MASTER}..{branch}")[1].strip()
    text = _read_ref(root, branch, rel)
    if not ahead.isdigit() or ahead == "0" or text is None:
        return None
    return {"branch": branch, "ahead": int(ahead), "yaml": text}


def validate(pid, text, registry=None, root=schema.NOMAD):
    """Parse and check a policy's text without saving it: the editor's YAML to form step."""
    _check_id(pid)
    reg = registry or schema.Registry.live()
    p = _parse_policy(pid, text, reg)
    rel, was = None, None
    for cand in (f"policies/{pid}.yaml", f"policies/types/{pid}.yaml"):
        was = _read_ref(root, MASTER, cand)
        if was is not None:
            rel = cand
            break
    rel = rel or (f"policies/{pid}.yaml" if p.role == "guardrail" else f"policies/types/{pid}.yaml")
    text = text if text.endswith("\n") else text + "\n"
    diff = "".join(difflib.unified_diff((was or "").splitlines(keepends=True), text.splitlines(keepends=True),
                                        f"a/{rel}", f"b/{rel}"))
    return {"ok": True, "id": pid, "role": p.role, "policy": yaml.safe_load(text), "file": rel, "diff": diff}


def _project(root, pid):
    from autopilot import projects
    for p in projects.load(_paths(root)["projects"], base=os.path.expanduser(projects.HUB_BASE)):
        if p.id == pid:
            return p
    raise ApiError(f"no project {pid!r}", 404)


def _layers(root, pid, registry):
    """(Project, Layers) as the compiler builds them: type, legacy-<id> when there is a baseline,
    the policies the projects.yaml entry attaches."""
    p = _project(root, pid)
    pols = {k: v for k, (v, _) in _load(root, registry).items()}
    attached = tuple(pols[x] for x in p.policies)
    path = os.path.join(_paths(root)["baseline"], pid + ".json")
    if os.path.exists(path):
        with open(path) as f:
            base = json.load(f)
        raw = tag.current_legacy(base, _paths(root)["legacy"])
        layers = compiler.layers_for(p.type, pols, (schema.validate(raw, registry),) + attached)
    else:
        layers = compiler.layers_for(p.type, pols, attached)
    return p, layers


def _decision(d):
    return {"effect": d.effect, "reason": d.reason, "layer": d.layer, "policy": d.policy,
            "statement": d.index, "sid": d.sid}


def effective(pid, root=schema.NOMAD, registry=None, universe=None):
    """Every item the project could be given, allowed or not, with the deciding statement."""
    reg = registry or schema.Registry.live()
    uni = universe or compiler.Universe.live()
    p, layers = _layers(root, pid, reg)
    items = []
    for c in schema.CHANNELS:
        for name in sorted(getattr(uni, c)):
            items.append({"kind": "mcp", "name": name, "channel": c,
                          **_decision(ev.decide(layers, "mcp", name, {"channel": c}))})
    for name in sorted(reg.skills):
        items.append({"kind": "skill", "name": name, **_decision(ev.decide(layers, "skill", name))})
    seen = set()
    for _, pol in layers.walk():                     # rules named by some statement, as written
        for st in pol.statements:
            if st.kind in ("mcp", "skill"):
                continue
            for m in st.match:
                if (st.kind, m) not in seen:
                    seen.add((st.kind, m))
                    items.append({"kind": st.kind, "name": m, **_decision(ev.decide(layers, st.kind, m))})
    settings = {k: {"value": s.value, "layer": s.layer, "policy": s.policy, "locked": s.locked,
                    "ignored": [list(x) for x in s.ignored]}
                for k, s in ev.effective_settings(layers).items()}
    return {"ok": True, "project": pid, "type": p.type,
            "layers": {name: [x.id for x in getattr(layers, name)] for name in ev.LAYERS},
            "items": items, "settings": settings}


def simulate(pid, kind, name, context=None, root=schema.NOMAD, registry=None):
    if kind not in schema.KINDS:
        raise ApiError(f"kind must be one of {', '.join(schema.KINDS)}")
    if not isinstance(name, str) or not name:
        raise ApiError("name must be a non-empty string")
    ctx = context or {}
    if not isinstance(ctx, dict) or set(ctx) - set(schema.WHEN_KEYS):
        raise ApiError(f"context keys must be among {', '.join(schema.WHEN_KEYS)}")
    reg = registry or schema.Registry.live()
    _, layers = _layers(root, pid, reg)
    return {"ok": True, "project": pid, "kind": kind, "name": name, "context": ctx,
            **_decision(ev.decide(layers, kind, name, {k: str(v) for k, v in ctx.items()}))}


def tag_dry_run(pid, new_type, root=schema.NOMAD, registry=None, universe=None):
    from nomad_projects import profiles
    reg = registry or schema.Registry.live()
    p = _project(root, pid)
    try:
        profiles.load(new_type, _paths(root)["profiles"])
    except profiles.ProfileError as e:
        raise ApiError(str(e)) from None
    path = os.path.join(_paths(root)["baseline"], pid + ".json")
    if not os.path.exists(path):
        raise ApiError(f"{pid} has no baseline (policies/baseline/{pid}.json); run config-snapshot.py", 409)
    with open(path) as f:
        base = json.load(f)
    pols = {k: v for k, (v, _) in _load(root, reg).items()}
    plan = tag.plan(p, new_type, base, pols, reg, universe or compiler.Universe.live(), _paths(root)["legacy"])
    return {"ok": True, "project": pid, "type": new_type, "can_save": plan.ok, "diff": plan.diff(),
            "proposed": [c.id for c in plan.proposed], "refused": list(plan.refused),
            "saved": tag_state(pid, root)}


def _tag_branch(pid):
    return f"policy/tag-{pid}"


def tag_state(pid, root=schema.NOMAD):
    """The project's saved tag, on its tag branch or else on master: {branch, ref, type, held},
    or None when it has never been tagged."""
    branch = _tag_branch(pid)
    ref = _base_ref(root, branch)
    text = _read_ref(root, ref, f"policies/legacy/legacy-{pid}.yaml")
    if text is None:
        return None
    reg_text = _read_ref(root, ref, "projects.yaml") or ""
    entry = next((e for e in (yaml.safe_load(reg_text) or {}).get("projects") or []
                  if isinstance(e, dict) and e.get("id") == pid), {})
    return {"branch": branch if ref == branch else None, "ref": ref, "type": entry.get("type"),
            "held": tag.held_ids(yaml.safe_load(text) or {})}


def tag_save(pid, new_type, root=schema.NOMAD, registry=None, universe=None):
    """Save a tag on branch policy/tag-<id>: the legacy-<id> policy with its holds, and the entry's
    type: line. Refused unless the plan is ok and its before/after diff is empty."""
    head = _git(root, "rev-parse", "--abbrev-ref", "HEAD")[1].strip()
    if head != MASTER:                    # the plan reads the checkout; the branch is cut from master
        raise ApiError(f"the hub checkout is on {head}, not {MASTER}: the tag is planned from it, "
                       "so try again when it is back on master", 409)
    branch = _tag_branch(pid)
    if _base_ref(root, branch) == branch:
        raise ApiError(f"{pid} already has a saved tag on {branch}: accept its changes there, or merge "
                       "or delete the branch before tagging again", 409)
    plan_out = tag_dry_run(pid, new_type, root, registry, universe)
    if not plan_out["can_save"]:
        raise ApiError(f"{pid}: tag refused: " + "; ".join(plan_out["refused"]), 409)
    if plan_out["diff"]:
        raise ApiError(f"{pid}: tag refused: the before/after diff is not empty", 409)
    reg = registry or schema.Registry.live()
    p = _project(root, pid)
    with open(os.path.join(_paths(root)["baseline"], pid + ".json")) as f:
        base = json.load(f)
    pols = {k: v for k, (v, _) in _load(root, reg).items()}
    plan = tag.plan(p, new_type, base, pols, reg, universe or compiler.Universe.live(), _paths(root)["legacy"])
    reg_text = _read_ref(root, MASTER, "projects.yaml") or ""
    try:
        reg_new = tag.set_type(reg_text, pid, new_type)
    except ValueError as e:
        raise ApiError(str(e), 404) from None
    rel = f"policies/legacy/legacy-{pid}.yaml"
    res = _on_branch(root, branch, {rel: tag.policy_text(plan.legacy), "projects.yaml": reg_new},
                     f"Tag {pid} as {new_type} in the cockpit ({_today()})\n\n"
                     "The before/after diff is empty: every add the type would make is held in "
                     f"legacy-{pid}. Saved by the cockpit's tag flow (#1398). Merging is a yellow action.")
    return {"ok": True, "project": pid, "type": new_type, "held": tag.held_ids(plan.legacy),
            "files": [rel, "projects.yaml"], **res}


def tag_accept(pid, change_id, root=schema.NOMAD):
    """Lift one held change from the project's saved tag, on branch policy/tag-<id>. This one is
    meant to change the configuration: the diff shows the line it adds."""
    if not isinstance(change_id, str) or not change_id.startswith("mcp:"):
        raise ApiError("change must be a held change id, e.g. mcp:gateway:+figma")
    _project(root, pid)
    branch = _tag_branch(pid)
    rel = f"policies/legacy/legacy-{pid}.yaml"
    text = _read_ref(root, _base_ref(root, branch), rel)
    if text is None:
        raise ApiError(f"{pid} has no saved tag; save the tag first", 409)
    try:
        new = tag.lift(yaml.safe_load(text) or {}, change_id)
    except ValueError as e:
        raise ApiError(str(e), 409) from None
    res = _on_branch(root, branch, {rel: tag.policy_text(new)},
                     f"Tag {pid}: accept {change_id} in the cockpit ({_today()})\n\n"
                     "Lifts one hold, so the type's add takes effect once merged and deployed. "
                     "Saved by the cockpit's tag flow (#1398). Merging is a yellow action.")
    return {"ok": True, "project": pid, "change": change_id, "held": tag.held_ids(new), **res}


# ---- write: on a branch ------------------------------------------------------------------------

def _git(cwd, *args):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def _base_ref(root, branch):
    """What a write builds on: the branch when it exists, else master. Never the working copy,
    which other sessions switch."""
    return branch if _git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] == 0 else MASTER


def _read_ref(root, ref, rel):
    """A file's text at `ref`, or None when it is not there."""
    rc, out = _git(root, "show", f"{ref}:{rel}")
    return None if rc else out


def _on_branch(root, branch, files, message):
    """Write {relative path: text} on `branch` (made from master if new) and commit. Returns
    {branch, commit, diff}. The worktree is temporary; the branch stays for review."""
    if _git(root, "check-ref-format", "--branch", branch)[0]:
        raise ApiError(f"not a usable branch name: {branch!r}")
    have = _git(root, "rev-parse", "--verify", "-q", f"refs/heads/{branch}")[0] == 0
    wt = tempfile.mkdtemp(prefix="nomad-policy-")
    os.rmdir(wt)
    rc, out = _git(root, "worktree", "add", "-q", wt, *([branch] if have else ["-b", branch, MASTER]))
    if rc:
        raise ApiError(f"git worktree add failed: {out[-300:]}", 500)
    try:
        for rel, text in files.items():
            path = os.path.join(wt, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)
        _git(wt, "add", "--", *files)
        if _git(wt, "diff", "--cached", "--quiet")[0]:
            rc, out = _git(wt, "-c", "user.email=nomad@itnomadlab.com", "-c", "user.name=NOMAD",
                           "commit", "-q", "-m", message)
            if rc:
                raise ApiError(f"commit failed: {out[-300:]}", 500)
        commit = _git(wt, "rev-parse", "--short", "HEAD")[1].strip()
        diff = _git(wt, "diff", f"{MASTER}...HEAD", "--", *files)[1]
    finally:
        _git(root, "worktree", "remove", "--force", wt)
    return {"branch": branch, "commit": commit, "diff": diff}


def _today():
    return datetime.date.today().isoformat()


def _parse_policy(pid, text, registry):
    if not isinstance(text, str) or not text.strip():
        raise ApiError("yaml must be the policy file's text")
    if len(text) > MAX_YAML:
        raise ApiError(f"yaml is over {MAX_YAML} bytes")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ApiError(f"invalid YAML: {e}") from None
    try:
        return schema.validate(raw, registry, expect_id=pid)
    except schema.PolicyError as e:
        raise ApiError(str(e)) from None


def _check_id(pid):
    if not isinstance(pid, str) or not ID_RE.match(pid):
        raise ApiError(f"not a policy id: {pid!r}")
    if pid.startswith("legacy-"):
        raise ApiError(f"{pid} belongs to the tag operation (scripts/policy-tag.py); it is not edited here", 403)


def _save(pid, text, root, registry, guardrail_ok):
    _check_id(pid)
    reg = registry or schema.Registry.live()
    new = _parse_policy(pid, text, reg)
    branch = f"policy/{pid}"
    old, rel, is_guard = None, None, new.role == "guardrail"
    for ref in dict.fromkeys((MASTER, _base_ref(root, branch))):     # as master has it, and the branch
        for cand in (f"policies/{pid}.yaml", f"policies/types/{pid}.yaml"):
            have = _read_ref(root, ref, cand)
            if have is None:
                continue
            try:
                was = schema.validate(yaml.safe_load(have), reg, expect_id=pid)
            except (schema.PolicyError, yaml.YAMLError):
                was = None
            is_guard = is_guard or _guardrail(was, cand) or "role: guardrail" in have
            old, rel = old or was, rel or cand
    if rel is None:
        rel = f"policies/{pid}.yaml" if new.role == "guardrail" else f"policies/types/{pid}.yaml"
    if is_guard and not guardrail_ok:
        raise ApiError(f"{pid} is a guardrail: only the operator saves it, from the cockpit, "
                       "with an explicit confirmation", 403)
    if not is_guard and guardrail_ok:
        raise ApiError(f"{pid} is not a guardrail; save it with the ordinary route")
    if old is not None and new.role != old.role and _attachments(root, pid)["types"] + _attachments(root, pid)["projects"]:
        raise ApiError(f"{pid} is attached; detach it before changing its role")
    text = text if text.endswith("\n") else text + "\n"
    res = _on_branch(root, branch, {rel: text},
                     f"Policy {pid}: {'new' if old is None else 'edited'} in the cockpit ({_today()})\n\n"
                     "Saved by the cockpit's policy editor (#1396). Merging is a yellow action"
                     + (", and for a guardrail red: the operator merges it." if is_guard else "."))
    return {"ok": True, "id": pid, "file": rel, "guardrail": is_guard, **res}


def save(pid, text, root=schema.NOMAD, registry=None):
    """Save a type or project policy on branch policy/<id>. Guardrails are refused."""
    return _save(pid, text, root, registry, False)


def save_guardrail(pid, text, confirm, root=schema.NOMAD, registry=None):
    """Save a guardrail on branch policy/<id>. `confirm` must equal the id: the operator typed
    it. The caller has already checked that the request came from the cockpit."""
    if confirm != pid:
        raise ApiError(f"a guardrail save needs confirm={pid!r}, typed by the operator", 403)
    return _save(pid, text, root, registry, True)


POLICIES_LINE = re.compile(r"^(?P<indent>\s*)policies:\s*\[(?P<ids>[^\]]*)\](?P<rest>.*)$")


def _profile_with(text, ids):
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = POLICIES_LINE.match(line.rstrip("\n"))
        if m and not m.group("indent"):
            lines[i] = f"policies: [{', '.join(ids)}]{m.group('rest')}\n"
            return "".join(lines)
    raise ApiError("the profile has no one-line `policies: [...]`; edit it by hand", 409)


def _project_with(text, pid, ids):
    lines = text.splitlines(keepends=True)
    start = next((i for i, l in enumerate(lines) if re.match(rf"\s*- id: {re.escape(pid)}\s*(#.*)?$", l)), None)
    if start is None:
        raise ApiError(f"projects.yaml: no entry {pid!r}", 404)
    indent = " " * (len(lines[start]) - len(lines[start].lstrip()) + 2)
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or lines[end].startswith(indent)):
        end += 1
    for i in range(start + 1, end):
        if lines[i].startswith(indent + "policies:"):
            m = POLICIES_LINE.match(lines[i].rstrip("\n"))
            if not m:
                raise ApiError(f"{pid}: its `policies:` is not one line; edit it by hand", 409)
            if ids:
                lines[i] = f"{indent}policies: [{', '.join(ids)}]{m.group('rest')}\n"
            else:
                del lines[i]
            return "".join(lines)
    if ids:
        at = next((i + 1 for i in range(start + 1, end) if re.match(rf"{indent}(path|type):", lines[i])), start + 1)
        while at < end and re.match(rf"{indent}(path|type):", lines[at]):
            at += 1
        lines.insert(at, f"{indent}policies: [{', '.join(ids)}]\n")
    return "".join(lines)


def attach(target, name, pid, detach=False, root=schema.NOMAD, registry=None):
    """Attach (or detach) policy `pid` to type `name` (profiles/<name>.yaml) or project `name`
    (projects.yaml), on branch policy/attach-<target>-<name>."""
    if target not in ("type", "project"):
        raise ApiError("target must be type or project")
    if not isinstance(name, str) or not ID_RE.match(name):
        raise ApiError(f"not a {target} id: {name!r}")
    _check_id(pid)
    reg = registry or schema.Registry.live()
    pols = _load(root, reg)
    if pid not in pols:
        raise ApiError(f"no policy {pid!r}", 404)
    pol, rel = pols[pid]
    if _guardrail(pol, rel):
        raise ApiError(f"{pid} is a guardrail: it applies to every project and is not attached", 403)
    if not rel.startswith("policies/types/"):
        raise ApiError(f"{pid} is not in policies/types/, where type and project policies live")
    branch = f"policy/attach-{target}-{name}"
    ref = _base_ref(root, branch)
    if target == "type":
        text = _read_ref(root, ref, f"profiles/{name}.yaml")
        if text is None:
            raise ApiError(f"no type {name!r}", 404)
        have = list((yaml.safe_load(text) or {}).get("policies") or [])
    else:
        text = _read_ref(root, ref, "projects.yaml") or ""
        entries = {e["id"]: e for e in (yaml.safe_load(text) or {}).get("projects") or [] if isinstance(e, dict)}
        if name not in entries:
            raise ApiError(f"no project {name!r}", 404)
        have = list(entries[name].get("policies") or [])
    if detach:
        if pid not in have:
            raise ApiError(f"{pid} is not attached to {target} {name}", 409)
        ids = [x for x in have if x != pid]
    else:
        if pid in have:
            raise ApiError(f"{pid} is already attached to {target} {name}", 409)
        ids = have + [pid]
    if target == "type":
        new, rel_out = _profile_with(text, ids), f"profiles/{name}.yaml"
    else:
        new, rel_out = _project_with(text, name, ids), "projects.yaml"
    _check_attach(target, name, new)
    verb = "detach" if detach else "attach"
    res = _on_branch(root, branch, {rel_out: new},
                     f"Policy {pid}: {verb} {'from' if detach else 'to'} {target} {name} ({_today()})\n\n"
                     "Saved by the cockpit's policy editor (#1396). Merging is a yellow action.")
    return {"ok": True, "target": target, "name": name, "policy": pid, "policies": ids, "file": rel_out, **res}


def _check_attach(target, name, text):
    """The edited file still parses, with the policies it names."""
    data = yaml.safe_load(text)
    if target == "type":
        from nomad_projects import profiles
        try:
            profiles.validate(data, name=name)
        except profiles.ProfileError as e:
            raise ApiError(str(e)) from None
        return
    entry = next((e for e in data.get("projects") or [] if isinstance(e, dict) and e.get("id") == name), None)
    from autopilot import projects
    try:
        projects.parse_policies(name, (entry or {}).get("policies"))
    except projects.ConfigError as e:
        raise ApiError(str(e)) from None

