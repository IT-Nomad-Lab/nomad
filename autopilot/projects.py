"""The autopilot's view of nomad/projects.yaml: every project, plus its `autopilot:` block.

Finding the registry and resolving entry paths is the dispatcher's rule, imported from
dispatcher/projects_registry.py rather than copied. On top of it this module validates the
optional per-project `autopilot:` mapping (schema in the projects.yaml header), applies the
defaults and returns dataclasses. A bad block raises ConfigError naming the project and field;
the supervisor treats that project as `error` instead of guessing.

    from autopilot.projects import load
    for p in load():
        if p.autopilot: ...

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import importlib.util
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _import_registry():
    """dispatcher/projects_registry.py, loaded by path so the dispatcher's folder is not put
    on sys.path (its dispatch.py makes a token file on import)."""
    f = os.path.join(NOMAD, "dispatcher", "projects_registry.py")
    spec = importlib.util.spec_from_file_location("nomad_projects_registry", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


projects_registry = _import_registry()

DEFAULT_TYPE = projects_registry.DEFAULT_TYPE   # an entry without `type:`
DEFAULT_CAP = 3           # PRD 5.4: per-project default; the cockpit can change it
FILTER = "{filter}"        # placeholder in a test command for an optional test filter
TEST_REF = "test:"         # gate step `test:<suite>` = that suite, unfiltered
BUILD_REF = "build"        # gate step `build` = the project's build command

KEYS = {"test", "pre_merge_gate", "milestone_gate", "build", "post_test", "locks",
        "flake_set", "known_failures", "cap", "working_dirs", "worktree_root",
        "deny_edit_globs", "requires_gpu", "results", "regenerated", "noise", "lanes",
        "model", "max_budget_usd", "loop_tests", "execution", "gate_network"}
MODELS = {"sonnet", "opus", "haiku", "fable"}   # role aliases only, never raw model ids
COMMAND_KEYS = {"run", "locks", "env"}
RESULT_FORMATS = ("exit", "nunit")   # how the merge queue reads a test suite's results
GATE_NETWORKS = ("host", "none")   # #1530: supervisor gates with the host network, or none
EXEC_MODES = ("legacy", "hybrid")    # #1488: who runs a task; legacy = autopilot/worker.py
EXEC_BACKENDS = ("claude", "codex", "local")
EXEC_PRIVACY = ("cloud_allowed", "local_only")
EXEC_KEYS = {"mode", "backends", "privacy", "gates", "wall_seconds", "prepare"}
GATE_KEYS = {"id", "run", "timeout", "read_roots"}
RESULTS_KEYS = {"format", "files"}
NOISE_KEYS = {"glob", "rule", "keys"}
# Content rules for `noise` (#996); worker.reset_noise applies them.
#   unity-default-platforms  a Unity .meta whose only change is added `platformSettings` entries
#                            with `overridden: 0` (default blocks for installed platform modules)
#   only-keys                a YAML file whose only changed lines are the listed `keys:`
NOISE_RULES = ("unity-default-platforms", "only-keys")


class ConfigError(ValueError):
    """An invalid projects.yaml entry. The message names the project and the field."""


@dataclass(frozen=True)
class Command:
    """One shell command, run from the task's worktree. `locks` are lock files it must hold
    (flock) for its whole run; `env` is added to its environment."""
    run: str
    locks: tuple = ()
    env: dict = field(default_factory=dict)

    def render(self, filter=None):
        """The command line, with `{filter}` replaced by the filter, or dropped when none."""
        if FILTER not in self.run:
            return self.run
        return " ".join(self.run.replace(FILTER, filter or "").split())


@dataclass(frozen=True)
class Autopilot:
    test: dict                      # suite name -> Command ("default" for a single command)
    pre_merge_gate: tuple           # Commands, run in order; all must pass before a merge
    milestone_gate: tuple           # Commands, run on the default branch when a Feature ends
    build: Command = None
    post_test: tuple = ()           # Commands run after every test run, pass or fail
    locks: tuple = ()               # every lock file any command above holds
    flake_set: frozenset = frozenset()
    known_failures: frozenset = frozenset()
    cap: int = DEFAULT_CAP
    working_dirs: tuple = ()
    worktree_root: str = ""
    deny_edit_globs: tuple = ()
    requires_gpu: bool = False
    results: str = "exit"           # test-suite results format: exit (code only) or nunit (XML)
    results_files: str = ""         # nunit: glob (relative to the worktree) to find the XML
                                    # when the command's output names no .xml file
    regenerated: tuple = ()         # tracked paths the tools rewrite on their own; reset to
                                    # HEAD before a worktree's dirty check
    noise: tuple = ()               # Noise rules: a changed file whose whole change is noise is
                                    # reset to HEAD before a worktree's dirty check
    lanes: tuple = ()               # task lanes this project takes, lower case; () = every lane
                                    # (#1001: projects sharing a board split it by lane)
    model: str = ""                 # worker model alias; "" = the global NOMAD_AUTOPILOT_MODEL
    max_budget_usd: float = None    # per-run spend cap; None = the global default (#1109)
    loop_tests: tuple = ()          # suites a worker runs while building; () = every suite.
                                    # The rest run only in the pre-merge gate (#1109)
    execution: "Execution" = None   # #1488: None = the legacy worker pool
    gate_network: str = "none"      # #1530, #1533: none = supervisor gates run without a network
                                    # (docs/prd/merge-gate-network.md)

    def is_flake(self, test_name):
        return _matches(self.flake_set, test_name)

    def is_known_failure(self, test_name):
        return _matches(self.known_failures, test_name)


@dataclass(frozen=True)
class ExecGate:
    """One independent check the hybrid controller runs on a candidate commit, in a sandbox
    with no network and an empty HOME. `read_roots` are the extra paths it may read."""
    id: str
    run: str
    timeout: int = 1800
    read_roots: tuple = ()


@dataclass(frozen=True)
class Execution:
    """#1488: `execution:` in a project's autopilot block. Off (legacy) unless mode is hybrid."""
    mode: str = "legacy"
    backends: tuple = ("claude",)   # spread across, in this order on a tie
    privacy: str = "cloud_allowed"
    gates: tuple = ()               # ExecGate; () = the pre-merge gate's commands
    wall_seconds: int = 3600
    prepare: str = ""               # trusted install step, with network, before the agent starts

    @property
    def hybrid(self):
        return self.mode == "hybrid"


@dataclass(frozen=True)
class Noise:
    """One `noise` entry: tracked files matching `glob` (fnmatch, relative to the repo; `*`
    also matches `/`) are reset to HEAD when their whole change is noise by `rule`."""
    glob: str
    rule: str
    keys: tuple = ()                # only-keys: the YAML keys whose lines may change


@dataclass(frozen=True)
class Project:
    id: str
    path: str                       # absolute, as listed (a symlink stays a symlink)
    group: str = ""
    aliases: tuple = ()
    dispatch: bool = True
    autopilot: Autopilot = None     # None: the project has no autopilot block
    type: str = "generic"           # project type (profiles/<type>.yaml); no `type:` = generic
    policies: tuple = ()            # project-layer permission policies (policies/types/<id>.yaml)

    @property
    def repo(self):
        """The real folder behind `path` (resolves symlinks such as ~/runematch)."""
        return os.path.realpath(self.path)


def _matches(names, test_name):
    """A listed name matches a test by its full name, its method name, or the method name's
    id prefix: `BLK6` matches `RuneMerge.Tests.BlockerTests.BLK6_OneHitPerResolution`."""
    method = test_name.rsplit(".", 1)[-1]
    return any(n in (test_name, method) or method.startswith(n + "_") for n in names)


# ---- validation ---------------------------------------------------------------------------

def _err(pid, fld, msg):
    return ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: {msg}")


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


def _path(p, base):
    p = os.path.expanduser(p)
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(base, p))


def _command(pid, fld, v):
    """A command: a string, or {run, locks, env}."""
    if isinstance(v, str):
        return Command(_str(pid, fld, v))
    if not isinstance(v, dict):
        raise _err(pid, fld, f"expected a command string or a mapping with `run`, got {v!r}")
    extra = set(v) - COMMAND_KEYS
    if extra:
        raise _err(pid, fld, f"unknown key(s) {sorted(extra)}; allowed: {sorted(COMMAND_KEYS)}")
    if "run" not in v:
        raise _err(pid, fld, "a command mapping needs `run`")
    env = v.get("env") or {}
    if not isinstance(env, dict):
        raise _err(pid, f"{fld}.env", f"expected a mapping, got {type(env).__name__}")
    env = {str(k): _envval(pid, f"{fld}.env.{k}", x) for k, x in env.items()}
    locks = tuple(os.path.normpath(os.path.expanduser(p))
                  for p in _str_list(pid, f"{fld}.locks", v.get("locks")))
    for i, p in enumerate(locks):
        if not os.path.isabs(p):
            raise _err(pid, f"{fld}.locks[{i}]", f"a lock must be an absolute path, got {p!r}")
    return Command(_str(pid, f"{fld}.run", v["run"]), locks, env)


def _envval(pid, fld, x):
    if isinstance(x, bool) or not isinstance(x, (str, int, float)):
        raise _err(pid, fld, f"expected a string or number, got {x!r}")
    return str(x)


def _gate(pid, fld, v, tests, build):
    if not isinstance(v, list):
        raise _err(pid, fld, f"expected a list of steps, got {type(v).__name__}")
    out = []
    for i, step in enumerate(v):
        f = f"{fld}[{i}]"
        if isinstance(step, str) and step.strip().startswith(TEST_REF):
            suite = step.strip()[len(TEST_REF):].strip()
            if suite not in tests:
                raise _err(pid, f, f"no test suite {suite!r}; `test` defines {sorted(tests) or 'none'}")
            out.append(tests[suite])
        elif isinstance(step, str) and step.strip() == BUILD_REF:
            if build is None:
                raise _err(pid, f, "`build` step but the project has no `build` command")
            out.append(build)
        else:
            out.append(_command(pid, f, step))
    return tuple(out)


def parse_type(pid, v):
    """Validate one project's `type:`. None = generic. Anything else must name a profile in
    profiles/<type>.yaml (nomad_projects.profiles); an unknown or invalid one is a ConfigError
    naming the project and the field. `generic` is never looked up, so a profile problem cannot
    break a project that did not ask for a type."""
    if v is None:
        return DEFAULT_TYPE
    if not isinstance(v, str) or not v.strip():
        raise ConfigError(f"projects.yaml: project '{pid}': type: expected a non-empty string, "
                          f"got {v!r}")
    t = v.strip()
    if t == DEFAULT_TYPE:
        return t
    from nomad_projects import profiles
    try:
        profiles.load(t)
    except profiles.ProfileError as e:
        try:
            known = sorted(profiles.load_all())
        except profiles.ProfileError:
            known = []
        raise ConfigError(f"projects.yaml: project '{pid}': type: no usable profile {t!r} "
                          f"({e}); known types: {', '.join(known) or 'none'}") from e
    return t


def parse_policies(pid, v):
    """Validate one project's `policies:`, the permission policies attached to it alone (PRD
    6.5 layer 3, e.g. engine-unity once a game's PRD settles on Unity). None = none. Each id
    must have a file in policies/types/; nomad_policy checks what is in it."""
    if v is None:
        return ()
    if not isinstance(v, list):
        raise ConfigError(f"projects.yaml: project '{pid}': policies: expected a list of policy "
                          f"ids, got {type(v).__name__}")
    from nomad_projects import profiles
    out = []
    for i, x in enumerate(v):
        if not isinstance(x, str) or not profiles.ID_RE.match(x.strip()):
            raise ConfigError(f"projects.yaml: project '{pid}': policies[{i}]: not a policy id: {x!r}")
        x = x.strip()
        if not os.path.isfile(os.path.join(profiles.TYPE_POLICIES_DIR, x + ".yaml")):
            raise ConfigError(f"projects.yaml: project '{pid}': policies[{i}]: no policy {x!r} "
                              f"(no policies/types/{x}.yaml)")
        if x in out:
            raise ConfigError(f"projects.yaml: project '{pid}': policies[{i}]: {x!r} is listed twice")
        out.append(x)
    return tuple(out)


def parse_autopilot(pid, repo, v, default_cap=DEFAULT_CAP):
    """Validate one project's `autopilot:` mapping. `repo` is the project's real folder."""
    if not isinstance(v, dict):
        raise ConfigError(f"projects.yaml: project '{pid}': autopilot: expected a mapping, "
                          f"got {type(v).__name__}")
    extra = set(v) - KEYS
    if extra:
        raise _err(pid, min(extra), f"unknown field; allowed: {', '.join(sorted(KEYS))}")

    t = v.get("test")
    if t is None:
        tests = {}
    elif isinstance(t, dict) and "run" not in t:
        if not t:
            raise _err(pid, "test", "empty mapping; give a command or suite: command pairs")
        tests = {str(k): _command(pid, f"test.{k}", c) for k, c in t.items()}
    else:
        tests = {"default": _command(pid, "test", t)}

    build = _command(pid, "build", v["build"]) if v.get("build") is not None else None

    if v.get("pre_merge_gate") is None:
        pre = tuple(tests.values())        # default: every suite, unfiltered
    else:
        pre = _gate(pid, "pre_merge_gate", v["pre_merge_gate"], tests, build)
    if v.get("milestone_gate") is None:
        mile = pre + ((build,) if build and build not in pre else ())   # PRD 5.5
    else:
        mile = _gate(pid, "milestone_gate", v["milestone_gate"], tests, build)

    post = tuple(_command(pid, f"post_test[{i}]", c)
                 for i, c in enumerate(_list(pid, "post_test", v.get("post_test"))))

    declared = tuple(os.path.normpath(os.path.expanduser(p))
                     for p in _str_list(pid, "locks", v.get("locks")))
    for i, p in enumerate(declared):
        if not os.path.isabs(p):
            raise _err(pid, f"locks[{i}]", f"a lock must be an absolute path, got {p!r}")
    used = [lk for c in (*tests.values(), *pre, *mile, *post, *([build] if build else []))
            for lk in c.locks]
    for lk in used:
        if lk not in declared:
            raise _err(pid, "locks", f"{lk} is held by a command but not listed in `locks`")

    flakes = frozenset(_str_list(pid, "flake_set", v.get("flake_set")))
    known = frozenset(_str_list(pid, "known_failures", v.get("known_failures")))
    both = flakes & known
    if both:
        raise _err(pid, "known_failures", f"{sorted(both)} also in flake_set; pick one")

    cap = v.get("cap", default_cap)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise _err(pid, "cap", f"expected a positive integer, got {cap!r}")

    gpu = v.get("requires_gpu", False)
    if not isinstance(gpu, bool):
        raise _err(pid, "requires_gpu", f"expected true or false, got {gpu!r}")

    gate_net = v.get("gate_network", "none")
    if gate_net not in GATE_NETWORKS:
        raise _err(pid, "gate_network", f"expected one of {GATE_NETWORKS}, got {gate_net!r}")

    fmt, files = _results(pid, v.get("results"))

    regen = _str_list(pid, "regenerated", v.get("regenerated"))
    for i, p in enumerate(regen):
        if os.path.isabs(p) or ".." in p.replace("\\", "/").split("/") or not p.strip("./"):
            raise _err(pid, f"regenerated[{i}]", f"expected a path inside the repo, got {p!r}")

    noise = tuple(_noise(pid, f"noise[{i}]", n) for i, n in enumerate(_list(pid, "noise", v.get("noise"))))

    lanes = tuple(dict.fromkeys(x.lower() for x in _str_list(pid, "lanes", v.get("lanes"))))
    if v.get("lanes") is not None and not lanes:
        raise _err(pid, "lanes", "empty list; leave `lanes` out to take every lane")

    model = v.get("model")
    if model is not None and model not in MODELS:
        raise _err(pid, "model", f"expected one of {sorted(MODELS)} (a role alias), got {model!r}")

    budget = v.get("max_budget_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float))
                               or budget <= 0):
        raise _err(pid, "max_budget_usd", f"expected a positive number, got {budget!r}")

    loop = _str_list(pid, "loop_tests", v.get("loop_tests"))
    if v.get("loop_tests") is not None and not loop:
        raise _err(pid, "loop_tests", "empty list; leave `loop_tests` out to run every suite")
    for i, name in enumerate(loop):
        if name not in tests:
            raise _err(pid, f"loop_tests[{i}]", f"no test suite {name!r}; `test` defines {sorted(tests) or 'none'}")

    wt = v.get("worktree_root")
    wt = _path(_str(pid, "worktree_root", wt), repo) if wt is not None else repo + "-wt"

    return Autopilot(
        test=tests, pre_merge_gate=pre, milestone_gate=mile, build=build, post_test=post,
        locks=declared, flake_set=flakes, known_failures=known, cap=cap,
        working_dirs=tuple(_path(p, repo) for p in _str_list(pid, "working_dirs",
                                                               v.get("working_dirs"))),
        worktree_root=wt,
        deny_edit_globs=_str_list(pid, "deny_edit_globs", v.get("deny_edit_globs")),
        requires_gpu=gpu, results=fmt, results_files=files, regenerated=regen, noise=noise,
        lanes=lanes, model=model or "", max_budget_usd=budget, loop_tests=loop,
        execution=_execution(pid, repo, v.get("execution"), pre), gate_network=gate_net)


def _execution(pid, repo, v, pre):
    """The `execution:` block (#1488), or None when it is absent."""
    if v is None:
        return None
    if not isinstance(v, dict):
        raise _err(pid, "execution", f"expected a mapping, got {type(v).__name__}")
    extra = set(v) - EXEC_KEYS
    if extra:
        raise _err(pid, f"execution.{min(extra)}", f"unknown field; allowed: {', '.join(sorted(EXEC_KEYS))}")
    mode = v.get("mode", "legacy")
    if mode not in EXEC_MODES:
        raise _err(pid, "execution.mode", f"expected one of {list(EXEC_MODES)}, got {mode!r}")
    backends = _str_list(pid, "execution.backends", v.get("backends")) or ("claude",)
    for i, b in enumerate(backends):
        if b not in EXEC_BACKENDS:
            raise _err(pid, f"execution.backends[{i}]", f"expected one of {list(EXEC_BACKENDS)}, got {b!r}")
    if len(set(backends)) != len(backends):
        raise _err(pid, "execution.backends", "a backend is listed twice")
    privacy = v.get("privacy", "cloud_allowed")
    if privacy not in EXEC_PRIVACY:
        raise _err(pid, "execution.privacy", f"expected one of {list(EXEC_PRIVACY)}, got {privacy!r}")
    if privacy == "local_only" and set(backends) - {"local"}:
        raise _err(pid, "execution.backends", "privacy local_only allows only the local backend")
    wall = v.get("wall_seconds", 3600)
    if isinstance(wall, bool) or not isinstance(wall, int) or wall < 60:
        raise _err(pid, "execution.wall_seconds", f"expected an integer of at least 60, got {wall!r}")
    gates = []
    for i, g in enumerate(_list(pid, "execution.gates", v.get("gates"))):
        fld = f"execution.gates[{i}]"
        if not isinstance(g, dict):
            raise _err(pid, fld, f"expected a mapping, got {type(g).__name__}")
        if set(g) - GATE_KEYS:
            raise _err(pid, f"{fld}.{min(set(g) - GATE_KEYS)}", f"unknown field; allowed: {', '.join(sorted(GATE_KEYS))}")
        timeout = g.get("timeout", 1800)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise _err(pid, f"{fld}.timeout", f"expected a positive integer, got {timeout!r}")
        roots = tuple(_path(r, repo) for r in _str_list(pid, f"{fld}.read_roots", g.get("read_roots")))
        if "/" in roots:
            raise _err(pid, f"{fld}.read_roots", "the filesystem root cannot be a read root")
        gates.append(ExecGate(_str(pid, f"{fld}.id", g.get("id")), _str(pid, f"{fld}.run", g.get("run")),
                              timeout, roots))
    if len({g.id for g in gates}) != len(gates):
        raise _err(pid, "execution.gates", "two gates share an id")
    if mode == "hybrid" and not gates:
        # The sandboxed check cannot hold locks or take an env: such a gate must be spelled out.
        for i, c in enumerate(pre):
            if c.locks or c.env:
                raise _err(pid, "execution.gates", f"pre_merge_gate[{i}] holds locks or sets env; "
                                                   "list the hybrid gates explicitly")
        gates = [ExecGate(f"pre-merge-{i + 1}", c.render()) for i, c in enumerate(pre)]
        if not gates:
            raise _err(pid, "execution.gates", "hybrid mode needs a gate; the project has no test")
    prepare = v.get("prepare", "")
    if prepare != "":
        prepare = _str(pid, "execution.prepare", prepare)
    return Execution(mode, backends, privacy, tuple(gates), wall, prepare)


def _noise(pid, fld, v):
    """`{glob: "Assets/**/*.meta", rule: unity-default-platforms}` or
    `{glob: ..., rule: only-keys, keys: [productName]}`."""
    if not isinstance(v, dict):
        raise _err(pid, fld, f"expected a mapping with `glob` and `rule`, got {v!r}")
    extra = set(v) - NOISE_KEYS
    if extra:
        raise _err(pid, fld, f"unknown key(s) {sorted(extra)}; allowed: {sorted(NOISE_KEYS)}")
    g = _str(pid, f"{fld}.glob", v.get("glob"))
    if os.path.isabs(g) or ".." in g.replace("\\", "/").split("/"):
        raise _err(pid, f"{fld}.glob", f"expected a glob inside the repo, got {g!r}")
    rule = v.get("rule")
    if rule not in NOISE_RULES:
        raise _err(pid, f"{fld}.rule", f"expected one of {', '.join(NOISE_RULES)}, got {rule!r}")
    keys = _str_list(pid, f"{fld}.keys", v.get("keys"))
    if rule == "only-keys" and not keys:
        raise _err(pid, f"{fld}.keys", "only-keys needs a non-empty list of keys")
    if rule != "only-keys" and keys:
        raise _err(pid, f"{fld}.keys", f"only the only-keys rule takes keys, not {rule}")
    return Noise(g, rule, keys)


def _results(pid, v):
    """`results: nunit` or `{format: nunit, files: Logs/test_*.xml}`; default exit code only."""
    if v is None:
        return "exit", ""
    if isinstance(v, str):
        v = {"format": v}
    if not isinstance(v, dict):
        raise _err(pid, "results", f"expected one of {RESULT_FORMATS} or a mapping, got {v!r}")
    extra = set(v) - RESULTS_KEYS
    if extra:
        raise _err(pid, "results", f"unknown key(s) {sorted(extra)}; allowed: {sorted(RESULTS_KEYS)}")
    fmt = v.get("format", "exit")
    if fmt not in RESULT_FORMATS:
        raise _err(pid, "results.format", f"expected one of {RESULT_FORMATS}, got {fmt!r}")
    files = v.get("files")
    if files is not None:
        files = _str(pid, "results.files", files)
        if os.path.isabs(files) or ".." in files.split("/"):
            raise _err(pid, "results.files", f"a glob relative to the worktree, got {files!r}")
    return fmt, files or ""


def _list(pid, fld, v):
    if v is None:
        return []
    if not isinstance(v, list):
        raise _err(pid, fld, f"expected a list, got {type(v).__name__}")
    return v


# ---- loading ------------------------------------------------------------------------------

def registry_file(roots=None, override=None):
    """The registry path by the dispatcher's rule: NOMAD_REGISTRY, else nomad/projects.yaml
    under the first of PROJECT_ROOTS (default: the folder holding this checkout's nomad/)."""
    if override is None:
        override = os.environ.get("NOMAD_REGISTRY", "")
    if roots is None:
        roots = os.environ.get("PROJECT_ROOTS", os.path.dirname(NOMAD)).split(":")
    return projects_registry.registry_file(roots, override)


def load(path=None, default_cap=DEFAULT_CAP, base=None):
    """Every project in the registry, in file order. Raises FileNotFoundError without a
    registry and ConfigError on an invalid entry (duplicate id, bad autopilot block).
    `base` is the folder entry paths are relative to (default: the dispatcher's rule, the
    folder holding the registry's nomad/)."""
    f = path or registry_file()
    if not f or not os.path.isfile(f):
        raise FileNotFoundError(f"no projects.yaml found ({f or 'searched PROJECT_ROOTS'})")
    out, seen = [], set()
    for e in projects_registry.read_entries(f, base):
        pid = e["id"]
        if pid in seen:
            raise ConfigError(f"projects.yaml: project '{pid}': id: listed twice")
        seen.add(pid)
        repo = os.path.realpath(e["path"])
        ap = e.get("autopilot")
        out.append(Project(
            id=pid, path=e["path"], group=str(e.get("group") or ""),
            aliases=tuple(str(a).lower() for a in e.get("aliases") or []),
            dispatch=e.get("dispatch", True) is not False,
            type=parse_type(pid, e.get("type")), policies=parse_policies(pid, e.get("policies")),
            autopilot=None if ap is None else parse_autopilot(pid, repo, ap, default_cap)))
    return out


def get(project_id, path=None, default_cap=DEFAULT_CAP, base=None):
    """One project by id, or None."""
    return next((p for p in load(path, default_cap, base) if p.id == project_id), None)


# ---- the hub registry (what the supervisor runs against) --------------------------------------
#
# The dispatcher's rule finds nomad/projects.yaml next to the running checkout and resolves entry
# paths against that checkout's parent. Run from a worktree (~/nomad-wt/<x>) that resolved
# `runematch` to ~/nomad-wt/runematch (#875). The supervisor must act on the operator's real
# projects wherever its code lives, so it reads the hub's file and resolves entries against the
# home folder, which is what the file's header says `path` is relative to.
#
# It reads the hub's committed file on master, not the working copy (#1020): other sessions keep
# ~/nomad on their own branches, so the working copy is whatever branch they left there.

HUB_REPO = "~/nomad"
HUB_REF = "master"
HUB_FILE = "~/nomad/projects.yaml"
HUB_CACHE = "~/.local/state/nomad-autopilot/projects.master.yaml"
HUB_GOOD = "~/.local/state/nomad-autopilot/projects.master.good.yaml"
HUB_BASE = "~"

log = logging.getLogger("autopilot.projects")
_warned = set()
_stale = []         # [master's content, its parse error] while the last-good copy is in use


def hub_file():
    """The supervisor's registry file. NOMAD_PROJECTS_FILE wins; else `projects.yaml` on the hub's
    master (NOMAD_PROJECTS_REF), copied to HUB_CACHE (rewritten only when it changes); else, when
    git can't produce it, the hub's working copy, with one warning. Never the running checkout's.
    A changed master is parsed with the running code first. If it raises ConfigError and a
    last-good copy (HUB_GOOD) exists, that copy is returned instead, with one warning per error
    (#1026); without one, the bad file is returned so the error shows as before."""
    override = os.environ.get("NOMAD_PROJECTS_FILE")
    if override:
        return os.path.expanduser(override)
    repo = os.path.expanduser(HUB_REPO)
    working = os.path.expanduser(HUB_FILE)
    ref = os.environ.get("NOMAD_PROJECTS_REF") or HUB_REF
    try:
        p = subprocess.run(["git", "-C", repo, "show", f"{ref}:projects.yaml"],
                           capture_output=True, text=True, timeout=10)
        why = "" if p.returncode == 0 else (p.stderr.strip().splitlines() or ["git failed"])[-1]
    except (OSError, subprocess.TimeoutExpired) as e:
        p, why = None, f"{type(e).__name__}: {e}"
    if why:
        return _fall_back(working, f"cannot read {ref}:projects.yaml in {repo}: {why}")
    cache = os.path.expanduser(HUB_CACHE)
    good = os.path.expanduser(HUB_GOOD)
    try:
        old = None
        if os.path.isfile(cache):
            with open(cache) as f:
                old = f.read()
        if old != p.stdout:
            if _stale and _stale[0] == p.stdout and os.path.isfile(good):
                return good                     # the same bad master as last pass: no reparse
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            tmp = f"{cache}.tmp-{os.getpid()}"
            with open(tmp, "w") as f:
                f.write(p.stdout)
            bad = _parse_error(tmp)
            if bad and os.path.isfile(good):
                os.remove(tmp)
                return _keep_last_good(p.stdout, bad, good)
            os.replace(tmp, cache)
            if not bad:
                _save_good(cache, good)
        elif not os.path.isfile(good) and not _parse_error(cache):
            _save_good(cache, good)             # first start after the upgrade: seed last-good
    except OSError as e:
        return _fall_back(working, f"cannot write {cache}: {e}")
    _warned.clear()
    _stale.clear()
    return cache


def _parse_error(path):
    """The ConfigError the running code raises on `path`, as text, or "" when it parses."""
    try:
        load(path, base=hub_base())
    except ConfigError as e:
        return str(e)
    return ""


def _save_good(cache, good):
    tmp = f"{good}.tmp-{os.getpid()}"
    shutil.copyfile(cache, tmp)
    os.replace(tmp, good)


def _keep_last_good(content, err, good):
    """#1026: master no longer parses with the running code (a merge added a field this code
    doesn't know yet). Keep the passes on the last registry that did, until a redeploy."""
    if err not in _warned:
        _warned.add(err)
        since = datetime.fromtimestamp(os.path.getmtime(good)).isoformat(timespec="seconds")
        log.warning("registry: master's projects.yaml doesn't parse with the running supervisor "
                    "(%s); using the last good registry from %s. Redeploy: "
                    "scripts/autopilot-deploy.sh", err, since)
    _stale[:] = [content, err]
    return good


def registry_stale():
    """The parse error while hub_file() serves the last-good registry, else None. /health shows
    it as `registry_stale`."""
    return _stale[1] if _stale else None


def _fall_back(working, why):
    if why not in _warned:
        _warned.add(why)
        log.warning("registry: %s; using the working copy %s", why, working)
    return working


def hub_base():
    """The folder entry paths resolve against: NOMAD_PROJECTS_BASE, else the home folder. The
    same for an overriding file, so a worktree copy of projects.yaml still names real repos."""
    return os.path.expanduser(os.environ.get("NOMAD_PROJECTS_BASE") or HUB_BASE)


def hub_load(default_cap=DEFAULT_CAP):
    return load(hub_file(), default_cap, hub_base())


def hub_get(project_id, default_cap=DEFAULT_CAP):
    """One project from the hub registry, or None. The supervisor's registry."""
    return get(project_id, hub_file(), default_cap, hub_base())
