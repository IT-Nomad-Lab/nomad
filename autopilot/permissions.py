"""Per-worker permission profile: the shared template plus one project's `autopilot:` entry.

PRD docs/prd/autopilot.md 5.8, task #875. The operator reviews `permissions.template.json`
once (README-permissions.md explains it). For each worker this module renders it:

- fills {worktree}, {repo} and {state_dir}, and renders a rule holding {default_branch} once
  per protected branch (protected_branches, #989);
- writes one wrapper script per project command (tests, the pre-merge gate, build, post_test)
  and allows exactly those wrappers. A project command is never allowed by its own prefix:
  `Bash(scripts/unity.sh *)` would allow every argument, and `flock`/`VAR=x` prefixes don't
  match prefix rules at all (the RuneMatch trial);
- adds the project's `deny_edit_globs`, read-only denies for its `working_dirs`, and
  `additionalDirectories` (worktree, main checkout, lock folders, working_dirs).

A project entry can only narrow the template. `check_project` rejects an entry that would
widen it (a command that runs something the template denies or hides a command from that
check, a working dir that is `/`, the home folder or holds agent config, a malformed glob), and
`check_narrowing` proves the rendered settings allow nothing beyond the template plus the
wrappers and keep every template deny.

    from autopilot import permissions, projects
    p = projects.get("runematch")
    path, wrappers = permissions.write(p, "/mnt/c/.../RuneMatch-wt/ap-833", task_id=833)
    claude -p ... + permissions.cli_args(path)

Run the tests: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import json
import os
import re
import shlex
from dataclasses import dataclass

from autopilot.projects import FILTER, ConfigError

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "permissions.template.json")
STATE_DIR = os.path.expanduser("~/.local/state/nomad-autopilot")
GPU_LOCK = "/tmp/nomad-gpu.lock"   # requires_gpu: test and build wrappers hold it (PRD 5.4)

# A test filter an agent may pass to a wrapper. Strict on purpose: no spaces, quotes, shell
# operators, globs or a leading '-' (so it can't become an option of the test runner).
FILTER_RE = re.compile(r"^[A-Za-z0-9_.,:+=/][A-Za-z0-9_.,:+=/-]{0,199}$")
FILTER_CHARS = "A-Za-z0-9_.,:+=/-"          # the same set, for the wrapper's `case`
FILTER_MAX = 200
# Paths that end up inside rules: no spaces, globs, quotes or parentheses.
SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./@+-]*$")
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")
PROTECTED = ("main", "master")              # always protected, whatever the lookup finds
ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_LOCKS = 7                               # file descriptors 9 down to 3

# Commands a project command may not contain: they would hide a command from the deny check.
OPAQUE = ("sh -c", "bash -c", "zsh -c", "eval", "sudo", "su", "xargs", "env -S")
CONFIG_DIRS = ("~/.claude", "~/.config", "~/.ssh", "~/.gnupg", "~/.local/state/nomad-autopilot")


@dataclass(frozen=True)
class Wrapper:
    name: str                # <project>-<command>, e.g. runematch-test-PlayMode
    path: str                # absolute path of the script
    command: object          # the projects.Command it runs
    takes_filter: bool       # accepts one optional test-filter argument
    script: str              # the script text

    @property
    def rules(self):
        """The allow rules for this wrapper: exact, plus one argument if it takes a filter."""
        return [f"Bash({self.path})"] + ([f"Bash({self.path} *)"] if self.takes_filter else [])


# ---- template ---------------------------------------------------------------------------------

def load_template(path=TEMPLATE):
    with open(path) as f:
        return json.load(f)


def _strip_private(d):
    if isinstance(d, dict):
        return {k: _strip_private(v) for k, v in d.items() if not k.startswith("_")}
    if isinstance(d, list):
        return [_strip_private(x) for x in d]
    return d


def _fill(rule, values):
    for k, v in values.items():
        rule = rule.replace("{" + k + "}", v)
    if re.search(r"\{[a-z_]+\}", rule):
        raise ValueError(f"permissions template: unknown placeholder in {rule!r}")
    return rule


def _fill_all(rule, values, branches):
    """A rule holding {default_branch} renders once per protected branch; any other, once."""
    if "{default_branch}" not in rule:
        return [_fill(rule, values)]
    return [_fill(rule, {**values, "default_branch": b}) for b in branches]


def protected_branches(repo, default_branch=None):
    """The branches no worker may switch its worktree to (#989): the project's default branch,
    looked up in `repo` when not given, plus main and master in case the lookup finds none."""
    if default_branch is None:
        from autopilot.worker import WorkerError, default_branch as lookup
        try:
            default_branch = lookup(repo)
        except WorkerError:
            default_branch = None
    out = list(PROTECTED)
    if default_branch and default_branch not in out:
        out.append(default_branch)
    for b in out:
        if not BRANCH_RE.match(b) or ".." in b:
            raise ConfigError(f"permissions: default branch {b!r} can't go into a permission "
                              "rule (letters, digits and _./- only)")
    return out


def render_template(worktree, repo, state_dir=STATE_DIR, template=None, branches=PROTECTED):
    """The template with its placeholders filled; no project additions yet. `branches` fills
    {default_branch} (see protected_branches)."""
    t = _strip_private(template if template is not None else load_template())
    values = {"worktree": _safe_path("worktree", worktree), "repo": _safe_path("repo", repo),
              "state_dir": _safe_path("state_dir", state_dir)}
    perms = t.setdefault("permissions", {})
    for key in ("allow", "deny", "ask"):
        if key in perms:
            rendered = [x for r in perms[key] for x in _fill_all(r, values, branches)]
            perms[key] = list(dict.fromkeys(rendered))
    return t


def _safe_path(what, p):
    p = os.path.normpath(os.path.expanduser(p))
    if not SAFE_PATH_RE.match(p) or p == "/":
        raise ConfigError(f"permissions: {what} {p!r} must be an absolute path of letters, "
                          "digits and ._@+-/ only (it goes into permission rules)")
    return p


def _under(path, parent):
    return path == parent or path.startswith(parent.rstrip("/") + "/")


# ---- narrowing check (project entry) ----------------------------------------------------------

def _bash_rule_regex(pattern):
    """A Bash(...) rule body as a regex, by Claude Code's documented matching: `*` matches
    anything; a trailing ` *` or `:*` also matches the bare command (word boundary)."""
    if pattern.endswith(":*"):
        pattern = pattern[:-2] + " *"
    if pattern.endswith(" *"):
        head = pattern[:-2]
        return re.compile("^" + ".*".join(map(re.escape, head.split("*"))) + r"(\s.*)?$", re.S)
    return re.compile("^" + ".*".join(map(re.escape, pattern.split("*"))) + "$", re.S)


def _deny_regexes(settings):
    out = []
    for r in settings.get("permissions", {}).get("deny", []):
        m = re.match(r"^Bash\((.*)\)$", r, re.S)
        if m:
            out.append((r, _bash_rule_regex(m.group(1))))
    return out


_SPLIT = re.compile(r"&&|\|\||[;|&\n]|\$\(|`")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*\s+")
_GIT_OPTS = re.compile(r"^git\s+(?:(?:-C|-c)\s+\S+\s+|--git-dir=\S+\s+|--work-tree=\S+\s+)+")
_STRIPPED = re.compile(r"^(env|timeout(\s+-\S+)*\s+\S+|time|nice(\s+-n\s*-?\d+)?|nohup|command|"
                       r"builtin|stdbuf(\s+-\S+)*)\s+")


def _segments(run):
    """The simple commands in a shell line, with leading VAR=x and wrappers such as `timeout`
    removed. Conservative: it splits on operators even inside quotes (a false alarm, never a
    miss)."""
    out = []
    for seg in _SPLIT.split(run.replace(FILTER, "FILTER")):
        s = " ".join(seg.replace("(", " ").replace(")", " ").split())
        while True:
            t = _ASSIGN.sub("", s)
            t = _STRIPPED.sub("", t)
            t = _GIT_OPTS.sub("git ", t)
            if t == s:
                break
            s = t
        if s:
            out.append(s)
    return out


def _check_command(pid, fld, cmd, denies):
    run = cmd.run
    if "\n" in run or "\r" in run:
        raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: a command must be "
                          "one line")
    if FILTER in run:
        try:
            words = shlex.split(run, posix=False)   # keeps quotes, so "{filter}" != {filter}
        except ValueError as e:
            raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: {e}")
        if words.count(FILTER) != run.count(FILTER):
            raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: {FILTER} must "
                              "be a word of its own, outside quotes")
    for seg in _segments(run):
        for bad in OPAQUE:
            if seg == bad or seg.startswith(bad + " "):
                raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: `{bad}` "
                                  "hides a command from the permission check; call a script "
                                  "in the repo instead")
        for rule, rx in denies:
            if rx.match(seg):
                raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}: runs "
                                  f"`{seg}`, which the template denies ({rule}); a project "
                                  "entry may only narrow the template")
    for k in cmd.env:
        if not ENV_RE.match(k):
            raise ConfigError(f"projects.yaml: project '{pid}': autopilot.{fld}.env: bad "
                              f"variable name {k!r}")
    for lk in cmd.locks:
        _safe_path(f"project '{pid}' lock", lk)


def _commands(ap):
    """(field name, Command) for every command a worker may run: tests, pre-merge gate, build,
    post_test. The milestone gate is the supervisor's, so it gets no wrapper."""
    out = [(f"test.{k}", c) for k, c in ap.test.items()]
    out += [(f"pre_merge_gate[{i}]", c) for i, c in enumerate(ap.pre_merge_gate)]
    if ap.build is not None:
        out.append(("build", ap.build))
    out += [(f"post_test[{i}]", c) for i, c in enumerate(ap.post_test)]
    return out


def check_project(project, template_rendered):
    """Raise ConfigError if the project's entry would widen the template."""
    ap = project.autopilot
    if ap is None:
        raise ConfigError(f"projects.yaml: project '{project.id}': no autopilot block")
    if not NAME_RE.match(project.id):
        raise ConfigError(f"projects.yaml: project '{project.id}': id must match {NAME_RE.pattern}"
                          " to name its wrappers")
    denies = _deny_regexes(template_rendered)
    for fld, c in _commands(ap):
        _check_command(project.id, fld, c, denies)
    for k in ap.test:
        if not NAME_RE.match(k):
            raise ConfigError(f"projects.yaml: project '{project.id}': autopilot.test.{k}: suite "
                              f"name must match {NAME_RE.pattern}")
    home = os.path.realpath(os.path.expanduser("~"))
    for i, wd in enumerate(ap.working_dirs):
        fld = f"projects.yaml: project '{project.id}': autopilot.working_dirs[{i}]"
        real = os.path.realpath(_safe_path(fld, wd))
        if _under(home, real):
            raise ConfigError(f"{fld}: {wd} is the home folder or above it; list the folder "
                              "the worker needs")
        for c in CONFIG_DIRS:
            c = os.path.realpath(os.path.expanduser(c))
            if _under(real, c) or _under(c, real):
                raise ConfigError(f"{fld}: {wd} holds agent config or secrets ({c})")
    for i, g in enumerate(ap.deny_edit_globs):
        if g.startswith("!") or re.search(r"[()\s\"'\\]", g):
            raise ConfigError(f"projects.yaml: project '{project.id}': "
                              f"autopilot.deny_edit_globs[{i}]: {g!r} is not a plain glob")
    if len(_locks_for(ap, True)) > MAX_LOCKS:
        raise ConfigError(f"projects.yaml: project '{project.id}': autopilot.locks: at most "
                          f"{MAX_LOCKS} locks per command")


# ---- wrappers -----------------------------------------------------------------------------------

def validate_filter(f):
    """True when `f` is a test filter a wrapper accepts (same rule as the script's check)."""
    return isinstance(f, str) and bool(FILTER_RE.match(f))


def _locks_for(ap, heavy, cmd=None):
    locks = set(cmd.locks if cmd else ap.locks)
    if heavy and ap.requires_gpu:
        locks.add(GPU_LOCK)
    return sorted(locks)          # one order everywhere, so two wrappers can't deadlock


def _script(project, name, cmd, locks, worktree_root):
    takes = FILTER in cmd.run
    run = cmd.run.replace(FILTER, '${filter:+"$filter"}') if takes else cmd.run
    q = shlex.quote
    lines = [
        "#!/bin/sh",
        "# Generated by nomad autopilot/permissions.py (task #875). Do not edit: it is rewritten",
        "# from projects.yaml each time a worker starts. Workers may run it; nobody edits it.",
        f"# project: {project.id}   wrapper: {name}",
        f"# runs: {cmd.run}",
        "set -eu",
        "filter=",
    ]
    if takes:
        lines += [
            'if [ "$#" -gt 1 ]; then',
            f'  echo "{name}: takes at most one test filter" >&2; exit 64',
            "fi",
            'if [ "$#" -eq 1 ]; then',
            '  case $1 in',
            f"    ''|-*|*[!{FILTER_CHARS}]*)",
            f'      echo "{name}: a filter uses only {FILTER_CHARS} and does not start with -" >&2',
            "      exit 64;;",
            "  esac",
            f'  if [ "${{#1}}" -gt {FILTER_MAX} ]; then',
            f'    echo "{name}: filter longer than {FILTER_MAX} characters" >&2; exit 64',
            "  fi",
            "  filter=$1",
            "fi",
        ]
    else:
        lines += [
            'if [ "$#" -gt 0 ]; then',
            f'  echo "{name}: takes no arguments" >&2; exit 64',
            "fi",
        ]
    lines += [
        "top=$(git rev-parse --show-toplevel)",
        "case $top in",
        f"  {q(worktree_root)}/*) ;;",
        f'  *) echo "{name}: run it from a task worktree under {worktree_root}" >&2; exit 65;;',
        "esac",
        'cd "$top"',
    ]
    lines += [f"export {k}={q(v)}" for k, v in sorted(cmd.env.items())]
    for fd, lk in zip(range(9, 9 - MAX_LOCKS, -1), locks):
        lines += [f"exec {fd}>>{q(lk)}", f"flock {fd}"]
    lines.append(run)
    return "\n".join(lines) + "\n", takes


def wrappers(project, state_dir=STATE_DIR):
    """One Wrapper per distinct command a worker may run, in declaration order."""
    ap = project.autopilot
    bin_dir = os.path.join(_safe_path("state_dir", state_dir), "bin")
    wt_root = _safe_path("worktree_root", ap.worktree_root)
    named = [(f"test-{k}", c, True) for k, c in ap.test.items()]
    seen = [c for _, c, _ in named]          # Command holds a dict, so a list, not a set
    for i, c in enumerate(ap.pre_merge_gate):
        if c not in seen:
            named.append((f"gate-{i + 1}", c, False))
            seen.append(c)
    if ap.build is not None and ap.build not in seen:
        named.append(("build", ap.build, True))
    for i, c in enumerate(ap.post_test):
        named.append((f"post-test-{i + 1}", c, False))
    out = []
    for suffix, c, heavy in named:
        name = f"{project.id}-{suffix}"
        text, takes = _script(project, name, c, _locks_for(ap, heavy, c), wt_root)
        out.append(Wrapper(name, os.path.join(bin_dir, name), c, takes, text))
    return out


# ---- grants (#979, docs/prd/ap-permission-grants.md 5.2 and 5.4) -------------------------------
#
# A grant is the operator's `grant: <action> <arg>` line (tasks.Grant). Each one opens exactly one
# named action for one run: build_settings(grants=...) removes only the denies listed in the PRD
# and adds one exact allow. With no grants, build_settings takes none of this code.

REL_PATH_RE = re.compile(r"^[A-Za-z0-9_.@+-][A-Za-z0-9_./@+-]*$")
GLOB_CHARS = set("*?[]{}")


class GrantRefused(ValueError):
    """A grant line that opens nothing; the message is the reason the operator is told."""


@dataclass(frozen=True)
class GrantFacts:
    """What the grant checks need to know about the repo, read by the worker (git) at launch.
    files: the worktree's tracked and untracked files, relative; checked_out: every branch a
    worktree holds; worktrees: every path `git worktree list` gives."""
    default_branch: str
    own_branch: str
    files: tuple = ()
    checked_out: frozenset = frozenset()
    worktrees: frozenset = frozenset()


def protected_edit_denies(repo, state_dir, working_dirs=()):
    """The Edit denies no grant ever removes: the main checkout, the state folder, ~/.claude and
    the project's working_dirs."""
    out = {f"Edit(/{repo}/**)", f"Edit(/{state_dir}/**)", "Edit(~/.claude/**)"}
    return out | {f"Edit(/{_safe_path('working dir', d)}/**)" for d in working_dirs}


def _edit_rule(rule):
    m = re.match(r"^Edit\((.*)\)$", rule)
    return m.group(1) if m else None


def _check_edit_path(arg, worktree):
    if arg.startswith(("/", "~")):
        raise GrantRefused("an edit grant names a path relative to the worktree root")
    if GLOB_CHARS & set(arg):
        raise GrantRefused("an edit grant names one file, no glob characters")
    if ".." in arg.split("/") or not REL_PATH_RE.match(arg):
        raise GrantRefused("the path holds `..` or a character a permission rule can't hold")
    full = os.path.normpath(os.path.join(worktree, arg))
    if arg.endswith("/") or os.path.isdir(full):
        raise GrantRefused("an edit grant names one file, not a folder")
    return full


def edit_relaxation(arg, deny, worktree, protected, files):
    """(denies to drop, allow to add, exact denies to add back) for `grant: edit <arg>`.
    Raises GrantRefused when a protected deny covers the path, or a file the dropped denies also
    covered can't be named in an exact rule."""
    full = _check_edit_path(arg, worktree)
    for r in protected:
        if _edit_matches(_edit_rule(r), full, worktree, True):
            raise GrantRefused(f"{arg} is under a folder no grant opens ({r})")
    drops = [r for r in deny if r not in protected and _edit_rule(r) is not None
             and _edit_matches(_edit_rule(r), full, worktree, True)]
    redeny = []
    for f in files:
        other = os.path.normpath(os.path.join(worktree, f))
        if other == full or not any(_edit_matches(_edit_rule(r), other, worktree, True)
                                    for r in drops):
            continue
        if not SAFE_PATH_RE.match(other):
            raise GrantRefused(f"{f} would open with it and can't be named in an exact deny")
        redeny.append(f"Edit(/{other})")
    return drops, f"Edit(/{full})", redeny


def denied_edit_globs(project, paths):
    """[(path, glob)] for each repo-relative path one of the project's `deny_edit_globs` covers
    (first matching glob). #1025: an operator told to `grant: edit` such a path still saw the
    worker's Edit denied (#1001's projects.yaml), so the needs-operator text names these."""
    ap = getattr(project, "autopilot", None)
    globs = getattr(ap, "deny_edit_globs", ()) or ()
    out = []
    for p in paths:
        hit = next((g for g in globs if _edit_matches(g, os.path.join("/wt", p), "/wt", True)),
                   None)
        if hit:
            out.append((p, hit))
    return out


def deny_glob_note(project, paths):
    """A paragraph for a needs-operator comment naming the paths `deny_edit_globs` covers, or ""."""
    hits = denied_edit_globs(project, paths)
    if not hits:
        return ""
    listed = "\n".join(f"- `{p}` (`{g}`)" for p, g in hits)
    return ("\n\nThe project's `deny_edit_globs` in projects.yaml cover:\n" + listed + "\n\nA "
            "`grant: edit` won't unlock these for a worker. Resolve them yourself in the "
            "worktree, or change `deny_edit_globs` first.")


def _check_branch_name(name):
    if (not BRANCH_RE.match(name) or ".." in name or "//" in name or "@{" in name
            or name.endswith(("/", ".", ".lock"))):
        raise GrantRefused(f"`{name}` is not a valid branch name")


def check_grant(g, project, worktree, facts, protected=(), deny=()):
    """The relaxation (drops, allows, extra denies) one grant gives this run's profile, or
    GrantRefused with the reason (PRD 5.2's argument rules). `merge` never reaches a profile."""
    action, arg = g.action, g.arg
    if action == "edit":
        drops, allow, redeny = edit_relaxation(arg, deny, worktree, protected, facts.files)
        return drops, [allow], redeny
    if action == "delete branch":
        _check_branch_name(arg)
        if arg in protected_branches(project.repo, facts.default_branch):
            raise GrantRefused(f"`{arg}` is the default branch")
        if arg == facts.own_branch:
            raise GrantRefused(f"`{arg}` is this task's own branch")
        if arg in facts.checked_out:
            raise GrantRefused(f"`{arg}` is checked out in a worktree")
        return ([f"Bash(git -C {worktree} branch -d *)"],
                [f"Bash(git -C {worktree} branch -d {arg})"], [])
    if action == "delete worktree":
        if not SAFE_PATH_RE.match(arg) or arg != os.path.normpath(arg):
            raise GrantRefused("a worktree grant names an absolute path of letters, digits and "
                               "._@+-/ only")
        root = _safe_path("worktree_root", project.autopilot.worktree_root)
        if arg == root or not _under(arg, root):
            raise GrantRefused(f"{arg} is not under the project's worktree_root {root}")
        if arg == worktree:
            raise GrantRefused(f"{arg} is this task's own worktree")
        if _under(arg, project.repo):
            raise GrantRefused(f"{arg} is the main checkout")
        if arg not in facts.worktrees:
            raise GrantRefused(f"{arg} is not listed by `git worktree list`")
        return (["Bash(git worktree remove *)"],
                [f"Bash(git -C {worktree} worktree remove {arg})"], [])
    if action == "merge":
        raise GrantRefused("a merge grant goes to the merge queue, never to a worker")
    raise GrantRefused(f"unknown grant `{action}`")


def apply_grants(settings, grants, project, worktree, facts, state_dir=STATE_DIR):
    """Relax `settings` in place, one grant at a time. Returns (applied grants, [(grant,
    reason)] refused, allows added, denies dropped)."""
    perms = settings["permissions"]
    protected = protected_edit_denies(_safe_path("repo", project.repo),
                                      _safe_path("state_dir", state_dir),
                                      project.autopilot.working_dirs)
    applied, refused, added, dropped = [], [], [], []
    for g in grants:
        try:
            drops, allows, denies = check_grant(g, project, worktree, facts, protected,
                                                perms["deny"])
        except GrantRefused as e:
            refused.append((g, str(e)))
            continue
        perms["deny"] = [r for r in perms["deny"] if r not in drops]
        perms["deny"] += [r for r in denies if r not in perms["deny"]]
        perms["allow"] += [r for r in allows if r not in perms["allow"]]
        added += allows
        dropped += drops
        applied.append(g)
    return applied, refused, added, dropped


# ---- settings -----------------------------------------------------------------------------------

def build_settings(project, worktree, state_dir=STATE_DIR, template=None, default_branch=None,
                   grants=(), facts=None):
    """The settings dict for one worker of `project` in `worktree`. Raises ConfigError when the
    project entry would widen the template or a path can't go into a rule. `default_branch`
    is looked up in the main checkout when not given.

    `grants` (#979) relax it, each by exactly what check_grant gives; `facts` (GrantFacts) is
    then required. Use build_settings_with_grants to learn which were applied or refused."""
    return build_settings_with_grants(project, worktree, state_dir, template, default_branch,
                                      grants, facts)[0]


def build_settings_with_grants(project, worktree, state_dir=STATE_DIR, template=None,
                               default_branch=None, grants=(), facts=None):
    """(settings, applied grants, [(grant, reason)] refused). See build_settings."""
    ap = project.autopilot
    if ap is None:
        raise ConfigError(f"projects.yaml: project '{project.id}': no autopilot block")
    repo = _safe_path("repo", project.repo)
    worktree = _safe_path("worktree", worktree)
    if not _under(worktree, _safe_path("worktree_root", ap.worktree_root)):
        raise ConfigError(f"permissions: worktree {worktree} is not under the project's "
                          f"worktree_root {ap.worktree_root}")
    if _under(worktree, repo):
        raise ConfigError(f"permissions: worktree {worktree} is inside the main checkout "
                          f"{repo}, which workers may only read")
    branches = protected_branches(repo, default_branch)
    s = render_template(worktree, repo, state_dir, template, branches)
    check_project(project, s)
    perms = s["permissions"]
    ws = wrappers(project, state_dir)
    perms["allow"] = perms.get("allow", []) + [r for w in ws for r in w.rules]
    extra_deny = [f"Edit({g})" for g in ap.deny_edit_globs]
    extra_deny += [f"Edit(/{_safe_path('working dir', d)}/**)" for d in ap.working_dirs]
    perms["deny"] = perms.get("deny", []) + [d for d in extra_deny if d not in perms["deny"]]
    dirs = [worktree, repo]
    dirs += sorted({os.path.dirname(lk) for lk in _locks_for(ap, True)})
    dirs += [_safe_path("working dir", d) for d in ap.working_dirs]
    perms["additionalDirectories"] = list(dict.fromkeys(dirs))
    applied, refused, added, dropped = [], [], (), ()
    if grants:
        if facts is None:
            raise ConfigError("permissions: grants need the repo facts (GrantFacts)")
        applied, refused, added, dropped = apply_grants(s, grants, project, worktree, facts,
                                                        state_dir)
    check_narrowing(s, render_template(worktree, repo, state_dir, template, branches), ws,
                    added, dropped)
    return s, applied, refused


def check_narrowing(settings, template_rendered, wrapper_list, granted_allow=(),
                    granted_drop=()):
    """Raise ConfigError unless `settings` allows no more than the rendered template plus the
    wrappers plus `granted_allow`, and keeps every template deny but `granted_drop` (#979: the
    allows and dropped denies this run's grants account for)."""
    p, t = settings.get("permissions", {}), template_rendered.get("permissions", {})
    allowed = set(t.get("allow", [])) | {r for w in wrapper_list for r in w.rules}
    allowed |= set(granted_allow)
    extra = [r for r in p.get("allow", []) if r not in allowed]
    if extra:
        raise ConfigError(f"permissions: allow rules beyond the template: {extra}")
    missing = [r for r in t.get("deny", []) if r not in p.get("deny", [])
               and r not in granted_drop]
    if missing:
        raise ConfigError(f"permissions: template deny rules dropped: {missing}")
    if p.get("defaultMode") != t.get("defaultMode"):
        raise ConfigError("permissions: defaultMode differs from the template")
    for k in set(p) - {"allow", "deny", "ask", "defaultMode", "additionalDirectories"}:
        raise ConfigError(f"permissions: unexpected key {k!r}")
    for k in set(settings) - {"permissions"}:
        raise ConfigError(f"permissions: unexpected settings key {k!r}")


def write_wrappers(project, state_dir=STATE_DIR):
    """Write (or rewrite) the project's wrapper scripts under `state_dir`/bin and return them.
    The merge queue (#871) runs the gate through these too, so locks and env apply the same."""
    ws = wrappers(project, state_dir)
    for w in ws:
        os.makedirs(os.path.dirname(w.path), exist_ok=True)
        tmp = w.path + ".tmp"
        with open(tmp, "w") as f:
            f.write(w.script)
        os.chmod(tmp, 0o555)
        os.replace(tmp, w.path)
    return ws


def settings_path(project, worktree, state_dir=STATE_DIR, task_id=None):
    tag = str(task_id) if task_id is not None else os.path.basename(os.path.normpath(worktree))
    if not NAME_RE.match(tag):
        raise ConfigError(f"permissions: bad task tag {tag!r}")
    return os.path.join(state_dir, "settings", f"{project.id}-{tag}.json")


def write(project, worktree, state_dir=STATE_DIR, task_id=None, template=None,
          default_branch=None, grants=(), facts=None, refused=None):
    """Write the worker's settings file, its mcp.json and the project's wrappers under
    `state_dir`. Returns (settings path, [Wrapper]). Installs nothing else. With `grants`
    (#979), the refused ones are appended to the list `refused` as (grant, reason)."""
    s, _, why = build_settings_with_grants(project, worktree, state_dir, template,
                                           default_branch, grants, facts)
    if refused is not None:
        refused.extend(why)
    ws = write_wrappers(project, state_dir)
    path = settings_path(project, worktree, state_dir, task_id)
    write_mcp(os.path.dirname(path))
    with open(path + ".tmp", "w") as f:
        json.dump(s, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return path, ws


# The criteria drafter's run (#889): it reads nomad-pm and answers in its final message. The
# supervisor, not the run, posts the draft, so the run may write nothing at all.
CRITERIA_ALLOW = ("mcp__nomad-pm__get", "mcp__nomad-pm__tree", "mcp__nomad-pm__search")
CRITERIA_DENY = ("Bash", "Edit", "Write", "NotebookEdit", "WebFetch", "WebSearch", "Agent",
                 "mcp__nomad-pm__comment", "mcp__nomad-pm__transition",
                 "mcp__nomad-pm__update_fields", "mcp__nomad-pm__create",
                 "mcp__nomad-pm__create_plan", "mcp__nomad-pm__relate",
                 "mcp__nomad-pm__unrelate", "mcp__nomad-pm__blocked", "mcp__nomad-pm__watch")


def criteria_settings():
    """Settings for a criteria-draft run: nomad-pm reads only; no shell, no edits, no web."""
    return {"permissions": {"defaultMode": "dontAsk", "allow": list(CRITERIA_ALLOW),
                            "deny": list(CRITERIA_DENY)}}


def write_criteria(state_dir=STATE_DIR):
    """Write the criteria-draft settings file under `state_dir`; returns its path."""
    path = os.path.join(state_dir, "settings", "criteria-draft.json")
    write_mcp(os.path.dirname(path))
    with open(path + ".tmp", "w") as f:
        json.dump(criteria_settings(), f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return path


# The MCP servers a run gets. `--setting-sources ""` drops the user-scope servers in
# ~/.claude.json along with the settings (#832 and #842 ran with no nomad-pm at all), so the
# supervisor copies these entries into its own file and passes it with --strict-mcp-config:
# the run gets nomad-pm and nothing else, not the operator's claude.ai connectors either.
MCP_SERVERS = ("nomad-pm",)
MCP_FILE = "mcp.json"


def mcp_source():
    """Where the operator's MCP servers are defined; NOMAD_AUTOPILOT_MCP_SOURCE overrides it."""
    return os.environ.get("NOMAD_AUTOPILOT_MCP_SOURCE") or os.path.expanduser("~/.claude.json")


def mcp_config(source=None):
    """{"mcpServers": {...}} holding only MCP_SERVERS, copied from the user-scope config.
    Raises ConfigError when one is missing: a run without its tracker can only fail."""
    source = source or mcp_source()
    try:
        with open(source) as f:
            servers = json.load(f).get("mcpServers") or {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"permissions: can't read MCP servers from {source}: {e}")
    missing = [n for n in MCP_SERVERS if n not in servers]
    if missing:
        raise ConfigError(f"permissions: MCP server(s) {missing} not defined in {source}")
    return {"mcpServers": {n: servers[n] for n in MCP_SERVERS}}


def mcp_path(settings_file):
    """The MCP config that goes with a settings file: mcp.json in the same folder."""
    return os.path.join(os.path.dirname(settings_file), MCP_FILE)


def write_mcp(settings_dir, source=None):
    """Write mcp.json into `settings_dir` (rewritten each launch, so it follows the operator's
    config); returns its path."""
    cfg = mcp_config(source)
    path = os.path.join(settings_dir, MCP_FILE)
    os.makedirs(settings_dir, exist_ok=True)
    with open(path + ".tmp", "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(path + ".tmp", path)
    return path


def cli_args(settings_file):
    """The claude flags a worker runs with. `--setting-sources ""` keeps user, project and local
    settings out: the operator's ~/.claude/settings.json allows `git -C:*` and `git push:*`,
    and a repo's .claude/settings*.json could add allows too. It drops the user-scope MCP
    servers as well, so nomad-pm comes from the mcp.json that write/write_criteria put next to
    the settings file, and --strict-mcp-config keeps every other server out."""
    return ["--settings", settings_file, "--setting-sources", "",
            "--mcp-config", mcp_path(settings_file), "--strict-mcp-config",
            "--permission-mode", "dontAsk"]


# ---- local evaluation (an approximation, for tests and review) --------------------------------

def _glob_regex(glob):
    out, i = "", 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out, i = out + "(?:.*/)?", i + 3
        elif glob.startswith("**", i):
            out, i = out + ".*", i + 2
        elif glob[i] == "*":
            out, i = out + "[^/]*", i + 1
        elif glob[i] == "?":
            out, i = out + "[^/]", i + 1
        else:
            out, i = out + re.escape(glob[i]), i + 1
    return re.compile("^" + out + "$")


def _edit_matches(pattern, path, cwd, is_deny):
    """Claude Code's Edit(...) path forms: `//abs`, `~/home`, `/relative-to-settings` (not used
    here), and relative globs, which in a deny rule match at any depth under cwd."""
    if pattern.startswith("//"):
        return bool(_glob_regex(pattern[1:]).match(path))
    if pattern.startswith("~/"):
        return bool(_glob_regex(os.path.expanduser(pattern)).match(path))
    rel = os.path.relpath(path, cwd)
    if rel.startswith(".."):
        return False
    rx = _glob_regex(pattern)
    if is_deny or pattern.startswith("**/"):
        return any(rx.match("/".join(rel.split("/")[k:])) for k in range(rel.count("/") + 1))
    return bool(rx.match(rel))


def decide_edit(settings, path, cwd):
    """'deny', 'allow' or 'refuse' (no rule: dontAsk refuses) for editing `path`."""
    p = settings["permissions"]
    for kind in ("deny", "allow"):
        for r in p.get(kind, []):
            if r == "Edit" and kind == "allow":
                return "allow"
            m = re.match(r"^Edit\((.*)\)$", r)
            if m and _edit_matches(m.group(1), path, cwd, kind == "deny"):
                return kind
    return "refuse"


def decide_bash(settings, command):
    """'deny', 'allow' or 'refuse' for a Bash command line, by the documented rules: split on
    shell operators, deny if any part matches a deny rule, allow only if every part matches an
    allow rule, otherwise dontAsk refuses. Approximate: the real parser is Claude Code's, and
    the manual check in README-permissions.md is the authority."""
    p = settings["permissions"]
    def rx(kind):
        return [_bash_rule_regex(m.group(1)) for r in p.get(kind, [])
                for m in [re.match(r"^Bash\((.*)\)$", r, re.S)] if m]
    deny, allow = rx("deny"), rx("allow")
    parts = [" ".join(x.split()) for x in _SPLIT.split(command) if x.strip()]
    if any(d.match(_ASSIGN.sub("", s)) for s in parts for d in deny):
        return "deny"
    if parts and all(any(a.match(s) for a in allow) for s in parts):
        return "allow"
    return "refuse"
