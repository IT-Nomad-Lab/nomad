"""Tests for tier_gate.py: red asks, force push denies, green and yellow pass."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tier_gate  # noqa: E402


def bash(cmd):
    v = tier_gate.judge({"tool_name": "Bash", "tool_input": {"command": cmd}})
    return v[0] if v else None


@pytest.mark.parametrize("cmd", [
    # green: local, reversible
    "git status", "git commit -m 'x'", "git -C ~/nomad-wt/a commit -am x",
    "git switch -c feat/x", "git worktree add ../wt master", "git branch -d old",
    "pytest -q", "rm -rf /tmp/build", "rm -rf ./node_modules", "rm -r src/__pycache__",
    "rm file.txt", "docker compose up -d --force-recreate litellm", "docker compose down",
    "gh pr list", "gh pr view 12", "gh api repos/x/y", "npm install", "echo push it",
    "systemctl --user restart nomad-relay", "grep -rn DELETE_ME src",
    # yellow: local merge into the default branch
    "git merge --no-ff feat/x", "git -C ~/nomad merge feat/x", "git pull --ff-only",
    "cd ~/nomad && git switch master && git merge feat/x",
])
def test_green_and_yellow_pass(cmd):
    assert bash(cmd) is None


@pytest.mark.parametrize("cmd", [
    "git push", "git push origin feat/x", "git -C ~/nomad push origin master",
    "git -c core.x=y push", "cd x && git push -u origin HEAD", "FOO=1 git push",
    "sudo git push", "gh pr create --fill", "gh pr merge 12 --squash",
    "gh release create v1", "gh repo delete x/y", "gh api -X POST repos/x/y/issues",
    "gh api --method=DELETE repos/x", "npm publish", "twine upload dist/*",
    "docker push me/img", "docker volume rm data", "docker compose down -v",
    "docker system prune -a", "rm -rf ~/nomad/old", "rm -rf /tmp/../home/x",
    "rm -fr build", "rm -r --  docs", 'rm -rf "$DIR"',
    "psql -c 'DROP TABLE users'", "psql -c 'delete from users'", "echo 'TRUNCATE t' | psql",
    "tailscale funnel 443", "cloudflared tunnel --url localhost:8099", "./v2/publish.sh",
    "terraform apply", "kubectl delete pod x", "crontab -r",
    "ssh deploy@prod-web-1 uptime", "docker -H ssh://production.box ps",
])
def test_red_asks(cmd):
    assert bash(cmd) == "ask"


@pytest.mark.parametrize("cmd", [
    "git push --force", "git push -f origin x", "git -C ~/nomad push --force-with-lease",
    "git push origin +master", "git push -uf origin x", "git push origin --delete feat/x",
    "git push origin :feat/x", "git push --mirror", "git status && git push --force",
])
def test_force_push_denied(cmd):
    assert bash(cmd) == "deny"


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Gmail__send_message", "mcp__claude_ai_Gmail__reply",
    "mcp__claude_ai_Slack__slack_send_message", "mcp__claude_ai_Google_Drive__share_file",
    "mcp__claude_ai_Google_Calendar__create_event",
])
def test_outbound_tools_ask(tool):
    assert tier_gate.judge({"tool_name": tool, "tool_input": {}})[0] == "ask"


@pytest.mark.parametrize("tool", [
    "mcp__claude_ai_Gmail__create_draft", "mcp__claude_ai_Slack__slack_send_message_draft",
    "mcp__nomad-pm__transition", "Read", "Edit",
])
def test_drafts_and_local_tools_pass(tool):
    assert tier_gate.judge({"tool_name": tool, "tool_input": {}}) is None


def test_unparseable_with_red_keyword_asks():
    assert bash("git push 'unterminated") == "ask"


def test_heredoc_body_is_not_parsed_as_shell():
    assert bash("python3 - <<'EOF'\ns = 'don't rm -rf / or push'\nEOF") is None
    assert bash("cat > x.py <<EOF\nimport os  # git push here is just text\nEOF\npytest") is None


def test_heredoc_sql_still_asks():
    assert bash("psql <<'EOF'\nDELETE FROM users;\nEOF") == "ask"


def test_command_after_heredoc_still_judged():
    assert bash("cat > f <<'EOF'\nhello\nEOF\ngit push") == "ask"


def test_newline_separates_commands():
    assert bash("echo hi\ngit push") == "ask"
    assert bash("git status\ngit push --force") == "deny"
    assert bash("git \\\n  push origin x") == "ask"


def test_unparseable_without_red_keyword_passes():
    assert bash("echo 'unterminated") is None


def run_hook(payload):
    return subprocess.run([sys.executable, os.path.join(HERE, "tier_gate.py")],
                          input=payload, capture_output=True, text=True, timeout=10)


def test_hook_emits_decision_json():
    r = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}))
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert r.returncode == 0
    assert out["permissionDecision"] == "ask"
    assert "red tier" in out["permissionDecisionReason"]


def test_hook_silent_on_green_and_bad_input():
    assert run_hook(json.dumps({"tool_name": "Bash",
                                "tool_input": {"command": "git merge x"}})).stdout == ""
    r = run_hook("not json")
    assert r.returncode == 0 and r.stdout == ""


# ---- the cockpit's guardrail save (#1396): the operator's alone ----------------------------------

@pytest.mark.parametrize("tool, args", [
    ("Bash", {"command": "cat ~/.config/nomad/cockpit-token"}),
    ("Bash", {"command": "curl -s -X POST localhost:8099/api/policies/guardrail-save -d @x.json"}),
    ("Bash", {"command": "curl -H 'X-Nomad-Cockpit: x' localhost:8090/policies/save"}),
    ("Read", {"file_path": "/home/x/.config/nomad/cockpit-token"}),
    ("Grep", {"pattern": "x", "path": "/home/x/.config/nomad/cockpit-token"}),
    ("Edit", {"file_path": "/home/x/.config/nomad/cockpit-token", "old_string": "a", "new_string": "b"}),
    ("WebFetch", {"url": "http://localhost:8099/api/policies/guardrail-save"}),
    ("mcp__fetch__post", {"url": "http://127.0.0.1:8090/policies/guardrail_save"}),
])
def test_the_cockpit_token_and_the_guardrail_save_are_denied(tool, args):
    assert tier_gate.judge({"tool_name": tool, "tool_input": args})[0] == "deny"


@pytest.mark.parametrize("tool, args", [
    ("Edit", {"file_path": "v2/static/policies.js", "old_string": "a",
              "new_string": "fetch('/api/policies/guardrail-save')"}),   # the cockpit's own code
    ("Write", {"file_path": "docs/x.md", "content": "the cockpit-token file"}),
    ("Read", {"file_path": "/home/x/.config/nomad/dispatch-token"}),
    ("Bash", {"command": "curl -s localhost:8099/api/policies/save"}),
])
def test_the_rest_of_the_policy_api_passes(tool, args):
    assert tier_gate.judge({"tool_name": tool, "tool_input": args}) is None
