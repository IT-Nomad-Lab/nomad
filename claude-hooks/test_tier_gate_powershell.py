"""tier_gate.py on PowerShell commands (Windows sessions): the same red tier, PowerShell syntax."""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import tier_gate  # noqa: E402


def ps(cmd):
    v = tier_gate.judge({"tool_name": "PowerShell", "tool_input": {"command": cmd}})
    return v[0] if v else None


@pytest.mark.parametrize("cmd", [
    # G4 and friends: green
    "git status; Get-ChildItem", "git status", "git commit -m 'fix #12'", "Get-Content README.md",
    "git -C C:\\Users\\josia\\Projects\\bootalert log --oneline -5",
    "& 'C:\\Program Files\\Git\\cmd\\git.exe' status",
    "npm install", "pytest -q", "docker compose up -d", "gh pr view 3",
    "Remove-Item file.txt", "Remove-Item -Force .\\out.log", "del .\\x.tmp",
    "Remove-Item -Recurse -Force $env:TEMP\\build", "Remove-Item -Recurse 'C:\\Windows\\Temp\\x'",
    "rm -r C:\\Users\\josia\\AppData\\Local\\Temp\\cc-x", "Remove-Item -Recurse .\\node_modules",
    "Remove-Item -Recurse -Path $env:TMP\\a, $env:TEMP\\b",
    "Invoke-RestMethod -Uri https://ntfy.sh/x -Method Post -Body hi",
    "irm https://api.github.com/repos/x/y", "Invoke-WebRequest https://example.com -Method Post",
    "cmd /c dir", "cmd /c echo hi",
    "bash -c 'git status'", "wsl -d Ubuntu-24.04 -e git status", "wsl ls ~",
    "pwsh -c 'Get-Process'", "Start-Process notepad", "Get-Content $PROFILE",
    "git merge --no-ff feat/x", "Write-Output 'git push is red'",
    "# git push later\ngit status",
    "$out = git status", "Set-Location C:\\x; git log",
])
def test_green_passes(cmd):
    assert ps(cmd) is None


@pytest.mark.parametrize("cmd", [
    # G3 and the PowerShell red forms
    "git push origin main", "git -C C:\\repo push", "git.exe push", "& git push",
    "& 'C:\\Program Files\\Git\\cmd\\git.exe' push origin x", "$r = git push", "$r=git push",
    "Get-Date; git push", "git status && git push", "if ($ok) { git push }",
    "git `\n  push origin x", "gh pr create --fill", "gh release create v1",
    "docker push me/img", "npm publish", "docker compose down -v",
    "Remove-Item -Recurse -Force C:\\Users\\josia\\Projects\\x", "Remove-Item -r .\\src",
    "rm -r -fo C:\\data", "rd -Recurse D:\\old", "rmdir -rec stuff", "ri -Recurse x",
    "Remove-Item -LiteralPath C:\\x -Recurse", "Remove-Item -Path:C:\\x -Recurse",
    "Get-ChildItem C:\\x | Remove-Item -Recurse", "Remove-Item -Recurse $env:TEMP\\..\\x",
    "Remove-Item -Recurse $env:TEMP\\a, C:\\b", "rm -rf C:\\x",
    "cmd /c rd /s /q C:\\Users\\josia\\x", "cmd /c \"rmdir /S C:\\x\"", "cmd /c del /s *.log",
    "cmd /c git push",
    "Invoke-RestMethod -Uri https://api.github.com/repos/x/y/issues -Method Post -Body $b",
    "irm -Method:Delete https://api.github.com/repos/x/y",
    "iwr https://hooks.slack.com/services/x -Method POST -Body '{}'",
    "Invoke-RestMethod https://api.prod.example.com/x -Method Put",
    "Set-ExecutionPolicy Unrestricted", "Set-ExecutionPolicy -Scope CurrentUser Bypass",
    "Add-Content $PROFILE 'Set-Alias x y'", "Set-Content -Path $PROFILE -Value ''",
    "'alias' >> $PROFILE", "Start-Process pwsh -Verb RunAs", "saps cmd -Verb runas",
    "bash -c 'git push'", "bash -lc \"rm -rf ~/x\"", "wsl git push", "wsl -e git push",
    "wsl -d Ubuntu-24.04 -- rm -rf ~/nomad", "wsl -e bash -c 'git push'",
    "wsl bash -lc 'git push origin x'", "wsl -d Ubuntu-24.04 -e sh -c 'gh pr create'", "wsl --unregister Ubuntu-24.04",
    "powershell -Command 'git push'", "pwsh -c \"Remove-Item -Recurse C:\\x\"",
    "powershell -EncodedCommand ZwBpAHQA", "psql -c 'DROP TABLE users'",
    "ssh deploy@prod-web-1 uptime", "tailscale funnel 443",
])
def test_red_asks(cmd):
    assert ps(cmd) == "ask", cmd


@pytest.mark.parametrize("cmd", [
    # G5: force push denied, in every wrapper
    "git push --force origin x", "git push -f", "git push origin +main",
    "git status; git push --force-with-lease", "cmd /c git push -f",
    "wsl git push --force", "wsl -e bash -c 'git push -f'", "bash -c 'git push --mirror'", "pwsh -c 'git push -f'",
    "git push origin --delete feat/x",
])
def test_force_push_denied(cmd):
    assert ps(cmd) == "deny", cmd


@pytest.mark.parametrize("cmd", [
    "Get-Content ~/.config/nomad/cockpit-token",
    "irm http://127.0.0.1:8090/policies/guardrail-save -Method Post",
    "wsl cat ~/.config/nomad/cockpit_token",
])
def test_cockpit_only_denied(cmd):
    assert ps(cmd) == "deny", cmd


def test_unparseable_with_red_keyword_asks():
    assert ps("git push 'unterminated") == "ask"
    assert ps("Remove-Item \"C:\\x") == "ask"


def test_unparseable_without_red_keyword_passes():
    assert ps("Write-Output 'unterminated") is None


def test_backslash_is_literal():
    # In bash a backslash escapes; in PowerShell it's a path separator and must survive.
    assert tier_gate.ps_segments("git -C C:\\a\\b log") == [["git", "-C", "C:\\a\\b", "log"]]


def test_deep_nesting_asks():
    assert tier_gate.judge_powershell("git status", depth=5) == (
        "ask", "nests shells too deep to judge")


def test_bash_verdicts_unchanged():
    """The Bash path doesn't go through any PowerShell rule."""
    b = lambda c: tier_gate.judge({"tool_name": "Bash", "tool_input": {"command": c}})
    assert b("Remove-Item -Recurse C:\\x") is None
    assert b("git push")[0] == "ask"


def test_hook_output_for_powershell():
    r = subprocess.run([sys.executable, os.path.join(HERE, "tier_gate.py")],
                       input=json.dumps({"tool_name": "PowerShell",
                                         "tool_input": {"command": "git push origin main"}}),
                       capture_output=True, text=True, timeout=10)
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert r.returncode == 0 and out["permissionDecision"] == "ask"
