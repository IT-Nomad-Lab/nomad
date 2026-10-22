"""The `policies/*.yaml` format and its validator (#1390, PRD docs/prd/project-types-and-inline-asks.md 6.5).

A policy file is one mapping. Unknown fields, unknown MCP or skill names and bad values are
refused with a PolicyError that names the policy and the field, so the cockpit can show it
before a save. Nothing here enforces anything; nomad_policy/evaluator.py decides, later tasks
compile the result.

    id: website-default            # required, equals the file name without .yaml
    role: permission               # required: guardrail (layer 1, the ceiling) | permission (layers 2, 3)
    description: text              # optional
    statements:                    # optional
      - sid: figma-read            # optional label shown by the simulator
        effect: allow              # allow | ask | deny   (a guardrail holds deny and ask, never allow)
        kind: mcp                  # mcp | skill | tool | agent | action | command | path
        match: [figma, "nomad-*"]  # names or globs; mcp and skill names must exist
        when:                      # optional; every key given must match
          lane: [code]             #   lane, tier (green|yellow|red), task_type: names or globs
          tier: green
          channel: gateway         #   channel (mcp only): gateway | local | repo, how it is reached
    settings:                      # values, not permissions (SETTING_KEYS)
      cap: 3
      model: sonnet
    locks: [cap]                   # guardrail only: settings keys no lower layer may change

Effects: deny stops, ask hands the call to the operator (red tier), allow grants. The evaluator
ranks them deny > ask > allow (nomad_policy/evaluator.py).

Kinds. `mcp`, `skill`, `agent`, `action` match a name by glob. `tool` matches a Claude Code
permission rule by glob (`Bash(git push *)`, `Edit(**/CLAUDE.md)`, `mcp__nomad-pm`). `command`
matches a shell command line by regular expression (re.search, case-insensitive), as the
patterns of .claude/hooks/guard.py do; each pattern must compile. `path` matches a file path
relative to the project by glob, and a directory entry covers everything beneath it.

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import fnmatch
import glob
import importlib.util
import json
import os
import re
from dataclasses import dataclass, field

import yaml

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
POLICIES_DIR = os.path.join(NOMAD, "policies")

ROLES = ("guardrail", "permission")
EFFECTS = ("allow", "ask", "deny")
KINDS = ("mcp", "skill", "tool", "agent", "action", "command", "path")
WHEN_KEYS = ("lane", "tier", "task_type", "channel")
TIERS = ("green", "yellow", "red")
# How a project reaches an MCP server (#1392), one compile target each: the LiteLLM gateway key,
# the local-model set (~/.config/nomad/mcp-grants.json), the repo's own .mcp.json.
CHANNELS = ("gateway", "local", "repo")
MODELS = {"sonnet", "opus", "haiku", "fable"}   # role aliases, as autopilot.projects.MODELS
POLICY_KEYS = {"id", "role", "description", "statements", "settings", "locks"}
STATEMENT_KEYS = {"sid", "effect", "kind", "match", "when"}
STEP_KEYS = {"run", "locks", "env"}             # a gate step, as autopilot.projects.COMMAND_KEYS
# Settings keys use the names of the projects.yaml `autopilot:` block they will compile to.
SETTING_KEYS = ("cap", "model", "max_budget_usd", "pre_merge_gate", "milestone_gate",
                "deny_edit_globs", "default_mode")


class PolicyError(ValueError):
    """An invalid policy. The message names the policy and the field."""


def _err(pid, fld, msg):
    return PolicyError(f"policy {pid}: {fld}: {msg}")


@dataclass(frozen=True)
class Statement:
    effect: str
    kind: str
    match: tuple
    when: dict = field(default_factory=dict)     # key -> tuple of names or globs
    sid: str = ""


@dataclass(frozen=True)
class Policy:
    id: str
    role: str
    statements: tuple = ()
    settings: dict = field(default_factory=dict)
    locks: tuple = ()
    description: str = ""


@dataclass(frozen=True)
class Registry:
    """The names a policy may mention: MCP servers and skills."""
    mcps: frozenset = frozenset()
    skills: frozenset = frozenset()

    @classmethod
    def live(cls, home=None):
        """MCP servers: the META table of scripts/mcp-registry.py (it refuses a server with no
        entry) plus what scripts/mcp-sync.py reads from ~/nomad/.mcp.json and ~/.claude.json.
        Skills: the folders of nomad-plugin/skills and ~/.claude/skills, plus the skills of
        installed Claude Code plugins as `<plugin>:<skill>` (plugin_skills)."""
        home = home or os.path.expanduser("~")
        mcps = set(_load_script("mcp-registry.py").META)
        mcps |= set(_load_script("mcp-sync.py").registry())
        skills = set()
        for root in (os.path.join(NOMAD, "nomad-plugin", "skills"),
                     os.path.join(home, ".claude", "skills")):
            if os.path.isdir(root):
                skills |= {d for d in os.listdir(root) if os.path.isdir(os.path.join(root, d))}
        skills |= plugin_skills(os.path.join(home, ".claude", "plugins"))
        return cls(frozenset(mcps), frozenset(skills))


def plugin_skills(root):
    """`<plugin>:<skill>` for every skill of an installed Claude Code plugin, the name a session
    loads it by (#1404). Two places hold plugins: installed_plugins.json, whose entries point at
    an installPath under cache/, and synced/<org>/<plugin>/ (plugins synced from claude.ai).
    A skill is a folder of the plugin's skills/ with a SKILL.md. The plugin's name is the one in
    its .claude-plugin/plugin.json (or plugin.json), else the install key before `@`, else the
    synced folder name before `~`. Trashed plugins (.trash/) are not installed and are not read."""
    found = set()

    def add(path, fallback):
        name = fallback
        for meta in (os.path.join(path, ".claude-plugin", "plugin.json"), os.path.join(path, "plugin.json")):
            try:
                with open(meta, encoding="utf-8") as f:
                    name = json.load(f).get("name") or fallback
                break
            except (OSError, ValueError, AttributeError):
                continue
        sk = os.path.join(path, "skills")
        if os.path.isdir(sk):
            found.update(f"{name}:{d}" for d in os.listdir(sk)
                         if os.path.isfile(os.path.join(sk, d, "SKILL.md")))

    try:
        with open(os.path.join(root, "installed_plugins.json"), encoding="utf-8") as f:
            installed = json.load(f).get("plugins") or {}
    except (OSError, ValueError, AttributeError):
        installed = {}
    for key, entries in installed.items():
        for e in entries if isinstance(entries, list) else ():
            if isinstance(e, dict) and e.get("installPath"):
                add(e["installPath"], key.split("@", 1)[0])
    for org in glob.glob(os.path.join(root, "synced", "*", "")):
        for d in os.listdir(org):
            if os.path.isdir(os.path.join(org, d)):
                add(os.path.join(org, d), d.split("~", 1)[0])
    return found


def _load_script(name):
    spec = importlib.util.spec_from_file_location(
        "nomad_policy_" + name.replace("-", "_").removesuffix(".py"),
        os.path.join(NOMAD, "scripts", name))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _strings(pid, fld, v, nonempty=False):
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise _err(pid, fld, f"expected a list of non-empty strings, got {v!r}")
    if nonempty and not v:
        raise _err(pid, fld, "expected at least one entry")
    return tuple(v)


def _check_names(pid, fld, kind, names, known):
    """A literal mcp or skill name must exist; a glob must match at least one."""
    for i, n in enumerate(names):
        if any(c in n for c in "*?["):
            if not fnmatch.filter(known, n):
                raise _err(pid, f"{fld}[{i}]", f"glob {n!r} matches no known {kind}")
        elif n not in known:
            raise _err(pid, f"{fld}[{i}]", f"unknown {kind} {n!r}")


def _statement(pid, i, s, role, reg):
    f = f"statements[{i}]"
    if not isinstance(s, dict):
        raise _err(pid, f, f"expected a mapping, got {type(s).__name__}")
    for k in s:
        if k not in STATEMENT_KEYS:
            raise _err(pid, f"{f}.{k}", f"unknown field (known: {sorted(STATEMENT_KEYS)})")
    effect, kind = s.get("effect"), s.get("kind")
    if effect not in EFFECTS:
        raise _err(pid, f"{f}.effect", f"expected one of {list(EFFECTS)}, got {effect!r}")
    if role == "guardrail" and effect == "allow":
        raise _err(pid, f"{f}.effect", "a guardrail only denies or asks; grants belong in a type or project policy")
    if kind not in KINDS:
        raise _err(pid, f"{f}.kind", f"expected one of {list(KINDS)}, got {kind!r}")
    match = _strings(pid, f"{f}.match", s.get("match"), nonempty=True)
    if kind == "mcp":
        _check_names(pid, f"{f}.match", "MCP server", match, reg.mcps)
    elif kind == "skill":
        _check_names(pid, f"{f}.match", "skill", match, reg.skills)
    elif kind == "command":
        for i, pat in enumerate(match):
            try:
                re.compile(pat)
            except re.error as e:
                raise _err(pid, f"{f}.match[{i}]", f"not a valid regular expression: {e}") from e
    when = {}
    raw = s.get("when", {})
    if not isinstance(raw, dict):
        raise _err(pid, f"{f}.when", f"expected a mapping, got {type(raw).__name__}")
    for k, v in raw.items():
        if k not in WHEN_KEYS:
            raise _err(pid, f"{f}.when.{k}", f"unknown condition (known: {list(WHEN_KEYS)})")
        vals = _strings(pid, f"{f}.when.{k}", [v] if isinstance(v, str) else v, nonempty=True)
        if k == "tier" and not set(vals) <= set(TIERS):
            raise _err(pid, f"{f}.when.tier", f"expected {list(TIERS)}, got {list(vals)}")
        if k == "channel" and not set(vals) <= set(CHANNELS):
            raise _err(pid, f"{f}.when.channel", f"expected {list(CHANNELS)}, got {list(vals)}")
        when[k] = vals
    sid = s.get("sid", "")
    if not isinstance(sid, str):
        raise _err(pid, f"{f}.sid", f"expected a string, got {sid!r}")
    return Statement(effect, kind, match, when, sid)


def _step(pid, fld, step):
    if isinstance(step, str) and step.strip():
        return
    if isinstance(step, dict) and isinstance(step.get("run"), str) and step["run"].strip() \
            and set(step) <= STEP_KEYS:
        return
    raise _err(pid, fld, f"expected a command string or a mapping with `run`, got {step!r}")


def _setting(pid, key, v):
    f = f"settings.{key}"
    if key == "cap":
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise _err(pid, f, f"expected a positive integer, got {v!r}")
    elif key == "model":
        if v not in MODELS:
            raise _err(pid, f, f"expected a role alias {sorted(MODELS)}, got {v!r}")
    elif key == "max_budget_usd":
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise _err(pid, f, f"expected a positive number, got {v!r}")
    elif key == "deny_edit_globs":
        _strings(pid, f, v)
    elif key == "default_mode":                # Claude Code's permissions.defaultMode
        if not isinstance(v, str) or not v:
            raise _err(pid, f, f"expected a non-empty string, got {v!r}")
    else:                                    # pre_merge_gate, milestone_gate
        if not isinstance(v, list):
            raise _err(pid, f, f"expected a list of steps, got {type(v).__name__}")
        for i, step in enumerate(v):
            _step(pid, f"{f}[{i}]", step)


def validate(raw, registry, expect_id=None):
    """A parsed YAML mapping -> Policy, or PolicyError. `expect_id` = the file's stem."""
    if not isinstance(raw, dict):
        raise PolicyError(f"policy {expect_id or '?'}: expected a mapping, got {type(raw).__name__}")
    pid = raw.get("id")
    if not isinstance(pid, str) or not pid:
        raise PolicyError(f"policy {expect_id or '?'}: id: expected a non-empty string, got {pid!r}")
    if expect_id is not None and pid != expect_id:
        raise _err(pid, "id", f"must equal the file name {expect_id!r}")
    for k in raw:
        if k not in POLICY_KEYS:
            raise _err(pid, k, f"unknown field (known: {sorted(POLICY_KEYS)})")
    role = raw.get("role")
    if role not in ROLES:
        raise _err(pid, "role", f"expected one of {list(ROLES)}, got {role!r}")
    desc = raw.get("description", "")
    if not isinstance(desc, str):
        raise _err(pid, "description", f"expected a string, got {desc!r}")
    stmts = raw.get("statements", [])
    if not isinstance(stmts, list):
        raise _err(pid, "statements", f"expected a list, got {type(stmts).__name__}")
    statements = tuple(_statement(pid, i, s, role, registry) for i, s in enumerate(stmts))
    settings = raw.get("settings", {})
    if not isinstance(settings, dict):
        raise _err(pid, "settings", f"expected a mapping, got {type(settings).__name__}")
    for k, v in settings.items():
        if k not in SETTING_KEYS:
            raise _err(pid, f"settings.{k}", f"unknown setting (known: {list(SETTING_KEYS)})")
        _setting(pid, k, v)
    locks = _strings(pid, "locks", raw.get("locks", []))
    if locks and role != "guardrail":
        raise _err(pid, "locks", "only a guardrail can lock a setting")
    for i, k in enumerate(locks):
        if k not in settings:
            raise _err(pid, f"locks[{i}]", f"{k!r} is locked but the policy gives it no value")
    return Policy(pid, role, statements, dict(settings), locks, desc)


def load_file(path, registry):
    """One policies/<id>.yaml -> Policy."""
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise PolicyError(f"policy {stem}: not valid YAML: {e}") from e
    return validate(raw, registry, expect_id=stem)


def load_dir(registry, path=POLICIES_DIR):
    """Every `<path>/*.yaml` -> {id: Policy}. The first bad file raises."""
    return {p.id: p for p in (load_file(f, registry) for f in sorted(glob.glob(os.path.join(path, "*.yaml"))))}
