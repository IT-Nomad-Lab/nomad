"""Nothing reaches paid usage on its own (#960). The operator's rule: Claude runs on the Max plan
only, and the paid `gpt` key stays configured but is never a fallback.

- Every place that launches the `claude` CLI blanks ANTHROPIC_API_KEY, so a key in the
  environment can never switch a run to API billing.
- No LiteLLM fallback chain names `gpt`; the `gpt` model entry itself stays.

Run: uv run --no-project --with pytest --with pyyaml python -m pytest autopilot -q
"""
import os
import re
import subprocess
import sys

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

# Every Claude launcher in the repo, by path. A new one must be added here (the scan below fails
# until it is) and must blank the key.
LAUNCHERS = {
    "autopilot/worker.py",
    "autopilot/criteria.py",
    "dispatcher/dispatch.py",
    "dispatcher/termd.py",
    "dispatcher/sessions.py",
    "claude-bridge/bridge.py",
    # The OWUI agent bridge (#1405): Run._env blanks the key and drops ANTHROPIC_AUTH_TOKEN; the
    # local backend's env (LiteLLM base URL and key) blanks it again; the unit sets it empty; and a
    # run whose init event reports an apiKeySource other than the login is refused.
    "agent-bridge/runner.py",
}
# what launching the CLI looks like: an argv list starting with the claude path, or an exec of it
LAUNCH = re.compile(r"\[\s*(?:self\.)?claude\s*,|\[\s*CLAUDE\s*,|execvp\(\s*TERM_CMD")
BLANK = re.compile(r"""["']ANTHROPIC_API_KEY["']\s*(?::|\]\s*=)\s*["']["']""")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "data", "models"}


def _sources():
    for top, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for f in files:
            if f.endswith(".py") and not f.startswith("test_") and f != "conftest.py":
                path = os.path.join(top, f)
                yield os.path.relpath(path, ROOT), path


def test_every_claude_launcher_is_known():
    found = set()
    for rel, path in _sources():
        try:
            src = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        if LAUNCH.search(src):
            found.add(rel)
    assert found <= LAUNCHERS, f"new Claude launcher(s), add them to LAUNCHERS: {found - LAUNCHERS}"


def test_every_claude_launcher_blanks_the_api_key():
    for rel in sorted(LAUNCHERS):
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        assert BLANK.search(src), f"{rel} launches claude without ANTHROPIC_API_KEY=''"


def test_the_dispatcher_run_has_an_empty_api_key(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "dispatcher"))
    os.environ.setdefault("NOMAD_DISPATCH_TOKEN", "test-token")
    import dispatch
    seen = {}

    def run(cmd, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, '{"result": "ok", "total_cost_usd": 0}', "")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-blanked")
    monkeypatch.setattr(dispatch.subprocess, "run", run)
    dispatch.run_claude("/tmp", "do it", "build")
    assert seen["env"]["ANTHROPIC_API_KEY"] == ""


def test_the_criteria_drafter_has_an_empty_api_key(tmp_path, monkeypatch):
    import criteria
    import tasks as tk
    seen = {}

    class Proc:
        pid = 1

        def poll(self):
            return None

    def popen(cmd, **kw):
        seen.update(kw)
        return Proc()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-blanked")
    d = criteria.CriteriaDrafter(pm=None, claude="claude", state_dir=str(tmp_path), popen=popen)
    task = tk.Task(id=5, subject="x", type="Task", status="New", lane="dev")
    d.launch("proj", task)
    assert seen["env"]["ANTHROPIC_API_KEY"] == ""


BRIDGE_ENV = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import backends, runner
os.environ["ANTHROPIC_API_KEY"] = "sk-should-be-blanked"
os.environ["ANTHROPIC_AUTH_TOKEN"] = "should-be-dropped"
run = runner.Run("chat", store=None, gates=None)
claude = run._env()
be = backends.Backends.__new__(backends.Backends)
be.litellm_key = lambda: "litellm-key"
run.env_extra = be.env("local")
local = run._env()
print(json.dumps({k: [e.get("ANTHROPIC_API_KEY"), e.get("ANTHROPIC_AUTH_TOKEN")]
                  for k, e in (("claude", claude), ("local", local))}))
"""


def test_the_agent_bridge_run_has_an_empty_api_key():
    """In a subprocess: agent-bridge's module names (projects, backends) would shadow autopilot's."""
    got = subprocess.run([sys.executable, "-c", BRIDGE_ENV, os.path.join(ROOT, "agent-bridge")],
                         capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert got.returncode == 0, got.stderr
    import json
    env = json.loads(got.stdout.strip().splitlines()[-1])
    assert env["claude"] == ["", None]                  # Max login only
    assert env["local"] == ["", "litellm-key"]          # LiteLLM's own key, never the Anthropic one


def _config():
    with open(os.path.join(ROOT, "litellm", "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_no_litellm_fallback_chain_names_gpt():
    chains = _config()["router_settings"]["fallbacks"]
    assert chains, "no fallbacks section"
    names = {k for c in chains for k in c}
    assert "longdoc" in names
    for c in chains:
        for alias, targets in c.items():
            assert "gpt" not in targets, f"fallback chain {alias!r} names gpt: {targets}"


def test_the_gpt_model_entry_is_still_defined():
    [gpt] = [m for m in _config()["model_list"] if m["model_name"] == "gpt"]
    assert gpt["litellm_params"]["model"] == "openai/gpt-4o"
