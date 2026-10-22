"""Project-type profiles: profiles/<type>.yaml, and the validator for them.

PRD docs/prd/project-types-and-inline-asks.md 6.2. A profile says what a project of one type
gets: interview questions, stack defaults, policies, agent roles, gates, required files and
autopilot defaults. This module only loads and checks the files; nothing applies a profile to a
project yet (#1369), and tagging a project with a type must not change its configuration.

A profile is a bundle of policies (PRD 6.5): it does not list MCPs or skills itself, it names
policy ids in `policies`. Those are type-layer `role: permission` policies in
policies/types/<id>.yaml (nomad_policy/schema.py format), and they carry the MCP, skill and tool
grants. `layers_for(profile)` returns the evaluator's Layers with those policies as the type
layer, so nomad_policy.evaluator gives the profile's effective policy.

The validator rejects an unknown field, an unknown policy id, a guardrail policy (guardrails
apply to every project and are not chosen by a type) and a duplicate id. The policy files
themselves are checked by nomad_policy (unknown MCP or skill names). A bad profile raises
ProfileError naming the profile and field.

    from nomad_projects.profiles import load, load_all, layers_for
    p = load("generic")
    layers_for(p)

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_projects -q
"""
import os
import re
from dataclasses import dataclass, field

import yaml

from nomad_policy import evaluator, schema

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_DIR = os.path.join(NOMAD, "profiles")
POLICIES_DIR = schema.POLICIES_DIR
TYPE_POLICIES_DIR = os.path.join(POLICIES_DIR, "types")   # policies/types/<id>.yaml

FIELDS = {"id", "title", "description", "interview", "stack", "policies", "agents",
          "gates", "files", "autopilot"}
MOVED = {"mcps": "policies", "skills": "policies"}     # fields that became policy statements
REQUIRED = {"id", "title", "description"}
INTERVIEW_KEYS = {"ask", "skip"}
QUESTION_KEYS = {"id", "question", "kind", "options"}
QUESTION_KINDS = {"single", "multi", "text", "approve"}
AGENT_KEYS = {"role", "lane"}
GATE_KEYS = {"pre_merge", "milestone"}
AUTOPILOT_KEYS = {"cap", "model", "max_budget_usd", "lanes"}
MODELS = {"sonnet", "opus", "haiku", "fable"}   # role aliases only, as in projects.yaml
ID_RE = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")


class ProfileError(ValueError):
    """An invalid profile. The message names the profile and the field."""


@dataclass(frozen=True)
class Profile:
    id: str
    title: str
    description: str
    interview: dict = field(default_factory=dict)   # {"ask": [question], "skip": [question id]}
    stack: dict = field(default_factory=dict)
    policies: tuple = ()             # ids of type-layer permission policies
    agents: tuple = ()               # ({"role", "lane"}, ...)
    gates: dict = field(default_factory=dict)       # {"pre_merge": (step,), "milestone": (step,)}
    files: tuple = ()
    autopilot: dict = field(default_factory=dict)


def load_policies(registry=None, dirs=(POLICIES_DIR, TYPE_POLICIES_DIR)):
    """Every policy a profile may name: {id: Policy} from policies/ and policies/types/.
    `registry` is the nomad_policy Registry for MCP and skill names (default: the live one)."""
    registry = registry or schema.Registry.live()
    out = {}
    for d in dirs:
        if os.path.isdir(d):
            out.update(schema.load_dir(registry, d))
    return out


# ---- validation ---------------------------------------------------------------------------

def _err(pid, fld, msg):
    return ProfileError(f"profile '{pid}': {fld}: {msg}")


def _str(pid, fld, v):
    if not isinstance(v, str) or not v.strip():
        raise _err(pid, fld, f"expected a non-empty string, got {v!r}")
    return v.strip()


def _str_list(pid, fld, v):
    if v is None:
        return ()
    if not isinstance(v, list):
        raise _err(pid, fld, f"expected a list of strings, got {type(v).__name__}")
    return tuple(_str(pid, f"{fld}[{i}]", x) for i, x in enumerate(v))


def _mapping(pid, fld, v, keys):
    """A mapping with only `keys` (None = empty)."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise _err(pid, fld, f"expected a mapping, got {type(v).__name__}")
    extra = set(v) - keys
    if extra:
        raise _err(pid, fld, f"unknown key(s) {sorted(map(str, extra))}; allowed: {sorted(keys)}")
    return v


def _question(pid, fld, q):
    q = _mapping(pid, fld, q, QUESTION_KEYS)
    for k in ("id", "question"):
        if k not in q:
            raise _err(pid, fld, f"a question needs `{k}`")
    out = {"id": _str(pid, f"{fld}.id", q["id"]), "question": _str(pid, f"{fld}.question", q["question"])}
    if "kind" in q:
        if q["kind"] not in QUESTION_KINDS:
            raise _err(pid, f"{fld}.kind", f"expected one of {sorted(QUESTION_KINDS)}, got {q['kind']!r}")
        out["kind"] = q["kind"]
    if "options" in q:
        out["options"] = _str_list(pid, f"{fld}.options", q["options"])
    return out


def _interview(pid, v):
    v = _mapping(pid, "interview", v, INTERVIEW_KEYS)
    ask = v.get("ask") or []
    if not isinstance(ask, list):
        raise _err(pid, "interview.ask", f"expected a list, got {type(ask).__name__}")
    return {"ask": tuple(_question(pid, f"interview.ask[{i}]", q) for i, q in enumerate(ask)),
            "skip": _str_list(pid, "interview.skip", v.get("skip"))}


def _agents(pid, v):
    if v is None:
        return ()
    if not isinstance(v, list):
        raise _err(pid, "agents", f"expected a list, got {type(v).__name__}")
    out = []
    for i, a in enumerate(v):
        fld = f"agents[{i}]"
        a = _mapping(pid, fld, a, AGENT_KEYS)
        if "role" not in a:
            raise _err(pid, fld, "an agent needs `role`")
        agent = {"role": _str(pid, f"{fld}.role", a["role"])}
        if "lane" in a:
            agent["lane"] = _str(pid, f"{fld}.lane", a["lane"])
        out.append(agent)
    return tuple(out)


def _gates(pid, v):
    v = _mapping(pid, "gates", v, GATE_KEYS)
    return {k: _str_list(pid, f"gates.{k}", v.get(k)) for k in sorted(GATE_KEYS)}


def _autopilot(pid, v):
    v = _mapping(pid, "autopilot", v, AUTOPILOT_KEYS)
    out = dict(v)
    if "cap" in v and (isinstance(v["cap"], bool) or not isinstance(v["cap"], int) or v["cap"] < 1):
        raise _err(pid, "autopilot.cap", f"expected a positive integer, got {v['cap']!r}")
    if "model" in v and v["model"] not in MODELS:
        raise _err(pid, "autopilot.model", f"expected one of {sorted(MODELS)}, got {v['model']!r}")
    if "max_budget_usd" in v:
        b = v["max_budget_usd"]
        if isinstance(b, bool) or not isinstance(b, (int, float)) or b <= 0:
            raise _err(pid, "autopilot.max_budget_usd", f"expected a positive number, got {b!r}")
    if "lanes" in v:
        out["lanes"] = tuple(s.lower() for s in _str_list(pid, "autopilot.lanes", v["lanes"]))
    return out


def _policies(pid, v, policies):
    ids = _str_list(pid, "policies", v)
    if not ids:
        return ids
    known = load_policies() if policies is None else policies
    for i, p in enumerate(ids):
        if p not in known:
            raise _err(pid, f"policies[{i}]", f"unknown policy {p!r}; no policies/types/{p}.yaml")
        if known[p].role != "permission":
            raise _err(pid, f"policies[{i}]", f"{p!r} is a {known[p].role} policy; a profile "
                       "takes permission policies only (guardrails apply to every project)")
        if p in ids[:i]:
            raise _err(pid, f"policies[{i}]", f"{p!r} is listed twice")
    return ids


def validate(data, policies=None, name=None):
    """Check a parsed profile and return a Profile. `name` is the file's stem, which must equal
    `id`. `policies` is {id: Policy} (default: load_policies(), read only if the profile names
    any)."""
    pid = name or (data.get("id") if isinstance(data, dict) else None) or "?"
    if not isinstance(data, dict):
        raise _err(pid, "(file)", f"expected a mapping, got {type(data).__name__}")
    for k in data:
        if k in MOVED:
            raise _err(pid, k, f"no longer a profile field; grant it in a type policy and list that in `{MOVED[k]}` (#1394)")
    extra = set(data) - FIELDS
    if extra:
        raise _err(pid, "(file)", f"unknown field(s) {sorted(map(str, extra))}; allowed: {sorted(FIELDS)}")
    missing = REQUIRED - set(data)
    if missing:
        raise _err(pid, "(file)", f"missing required field(s) {sorted(missing)}")
    id_ = _str(pid, "id", data["id"])
    if not ID_RE.match(id_):
        raise _err(pid, "id", f"expected lower-case words joined by '-', got {id_!r}")
    if name and id_ != name:
        raise _err(pid, "id", f"must equal the file name '{name}', got {id_!r}")

    stack = data.get("stack")
    if stack is not None and not isinstance(stack, dict):
        raise _err(pid, "stack", f"expected a mapping, got {type(stack).__name__}")

    pols = _policies(pid, data.get("policies"), policies)

    return Profile(
        id=id_, title=_str(pid, "title", data["title"]),
        description=_str(pid, "description", data["description"]),
        interview=_interview(pid, data.get("interview")), stack=dict(stack or {}),
        policies=pols, agents=_agents(pid, data.get("agents")),
        gates=_gates(pid, data.get("gates")), files=_str_list(pid, "files", data.get("files")),
        autopilot=_autopilot(pid, data.get("autopilot")))


# ---- loading ------------------------------------------------------------------------------

def load(type_id, profiles_dir=PROFILES_DIR, **kw):
    """The validated profile for one type. Raises ProfileError if the file is missing or bad."""
    if not ID_RE.match(type_id):
        raise ProfileError(f"profile '{type_id}': not a valid type id")
    path = os.path.join(profiles_dir, type_id + ".yaml")
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError:
        raise ProfileError(f"profile '{type_id}': no file {path}") from None
    except yaml.YAMLError as e:
        raise ProfileError(f"profile '{type_id}': invalid YAML: {e}") from None
    return validate(data, name=type_id, **kw)


def load_all(profiles_dir=PROFILES_DIR, **kw):
    """Every profile in the folder, by id. The first bad one raises."""
    names = sorted(f[:-5] for f in os.listdir(profiles_dir) if f.endswith(".yaml"))
    kw.setdefault("policies", load_policies())
    return {n: load(n, profiles_dir, **kw) for n in names}


def layers_for(profile, policies=None, guardrails=()):
    """The evaluator's Layers for a project of this type: `guardrails` (layer 1, policies the
    caller loaded) and the profile's policies as the type layer. Feed it to
    nomad_policy.evaluator.decide / effective_settings / values."""
    if policies is None:
        policies = load_policies() if profile.policies else {}
    return evaluator.Layers(tuple(guardrails), tuple(policies[p] for p in profile.policies))
