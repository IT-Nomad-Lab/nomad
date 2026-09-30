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
import os
from dataclasses import dataclass, field

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

DEFAULT_CAP = 3            # PRD 5.4: per-project default; the cockpit can change it
FILTER = "{filter}"        # placeholder in a test command for an optional test filter
TEST_REF = "test:"         # gate step `test:<suite>` = that suite, unfiltered
BUILD_REF = "build"        # gate step `build` = the project's build command

KEYS = {"test", "pre_merge_gate", "milestone_gate", "build", "post_test", "locks",
        "flake_set", "known_failures", "cap", "working_dirs", "worktree_root",
        "deny_edit_globs", "requires_gpu", "results", "regenerated"}
COMMAND_KEYS = {"run", "locks", "env"}
RESULT_FORMATS = ("exit", "nunit")   # how the merge queue reads a test suite's results
RESULTS_KEYS = {"format", "files"}


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

    def is_flake(self, test_name):
        return _matches(self.flake_set, test_name)

    def is_known_failure(self, test_name):
        return _matches(self.known_failures, test_name)


@dataclass(frozen=True)
class Project:
    id: str
    path: str                       # absolute, as listed (a symlink stays a symlink)
    group: str = ""
    aliases: tuple = ()
    dispatch: bool = True
    autopilot: Autopilot = None     # None: the project has no autopilot block

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

    fmt, files = _results(pid, v.get("results"))

    regen = _str_list(pid, "regenerated", v.get("regenerated"))
    for i, p in enumerate(regen):
        if os.path.isabs(p) or ".." in p.replace("\\", "/").split("/") or not p.strip("./"):
            raise _err(pid, f"regenerated[{i}]", f"expected a path inside the repo, got {p!r}")

    wt = v.get("worktree_root")
    wt = _path(_str(pid, "worktree_root", wt), repo) if wt is not None else repo + "-wt"

    return Autopilot(
        test=tests, pre_merge_gate=pre, milestone_gate=mile, build=build, post_test=post,
        locks=declared, flake_set=flakes, known_failures=known, cap=cap,
        working_dirs=tuple(_path(p, repo) for p in _str_list(pid, "working_dirs",
                                                               v.get("working_dirs"))),
        worktree_root=wt,
        deny_edit_globs=_str_list(pid, "deny_edit_globs", v.get("deny_edit_globs")),
        requires_gpu=gpu, results=fmt, results_files=files, regenerated=regen)


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

HUB_FILE = "~/nomad/projects.yaml"
HUB_BASE = "~"


def hub_file():
    """NOMAD_PROJECTS_FILE, else ~/nomad/projects.yaml. Never the running checkout's copy."""
    return os.path.expanduser(os.environ.get("NOMAD_PROJECTS_FILE") or HUB_FILE)


def hub_base():
    """The folder entry paths resolve against: NOMAD_PROJECTS_BASE, else the home folder. The
    same for an overriding file, so a worktree copy of projects.yaml still names real repos."""
    return os.path.expanduser(os.environ.get("NOMAD_PROJECTS_BASE") or HUB_BASE)


def hub_load(default_cap=DEFAULT_CAP):
    return load(hub_file(), default_cap, hub_base())


def hub_get(project_id, default_cap=DEFAULT_CAP):
    """One project from the hub registry, or None. The supervisor's registry."""
    return get(project_id, hub_file(), default_cap, hub_base())
