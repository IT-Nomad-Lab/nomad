"""Tests for .claude/hooks/guard.py (#1022), run the way the harness runs it: the tool call as
JSON on stdin, against a throwaway CLAUDE_PROJECT_DIR whose secret_scan.py is a stub.

git's global options (`-C path`, `-c k=v`) used to hide the subcommand from every git pattern;
guard.py now matches each pattern against the command with them folded away as well.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
GUARD = os.path.join(REPO, ".claude", "hooks", "guard.py")

STUB_SCAN = """import os, sys
open(os.path.join(os.path.dirname(__file__), "scanned"), "w").write("yes")
if os.path.exists(os.path.join(os.path.dirname(__file__), "fail")):
    sys.stderr.write("a.txt:1 generic-api-key ab****yz")
    sys.exit(1)
"""


@pytest.fixture
def proj(tmp_path):
    hooks = tmp_path / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    (hooks / "secret_scan.py").write_text(STUB_SCAN)
    return tmp_path


def run(proj, payload):
    """(decision, reason) the guard gives, or (None, "") when it defers."""
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(proj)}
    p = subprocess.run([sys.executable, GUARD], input=stdin, capture_output=True, text=True,
                       env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None, ""
    out = json.loads(p.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


def bash(proj, cmd):
    return run(proj, {"tool_name": "Bash", "tool_input": {"command": cmd}})[0]


# ---- writes to the gate's own files ------------------------------------------------------------

@pytest.mark.parametrize("tool,key,path", [
    ("Write", "file_path", ".claude/hooks/guard.py"),
    ("Edit", "file_path", ".claude/hooks/secret_scan.py"),
    ("MultiEdit", "file_path", ".claude/hooks/new_hook.py"),
    ("Write", "file_path", ".claude/settings.json"),
    ("Edit", "file_path", ".githooks/pre-commit"),
    ("NotebookEdit", "notebook_path", ".gitleaks.toml"),
    ("Write", "file_path", "src/../.claude/hooks/guard.py"),
])
def test_writes_to_the_gate_are_denied(proj, tool, key, path):
    decision, reason = run(proj, {"tool_name": tool, "tool_input": {key: path}})
    assert decision == "deny" and "secret gate" in reason
    absolute = run(proj, {"tool_name": tool, "tool_input": {key: str(proj / path)}})
    assert absolute[0] == "deny"


def test_a_symlink_into_the_gate_is_denied(proj):
    (proj / "innocent").symlink_to(proj / ".claude" / "hooks")
    assert run(proj, {"tool_name": "Write",
                      "tool_input": {"file_path": "innocent/guard.py"}})[0] == "deny"


@pytest.mark.parametrize("path", ["src/app.py", ".claude/agents/dev.md", "/etc/hosts",
                                  "~/elsewhere/.claude/hooks/guard.py", "", None])
def test_other_writes_defer(proj, path):
    assert run(proj, {"tool_name": "Write", "tool_input": {"file_path": path}})[0] is None


def test_other_tools_defer_even_on_the_gate(proj):
    assert run(proj, {"tool_name": "Read",
                      "tool_input": {"file_path": ".claude/hooks/guard.py"}})[0] is None


# ---- bypassing or disarming the commit gate ----------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git commit --no-verify -m x", "git commit -n -m x", "git commit -am x -n",
    "git merge --no-verify feat/x", "git rebase --no-verify master",
    "git config core.hooksPath /dev/null", "git config --unset core.hooksPath",
    "git config core.hooksPath --unset",
    "chmod -x .githooks/pre-commit", "rm .githooks/pre-commit", "rm -f .claude/hooks/secret_scan.py",
    "sed -i 's/deny/allow/' .claude/hooks/guard.py", "perl -i -pe 's/x/y/' .gitleaks.toml",
    "echo pass | tee .claude/hooks/secret_scan.py", "echo '' > .githooks/pre-push",
    "cat x >> .claude/hooks/guard.py", "mv .claude/hooks/guard.py /tmp/g.py",
    "truncate -s0 .claude/hooks/secret_scan.py", "rm .gitleaks.toml",
    "git -C ~/nomad commit --no-verify -m x", "git -c user.name=x commit -n -m x",
    "git -c core.hooksPath=/dev/null commit -m x", "git -c core.hookspath= commit -m x",
    "git --config-env=core.hooksPath=EMPTY commit -m x",
    "git -C . config core.hooksPath /dev/null",
    "perl -pi -e 's/deny/allow/' .claude/hooks/guard.py", "sed -ni 's/x/y/' .gitleaks.toml",
    "sed --in-place 's/x/y/' .githooks/pre-commit",
])
def test_commit_gate_bypass_is_denied(proj, cmd):
    decision, reason = run(proj, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert decision == "deny" and "pre-commit secret gate" in reason


@pytest.mark.parametrize("cmd", ["git config core.hooksPath", "git config --get core.hooksPath",
                                 "cat .claude/hooks/guard.py", "python3 .claude/hooks/secret_scan.py",
                                 "git -C ~/nomad config core.hooksPath",
                                 "sed -n 1,20p .claude/hooks/guard.py",
                                 "perl -ne 'print if /deny/' .claude/hooks/guard.py",
                                 "git -C ~/nomad log --oneline", "git -c color.ui=never status"])
def test_reading_or_running_the_gate_is_allowed(proj, cmd):
    """Verifying the gate is on must stay possible, or the guard gets switched off."""
    assert bash(proj, cmd) is None
    assert not (proj / ".claude" / "hooks" / "scanned").exists()


# ---- the commit secret scan (fail-closed) ------------------------------------------------------

def test_a_commit_runs_the_scan_and_passes(proj):
    assert bash(proj, "git commit -m 'feat: x'") is None
    assert (proj / ".claude" / "hooks" / "scanned").exists()


def test_a_failed_scan_denies_the_commit(proj):
    (proj / ".claude" / "hooks" / "fail").write_text("")
    decision, reason = run(proj, {"tool_name": "Bash", "tool_input": {"command": "git commit -am x"}})
    assert decision == "deny" and "Secret scan FAILED" in reason and "ab****yz" in reason


def test_a_missing_scanner_denies_the_commit(proj):
    (proj / ".claude" / "hooks" / "secret_scan.py").unlink()
    decision, reason = run(proj, {"tool_name": "Bash", "tool_input": {"command": "git commit -m x"}})
    assert decision == "deny" and "fail-closed" in reason


def test_a_dry_run_commit_is_not_scanned(proj):
    assert bash(proj, "git commit --dry-run -m x") is None
    assert not (proj / ".claude" / "hooks" / "scanned").exists()


@pytest.mark.parametrize("cmd", ["git --no-pager commit -m x", "cd sub && git commit -m x",
                                 "git -C /home/x/repo commit -m x", "git -c user.name=x commit -m x",
                                 "git -C 'my repo' commit -m x", "git --git-dir=.git commit -m x"])
def test_commit_variants_are_scanned(proj, cmd):
    bash(proj, cmd)
    assert (proj / ".claude" / "hooks" / "scanned").exists()


# ---- secrets -----------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "cat .env", "head -5 ~/nomad/.env", "tail .env.local", "less config/.env",
    "echo $OPENAI_API_KEY", "echo ${GITHUB_TOKEN}", "echo $DB_PASSWORD",
    "git add .env", "printenv | grep TOKEN", "ssh -i ~/.ssh/id_rsa host", "cat server.pem",
])
def test_secret_exposure_is_denied(proj, cmd):
    decision, reason = run(proj, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert decision == "deny" and "expose a secret" in reason


@pytest.mark.parametrize("cmd", ["cat .envrc", "echo $HOME", "grep -c KEY README.md",
                                 "git add src/env.py"])
def test_near_misses_are_not_secrets(proj, cmd):
    assert bash(proj, cmd) is None


# ---- irreversible / outward-facing -------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git push", "git push origin feat/x", "git push --force origin x", "git rebase -i main",
    "git reset --hard origin/master", "gh pr merge 12", "gh release create v1", "npm publish",
    "twine upload dist/*", "docker push img", "docker compose down -v", "docker volume rm data",
    "terraform apply", "kubectl delete pod x", "cloudflared tunnel run", "tailscale funnel 443",
    "v2/publish.sh", "n8n update:workflow --id=1 --active=true", "rm -rf build",
    "rm -rf ~/nomad-wt/ap-1", "psql -c 'DROP TABLE users'", "psql -c 'truncate jobs'",
    "psql -c 'delete from jobs'", "python3 send_email.py",
    "git -C ~/nomad push", "git -C ~/nomad push origin master", "git -c http.extraHeader=x push",
    "git --git-dir=/r/.git --work-tree=/r push", "git -C ~/nomad reset --hard origin/master",
])
def test_irreversible_commands_ask(proj, cmd):
    decision, reason = run(proj, {"tool_name": "Bash", "tool_input": {"command": cmd}})
    assert decision == "ask" and "human gate" in reason


@pytest.mark.parametrize("cmd", [
    "git merge feat/x", "git merge --ff-only feat/x", "git -C ~/nomad merge feat/x",   # #1005
    "git status", "git log --oneline", "rm -rf /tmp/build", "rm -rf ./tmp/x",
    "rm -rf /var/tmp/x", "docker compose down", "docker compose up -d", "ls -la",
])
def test_routine_commands_defer(proj, cmd):
    assert bash(proj, cmd) is None


def test_a_secret_wins_over_irreversible(proj):
    assert bash(proj, "cat .env && git push") == "deny"


# ---- input -------------------------------------------------------------------------------------

@pytest.mark.parametrize("stdin", ["not json", "", "{"])
def test_unreadable_input_asks(proj, stdin):
    assert run(proj, stdin)[0] == "ask"


@pytest.mark.parametrize("payload", [{}, {"tool_name": "Bash"}, {"tool_name": "Bash", "tool_input": None},
                                     {"tool_name": "Bash", "tool_input": {"command": None}},
                                     {"tool_name": "Glob", "tool_input": {"pattern": "**"}}])
def test_empty_or_other_calls_defer(proj, payload):
    assert run(proj, payload)[0] is None


# ---- the cockpit's guardrail save (#1396) ------------------------------------------------------

@pytest.mark.parametrize("cmd", ["cat ~/.config/nomad/cockpit-token",
                                 "curl -X POST localhost:8099/api/policies/guardrail-save -d @x",
                                 "curl -H 'X-Nomad-Cockpit: x' localhost:8090/policies/save"])
def test_the_cockpit_token_and_the_guardrail_save_are_denied(proj, cmd):
    assert bash(proj, cmd) == "deny"


def test_a_write_to_the_cockpit_token_is_denied(proj):
    got = run(proj, {"tool_name": "Write", "tool_input": {"file_path": "/home/x/.config/nomad/cockpit-token"}})
    assert got[0] == "deny"


def test_the_cockpit_code_may_name_the_route(proj):
    got = run(proj, {"tool_name": "Edit", "tool_input": {"file_path": "v2/static/p.js",
                                                          "new_string": "/api/policies/guardrail-save"}})
    assert got[0] is None
