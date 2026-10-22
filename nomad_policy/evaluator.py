"""The one policy evaluator (#1390, PRD docs/prd/project-types-and-inline-asks.md 6.5).

The policy compiler and the cockpit simulator both call this module, so what the cockpit shows
is what gets enforced. It reads validated Policy objects (nomad_policy/schema.py) and enforces
nothing itself.

Layers: 1 guardrails (apply to every project), 2 policies attached to the project's type,
3 policies attached to the project. Inside a layer the policy list is in attachment order.

Access (`decide`), AWS-like:
  1. an explicit deny in ANY layer wins;
  2. else an `ask` in ANY layer hands the call to the operator (red tier). It ranks above an
     allow, so no grant can skip it, and it needs no allow to be reached: the operator's yes
     is what lets the call through;
  3. else an allow from a type or project policy grants (guardrails hold deny and ask, never allow);
  4. else deny by default.
Every Decision names the statement that decided it, for the "effective access" view.

Settings (`effective_settings`): the most specific layer wins (project over type over guardrail,
later policy over earlier inside a layer), unless a guardrail locks the key: then the guardrail's
value stands and a lower layer's attempt is reported in `ignored`.

A `when` condition names a request attribute (lane, tier, task_type, channel). Attribute missing
from the request: an allow does not match, a deny does (fail closed, so a partial simulator
request can only look stricter than the real one).

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import fnmatch
import re
from dataclasses import dataclass

LAYERS = ("guardrail", "type", "project")   # least to most specific


@dataclass(frozen=True)
class Layers:
    """The policies in force for one project, by layer."""
    guardrail: tuple = ()
    type: tuple = ()
    project: tuple = ()

    def __post_init__(self):
        for name in LAYERS:
            want = "guardrail" if name == "guardrail" else "permission"
            for p in getattr(self, name):
                if p.role != want:
                    raise ValueError(f"policy {p.id} has role {p.role!r}; the {name} layer takes {want} policies")

    def walk(self):
        """(layer name, policy) from the least to the most specific layer."""
        for name in LAYERS:
            for p in getattr(self, name):
                yield name, p


@dataclass(frozen=True)
class Decision:
    effect: str          # allow | ask | deny
    reason: str          # explicit-deny | ask-required | allowed | default-deny
    layer: str = ""      # layer of the deciding statement ("" for default-deny)
    policy: str = ""     # id of the deciding policy
    index: int = -1      # position of the deciding statement in that policy
    sid: str = ""        # its label, if it has one

    @property
    def allowed(self):
        return self.effect == "allow"

    @property
    def needs_approval(self):
        return self.effect == "ask"


@dataclass(frozen=True)
class Setting:
    value: object
    layer: str
    policy: str
    locked: bool = False
    ignored: tuple = ()  # (layer, policy, value) a lock overrode


def _glob(name, patterns):
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def _hit(kind, name, patterns):
    """Does `name` match one of the statement's patterns, in the way its kind reads them?"""
    if kind == "command":                               # regular expressions, as guard.py reads them
        return any(re.search(p, name, re.I) for p in patterns)
    if kind == "path":                                  # a directory entry covers what is beneath it
        return any(fnmatch.fnmatchcase(name, p) or name.startswith(p.rstrip("/") + "/") for p in patterns)
    return _glob(name, patterns)


def _matches(st, kind, name, ctx):
    if st.kind != kind or not _hit(kind, name, st.match):
        return False
    for key, patterns in st.when.items():
        have = ctx.get(key)
        if have is None:
            if st.effect == "allow":
                return False
            continue
        if not _glob(str(have), patterns):
            return False
    return True


def decide(layers, kind, name, context=None):
    """May a project with these layers use `name` of `kind` (mcp, skill, tool, agent, action,
    command, path)? Returns allow, ask (the operator decides) or deny."""
    ctx = context or {}
    for layer, p in layers.walk():
        for i, st in enumerate(p.statements):
            if st.effect == "deny" and _matches(st, kind, name, ctx):
                return Decision("deny", "explicit-deny", layer, p.id, i, st.sid)
    for layer, p in layers.walk():
        for i, st in enumerate(p.statements):
            if st.effect == "ask" and _matches(st, kind, name, ctx):
                return Decision("ask", "ask-required", layer, p.id, i, st.sid)
    for layer, p in reversed(list(layers.walk())):      # report the most specific grant
        for i, st in enumerate(p.statements):
            if st.effect == "allow" and _matches(st, kind, name, ctx):
                return Decision("allow", "allowed", layer, p.id, i, st.sid)
    return Decision("deny", "default-deny")


def effective_settings(layers):
    """{key: Setting} for every key some policy sets. Keys nobody sets are absent."""
    out = {}
    locked = {}                                          # key -> Setting of the guardrail's lock
    for layer, p in layers.walk():
        for key, value in p.settings.items():
            if key in locked and layer != "guardrail":
                s = locked[key]
                if value != s.value:
                    out[key] = Setting(s.value, s.layer, s.policy, True, s.ignored + ((layer, p.id, value),))
                    locked[key] = out[key]
                continue
            out[key] = Setting(value, layer, p.id, key in locked)
            if key in locked:                            # a later guardrail restates a locked key
                locked[key] = out[key]
        if layer == "guardrail":
            for key in p.locks:
                out[key] = locked[key] = Setting(p.settings[key], layer, p.id, True)
    return out


def values(layers):
    """Just the winning values: {key: value}."""
    return {k: s.value for k, s in effective_settings(layers).items()}
