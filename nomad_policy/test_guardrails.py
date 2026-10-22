"""Layer 1 as imported from today's rules (#1391): policies/guardrail-guard-hook.yaml (from
.claude/hooks/guard.py), policies/guardrail-autopilot-worker.yaml and
policies/autopilot-worker-base.yaml (from autopilot/permissions.template.json).

The gate is an identical-output test, not a list of samples: the policies are rendered back to
the guard.py rule sets and to the template's `permissions` block and must equal today's content
exactly, in order, so every rule is in exactly one statement and nothing was added or lost.
Further tests ask the evaluator and the real guard.py the same questions.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest nomad_policy -q
"""
import importlib.util
import json
import os
import subprocess
import sys

import pytest

from nomad_policy import evaluator as ev
from nomad_policy import schema

NOMAD = schema.NOMAD
REG = schema.Registry()                         # these policies name no MCP server and no skill
GUARD_PY = os.path.join(NOMAD, ".claude", "hooks", "guard.py")
TEMPLATE = os.path.join(NOMAD, "autopilot", "permissions.template.json")


def _load(pid):
    return schema.load_file(os.path.join(schema.POLICIES_DIR, pid + ".yaml"), REG)


GUARD, WORKER_DENY, WORKER_ALLOW = (_load(p) for p in
                                    ("guardrail-guard-hook", "guardrail-autopilot-worker", "autopilot-worker-base"))
LAYERS = ev.Layers(guardrail=(GUARD, WORKER_DENY), type=(WORKER_ALLOW,))


def _guard_module():
    spec = importlib.util.spec_from_file_location("guard_under_test", GUARD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _guard_module()


def _rules(policy, sid):
    """The match list of the one statement with this sid."""
    found = [s for s in policy.statements if s.sid == sid]
    assert len(found) == 1, f"{policy.id}: expected one statement {sid!r}, found {len(found)}"
    return list(found[0].match)


def render_guard(policy):
    """The guard.py rule sets, rebuilt from the policy."""
    return {"COMMIT_BYPASS": _rules(policy, "guard-commit-bypass"),
            "SECRETS": _rules(policy, "guard-secret-exposure"),
            "IRREVERSIBLE": _rules(policy, "guard-irreversible"),
            "PROTECTED_PATHS": tuple(_rules(policy, "guard-protected-paths"))}


def render_template_permissions(deny_policy, allow_policy):
    """The template's `permissions` block, rebuilt from the policies (placeholders unfilled)."""
    def rules(policy, effect):
        return [m for s in policy.statements if s.effect == effect for m in s.match]
    return {"defaultMode": deny_policy.settings["default_mode"],
            "allow": rules(allow_policy, "allow"),
            "deny": rules(deny_policy, "deny")}


# ---- identical output -------------------------------------------------------------------------

def test_guard_rule_sets_render_back_identically():
    assert render_guard(GUARD) == {k: getattr(guard, k) for k in
                                   ("COMMIT_BYPASS", "SECRETS", "IRREVERSIBLE", "PROTECTED_PATHS")}


def test_template_renders_back_identically():
    with open(TEMPLATE) as f:
        want = json.load(f)["permissions"]
    got = render_template_permissions(WORKER_DENY, WORKER_ALLOW)
    assert got == want
    assert set(got) == set(want)                 # no key added (the template has no `ask` list)


def test_every_rule_is_in_exactly_one_statement():
    """Order-preserving equality above covers content; this covers duplicates across statements."""
    for policy in (GUARD, WORKER_DENY, WORKER_ALLOW):
        seen = [(s.kind, m) for s in policy.statements for m in s.match]
        assert len(seen) == len(set(seen)), f"{policy.id}: a rule appears twice"
    sids = [s.sid for p in (GUARD, WORKER_DENY, WORKER_ALLOW) for s in p.statements]
    assert len(sids) == len(set(sids)) and "" not in sids


def test_the_policy_files_have_the_right_shape():
    assert (GUARD.role, WORKER_DENY.role, WORKER_ALLOW.role) == ("guardrail", "guardrail", "permission")
    assert {s.effect for s in GUARD.statements} == {"deny", "ask"}
    assert {s.effect for s in WORKER_DENY.statements} == {"deny"}
    assert {s.effect for s in WORKER_ALLOW.statements} == {"allow"}
    assert WORKER_DENY.locks == ("default_mode",)
    assert ev.values(LAYERS)["default_mode"] == "dontAsk"


def test_every_policy_file_in_the_repo_loads():
    assert set(schema.load_dir(REG)) >= {"guardrail-guard-hook", "guardrail-autopilot-worker", "autopilot-worker-base"}


def test_a_lower_layer_cannot_loosen_the_worker_mode():
    loose = schema.validate({"id": "loose", "role": "permission", "settings": {"default_mode": "bypassPermissions"}}, REG)
    lay = ev.Layers(guardrail=(GUARD, WORKER_DENY), type=(WORKER_ALLOW,), project=(loose,))
    s = ev.effective_settings(lay)["default_mode"]
    assert s.value == "dontAsk" and s.locked and s.ignored[0][1] == "loose"


# ---- the evaluator agrees with the template ---------------------------------------------------

@pytest.mark.parametrize("rule, effect", [
    ("Bash(git status -s)", "allow"), ("Bash(git -C {worktree} commit -m x)", "allow"),
    ("Read", "allow"), ("mcp__nomad-pm", "allow"),
    ("Bash(git push origin x)", "deny"), ("Bash(git -C {worktree} reset --hard)", "deny"),
    ("Bash(git switch master x)", "default-deny"),   # {default_branch} is a placeholder until the compiler fills it
    ("Bash(git switch {default_branch} x)", "deny"), ("Bash(rm -rf build)", "deny"),
    ("Bash(gh api -X DELETE repos/a/b)", "deny"), ("Edit(//home/me/CLAUDE.md)", "deny"),
    ("Bash(curl http://x)", "default-deny"),
])
def test_template_rules_decide_as_the_template_does(rule, effect):
    d = ev.decide(LAYERS, "tool", rule)
    assert (d.reason if effect == "default-deny" else d.effect) == effect


def test_a_deny_in_the_template_beats_a_template_allow():
    # `Bash(git -C {worktree} commit *)` is allowed, `... commit * --amend*` is denied.
    assert ev.decide(LAYERS, "tool", "Bash(git -C {worktree} commit -m x)").allowed
    d = ev.decide(LAYERS, "tool", "Bash(git -C {worktree} commit -m x --amend)")
    assert d.effect == "deny" and d.policy == "guardrail-autopilot-worker"


# ---- the evaluator agrees with the running guard.py -------------------------------------------

def guard_verdict(tool, **tool_input):
    """What guard.py itself says, run as the harness runs it. 'none' = defers to normal permissions."""
    run = subprocess.run([sys.executable, GUARD_PY], input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
                         capture_output=True, text=True, timeout=30,
                         env={**os.environ, "CLAUDE_PROJECT_DIR": NOMAD})
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"] if run.stdout.strip() else "none"


def policy_verdict(kind, name):
    d = ev.decide(ev.Layers(guardrail=(GUARD,)), kind, name)
    return "none" if d.reason == "default-deny" else d.effect


COMMANDS = [
    "git push origin feat/x", "git push --force origin x", "git rebase main", "git reset --hard origin/main",
    "gh pr merge 3", "npm publish", "docker push img", "docker volume rm v", "docker compose down -v",
    "terraform apply", "kubectl delete pod x", "tailscale funnel 8080", "cloudflared tunnel run",
    "rm -rf /home/x/data", "rm -rf /tmp/x", "psql -c 'DROP TABLE a'", "psql -c 'delete from a'",
    "send_email --to x", "n8n update:workflow --id 1 --active", "echo --active=true",
    "git commit --no-verify -m x", "git commit -n -m x", "git config core.hooksPath /x", "git -c core.hooksPath=/x log",
    "chmod -x .githooks/pre-commit", "rm .githooks/pre-commit", "sed -i s/a/b/ .claude/hooks/guard.py",
    "tee .gitleaks.toml", "echo x > scripts/guard.py", "mv secret_scan.py y",
    "cat .env", "cat .env.local", "echo $API_KEY", "git add .env", "printenv GITHUB_TOKEN", "cat ~/.ssh/id_rsa", "ls certs/a.pem",
    "git status", "git config core.hooksPath", "ls -la", "rm -rf /tmp/build", "cat README.md", "git pull",
]


@pytest.mark.parametrize("cmd", COMMANDS)
def test_command_verdict_matches_guard_py(cmd):
    assert policy_verdict("command", cmd) == guard_verdict("Bash", command=cmd)


@pytest.mark.parametrize("path", [".claude/hooks/guard.py", ".claude/hooks/secret_scan.py", ".claude/settings.json",
                                  ".githooks/pre-commit", ".gitleaks.toml", "./.claude/hooks/x.py",
                                  ".claude/agents/dev.md", "src/app.py", "docs/SDLC.md"])
def test_path_verdict_matches_guard_py(path):
    norm = os.path.normpath(path)               # guard.py resolves the path against the project first
    assert policy_verdict("path", norm) == guard_verdict("Write", file_path=path)


def test_what_does_not_fit_a_statement_is_named_in_the_policy():
    """The commit secret scan and the unparseable-input ask live only in guard.py; the file says so."""
    assert "secret scan" in GUARD.description and "COMMIT_SAFE" in GUARD.description


# ---- the agent-bridge hook (#1392) ------------------------------------------------------------

from nomad_policy import compiler  # noqa: E402

BRIDGE = _load(compiler.BRIDGE_GUARDRAIL)
BRIDGE_HOOK = os.path.join(NOMAD, compiler.BRIDGE_HOOK)


def test_bridge_rule_lists_render_back_identically():
    assert compiler.compile_bridge_hook(BRIDGE) == compiler.bridge_hook_rules()
    assert compiler.check_bridge_hook(BRIDGE) == []


def test_the_bridge_policy_has_the_right_shape():
    assert BRIDGE.role == "guardrail" and {s.kind for s in BRIDGE.statements} == {"command"}
    assert [s.sid for s in BRIDGE.statements if s.effect == "deny"] == ["bridge-stash", "bridge-cockpit-only", "bridge-force-push"]
    seen = [m for s in BRIDGE.statements for m in s.match]
    assert len(seen) == len(set(seen))
    assert "shared-checkout commit" in BRIDGE.description and "case-insensitive" in BRIDGE.description


def test_the_bridge_check_bites_when_the_hook_drifts():
    class Drifted:
        STASH = __import__("re").compile(r"\bgit\s+stash\b")
        FORCE = __import__("re").compile(r"--force")
        ASK = []
    gaps = compiler.check_bridge_hook(BRIDGE, Drifted)
    assert len(gaps) == 1 and "STASH, COCKPIT, FORCE, ASK" in gaps[0]


def bridge_verdict(cmd, cwd="/tmp"):
    """What policy_hook.py itself says, run as the bridge runs it. 'none' = no decision."""
    run = subprocess.run([sys.executable, BRIDGE_HOOK], capture_output=True, text=True, timeout=30,
                         input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "cwd": cwd}))
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)["hookSpecificOutput"]["permissionDecision"] if run.stdout.strip() else "none"


BRIDGE_COMMANDS = [
    "git stash", "git -C /x stash push -m a", "git stash list", "git stash show -p",
    "git push origin feat/x", "git push --force origin x", "git push -f", "git push --force-with-lease",
    "gh pr create --fill", "gh release delete v1", "gh pr view 3",
    "rm -rf build", "rm -f a.txt", "rm a.txt",
    "git reset --hard HEAD", "git clean -fd", "git branch -D x", "git worktree remove ../wt",
    "docker compose down -v", "docker volume rm v", "docker system prune", "docker rm c", "docker ps",
    "docker compose up -d x", "docker compose build x", "systemctl --user restart nomad-x", "systemctl status x",
    "psql -c 'DROP TABLE a'", "psql -c 'delete from a'", "tailscale serve 80", "tailscale status",
    "npm publish", "twine upload dist/*", "kubectl apply -f x", "./deploy.sh", "git status", "ls -la",
    "cat ~/.config/nomad/cockpit-token", "curl -X POST localhost:8099/api/policies/guardrail-save",
    "curl -H 'X-Nomad-Cockpit: x' localhost:8090/policies/save", "curl localhost:8099/api/policies/save",
]


@pytest.mark.parametrize("cmd", BRIDGE_COMMANDS)
def test_bridge_command_verdict_matches_policy_hook(cmd):
    d = ev.decide(ev.Layers(guardrail=(BRIDGE,)), "command", cmd)
    assert ("none" if d.reason == "default-deny" else d.effect) == bridge_verdict(cmd)


def test_what_the_bridge_policy_cannot_hold_stays_in_the_hook():
    """The shared-checkout commit rule depends on the working directory: the hook denies it, the
    policy has no statement for it (and says so in its description)."""
    assert bridge_verdict("git commit -m x", cwd=os.path.expanduser("~/nomad")) == "deny"
    assert bridge_verdict("git commit --only a -m x", cwd=os.path.expanduser("~/nomad")) == "none"
    assert ev.decide(ev.Layers(guardrail=(BRIDGE,)), "command", "git commit -m x").reason == "default-deny"
