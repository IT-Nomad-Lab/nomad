"""The policy compiler (#1392, PRD docs/prd/project-types-and-inline-asks.md 6.5).

Turns one project's effective policy into what today's enforcement points read, in the shape of
its #1389 baseline (policies/baseline/<id>.json), so the two compare byte for byte:

  mcp.gateway.servers      the LiteLLM gateway grants on the project's key (mcp-gateway/grant.py)
  mcp.local_model_set      the local-model MCP set (~/.config/nomad/mcp-grants.json)
  mcp.repo_mcp_json        the server names of the repo's .mcp.json
  autopilot                the projects.yaml `autopilot:` block, policy settings applied
  autopilot_permissions    autopilot/permissions.py rendering, from a template the policies build

Layers: guardrail = guardrail-guard-hook + guardrail-autopilot-worker; type =
autopilot-worker-base + the policies of the project's profile (profiles/<type>.yaml); project =
the policies given, by default legacy-<project> built from its baseline (nomad_policy/legacy.py).

It decides nothing itself: every grant is nomad_policy.evaluator.decide, every setting
evaluator.values, and the rendering is autopilot/permissions.py's own. It only compiles and
compares. Writing the result back (gateway grants, mcp-grants.json, projects.yaml) is the tag
operation's job (#1393).

The agent-bridge policy hook (agent-bridge/policy_hook.py) is its own enforcement point, not a
project layer: its rule lists are rendered from policies/guardrail-agent-bridge.yaml and must
equal the hook's (check_bridge_hook); a difference is reported in `gaps` and fails --check.

Not compiled, and reported in `gaps`: an MCP server a policy allows on the gateway for a project with
no gateway key; an MCP `ask` (an enforcement point here can only grant or not).

Run: uv run --no-project --with pyyaml python scripts/policy-compile.py [--check] [project ...]
"""
import dataclasses
import importlib.util
import json
import os

from nomad_policy import evaluator as ev
from nomad_policy import schema
from nomad_policy.legacy import GATEWAY_ENTRY, legacy_policy

NOMAD = schema.NOMAD
BASELINE = os.path.join(NOMAD, "policies", "baseline")
GUARDRAILS = ("guardrail-guard-hook", "guardrail-autopilot-worker")
# Applies to the agent bridge's own sessions only, so it is not in any project's guardrail layer.
BRIDGE_GUARDRAIL = "guardrail-agent-bridge"
BRIDGE_HOOK = "agent-bridge/policy_hook.py"
WORKER_BASE = "autopilot-worker-base"
# Policy settings -> autopilot.projects.Autopilot fields. default_mode goes to the template.
AUTOPILOT_KEYS = ("cap", "model", "max_budget_usd", "deny_edit_globs", "pre_merge_gate",
                  "milestone_gate")
GATES = ("pre_merge_gate", "milestone_gate")


def _script(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(NOMAD, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def snapshot_module():
    """scripts/config-snapshot.py, the baseline's own renderer."""
    return _script("nomad_config_snapshot", "scripts/config-snapshot.py")


# ---- universes: which names each enforcement point can hold -------------------------------------

@dataclasses.dataclass(frozen=True)
class Universe:
    gateway: frozenset      # servers the gateway serves (META `reached` = gateway ...)
    local: frozenset        # servers a local-model run can start (scripts/mcp-sync.py)
    repo: frozenset         # any registered server

    @classmethod
    def live(cls):
        meta = schema._load_script("mcp-registry.py").META
        local = frozenset(schema._load_script("mcp-sync.py").registry())
        gateway = frozenset(n for n, row in meta.items() if row[3].startswith("gateway"))
        return cls(gateway, local, frozenset(meta) | local)


# ---- layers ------------------------------------------------------------------------------------

def layers_for(project_type, policies, project_policies=()):
    """The evaluator's Layers for one project. `policies` = {id: Policy} (profiles.load_policies)."""
    from nomad_projects import profiles
    prof = profiles.load(project_type)
    return ev.Layers(guardrail=tuple(policies[g] for g in GUARDRAILS),
                     type=(policies[WORKER_BASE],) + tuple(policies[p] for p in prof.policies),
                     project=tuple(project_policies))


def legacy_layers(baseline, project_type, policies, registry, attached=(), raw=None):
    """layers_for with the project's legacy-<id> policy first, then the policies its
    projects.yaml entry attaches (`policies:`, autopilot.projects.Project.policies). `raw` is a
    saved legacy-<id> (nomad_policy/tag.py); None builds it from the baseline."""
    return layers_for(project_type, policies,
                      (schema.validate(raw or legacy_policy(baseline), registry),) + tuple(attached))


# ---- the enforcement points --------------------------------------------------------------------

def _allowed(layers, channel, names):
    return sorted(n for n in names if ev.decide(layers, "mcp", n, {"channel": channel}).allowed)


def _asked(layers, channel, names):
    return sorted(n for n in names if ev.decide(layers, "mcp", n, {"channel": channel}).needs_approval)


def compile_mcp(layers, universe, observed):
    """(mcp block, gaps). `observed` is the baseline's mcp block: it says what exists (a gateway
    key, a grants file, a repo .mcp.json and its gateway entry), the policy says what is in it."""
    gaps = []
    gw_seen = observed.get("gateway") or {}
    key = bool(gw_seen.get("key_present"))
    gw = _allowed(layers, "gateway", universe.gateway)
    if gw and not key:
        gaps.append(f"gateway: {gw} allowed but the project has no gateway key")
    local = _allowed(layers, "local", universe.local)
    repo = _allowed(layers, "repo", universe.repo)
    repo_seen = observed.get("repo_mcp_json")
    if repo_seen is not None and GATEWAY_ENTRY in repo_seen:
        repo = sorted(set(repo) | {GATEWAY_ENTRY})
    for c, names in (("gateway", universe.gateway), ("local", universe.local), ("repo", universe.repo)):
        asked = _asked(layers, c, names)
        if asked:
            gaps.append(f"{c}: {asked} need an ask, which this point cannot hold; left out")
    out = {"gateway": {"key_present": key, "servers": gw if key else []},
           "local_model_set": None if observed.get("local_model_set") is None and not local else local,
           "repo_mcp_json": None if repo_seen is None and not repo else repo}
    if gw_seen.get("servers") is None and key:          # the gateway was unreachable at snapshot time
        out["gateway"]["servers"] = None
    return out, gaps


def compile_autopilot(layers, ap, project_id=""):
    """The Autopilot block with the policy's settings applied (None stays None). Keys no policy
    sets keep the projects.yaml value."""
    if ap is None:
        return None
    from autopilot import projects
    vals = ev.values(layers)
    change = {}
    for k in AUTOPILOT_KEYS:
        if k not in vals:
            continue
        v = vals[k]
        if k in GATES:      # the autopilot's own parser: `test:<suite>`, `build`, locks, env
            v = projects._gate(project_id, f"policy settings.{k}", list(v), ap.test, ap.build)
        elif k == "deny_edit_globs":
            v = tuple(v)
        change[k] = v
    return dataclasses.replace(ap, **change)


def compile_template(layers):
    """The autopilot permission template, rebuilt from the policies: tool denies and asks from
    every layer, tool allows from the type and project layers, defaultMode from the settings."""
    def rules(effect, names=ev.LAYERS):
        return list(dict.fromkeys(m for layer, p in layers.walk() if layer in names
                                  for s in p.statements if s.kind == "tool" and s.effect == effect
                                  for m in s.match))
    perms = {"defaultMode": ev.values(layers)["default_mode"],
             "allow": rules("allow", ("type", "project")), "deny": rules("deny")}
    ask = rules("ask")
    if ask:
        perms["ask"] = ask
    return {"permissions": perms}


def compile_project(project, layers, universe, observed_mcp, src=None):
    """(compiled fields in baseline shape, gaps) for one autopilot.projects.Project."""
    snap = snapshot_module()
    mcp, gaps = compile_mcp(layers, universe, observed_mcp)
    ap = compile_autopilot(layers, project.autopilot, project.id)
    p = dataclasses.replace(project, autopilot=ap)
    perms = snap.render_permissions(p, src or snap.Sources(), template=compile_template(layers))
    gaps.extend(check_bridge_hook())
    return snap.plain({"mcp": mcp, "autopilot": ap, "autopilot_permissions": perms}), gaps


def compile_bridge_hook(policy):
    """The hook's rule lists (STASH, COCKPIT, FORCE, ASK patterns, in order), rebuilt from the policy."""
    def rules(pred):
        return [m for s in policy.statements if pred(s) for m in s.match]
    return {"STASH": rules(lambda s: s.sid == "bridge-stash"),
            "COCKPIT": rules(lambda s: s.sid == "bridge-cockpit-only"),
            "FORCE": rules(lambda s: s.sid == "bridge-force-push"),
            "ASK": rules(lambda s: s.effect == "ask")}


def bridge_hook_rules(hook=None):
    """The same lists, read from the live hook module."""
    h = hook or _script("nomad_bridge_policy_hook", BRIDGE_HOOK)
    cockpit = getattr(h, "COCKPIT", None)
    return {"STASH": [h.STASH.pattern], "COCKPIT": [cockpit.pattern] if cockpit else [],
            "FORCE": [h.FORCE.pattern],
            "ASK": [rx.pattern for rx, _ in h.ASK]}


def check_bridge_hook(policy=None, hook=None):
    """[] when the hook holds exactly the policy's rules, else one gap naming the lists that differ."""
    if policy is None:
        policy = schema.load_file(os.path.join(schema.POLICIES_DIR, BRIDGE_GUARDRAIL + ".yaml"),
                                  schema.Registry())
    want, got = compile_bridge_hook(policy), bridge_hook_rules(hook)
    bad = [k for k in want if want[k] != got[k]]
    return [f"agent-bridge policy hook: {BRIDGE_HOOK} differs from {policy.id} in "
            + ", ".join(bad)] if bad else []


FIELDS = ("mcp", "autopilot", "autopilot_permissions")


def baseline_fields(baseline):
    return {k: baseline[k] for k in FIELDS}


def render(d):
    return json.dumps(d, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
