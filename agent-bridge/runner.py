"""One Claude Code turn as a `Run`: the process, its event log, and its pending requests.

The process is `claude -p --input-format stream-json --output-format stream-json
--permission-prompt-tool stdio`, started in the chat's project directory with `--resume`
on the chat's session id. ANTHROPIC_API_KEY is blanked and the init event's apiKeySource is
checked, so a run can only use the local Claude Code (Max) login.

The reader thread owns the process. It turns events into bridge events (events.Renderer), answers
`can_use_tool` control requests only from a decision the operator made (gates.Gates), and closes
the run on the `result` event. Clients (the HTTP stream) only read the event log from a cursor,
so a dropped connection doesn't lose or stop the run.
"""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid

import backends
import events
from gates import Gates, Pending

CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
HERE = os.path.dirname(os.path.abspath(__file__))
HOOK = os.path.join(HERE, "policy_hook.py")
OK_KEY_SOURCES = ("none", "", "oauth", "claude.ai", "login", "subscription")
MODELS = {"sonnet": "sonnet", "opus": "opus", "haiku": "haiku"}

AGENT_SYSTEM = (
    "You are NOMAD's agent, running Claude Code for the operator inside an Open WebUI chat. You "
    "have the full toolset, subagents and the configured MCP servers (nomad-pm and the rest). "
    "Follow docs/SDLC.md: work on a feature branch or worktree, never on master/main, never "
    "`git stash` in ~/nomad, commit with `--only`, never force-push. Anything destructive, a push, a "
    "deploy or a delete goes through the operator's approval: the permission prompt is shown to "
    "them in the chat, so just run the tool and wait. Use AskUserQuestion when a choice is the "
    "operator's. New projects go through nomad-pm. Report only what a tool result in this "
    "conversation confirmed; if a command failed or didn't run, say so. Don't write a \"Pending\" "
    "list or a run footer: the bridge adds them. To show the operator a file (image, PDF, csv, "
    "markdown, audio, docx/xlsx/pptx), create it at an absolute path (Write tool, or any tool or command) and say its "
    "name: the bridge shows it in the chat. Files the operator attached are listed in the message."
)


class Refused(Exception):
    pass


class Run:
    def __init__(self, chat_id, store, gates: Gates, background=False):
        self.run_id = uuid.uuid4().hex[:8]
        self.chat_id, self.store, self.gates, self.background = chat_id, store, gates, background
        self.events: list[dict] = []
        self.cond = threading.Condition()
        self.status = "starting"           # starting running waiting done failed stopped
        self.proc = None
        self.render = events.Renderer()
        self.cost = None
        self.session_id = ""
        self.started = time.time()
        self.finished = False
        self.delivered = 0                 # events already streamed to a client
        self.backend = "claude"
        self.model = ""
        self.env_extra: dict = {}
        self.unavailable = False           # failed as "this vendor can't serve you", nothing ran
        self.failure = ""

    # --- event log
    def emit(self, ev: dict):
        with self.cond:
            self.events.append(ev)
            self.cond.notify_all()

    def stream(self, cursor: int = 0, poll: float = 15.0):
        """Yield (index, event) from `cursor` until the run is over; a `keepalive` between."""
        i = cursor
        while True:
            with self.cond:
                while i >= len(self.events) and not self.finished:
                    if not self.cond.wait(poll):
                        break
                batch = self.events[i:]
                done = self.finished and i + len(batch) >= len(self.events)
            for ev in batch:
                i += 1
                yield i, ev
            if not batch:
                if done:
                    return
                yield i, {"type": "keepalive"}
            elif done:
                return

    # --- lifecycle
    @property
    def agent_label(self) -> str:
        return backends.Backends.label(self.backend, self.model)

    def _env(self):
        env = {**os.environ, "ANTHROPIC_API_KEY": "", "CLAUDE_CODE_ENTRYPOINT": "nomad-agent-bridge"}
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.update(self.env_extra)
        return env

    def start(self, cmd, cwd, message):
        self.render.watch(cwd)
        env = self._env()
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         bufsize=1, start_new_session=True)
        except OSError as e:
            self._finish("failed", f"could not start claude: {e}")
            return
        self.status = "running"
        self._send({"type": "user", "message": {"role": "user", "content": message}})
        threading.Thread(target=self._read, daemon=True, name=f"run-{self.run_id}").start()

    def _send(self, obj):
        try:
            self.proc.stdin.write(json.dumps(obj) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def stop(self):
        """Kill the process group and any request it was waiting on. Idempotent."""
        if self.finished:
            return False
        self.status = "stopped"
        self.gates.cancel_run(self.run_id)
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
        return True

    def _finish(self, status, summary):
        if self.finished:
            return
        if self.status != "stopped":
            self.status = status
        else:
            status = "stopped"
        op = [f"Approve or deny run `{p.run_id}` — {p.tool}" for p in self.gates.pending(self.chat_id)
              if p.run_id == self.run_id]
        agent = self.render.open_todos() + getattr(self, "extra_agent", [])
        if status == "stopped":
            agent.append("Run was stopped before it finished; resend the request to continue")
        if status == "failed":
            op.append(f"Look at the failure above: {summary[:140]}")
        self.failure = summary if status == "failed" else ""
        self.unavailable = status == "failed" and backends.classify(
            summary, self.render.ok + self.render.failed)
        self.emit({"type": "delta", "text": self.render.footer(
            self.run_id, op, agent, status, self.cost, self.agent_label)})
        self.store.run_end(self.run_id, status, summary)
        self.emit({"type": "done", "run_id": self.run_id, "status": status,
                   "session_id": self.session_id, "cost_usd": self.cost, "backend": self.backend,
                   "agent": self.agent_label, "model": self.model, "unavailable": self.unavailable})
        with self.cond:
            self.finished = True
            self.cond.notify_all()

    def _read(self):
        summary, failed = "", None
        try:
            for raw in self.proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                t = e.get("type")
                if t == "control_request":
                    self._control(e)
                    continue
                for ev in self.render.feed(e):
                    self.emit(ev)
                if t == "system" and e.get("subtype") == "init":
                    self.session_id = e.get("session_id") or self.session_id
                    self.model = e.get("model") or self.model
                    src = self.render.api_key_source
                    if self.backend == "claude" and src not in OK_KEY_SOURCES:
                        failed = f"refused: claude reports apiKeySource={src!r}, not the local Max login"
                        self.stop()
                        self.status = "failed"
                        break
                if t == "result":
                    self.cost = e.get("total_cost_usd") if self.backend == "claude" else None
                    self.session_id = e.get("session_id") or self.session_id
                    summary = str(e.get("result") or "")
                    if e.get("is_error"):
                        failed = summary or str(e.get("subtype") or "error")
                    break
        except Exception as ex:                      # a reader bug must not leave the run hanging
            failed = f"bridge error: {type(ex).__name__}: {ex}"
        finally:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=10)
            except Exception:
                self.stop()
        if failed:
            self.emit({"type": "delta", "text": f"\n\n⚠ {failed}\n"})
            self._finish("failed", failed)
        elif self.status == "stopped":
            self._finish("stopped", "stopped by the operator")
        else:
            err = ""
            if self.proc.returncode not in (0, None) and not summary:
                err = (self.proc.stderr.read() or "")[-400:]
            if err:
                self.emit({"type": "delta", "text": f"\n\n⚠ claude exited {self.proc.returncode}: {err}\n"})
                self._finish("failed", err)
            else:
                self._finish("done", summary)

    # --- permission requests
    def _control(self, e):
        req = e.get("request") or {}
        if req.get("subtype") != "can_use_tool":
            self._send({"type": "control_response", "response": {
                "subtype": "error", "request_id": e.get("request_id"), "error": "unsupported request"}})
            return
        tool, tin = req.get("tool_name") or "?", req.get("input") or {}
        question = tool == "AskUserQuestion"
        p = Pending(self.run_id, self.chat_id, e.get("request_id"), tool, tin,
                    "question" if question else "permission",
                    req.get("description") or req.get("decision_reason") or "")
        self.gates.open(p)
        self.status = "waiting"
        self.emit({"type": "gate", "run_id": self.run_id, "ask_id": p.ask_id, "kind": p.kind, "tool": tool,
                   "summary": events.summarize(tool, tin),
                   "reason": req.get("description") or req.get("decision_reason") or "",
                   "questions": tin.get("questions") if question else None})
        d = self.gates.wait(p)
        self.status = "running"
        approved = d.get("decision") == "approve"
        if approved:
            upd = {**tin, "answers": d.get("answers") or {}} if question else tin
            resp = {"behavior": "allow", "updatedInput": upd}
            self._line(f"✅ approved: **{tool}** (run `{self.run_id}`)")
        else:
            why = d.get("note") or "the operator denied this"
            resp = {"behavior": "deny", "message": why}
            self._line(f"⛔ denied: **{tool}** — {why}")
        self._send({"type": "control_response", "response": {
            "subtype": "success", "request_id": e.get("request_id"), "response": resp}})

    def _line(self, text):
        self.emit({"type": "delta", "text": f"\n> {text}\n\n"})


class CodexRun(Run):
    """Same run, but the process is `codex exec --json`: no permission channel (it is sandboxed
    instead), and its items are translated into the events the Renderer already knows."""
    backend = "codex"

    def start(self, cmd, cwd, message):
        self.render.watch(cwd)
        try:
            self.proc = subprocess.Popen(cmd, cwd=cwd, env=self._env(), stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         bufsize=1, start_new_session=True)
        except OSError as e:
            self._finish("failed", f"could not start codex: {e}")
            return
        self.status = "running"
        self.cwd = cwd
        self._dirty0 = set(self._porcelain())
        self.tr = backends.CodexTranslator()
        try:
            self.proc.stdin.write(message)
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        threading.Thread(target=self._read, daemon=True, name=f"run-{self.run_id}").start()

    def _read(self):
        failed = ""
        try:
            for raw in self.proc.stdout:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                for be in self.tr.feed(e):
                    for ev in self.render.feed(be):
                        self.emit(ev)
                self.session_id = self.tr.thread_id or self.session_id
        except Exception as ex:
            failed = f"bridge error: {type(ex).__name__}: {ex}"
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.stop()
        self.model = backends.Backends.codex_model(self.tr.thread_id) or self.model
        if self.tr.thread_id:
            self.store.set_session(self.chat_id, "codex", self.tr.thread_id, self.cwd)
        if not failed and self.tr.error:
            failed = self.tr.error
        if not failed and self.status != "stopped" and not self.tr.turn_done:
            failed = (self.proc.stderr.read() or "")[-400:] or f"codex exited {self.proc.returncode}"
        if failed:
            self.emit({"type": "delta", "text": f"\n\n⚠ {failed}\n"})
            self._finish("failed", failed)
        elif self.status == "stopped":
            self._finish("stopped", "stopped by the operator")
        else:
            if set(self._porcelain()) - self._dirty0:
                self.extra_agent = ["Commit the changes: Codex's sandbox can't write .git. Say "
                                    "`!claude commit the changes` when Claude Code is back, or commit by hand"]
            self._finish("done", "")

    def _porcelain(self) -> list:
        try:
            r = subprocess.run(["git", "-C", self.cwd, "status", "--porcelain"], capture_output=True,
                               text=True, timeout=10)
            return r.stdout.splitlines()
        except Exception:
            return []


class RunManager:
    def __init__(self, store, gates=None, claude=CLAUDE, be=None):
        self.store, self.gates, self.claude = store, gates or Gates(store), claude
        self.be = be or backends.Backends(claude)
        self.runs: dict[str, Run] = {}
        self._lock = threading.Lock()
        self._settings = None

    def _settings_file(self) -> str:
        if not self._settings:
            fd, path = tempfile.mkstemp(prefix="agent-bridge-settings-", suffix=".json")
            with os.fdopen(fd, "w") as f:
                json.dump({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                    {"type": "command", "command": f"python3 {HOOK}"}]}]}}, f)
            self._settings = path
        return self._settings

    def active(self, chat_id: str):
        with self._lock:
            return next((r for r in self.runs.values() if r.chat_id == chat_id and not r.finished), None)

    def get(self, run_id: str):
        return self.runs.get(run_id)

    def start(self, chat_id, cwd, project, message, model="sonnet", background=False,
              backend="claude", handoff="") -> Run:
        cur = self.active(chat_id)
        if cur:
            raise Refused(f"run `{cur.run_id}` is still {cur.status} in this chat; wait for it or stop it")
        chat = self.store.chat(chat_id)
        self.be.mcp_sync()                  # a server installed since the last run reaches every backend
        if backend == "claude":
            resume = bool(chat and chat["cwd"] == cwd)
            sid = chat["session_id"] if resume else str(uuid.uuid4())
            cmd = [self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
                   "--model", MODELS.get(model, "sonnet"),
                   *(["--resume", sid] if resume else ["--session-id", sid]),
                   "--settings", self._settings_file(), "--append-system-prompt", AGENT_SYSTEM]
            run = Run(chat_id, self.store, self.gates, background)
            self.store.bind(chat_id, sid, cwd, project)
        elif backend == "local":
            try:
                mcp_cfg = self.be.mcp_config(project)
            except ValueError as e:         # too many servers for the local window
                raise Refused(str(e))
            old = self.store.session(chat_id, "local")
            resume = bool(old and old["cwd"] == cwd)
            sid = old["session_id"] if resume else str(uuid.uuid4())
            cmd = [self.claude, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                   "--verbose", "--include-partial-messages", "--permission-prompt-tool", "stdio",
                   "--model", backends.LOCAL_MODEL,
                   # every registered MCP server, deferred by tool search (env), no skills: the
                   # schemas alone are ~420k tokens and the local window is 64k
                   "--strict-mcp-config", "--mcp-config", mcp_cfg or '{"mcpServers":{}}',
                   "--disable-slash-commands",
                   *(["--resume", sid] if resume else ["--session-id", sid]),
                   "--settings", self._settings_file(), "--append-system-prompt", AGENT_SYSTEM]
            run = Run(chat_id, self.store, self.gates, background)
            run.env_extra = self.be.env("local")
            self.store.set_session(chat_id, "local", sid, cwd)
        else:                                            # codex
            old = self.store.session(chat_id, "codex")
            resume = bool(old and old["cwd"] == cwd)
            sid = old["session_id"] if resume else ""
            cmd = self.be.codex_cmd(sid if resume else "")
            run = CodexRun(chat_id, self.store, self.gates, background)
            message = (("" if resume else backends.CODEX_PREAMBLE + "\n\n") + handoff + message)
        if backend in ("claude", "local") and handoff:
            message = handoff + message
        run.backend = backend
        run.session_id = sid
        self.store.run_start(run.run_id, chat_id, background)
        with self._lock:
            self.runs[run.run_id] = run
            for k in [k for k, r in self.runs.items() if r.finished and time.time() - r.started > 3600]:
                del self.runs[k]
        run.emit({"type": "status", "text": f"agent run {run.run_id} · {run.agent_label} · "
                  f"{project or 'general'} · {cwd} · {'resumed' if resume else 'new'} session"})
        run.start(cmd, cwd, message)
        if backend == "codex":
            run.emit({"type": "delta", "text": f"> ℹ️ **{run.agent_label}** — stand-in for Claude Code: "
                      "it edits files and runs commands in the project folder; it can't commit, push or "
                      "use the network, and asks no approvals.\n\n"})
        return run

    def stop(self, run_id="", chat_id="") -> dict:
        run = self.runs.get(run_id) if run_id else self.active(chat_id)
        if not run or run.finished:
            return {"ok": False, "error": "no running turn to stop"}
        run.stop()
        return {"ok": True, "run_id": run.run_id}
