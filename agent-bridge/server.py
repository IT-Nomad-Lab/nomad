#!/usr/bin/env python3
"""NOMAD agent bridge: Open WebUI chat turns run as real Claude Code sessions (docs/prd/owui-agent-bridge.md).

NDJSON over HTTP, the same shape the engine's /owui/chat speaks, so the pipe stays thin:

  POST /agent/chat    {chat_id, project, message}  → stream of bridge events (events.py)
  GET  /agent/attach  ?run_id=&cursor=             → the same stream, resumed (cursor=delivered:
                      from where the last consumer stopped)
  POST /agent/decide  {ask_id | run_id, decision, answers?, chat_id?}   approve | deny a pending ask
                      (first answer wins; a later one gets "already answered"; after a restart
                      the ask is `expired` and nothing is applied)
  POST /agent/answer  {text, chat_id?}  raw chat form `answer <ask_id> <json>` (gates.parse_answer);
                      the same text sent as a chat message to /agent/chat does the same
  POST /agent/stop    {run_id | chat_id}
  GET  /agent/pending ?chat_id=&state=  asks in any state: ask_id, run_id, chat_id, kind, tool,
                      summary, reason, created, state, questions (AskUserQuestion shape); an
                      answered one adds decision, answers and via (chat | api)
  GET  /agent/runs    ?chat_id=&unseen=1
  GET  /health        (no token)

Auth: X-Nomad-Token = ~/.config/nomad/owui-token, the engine's owui token. No token file → closed.
Bind: 127.0.0.1 plus the docker0 gateway via NOMAD_BRIDGE_IP, never 0.0.0.0.
"""
import hmac
import importlib.util
import json
import logging
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backends  # noqa: E402
import inbox  # noqa: E402
import projects  # noqa: E402
import runner  # noqa: E402
from store import Store  # noqa: E402

HOST = os.environ.get("AGENT_BRIDGE_HOST", "127.0.0.1")
PORT = int(os.environ.get("AGENT_BRIDGE_PORT", "8093"))
TOKEN_FILE = os.environ.get("AGENT_BRIDGE_TOKEN_FILE") or os.path.expanduser("~/.config/nomad/owui-token")

CMD = re.compile(r"^\s*(approve|allow|yes|deny|reject|no|stop|cancel|status)\b[ \t]*([0-9a-f]{8})?\s*$", re.I)
ANSWER = re.compile(r"^\s*answer\s+ask-[0-9a-f]+\b", re.I)
BG = re.compile(r"^\s*!bg\b\s*", re.I)
DEEP = re.compile(r"^\s*!deep\b\s*", re.I)
NEW = re.compile(r"^\s*!new\s*$", re.I)


def read_token() -> str:
    try:
        with open(TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def answers_for(questions: list, text: str) -> dict:
    """{question text: chosen option label} from "2", "Option label" or "1; 3" (one per question)."""
    parts = [p.strip() for p in text.split(";")] if len(questions or []) > 1 else [text.strip()]
    out = {}
    for i, q in enumerate(questions or []):
        raw = parts[i] if i < len(parts) else parts[-1]
        labels = [str(o.get("label") or "") for o in q.get("options") or []]
        picked = []
        for tok in [t.strip() for t in re.split(r",|\band\b", raw)] if q.get("multiSelect") else [raw]:
            if tok.isdigit() and 1 <= int(tok) <= len(labels):
                picked.append(labels[int(tok) - 1])
            else:
                hit = next((l for l in labels if l.lower() == tok.lower()), None)
                picked.append(hit or tok)
        out[str(q.get("question") or f"q{i + 1}")] = ", ".join(p for p in picked if p)
    return out


def describe(p) -> str:
    if p.kind == "question":
        qs = []
        for q in p.input.get("questions") or []:
            opts = "\n".join(f"  {i}. {o.get('label')}" + (f" — {o.get('description')}" if o.get("description") else "")
                             for i, o in enumerate(q.get("options") or [], 1))
            qs.append(f"**{q.get('question')}**\n{opts}")
        return "\n\n".join(qs)
    return f"**{p.tool}** `{__import__('events').summarize(p.tool, p.input)}`"


class App:
    def __init__(self, store=None, manager=None, notify=None):
        self.store = store or Store()
        self.mgr = manager or runner.RunManager(self.store)
        self.notify = notify or (lambda text: None)

    # ---- one chat message → an iterator of bridge events
    def chat(self, chat_id: str, project: str, message: str, backend: str = "", attachments=None):
        msg = message or ""
        gates = self.mgr.gates
        if NEW.match(msg):
            self.store.reset(chat_id)
            yield from self._say("Started a fresh Claude Code session for this chat; the next message begins it.")
            return
        if ANSWER.match(msg):                          # `answer <ask_id> <json>` from a card or the cockpit
            r = gates.answer_text(msg, chat_id, via="chat")
            if not r["ok"]:
                yield from self._say(f"⚠ {r['error']}.")
                return
            yield {"type": "status", "text": f"{r['decision']}d ask {r['ask_id']}"}
            yield from self._follow(self.mgr.get(r["run_id"]))
            return
        m = CMD.match(msg)
        pend = gates.pending(chat_id)
        if m:
            word, rid = m.group(1).lower(), m.group(2) or ""
            if word == "status":
                yield from self._say(self._status(chat_id))
            elif word in ("stop", "cancel"):
                yield from self._say(self._stop_text(self.mgr.stop(rid, chat_id)))
            else:
                ok = word in ("approve", "allow", "yes")
                answers = {}
                target = next((p for p in pend if not rid or p.run_id == rid), None)
                if ok and target is not None and target.kind == "question":
                    yield from self._say(f"Run `{target.run_id}` is asking a question, not for approval. Answer with "
                                         f"the option number.\n\n{describe(target)}")
                    return
                r = gates.decide(rid, "approve" if ok else "deny", answers, chat_id, via="chat")
                if not r["ok"]:
                    yield from self._say(f"⚠ {r['error']}.")
                    return
                run = self.mgr.get(r["run_id"])
                yield {"type": "status", "text": f"{r['decision']}d run {r['run_id']}"}
                yield from self._follow(run)
            return
        q = next((p for p in pend if p.kind == "question"), None)
        if q is not None:                              # a reply to AskUserQuestion
            r = gates.decide(q.run_id, "approve", answers_for(q.input.get("questions"), msg), chat_id,
                             via="chat")
            if r["ok"]:
                yield from self._follow(self.mgr.get(q.run_id))
                return
        if pend:
            yield from self._say("Run `%s` is waiting on you:\n\n%s\n\nSay `approve %s` or `deny %s`."
                                 % (pend[0].run_id, describe(pend[0]), pend[0].run_id, pend[0].run_id))
            return
        try:
            pid, cwd = projects.resolve(project)
        except ValueError as e:
            yield from self._say(f"⚠ {e}")
            return
        except Exception as e:
            yield from self._say(f"⚠ cannot read the project registry: {e}")
            return
        fm = backends.FORCE.match(msg)
        forced = fm.group(1).lower() if fm else (backend if backend in ("claude", "codex", "local") else "")
        if fm:
            msg = msg[fm.end():]
        bg = bool(BG.match(msg))
        model = "opus" if DEEP.match(msg) else "sonnet"
        text = DEEP.sub("", BG.sub("", msg, 1), 1).strip()
        if not text:
            yield from self._say("Say what to do. `!bg <task>` runs it in the background, `!deep` uses Opus, "
                                 "`!new` starts a fresh session, `status` and `stop` control a run.")
            return
        if attachments:
            saved, skipped = inbox.save(chat_id, attachments)
            text += inbox.note(saved, skipped)
        done = self.store.runs(chat_id, unseen_background=True)
        for d in done:
            yield {"type": "delta", "text": f"> ✔ background run `{d['run_id']}` finished: {d['status']} — "
                                            f"{(d.get('summary') or '')[:300]}\n\n"}
        order = self.mgr.be.order(forced)
        if not order:
            yield from self._say("⚠ No agent backend is available right now:\n\n" + self.mgr.be.report())
            return
        run = None
        for n, name in enumerate(order):
            if name == "local" and not self.mgr.be.local_up():
                yield {"type": "delta", "text": "> Starting the local model (about a minute; `private` is "
                       "unloaded while it runs).\n\n"}
                ok, why = self.mgr.be.ensure_local()
                if not ok:
                    self.mgr.be.mark_unavailable("local", why)
                    if n + 1 < len(order) and not forced:
                        continue
                    yield from self._say(f"⚠ {why}")
                    return
            elif name == "local":
                self.mgr.be.touch_local()
            try:
                run = self.mgr.start(chat_id, cwd, pid, text, model=model, background=bg,
                                     backend=name, handoff=self._handoff(chat_id, name))
            except runner.Refused as e:
                yield from self._say(f"⚠ {e}.")
                return
            if bg:
                break
            yield from self._follow(run)
            if not (run.unavailable and not forced and n + 1 < len(order)):
                return
            self.mgr.be.mark_unavailable(name, run.failure)
            yield {"type": "delta", "text": f"\n> ↪ **{backends.Backends.label(name)}** can't serve this "
                   f"right now; continuing on **{backends.Backends.label(order[n + 1])}**.\n\n"}
        if run is None:
            return
        if bg:
            threading.Thread(target=self._watch, args=(run, pid), daemon=True).start()
            yield {"type": "delta", "text": f"Started in the background as run `{run.run_id}` in `{pid}`. "
                   f"I'll report here on your next message and in Zulip when it finishes. "
                   f"`status` shows it, `stop {run.run_id}` stops it.\n"}
            yield {"type": "done", "run_id": run.run_id, "status": "background", "session_id": run.session_id,
                   "agent": run.agent_label, "backend": run.backend}
            return

    def _handoff(self, chat_id: str, backend: str) -> str:
        """A fresh session on a different backend doesn't know the chat: give it the last results."""
        if backend == "claude" or self.store.session(chat_id, backend):
            return ""
        prev = [r for r in self.store.runs(chat_id) if r.get("summary")][:2]
        if not prev:
            return ""
        notes = "\n".join(f"- run {r['run_id']} ({r['status']}): {r['summary'][:400]}" for r in reversed(prev))
        return ("Context: another agent already worked in this chat; its last results were:\n"
                f"{notes}\n\nOperator's new request:\n")

    def _watch(self, run, project):
        for _ in run.stream():
            pass
        try:
            self.notify(f"agent run {run.run_id} ({project}) finished: {run.status}")
        except Exception:
            pass

    def _follow(self, run, cursor=None):
        """Stream `run` from where the last consumer stopped (run.delivered), so a reply that
        arrives as a new message doesn't replay what the chat already shows."""
        if run is None:
            yield from self._say("⚠ that run is no longer known to the bridge.")
            return
        for i, ev in run.stream(run.delivered if cursor is None else cursor):
            run.delivered = max(run.delivered, i)
            yield ev

    @staticmethod
    def _say(text):
        yield {"type": "delta", "text": text + "\n"}
        yield {"type": "done", "status": "info"}

    def _status(self, chat_id):
        act = self.mgr.active(chat_id)
        lines = []
        if act:
            lines.append(f"Run `{act.run_id}` is {act.status} ({int(__import__('time').time() - act.started)}s).")
        for p in self.mgr.gates.pending(chat_id):
            lines.append(f"Waiting on you — run `{p.run_id}`:\n\n{describe(p)}")
        lines.append("Backends:\n" + self.mgr.be.report())
        lines.append(self.mgr.be.mcp_report())
        recent = self.store.runs(chat_id)[:5]
        if recent:
            lines.append("Recent runs: " + ", ".join(f"`{r['run_id']}` {r['status']}" for r in recent))
        return "\n\n".join(lines) or "No runs in this chat yet."

    @staticmethod
    def _stop_text(r):
        return f"Stopped run `{r['run_id']}`." if r["ok"] else f"⚠ {r['error']}."


def make_handler(app: App):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            want = read_token()
            if not want:
                self._json(503, {"error": "agent bridge closed: no owui-token"})
                return False
            if not hmac.compare_digest(self.headers.get("X-Nomad-Token", ""), want):
                self._json(401, {"error": "bad token"})
                return False
            return True

        def _stream(self, it):
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            try:
                for ev in it:
                    self.wfile.write(json.dumps(ev).encode() + b"\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                                   # client left; the run goes on

        def do_GET(self):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if u.path == "/health":
                return self._json(200, {"status": "ok", "claude": runner.CLAUDE})
            if not self._authed():
                return
            if u.path == "/agent/attach":
                run = app.mgr.get(q.get("run_id", ""))
                if not run:
                    return self._json(404, {"error": "unknown run"})
                cur = q.get("cursor") or "0"           # `delivered`: from where the last consumer stopped
                return self._stream(app._follow(run, None if cur == "delivered" else int(cur)))
            if u.path == "/agent/pending":
                # every ask of the chat in any state (pending | answered | timeout | cancelled | expired)
                return self._json(200, {"pending": app.mgr.gates.list_asks(q.get("chat_id", ""), q.get("state", ""))})
            if u.path == "/agent/state":
                # what NOMAD Auto needs to route a turn: is this chat already an agent session,
                # is a gate waiting, and may this project go to Claude Code at all. `local_up`:
                # is the local agent model (:8240) on the GPU, so NOMAD Local's chat stays on it
                chat = app.store.chat(q.get("chat_id", ""))
                try:
                    projects.resolve(q.get("project", ""))
                    eligible, why = True, ""
                except Exception as e:           # unknown project, local_only, missing folder
                    eligible, why = False, str(e)[:200]
                return self._json(200, {"session": bool(chat and chat.get("session_id")),
                                        "pending": len(app.mgr.gates.pending(q.get("chat_id", ""))),
                                        "eligible": eligible, "why": why,
                                        "local_up": app.mgr.be.local_up()})
            if u.path == "/agent/runs":
                return self._json(200, {"runs": app.store.runs(q.get("chat_id", ""), bool(q.get("unseen")))})
            self._json(404, {"error": "not found"})

        def do_POST(self):
            if not self._authed():
                return
            try:
                req = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                return self._json(400, {"error": "bad json"})
            p = urlparse(self.path).path
            if p == "/agent/chat":
                if not req.get("chat_id"):
                    return self._json(400, {"error": "chat_id required"})
                return self._stream(app.chat(str(req["chat_id"]), str(req.get("project") or ""),
                                             str(req.get("message") or ""),
                                             str(req.get("backend") or ""),
                                             req.get("attachments")))
            if p == "/agent/decide":
                r = app.mgr.gates.decide(str(req.get("run_id") or ""), str(req.get("decision") or ""),
                                         req.get("answers"), str(req.get("chat_id") or ""),
                                         ask_id=str(req.get("ask_id") or ""))
                return self._json(200 if r["ok"] else 409, r)
            if p == "/agent/answer":                   # raw chat text: `answer <ask_id> <json>`
                r = app.mgr.gates.answer_text(str(req.get("text") or ""), str(req.get("chat_id") or ""))
                return self._json(200 if r["ok"] else 409, r)
            if p == "/agent/stop":
                r = app.mgr.stop(str(req.get("run_id") or ""), str(req.get("chat_id") or ""))
                return self._json(200 if r["ok"] else 409, r)
            self._json(404, {"error": "not found"})
    return H


def zulip_notifier():
    try:
        import zulip
    except ImportError:
        return lambda text: None
    rc = os.path.expanduser(os.environ.get("NOMAD_ZULIPRC", "~/.config/nomad/zulip-bot.zuliprc"))

    def send(text):
        zulip.Client(config_file=rc).send_message(
            {"type": "stream", "to": "alerts", "topic": "agent-bridge", "content": text})
    return send


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    spec = importlib.util.spec_from_file_location(
        "bridge_bind", os.path.join(HERE, "..", "dispatcher", "bridge_bind.py"))
    bind = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bind)
    app = App(notify=zulip_notifier())

    def _sweep_inbox():                       # attachments older than inbox.KEEP_DAYS go (#1334)
        while True:
            try:
                inbox.sweep()
            except Exception as e:
                print(f"inbox sweep failed: {e}", flush=True)
            time.sleep(6 * 3600)
    threading.Thread(target=_sweep_inbox, daemon=True).start()
    print(f"NOMAD agent bridge on http://{HOST}:{PORT} (claude={runner.CLAUDE})", flush=True)
    bind.serve_http(HOST, PORT, make_handler(app))


if __name__ == "__main__":
    main()
