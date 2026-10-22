"""The cockpit's profile operations (#1377, PRD docs/prd/project-types-and-inline-asks.md 6.3).

List, get, validate and dry-run the project-type profiles in profiles/<type>.yaml, and save an
edit (#1379) on the branch `profile/<id>`, never on master: merging it is a yellow action by an
agent or the operator. The page is v2/cockpit_standards.py (#1378). Like nomad_policy/api.py,
the dispatcher runs this on the host and reads the files at `root`; a profile's pending edit is
read from its branch, never from the working copy. A new type saved but not merged lives only on
its branch, and list and get show it from there.

The dry run answers "what would a new project of this type get": the interview it asks, the
projects.yaml entry + New project writes (type and the profile's gates in the autopilot block,
dispatcher._registry_entry), the agents, files and stack, and the effective access from the
policy layers (guardrails, the worker base and the profile's policies), each item with the
statement that decided it. Given edited text, it also shows what the edit changes against the
profile on master, and which registered projects have this type.

A profile's MCPs and skills come from the policies it names (#1394), so the pickers offer
permission policies, each with the MCPs and skills it grants.
"""
import difflib
import json
import os
import re

import yaml

from nomad_policy import api, compiler, schema
from nomad_policy import evaluator as ev
from nomad_projects import create, profiles

ApiError = api.ApiError
MASTER = api.MASTER
LANES = ("comms", "research", "support", "ads", "dev", "manager")    # nomad-pm lanes (mcp-local/nomad_pm.py)


def _branch(pid):
    return f"profile/{pid}"


def _rel(pid):
    return f"profiles/{pid}.yaml"


def _check_id(pid):
    if not isinstance(pid, str) or not profiles.ID_RE.match(pid):
        raise ApiError(f"not a profile id: {pid!r}")


def _policies(root, registry):
    p = api._paths(root)
    return profiles.load_policies(registry, dirs=(p["policies"], p["types"]))


def _roles(root):
    d = os.path.join(root, ".claude", "agents")
    return sorted(f[:-3] for f in os.listdir(d) if f.endswith(".md")) if os.path.isdir(d) else []


def _parse(pid, text, pols):
    """(raw dict, Profile) for a profile's text, or ApiError naming the field."""
    _check_id(pid)
    if not isinstance(text, str) or not text.strip():
        raise ApiError("yaml must be the profile file's text")
    if len(text) > api.MAX_YAML:
        raise ApiError(f"yaml is over {api.MAX_YAML} bytes")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ApiError(f"invalid YAML: {e}") from None
    try:
        prof = profiles.validate(raw, policies=pols, name=pid)
    except profiles.ProfileError as e:
        raise ApiError(str(e)) from None
    for i, p in enumerate(prof.policies):         # every project has the base; legacy-* is one project's
        if p == compiler.WORKER_BASE or p.startswith("legacy-"):
            raise ApiError(f"profile {pid!r}: policies[{i}]: {p!r} is not a type policy; "
                           "a profile names the type policies the picker offers")
    return raw, prof


def _grants(pol):
    """What a permission policy grants and denies, for the picker: {mcp, skill, tool} names."""
    out = {}
    for st in pol.statements:
        out.setdefault(f"{st.kind}_{st.effect}", []).extend(st.match)
    return {k: sorted(set(v)) for k, v in sorted(out.items())}


def _typed(root):
    """{type: [project id]} from projects.yaml; an entry without `type` is generic."""
    out = {}
    for pid, e in api._projects_raw(root).items():
        out.setdefault(e.get("type") or "generic", []).append(pid)
    return {k: sorted(v) for k, v in out.items()}


# ---- read --------------------------------------------------------------------------------------

def list_profiles(root=schema.NOMAD, registry=None):
    """Every profile, a bad one with its error rather than a shorter list, and what the editor
    offers: the permission policies, agent roles, lanes and the schema's choices."""
    reg = registry or schema.Registry.live()
    pols = _policies(root, reg)
    typed = _typed(root)
    out = []
    for pid, raw in api._profiles(root).items():
        row = {"id": pid, "file": _rel(pid), "projects": typed.get(pid, []),
               "pending": _pending(root, pid) is not None, "new": False}
        try:
            p = profiles.validate(raw, policies=pols, name=pid)
            row.update(title=p.title, description=p.description, policies=list(p.policies), error=None)
        except profiles.ProfileError as e:
            row.update(title=(raw or {}).get("title") if isinstance(raw, dict) else None,
                       description=None, policies=[], error=str(e))
        out.append(row)
    for pid in _branch_only(root):
        text = _pending(root, pid)["yaml"]
        row = {"id": pid, "file": _rel(pid), "projects": typed.get(pid, []), "pending": True, "new": True}
        try:
            p = _parse(pid, text, pols)[1]
            row.update(title=p.title, description=p.description, policies=list(p.policies), error=None)
        except ApiError as e:
            row.update(title=None, description=None, policies=[], error=str(e))
        out.append(row)
    choices = {
        "policies": [{"id": k, "description": v.description, "grants": _grants(v)}
                     for k, v in sorted(pols.items()) if v.role == "permission"
                     and not k.startswith("legacy-") and k != compiler.WORKER_BASE],
        "roles": _roles(root), "lanes": list(LANES), "models": sorted(profiles.MODELS),
        "question_kinds": sorted(profiles.QUESTION_KINDS), "fields": sorted(profiles.FIELDS),
        "required": sorted(profiles.REQUIRED), "gate_keys": sorted(profiles.GATE_KEYS),
        "autopilot_keys": sorted(profiles.AUTOPILOT_KEYS)}
    return {"ok": True, "default": "generic", "profiles": out, "choices": choices}


def _pending(root, pid):
    return api._pending(root, _branch(pid), _rel(pid))


def _branch_only(root):
    """Ids of new types saved on their branch and not merged: no file in profiles/ yet."""
    rc, out = api._git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads/profile/")
    have = set(api._profiles(root))
    ids = [r[len("profile/"):] for r in out.split()] if rc == 0 else []
    return sorted(p for p in ids if p not in have and profiles.ID_RE.match(p) and _pending(root, p))


def get_profile(pid, root=schema.NOMAD, registry=None):
    """One profile: its text, the parsed form (or the error), and the unmerged edit if any. A new
    type that is only on its branch has `yaml` None, `new` true, and its text in `pending`."""
    _check_id(pid)
    path = os.path.join(root, _rel(pid))
    pending = _pending(root, pid)
    if os.path.isfile(path):
        with open(path) as f:
            text = f.read()
        raw = yaml.safe_load(text)
    elif pending:
        text, raw = None, yaml.safe_load(pending["yaml"])
    else:
        raise ApiError(f"no profile {pid!r}", 404)
    reg = registry or schema.Registry.live()
    out = {"ok": True, "id": pid, "file": _rel(pid), "yaml": text, "profile": raw, "new": text is None,
           "error": None, "projects": _typed(root).get(pid, []), "pending": pending}
    try:
        profiles.validate(raw, policies=_policies(root, reg), name=pid)
    except profiles.ProfileError as e:
        out["error"] = str(e)
    return out


def _diff(was, text, rel):
    text = text if text.endswith("\n") else text + "\n"
    return "".join(difflib.unified_diff((was or "").splitlines(keepends=True), text.splitlines(keepends=True),
                                        f"a/{rel}", f"b/{rel}"))


TOP_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):(?:\s|$)")


def _blocks(text):
    """The top-level fields of a profile's text, in order: [(key, lead, body)], where lead is the
    comments and blank lines above the key and body the key's lines; plus the comments after the
    last field."""
    out, pend = [], []
    for ln in text.splitlines(keepends=True):
        m = TOP_KEY.match(ln)
        if m:
            out.append((m.group(1), "".join(pend), ln))
            pend = []
        elif out and ln.strip() and ln[0] in " \t-":          # the field's value goes on
            k, lead, body = out[-1]
            out[-1] = (k, lead, body + "".join(pend) + ln)
            pend = []
        else:
            pend.append(ln)
    return out, "".join(pend)


def _bare(v):
    """A value with its empty lists and maps dropped, so `skip: []` equals no `skip`."""
    if isinstance(v, dict):
        v = {k: _bare(x) for k, x in v.items()}
        return {k: x for k, x in v.items() if x not in ({}, [], None)}
    if isinstance(v, list):
        return [_bare(x) for x in v]
    return v


def _keep_layout(pid, old, new, pols):
    """`new` (the form's text, written fresh) with each top-level field whose value did not change
    taken from `old` as written, comments and all, in old's order, so the diff of a form edit is
    the edit. Falls back to `new` when the result does not mean the same profile."""
    if not old:
        return new
    try:
        ob, tail = _blocks(old)
        nb = {k: (lead, body) for k, lead, body in _blocks(new)[0]}
        def val(k, body):
            return _bare((yaml.safe_load(body) or {}).get(k))
        parts = []
        for k, lead, body in ob:
            if k not in nb:                                   # the form leaves out an empty field
                if val(k, body) in ({}, [], None):
                    parts.append(lead + body)
                continue
            nbody = nb.pop(k)[1]
            same = val(k, body) == val(k, nbody)
            parts.append(lead + (body if same else nbody if nbody.endswith("\n") else nbody + "\n"))
        for k, (lead, body) in nb.items():                    # fields old did not have
            parts.append(body if body.endswith("\n") else body + "\n")
        out = "".join(parts) + tail
        if _parse(pid, out, pols)[1] != _parse(pid, new, pols)[1]:
            return new
        return out
    except (yaml.YAMLError, ApiError, AttributeError):
        return new


def _start(root, pid):
    """The text the editor started from: the branch's when there is one, else master's."""
    return api._read_ref(root, api._base_ref(root, _branch(pid)), _rel(pid))


def validate(pid, text, root=schema.NOMAD, registry=None, keep=False):
    """Check a profile's text without saving it. Returns the parsed form and the diff against
    master's file (empty for an unchanged file, the whole file for a new type). With `keep` (the
    form's text), unchanged fields keep their layout, as save would write them."""
    reg = registry or schema.Registry.live()
    pols = _policies(root, reg)
    raw, _ = _parse(pid, text, pols)
    if keep:
        text = _keep_layout(pid, _start(root, pid), text, pols)
    was = api._read_ref(root, MASTER, _rel(pid))
    return {"ok": True, "id": pid, "new": was is None, "profile": raw, "file": _rel(pid),
            "diff": _diff(was, text, _rel(pid))}


# ---- dry run -----------------------------------------------------------------------------------

def _layers(prof, pols):
    """A new project of this type: guardrails, then the worker base and the profile's policies.
    compiler.layers_for builds the same from the file on disk; this takes the profile in hand."""
    return ev.Layers(guardrail=tuple(pols[g] for g in compiler.GUARDRAILS),
                     type=(pols[compiler.WORKER_BASE],) + tuple(pols[p] for p in prof.policies),
                     project=())


def _decided(d):
    return {"effect": d.effect, "layer": d.layer, "policy": d.policy, "sid": d.sid}


def _access(layers, reg, uni):
    """MCPs per channel and skills the project would be allowed (or asked), and the tool rules
    as the autopilot's permission template has them."""
    mcp = {}
    for c in schema.CHANNELS:
        rows = []
        for name in sorted(getattr(uni, c)):
            d = ev.decide(layers, "mcp", name, {"channel": c})
            if d.allowed or d.needs_approval:
                rows.append({"name": name, **_decided(d)})
        mcp[c] = rows
    skills = []
    for name in sorted(reg.skills):
        d = ev.decide(layers, "skill", name)
        if d.allowed or d.needs_approval:
            skills.append({"name": name, **_decided(d)})
    perms = compiler.compile_template(layers)["permissions"]
    settings = {k: {"value": s.value, "layer": s.layer, "policy": s.policy, "locked": s.locked}
                for k, s in ev.effective_settings(layers).items()}
    return {"mcp": mcp, "skills": skills, "tools": perms, "settings": settings}


def _interview(raw):
    """The questions + New project asks for this type, after the type question."""
    state = {"type": raw["id"], "profile": create._profile(raw)}
    out = []
    for key, q, pq in create.questions(state)[1:]:
        row = {"key": key, "question": q}
        if pq:
            row.update({k: (list(v) if isinstance(v, (list, tuple)) else v)
                        for k, v in pq.items() if k in ("kind", "options")})
        out.append(row)
    return out


def _entry(prof):
    """The projects.yaml entry + New project writes for this type (dispatcher._registry_entry,
    with autopilot asked for): only `type` and the gates come from the profile."""
    lines = ["  - id: <new-project>", "    path: <path>"]
    if prof.id != "generic":
        lines.append(f"    type: {prof.id}")
    lines += ["    autopilot:", '      test: "<set by the first milestone>"']
    for key, k in (("pre_merge", "pre_merge_gate"), ("milestone", "milestone_gate")):
        if prof.gates.get(key):
            lines.append(f"      {k}: [{', '.join(json.dumps(x) for x in prof.gates[key])}]")
    return "\n".join(lines) + "\n"


def _gets(raw, prof, pols, reg, uni):
    layers = _layers(prof, pols)
    return {"interview": _interview(raw), "registry_entry": _entry(prof),
            "stack": dict(prof.stack), "agents": [dict(a) for a in prof.agents],
            "gates": {k: list(v) for k, v in prof.gates.items()}, "files": list(prof.files),
            "autopilot": {k: (list(v) if isinstance(v, tuple) else v) for k, v in prof.autopilot.items()},
            "policies": list(prof.policies),
            "layers": {n: [p.id for p in getattr(layers, n)] for n in ev.LAYERS},
            "access": _access(layers, reg, uni)}


def _by(r):
    return f"{r['effect']} by {r['layer']} {r['policy'] or 'default'}" + (f" ({r['sid']})" if r["sid"] else "")


def _render(gets):
    """One line per fact a project gets, in a fixed order, so a diff of two reads as a list
    of changes."""
    out = [f"ask {q['key']}: {q['question']}" + (f" [{', '.join(q['options'])}]" if q.get("options") else "")
           for q in gets["interview"]]
    out += [f"stack {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in sorted(gets["stack"].items())]
    out += [f"agent {a['role']}" + (f" ({a['lane']})" if a.get("lane") else "") for a in gets["agents"]]
    out += [f"gate {k}: {step}" for k, v in sorted(gets["gates"].items()) for step in v]
    out += [f"file {f}" for f in gets["files"]]
    out += [f"autopilot {k}: {json.dumps(v)}" for k, v in sorted(gets["autopilot"].items())]
    out += [f"policy {p}" for p in gets["policies"]]
    acc = gets["access"]
    out += [f"mcp {c}: {r['name']}, {_by(r)}" for c in schema.CHANNELS for r in acc["mcp"][c]]
    out += [f"skill {r['name']}, {_by(r)}" for r in acc["skills"]]
    out += [f"tool {k}: {m}" for k in ("allow", "ask", "deny") for m in acc["tools"].get(k, ())]
    out += [f"setting {k}: {json.dumps(s['value'])}, by {s['layer']} {s['policy']}" + (" (locked)" if s["locked"] else "")
            for k, s in sorted(acc["settings"].items())]
    return out


def dry_run(pid, text=None, root=schema.NOMAD, registry=None, universe=None):
    """What a new project of this type would get. With `text` (an edit), the same for the edit,
    plus `changes`: the unified diff of the two against master's profile, empty when the edit
    changes nothing a project gets. `projects` are the registered projects of this type, which
    an edit does not reach until they are re-tagged or recompiled."""
    _check_id(pid)
    reg = registry or schema.Registry.live()
    uni = universe or compiler.Universe.live()
    pols = _policies(root, reg)
    was = api._read_ref(root, MASTER, _rel(pid))
    base = None
    if was is not None:
        try:
            base = _gets(*_parse(pid, was, pols), pols, reg, uni)
        except ApiError as e:
            if text is None:
                raise ApiError(f"the profile on {MASTER} is invalid: {e}", 409) from None
    if text is None:
        if base is None:
            raise ApiError(f"no profile {pid!r} on {MASTER}", 404)
        gets, changes = base, None
    else:
        gets = _gets(*_parse(pid, text, pols), pols, reg, uni)
        changes = "\n".join(difflib.unified_diff(_render(base) if base else [], _render(gets),
                                                 f"{pid} on {MASTER}", f"{pid} edited", lineterm="", n=0))
    return {"ok": True, "id": pid, "edited": text is not None, "new": was is None,
            "gets": gets, "facts": _render(gets), "changes": changes, "projects": _typed(root).get(pid, [])}


# ---- save: on a branch -------------------------------------------------------------------------

def save(pid, text, root=schema.NOMAD, registry=None, keep=False):
    """Save a profile on branch profile/<id> (made from master if new), never on master. The text
    is validated first, so a bad profile is refused with the field named and nothing is written.
    With `keep` (the form's text), unchanged fields keep their layout and comments. A text equal
    to what the branch (or master) already has writes nothing. Returns the branch, the commit and
    the diff of the file against master."""
    reg = registry or schema.Registry.live()
    pols = _policies(root, reg)
    _parse(pid, text, pols)
    branch, rel = _branch(pid), _rel(pid)
    start = _start(root, pid)
    if keep:
        text = _keep_layout(pid, start, text, pols)
    text = text if text.endswith("\n") else text + "\n"
    was = api._read_ref(root, MASTER, rel)
    base = api._base_ref(root, branch)
    out = {"ok": True, "id": pid, "file": rel, "new": was is None}
    if start == text:
        return {**out, "unchanged": True, "branch": branch if base == branch else None, "commit": None,
                "diff": _diff(was, text, rel)}
    res = api._on_branch(root, branch, {rel: text},
                         f"Profile {pid}: {'new' if was is None else 'edited'} in the cockpit ({api._today()})\n\n"
                         "Saved by the cockpit's Standards page (#1379). Merging is a yellow action.")
    return {**out, "unchanged": False, **res}
