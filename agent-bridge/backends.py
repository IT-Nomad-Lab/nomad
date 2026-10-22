"""Which agent runs a turn: Claude Code on the Max login, Codex CLI, or a local model.

All three produce the same bridge events (events.Renderer), so the chat looks the same; only the
label says who ran it. `claude` is first. `codex` (ChatGPT plan, separate quota) and `local`
(the Claude Code harness pointed at LiteLLM's `longdoc-local` alias, i.e. llama.cpp on :8240) take over
when `claude` is out of quota, logged out or missing, *before any tool ran* (a half-done turn is
never silently re-run elsewhere).

Safety differs by backend and is said in the chat, not hidden:
  claude  permission prompts, policy hook, everything the operator's settings allow
  codex   no prompts: `sandbox_mode=workspace-write` (project folder + /tmp only, no network,
          `.git` read-only, so it edits and runs but cannot commit or push)
  local   same harness, prompts and hook as claude
"""
import glob
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
import time
import urllib.request

ORDER = ("claude", "codex", "local")
FORCE = re.compile(r"^\s*!(claude|codex|local)\b[ \t]*", re.I)
COOLDOWN_S = int(os.environ.get("AGENT_BRIDGE_COOLDOWN_S", "1800"))
MCP_SYNC = os.environ.get("AGENT_BRIDGE_MCP_SYNC", os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "mcp-sync.py"))
GPU_BUSY_RC = 75   # EX_TEMPFAIL from llama/qwen38.sh when /tmp/nomad-gpu.lock is held
LLAMA_SCRIPT = os.environ.get("AGENT_BRIDGE_LLAMA_SCRIPT", os.path.expanduser("~/nomad/llama/qwen38.sh"))
LOCAL_IDLE_S = int(os.environ.get("AGENT_BRIDGE_LOCAL_IDLE_S", "900"))
LOCAL_HEALTH = os.environ.get("AGENT_BRIDGE_LOCAL_HEALTH", "http://127.0.0.1:8240/health")
LITELLM_URL = os.environ.get("AGENT_BRIDGE_LITELLM", "http://127.0.0.1:4000")
# no fallback chain on purpose (#1504): `code` falls back to codex and fast, so a run labelled
# local could reach the cloud. autopilot/test_hybrid.py checks the live config keeps it local.
LOCAL_MODEL = os.environ.get("AGENT_BRIDGE_LOCAL_MODEL", "longdoc-local")
LOCAL_MODEL_VARS = ("ANTHROPIC_MODEL", "ANTHROPIC_SMALL_FAST_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                    "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL")
MCP_GRANTS = os.environ.get("AGENT_BRIDGE_MCP_GRANTS", os.path.expanduser("~/.config/nomad/mcp-grants.json"))
LOCAL_MAX_SERVERS = int(os.environ.get("AGENT_BRIDGE_LOCAL_MAX_SERVERS", "8"))


def _load_json(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}
# what the run line shows for the LiteLLM alias above (the alias names a route, not the weights)
LOCAL_MODEL_NAME = os.environ.get("AGENT_BRIDGE_LOCAL_MODEL_NAME", "qwen3.8-27b")
LITELLM_KEY_FILE = os.environ.get("AGENT_BRIDGE_LITELLM_KEY_FILE") or os.path.expanduser(
    "~/.config/nomad/litellm-key")
CODEX = shutil.which("codex") or "/home/linuxbrew/.linuxbrew/bin/codex"
WORKTREES = os.path.expanduser("~/nomad-wt")

# A failure that means "this vendor can't serve you now" (as opposed to a task that went wrong).
UNAVAILABLE = re.compile(
    r"(?i)usage limit|rate.?limit|limit (reached|exceeded)|quota|credit balance|overloaded|"
    r"\b429\b|not logged in|please run /login|/login|invalid api key|authentication|unauthori[sz]ed|"
    r"session limit|at capacity|could not start|not found|no such file")

CODEX_PREAMBLE = (
    "You are NOMAD's agent running as Codex CLI for the operator inside an Open WebUI chat, as a "
    "stand-in while Claude Code is unavailable. Follow docs/SDLC.md and ~/.codex/AGENTS.md. Your "
    "sandbox lets you edit files in this project and run commands, but not commit, push or reach "
    "the network: if a step needs that, stop and say exactly which command the operator or Claude "
    "Code must run. Report only what a command's output confirmed. Don't write a run footer."
)


class Backends:
    def __init__(self, claude_path: str, clock=time.time):
        self.claude_path = claude_path
        self.clock = clock
        self.cool: dict[str, tuple[float, str]] = {}      # backend -> (until, why)

    # --- availability
    def litellm_key(self) -> str:
        k = os.environ.get("AGENT_BRIDGE_LITELLM_KEY", "")
        if not k:
            try:
                with open(LITELLM_KEY_FILE) as f:
                    k = f.read().strip()
            except OSError:
                pass
        return k

    def available(self, name: str, ignore_cooldown: bool = False) -> tuple[bool, str]:
        until, why = self.cool.get(name, (0, ""))
        if until > self.clock() and not ignore_cooldown:
            return False, f"cooling down for {int((until - self.clock()) / 60) + 1} min ({why[:80]})"
        if name == "claude":
            ok = os.path.exists(self.claude_path)
            return ok, "" if ok else f"{self.claude_path} not found"
        if name == "codex":
            ok = bool(CODEX and os.path.exists(CODEX))
            return ok, "" if ok else "codex CLI not found"
        if name == "local":
            if not self.litellm_key():
                return False, f"no LiteLLM key in {LITELLM_KEY_FILE}"
            if self.local_up():
                return True, ""
            if os.path.exists(LLAMA_SCRIPT):
                return True, "starts on demand"
            return False, f"llama-server :8240 is down and {LLAMA_SCRIPT} is missing"
        return False, "unknown backend"

    @staticmethod
    def local_up() -> bool:
        try:
            urllib.request.urlopen(LOCAL_HEALTH, timeout=2).read()
            return True
        except Exception:
            return False

    def ensure_local(self) -> tuple[bool, str]:
        """Start the local model when it's down (it unloads `private` while it runs) and stop it
        again after LOCAL_IDLE_S without a local run, but only if the bridge started it."""
        if not self.local_up():
            try:
                r = subprocess.run([LLAMA_SCRIPT, "start"], capture_output=True, text=True, timeout=300,
                                   env={**os.environ, "QWEN38_MTP": os.environ.get("QWEN38_MTP", "0")})
            except Exception as e:
                return False, f"could not start the local model: {e.__class__.__name__}: {str(e)[:120]}"
            if r.returncode == GPU_BUSY_RC:     # llama/gpu-lock.sh: the GPU is taken, nothing was changed
                return False, "the GPU is busy (another workload holds admission); try again shortly: " \
                    + (r.stdout + r.stderr).strip()[-120:]
            if r.returncode != 0 or not self.local_up():
                return False, "the local model did not start: " + (r.stdout + r.stderr).strip()[-200:]
            self.started_local = True
        self.touch_local()
        return True, ""

    def touch_local(self):
        self.last_local = self.clock()
        if getattr(self, "started_local", False) and not getattr(self, "_idle_thread", None):
            self._idle_thread = threading.Thread(target=self._idle_stop, daemon=True)
            self._idle_thread.start()

    def _idle_stop(self):
        while True:
            time.sleep(min(60, max(1, LOCAL_IDLE_S // 3)))
            if self.clock() - self.last_local >= LOCAL_IDLE_S:
                try:
                    r = subprocess.run([LLAMA_SCRIPT, "stop"], capture_output=True, timeout=60)
                except BaseException:
                    self.started_local = False
                    self._idle_thread = None
                    raise
                if r.returncode == GPU_BUSY_RC:  # GPU busy: the model is still up, retry on the next tick
                    continue
                self.started_local = False
                self._idle_thread = None
                return

    def mark_unavailable(self, name: str, why: str):
        self.cool[name] = (self.clock() + COOLDOWN_S, why)

    def order(self, forced: str = "") -> list[str]:
        if forced:                      # the operator's choice beats a cooldown, not a missing binary
            return [forced] if self.available(forced, ignore_cooldown=True)[0] else []
        return [n for n in ORDER if self.available(n)[0]]

    def report(self) -> str:
        rows = []
        for n in ORDER:
            ok, why = self.available(n)
            rows.append(f"- **{self.label(n)}**: " + ("available" if ok else f"unavailable — {why}"))
        return "\n".join(rows)

    # --- how each is labelled and run
    @staticmethod
    def label(name: str, model: str = "") -> str:
        base = {"claude": "Claude Code · Max plan",
                "codex": "Codex CLI · ChatGPT plan · sandboxed, no approval prompts",
                "local": "Local model · Claude Code harness on LiteLLM"}[name]
        if name == "local" and model in ("", LOCAL_MODEL):
            model = LOCAL_MODEL_NAME
        return f"{base} · {model}" if model else base

    def env(self, name: str) -> dict:
        if name == "local":
            # tool search keeps the tool schemas out of the prompt: all MCP servers at once are
            # ~420k tokens, deferred ~30k, and the local window is 64k
            # every model slot, background calls included, goes to the same local alias
            models = {k: LOCAL_MODEL for k in LOCAL_MODEL_VARS}
            return {"ANTHROPIC_BASE_URL": LITELLM_URL, "ANTHROPIC_AUTH_TOKEN": self.litellm_key(),
                    "ANTHROPIC_API_KEY": "", "ENABLE_TOOL_SEARCH": "true", **models}
        return {}

    # ---- one MCP registry for all backends (scripts/mcp-sync.py, PRD mcp-all-backends)
    def _mcp(self):
        if getattr(self, "_mcp_mod", None) is None:
            spec = importlib.util.spec_from_file_location("mcp_sync", MCP_SYNC)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            self._mcp_mod = mod
        return self._mcp_mod

    def mcp_sync(self) -> str:
        """Bring Codex's config and the local MCP file up to date when a source changed. Cheap
        (mtime check) so it runs before every run. Returns an error text, "" when fine."""
        if MCP_SYNC == "off":                      # tests: never touch the operator's real files
            return ""
        try:
            m = self._mcp()
            stamp = tuple(os.path.getmtime(p) if os.path.exists(p) else 0
                          for p in (m.PROJECT_MCP, m.USER_CLAUDE, m.BACKENDS))
            if stamp != getattr(self, "_mcp_stamp", None) or not os.path.exists(m.LOCAL_JSON):
                m.sync()
                self._mcp_stamp = stamp
            return ""
        except Exception as e:
            return f"{e.__class__.__name__}: {str(e)[:160]}"

    def mcp_config(self, project: str = "") -> str:
        """Path of the local run's MCP file, or "" when the sync didn't produce one.

        With ~/.config/nomad/mcp-grants.json ({"default": [...], "projects": {name: [...]}}) the
        file holds only the project's servers (default tier plus its grants): the local window
        (65k) cannot carry all of them. Without that file, every server, as before."""
        if MCP_SYNC == "off":
            return ""
        try:
            p = self._mcp().LOCAL_JSON
            if not os.path.exists(p):
                return ""
            grants = _load_json(MCP_GRANTS)
            if not grants:
                return p
            want = set(grants.get("default") or []) | set((grants.get("projects") or {}).get(project or "", []))
            all_ = _load_json(p).get("mcpServers", {})
            sub = {n: s for n, s in all_.items() if n in want}
            if len(sub) > LOCAL_MAX_SERVERS:
                raise ValueError(f"{project or 'general'} needs {len(sub)} MCP servers; the local model "
                                 f"holds {LOCAL_MAX_SERVERS}. Trim its grants in {MCP_GRANTS}.")
            out = os.path.join(os.path.dirname(p), "mcp-local-" + re.sub(r"[^A-Za-z0-9_-]", "_", project or "general") + ".json")
            with open(out, "w") as f:
                json.dump({"mcpServers": sub}, f)
            os.chmod(out, 0o600)
            return out
        except ValueError:
            raise
        except Exception:
            return ""

    def mcp_report(self) -> str:
        if MCP_SYNC == "off":
            return "MCP servers: sync disabled"
        err = self.mcp_sync()
        if err:
            return f"MCP servers: sync failed ({err})"
        m = self._mcp()
        names = sorted(m.registry())
        out = [f"- Claude Code: {len(names)} servers (and the claude.ai connectors)"]
        for b in ("codex", "local"):
            w = m.withheld(b)
            out.append(f"- {b}: {len(names) - len(w & set(names))} servers"
                       + (f" (withheld: {', '.join(sorted(w))})" if w else ""))
        return "MCP servers:\n" + "\n".join(out)

    @staticmethod
    def codex_model(thread_id: str) -> str:
        """The model Codex really used, from its own session log (no model flag is passed)."""
        if not thread_id:
            return ""
        for path in glob.glob(os.path.expanduser(f"~/.codex/sessions/*/*/*/rollout-*{thread_id}.jsonl")):
            try:
                with open(path) as f:
                    for line in f:
                        m = re.search(r'"model"\s*:\s*"([^"]+)"', line)
                        if m:
                            return m.group(1)
            except OSError:
                pass
        return ""

    def codex_cmd(self, resume_id: str = "") -> list[str]:
        return [CODEX, "exec", *(["resume", resume_id] if resume_id else []), "--json",
                "--skip-git-repo-check", "-c", 'sandbox_mode="workspace-write"',
                "-c", f"sandbox_workspace_write.writable_roots=[{json.dumps(WORKTREES)}]", "-"]


def classify(text: str, tools_ran: int) -> bool:
    """True when this failure is 'the vendor can't serve you' and no tool had run: safe to hand over."""
    return tools_ran == 0 and bool(UNAVAILABLE.search(text or ""))


class CodexTranslator:
    """`codex exec --json` items → the Claude stream-json shapes events.Renderer already renders."""

    def __init__(self):
        self.thread_id = ""
        self.error = ""
        self.changed = 0
        self.turn_done = False
        self._started: set[str] = set()

    @staticmethod
    def _cmd(raw: str) -> str:
        try:
            parts = shlex.split(raw)
            if len(parts) >= 3 and parts[0].endswith("bash") and parts[1] in ("-c", "-lc"):
                return parts[2]
        except ValueError:
            pass
        return raw

    @staticmethod
    def _use(i, name, tin):
        return {"type": "assistant", "message": {"id": f"cx-{i}", "content": [
            {"type": "tool_use", "id": i, "name": name, "input": tin}]}}

    @staticmethod
    def _res(i, text, bad):
        return {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": i, "is_error": bad, "content": text}]}}

    def feed(self, e: dict) -> list[dict]:
        t = e.get("type")
        it = e.get("item") or {}
        out: list[dict] = []
        if t == "thread.started":
            self.thread_id = e.get("thread_id") or ""
            out.append({"type": "system", "subtype": "init", "session_id": self.thread_id,
                        "apiKeySource": "none"})
        elif t in ("error", "turn.failed"):
            msg = e.get("message") or (e.get("error") or {}).get("message") or json.dumps(e)[:300]
            self.error = msg
        elif t == "turn.completed":
            self.turn_done = True
        elif t in ("item.started", "item.completed"):
            kind, i = it.get("type"), it.get("id") or ""
            if kind == "agent_message" and t == "item.completed" and it.get("text"):
                out.append({"type": "assistant", "message": {"id": f"cx-{i}", "content": [
                    {"type": "text", "text": it["text"]}]}})
            elif kind == "command_execution":
                if i not in self._started:
                    self._started.add(i)
                    out.append(self._use(i, "Bash", {"command": self._cmd(it.get("command") or "")}))
                if t == "item.completed":
                    out.append(self._res(i, it.get("aggregated_output") or "",
                                         bool(it.get("exit_code")) or it.get("status") == "failed"))
            elif kind == "file_change" and t == "item.completed":
                for n, ch in enumerate(it.get("changes") or [{}]):
                    cid = f"{i}-{n}"
                    name = "Write" if ch.get("kind") == "add" else "Edit"
                    out += [self._use(cid, name, {"file_path": ch.get("path") or ""}),
                            self._res(cid, "", it.get("status") == "failed")]
                    self.changed += 1
            elif kind == "mcp_tool_call":
                name = f"mcp__{it.get('server')}__{it.get('tool')}"
                if i not in self._started:
                    self._started.add(i)
                    out.append(self._use(i, name, it.get("arguments") or {}))
                if t == "item.completed":
                    res = it.get("result") or it.get("error") or ""
                    out.append(self._res(i, res if isinstance(res, str) else json.dumps(res)[:700],
                                         it.get("status") == "failed"))
        return out
