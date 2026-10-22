"""NOMAD v2 · P1-6 — engine HTTP service + the human gate entry.

Endpoints (stdlib http.server; bound 0.0.0.0:8099 so containers can reach it):
  GET  /health
  POST /capture       {goal}                 → start a run; pauses at the gate
  POST /resume        {run_id, decision}     → resume a paused run (approved|rejected)
  POST /nocodb-hook   <NocoDB webhook body>  → THE GATE: a comms status flip resumes the run
  GET  /runs                                 → recent runs for the cockpit

The gate: the operator flips a comms row `status` to approved/rejected in NocoDB → NocoDB's
webhook POSTs here → we map status→decision and resume the paused run. Defense in depth: the
`.claude/hooks/guard.py` harness gate independently guards irreversible Execute actions.
(Per the contract an n8n relay may sit in front of /nocodb-hook; it is a thin pass-through and
is deferred to Phase 2 — the status-flip → resume semantics are unchanged.)
"""
import asyncio
import base64
import functools
import html
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import engine as engine_mod
import intents
import cockpit_asks
import cockpit_policies
import cockpit_standards
import owui
from engine import Engine, proposal_hash
from mcp_image import generate_image_impl   # direct local image gen for the nomad-image MCP bridge
from nocodb import NocoDB
import pm_board
import pm_follow
import pm_mention
from task_question import parse_question
from pm_openproject import MANAGER, OpenProject, PMError, _title

HOST = os.environ.get("NOMAD_V2_ENGINE_HOST", "127.0.0.1")   # localhost-only (security review)
PORT = int(os.environ.get("NOMAD_V2_ENGINE_PORT", "8099"))
GATE_POLL_SECS = int(os.environ.get("NOMAD_V2_GATE_POLL", "3"))
VOICE_URL = os.environ.get("NOMAD_VOICE_URL", "http://nomad-voice:8200")   # 3D: local Piper/Whisper
# nomad-detect is a host-native systemd unit, deliberately bound to localhost, so a container
# reaches it the same way it reaches anything else on the host loopback.
DETECT_URL = os.environ.get("NOMAD_DETECT_URL", "http://host.docker.internal:8214")
CONSOLE_URL = os.environ.get("NOMAD_CONSOLE_URL", "http://nomad-console:8000")  # v1 telemetry fallback only
TERM_BASE = os.environ.get("NOMAD_TERM_URL", "ws://host.docker.internal:8091")  # interactive project terminal
DISPATCH_URL = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090")  # project list
# nomad-autopilot supervisor (#864), host-native and bound to 127.0.0.1, reached like nomad-detect.
AUTOPILOT_URL = os.environ.get("NOMAD_AUTOPILOT_URL", "http://host.docker.internal:8092")


def _dispatch_token():
    """Shared secret for the Builder dispatcher (auto-generated on the host at
    ~/.config/nomad/dispatch-token). The dispatcher refuses every route but /health without
    it. Candidates cover the container mounts and the host; '' means the call will 401."""
    env = os.environ.get("NOMAD_DISPATCH_TOKEN")
    if env:
        return env
    for cand in ("/term-secret/dispatch-token", "/host/.config/nomad/dispatch-token",
                 os.path.expanduser("~/.config/nomad/dispatch-token")):
        try:
            return open(cand).read().strip()
        except Exception:
            continue
    return ""


DISPATCH_TOKEN = _dispatch_token()


def _term_token():
    """Shared secret for the terminal daemon (auto-generated on the host). Read from the mounted
    token dir or NOMAD_TERM_TOKEN; '' if unavailable (then termd, if it has a token, rejects)."""
    env = os.environ.get("NOMAD_TERM_TOKEN")
    if env:
        return env
    for cand in ("/term-secret/term-token",):
        try:
            return open(cand).read().strip()
        except Exception:
            continue
    return ""


TERM_TOKEN = _term_token()
eng = Engine()
db = eng.db
_resolved = set()   # run_ids already resumed (avoid re-processing rejected rows)

# Live cockpit: a version counter bumped on any run change → SSE pushes a refresh (no polling).
_runs_cv = threading.Condition()
_runs_ver = 0


def _bump_runs():
    """Signal that the runs feed changed → wake every open SSE stream."""
    global _runs_ver
    with _runs_cv:
        _runs_ver += 1
        _runs_cv.notify_all()


def _voice_tts(text: str) -> bytes:
    """Proxy to the local Piper TTS → WAV bytes."""
    r = urllib.request.Request(VOICE_URL + "/tts", method="POST",
                               headers={"Content-Type": "application/json"},
                               data=json.dumps({"text": text[:1200]}).encode())
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.read()


def _voice_stt(audio: bytes, ctype: str) -> dict:
    """Proxy raw audio to the local Whisper STT (multipart field 'file') → {text}."""
    b = b"----nomadv2voice"
    body = b"\r\n".join([
        b"--" + b,
        b'Content-Disposition: form-data; name="file"; filename="audio.webm"',
        b"Content-Type: " + ctype.encode(), b"", audio, b"--" + b + b"--", b""])
    r = urllib.request.Request(VOICE_URL + "/stt", method="POST", data=body,
                               headers={"Content-Type": b"multipart/form-data; boundary=".decode() + b.decode()})
    with urllib.request.urlopen(r, timeout=90) as resp:
        return json.loads(resp.read())


def _detect(text: str, lint: bool, external: bool) -> tuple:
    """Ask nomad-detect whether text reads as machine-written. Returns (status, body).

    Two modes, and they cost very different things. `/lint` is the style pass and answers instantly;
    `/detect` also runs the statistical one and can take a while, which is why the timeout is a
    minute and a half rather than the usual few seconds. `external` is the only path that leaves
    this box and it is off unless the caller asks for it by name.

    The detector's own verdict about our published writing is not trustworthy yet, and that is not
    this proxy's fault to fix: the human corpus is still 0 of 20 (see CLAUDE.md, open item 2). This
    moves the endpoint off the console, which is the part V2c is responsible for."""
    path = "/lint" if lint else "/detect"
    payload = {"text": text} if lint else {"text": text, "external": external}
    req = urllib.request.Request(DETECT_URL + path, method="POST",
                                 headers={"Content-Type": "application/json"},
                                 data=json.dumps(payload).encode())
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        # The detector's own refusal, passed through. Rewriting it as a 503 would say the service is
        # down when it is up and declining, and the two need different things done about them.
        body = {}
        try:
            body = json.loads(e.read() or b"{}")
        except Exception:
            pass
        # An empty body is common and would otherwise reach the caller as a bare {}, which reads
        # like a clean answer with no verdict in it.
        return e.code, body if body else {"error": f"detector returned {e.code}"}
    except Exception as e:
        return 503, {"error": f"detector unreachable: {str(e)[:160]}"}


def _pipeline_context(limit=12) -> str:
    """A compact snapshot of the live pipeline for the chat model to reason over."""
    runs = recent_runs(limit)
    if not runs:
        return "The pipeline is idle — no runs yet."
    counts = {}
    for r in runs:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    lines = ["Counts: " + ", ".join(f"{k}={v}" for k, v in counts.items()), "Recent runs:"]
    for r in runs:
        line = f"- [{r['status']}] {r.get('lane','?')} · {r.get('run_id','')}"
        p = r.get("proposal")
        if p:
            line += f" · proposes {p.get('action')} → {p.get('target','')}: {(p.get('preview') or '')[:80]}"
        lines.append(line)
    return "\n".join(lines)


# A run id is 12 lowercase hex characters. We accept a prefix too, because nobody retypes twelve
# of those by hand, and we resolve every candidate against the real run list rather than trusting
# the pattern — that resolution is the filter, so an ordinary hex-looking word matches nothing and
# costs nothing.
_RUN_ID_RE = re.compile(r"\b[0-9a-f]{6,32}\b")


def _resolve_run_ids(message: str, max_runs: int = 2):
    """Run ids named in `message`, as (resolved, ambiguous) id lists.

    An ambiguous prefix returns its candidates instead of picking one. Injecting the wrong run's
    error is worse than injecting none: the model would explain a failure that did not happen,
    confidently, and the operator has no way to tell.
    """
    tokens, seen = [], set()
    for tok in _RUN_ID_RE.findall(message.lower()):
        if tok not in seen:
            seen.add(tok)
            tokens.append(tok)
    if not tokens:
        return [], []
    resolved, ambiguous, known = [], [], None
    for tok in tokens[:4]:
        if len(tok) == 12 and db.find("comms", "run_id", tok):
            resolved.append(tok)
            continue
        if known is None:                       # only pay for the scan if a prefix showed up
            known = [r.get("run_id") for r in recent_runs(200) if r.get("run_id")]
        hits = [rid for rid in known if rid.startswith(tok)]
        if len(hits) == 1:
            resolved.append(hits[0])
        elif len(hits) > 1:
            ambiguous.append((tok, hits[:6]))
    return resolved[:max_runs], ambiguous[:max_runs]


def _run_context(run_ids, ambiguous) -> str:
    """The detail block for runs the operator named. `run_detail` already assembles the routing,
    the proposal, the error and the episodic log; this only has to make it readable and bounded."""
    blocks = []
    for rid in run_ids:
        d = run_detail(rid)
        if not d.get("status"):
            continue
        lines = [f"Run {rid} — status {d['status']}, lane {d.get('lane') or '?'}"]
        for label, key in (("goal", "goal"), ("intent", "intent"), ("target", "target"),
                           ("action", "action")):
            if d.get(key):
                lines.append(f"  {label}: {str(d[key])[:300]}")
        if d.get("error"):
            lines.append(f"  ERROR: {str(d['error'])[:600]}")
        det = d.get("detector")
        if isinstance(det, dict) and det.get("verdict"):
            lines.append(f"  detector: {det.get('verdict')}")
        if d.get("log"):
            lines.append("  log:")
            for e in d["log"][-8:]:
                lines.append(f"    {e.get('created_at') or ''} {e.get('agent') or ''}: "
                             f"{str(e.get('what') or '')[:200]}")
        blocks.append("\n".join(lines))
    for tok, hits in ambiguous:
        blocks.append(f"'{tok}' matches {len(hits)} runs ({', '.join(hits)}) — ask which one "
                      f"rather than guessing.")
    if not blocks:
        return ""
    return ("\n\nRUN DETAIL (the operator named these runs — answer from this, it is the record, "
            "and quote the actual error rather than speculating):\n" + "\n\n".join(blocks))


# ── V2c · the intent router's handlers ───────────────────────────────────────────────────────
#
# Ported from `nomad-console/server.py`. The console reached these over HTTP because it was a
# different service from the engine. Here they are the engine, so they call `eng` directly: one
# less hop, one less thing that can be up while the thing behind it is down, and no way for a
# handler to drift from the endpoint it was supposed to mirror.


def _pending_runs(limit: int = 40) -> list:
    """Runs sitting at the gate, newest first. `recent_runs` already sorts by `-Id`."""
    return [r for r in recent_runs(limit) if r.get("status") == "awaiting-approval"]


def _handle_diagnostics() -> str:
    """What this box is doing, answered out of what the engine can actually see.

    The console's version listed every service up or down, and this one deliberately does not.
    nomad-watch probes all 17 targets on a five-minute timer and posts changes to Zulip `#alerts`,
    with one all-clear a day so a quiet channel means healthy rather than dead. A second prober
    answering on demand would be a second thing to keep in step with the target list, and it would
    disagree with the first one the moment they drifted. So this reports the pipeline, the card and
    the engine's own heartbeat, and says plainly that service health is somewhere else rather than
    implying it checked."""
    runs = recent_runs(25)
    counts = {}
    for r in runs:
        counts[r.get("status") or "?"] = counts.get(r.get("status") or "?", 0) + 1
    t = _proc_telemetry()
    g = t.get("gpu") or {}
    L = ["**NOMAD diagnostic report**", ""]

    cpu, mem = t.get("cpu_percent"), t.get("mem_percent")
    L.append(f"CPU {cpu:.0f}% · RAM {mem:.0f}%" if cpu is not None and mem is not None
             else "CPU/RAM: no reading available")
    # A card that will not answer a field reports nothing for it. Zero would claim it is idle.
    if g.get("util") is not None and g.get("mem_total"):
        line = (f"GPU {g.get('name','?')}: {g['util']:.0f}% util · "
                f"{g['mem_used']:.0f}/{g['mem_total']:.0f} MiB")
        if g.get("temp") is not None:
            line += f" · {g['temp']:.0f}°C"
        L.append(line)
    elif g.get("name"):
        L.append(f"GPU {g['name']}: present, but it would not report utilisation")
    else:
        L.append("GPU: no reading available (nvidia-smi did not answer)")

    total = sum(counts.values())
    if total:
        L.append("Pipeline: " + " · ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    else:
        L.append("Pipeline: idle, no runs recorded")
    pend = counts.get("awaiting-approval", 0)
    if pend:
        L.append(f"⚠ {pend} awaiting your decision")
    failed = counts.get("failed", 0)
    if failed:
        L.append(f"⚠ {failed} failed — say “retry” in the cockpit, or check the run detail")

    # Same heartbeat /health exposes, which is what nomad-watch alarms on. Reported here too
    # because a diagnostic that says nominal while the sweep thread is dead is the failure this
    # heartbeat exists to catch, and the person asking is the one who can act on it.
    age = time.time() - _BEATS["sweep"]
    L.append(f"Stall sweep last ran {age:.0f}s ago" +
             ("" if age < engine_mod.SWEEP_SECS * 3 else " ⚠ that is overdue"))

    hot = g.get("temp") is not None and g["temp"] >= GPU_HOT_C
    L += ["", ("Pipeline and hardware nominal, Commander." if not pend and not failed and not hot
               else "Attention required, see the flagged items above.")]
    L.append("_Service health is nomad-watch's job, in Zulip `#alerts`. This report did not probe "
             "it, so nothing here says the other services are up._")
    return "\n".join(L)


def _handle_pipeline_capture(goal: str) -> str:
    """Everything that would touch the outside world goes in here, which is the point.

    Research and imperative actions both land on capture rather than running: the console learned
    that the hard way, and a chat surface that can act without the gate is the gate not existing."""
    if not goal:
        return "Give me a goal, for example `/capture research the best local STT engine`."
    try:
        st = eng.start_run(goal)
    except Exception as e:
        return f"⚠ Capture failed: {str(e)[:200]}"
    _bump_runs()
    if st.get("error"):
        return f"Capture failed: {st['error']}"
    action = (st.get("proposal") or {}).get("action", "?")
    return (f"Captured and routed to **{st.get('lane','?')}** ({action}), "
            f"run `{st.get('run_id','')}`. "
            f"It is awaiting your decision. Approve it in the cockpit, react ✅ in Zulip if the "
            f"action allows it, or say **approve** here. Nothing runs until you do.")


# run_id → (proposal_hash, time) of the proposal as chat last showed it (#806). A chat approval
# executes only the version recorded here, so the engine's stale-proposal check compares what the
# operator saw with what is stored, not the stored proposal with itself.
_CHAT_SHOWN = {}
CHAT_PIN_SECS = 1800


def _chat_preview(run: dict, d: dict, h: str) -> str:
    """One pending run as chat shows it before an approval. Action and target come from the same
    read as the hash, so the version named is the version described."""
    p = run.get("proposal") or {}
    p = {**p, **{k: d[k] for k in ("action", "target") if d.get(k)}}
    if d.get("action") == "dispatch_build" and (d.get("args") or {}).get("project"):
        p["target"] = d["args"]["project"]      # the repo the build runs in, not the Manager's words
    text = " ".join(str(p.get("preview") or "").split())
    text = text[:280] + ("…" if len(text) > 280 else "")
    warn = d["project_warning"] if "project_warning" in d else p.get("warning") or ""
    return (f"- `{run.get('run_id','')}` · {run.get('lane','?')} · {p.get('action','?')} → "
            f"{p.get('target','') or '(no target)'} · version `{h[:8]}`" + (f"\n  {text}" if text else "")
            + (f"\n  ⚠ {warn}" if warn else ""))


def _handle_gate_decision(decision: str, do_all: bool) -> str:
    """Approve or reject at the gate, by voice or text.

    Three things differ from the console's version, all deliberate.

    It pins the proposal hash to what chat showed (#806). `resume_run` requires a hash for an
    approval, and reading it at the moment of approving would pin whatever the payload says now,
    which the operator may never have seen. So the first "approve" shows the proposal and records
    its hash, and only a second "approve" within half an hour executes, against that hash. If the
    proposal changed in between, the engine refuses, and the reply says so.

    A bare "approve" with several runs pending names them and asks, instead of taking the newest.
    Picking one silently is the shape of defect this project keeps paying for: the operator
    believes they approved the thing they were thinking about, and the gate approves something
    else. Ambiguity at a gate resolves to a question, not to a guess.

    "Approve all" approves nothing until every pending run has been shown."""
    pend = _pending_runs()
    if not pend:
        return "Nothing is awaiting your approval right now."
    if len(pend) > 1 and not do_all:
        listed = "\n".join(
            f"- `{r.get('run_id','')}` · {r.get('lane','?')} · "
            f"{(r.get('proposal') or {}).get('action','?')} → "
            f"{(r.get('proposal') or {}).get('target','') or '(no target)'}"
            for r in pend[:6])
        more = f"\n…and {len(pend) - 6} more." if len(pend) > 6 else ""
        return (f"{len(pend)} runs are waiting, so I am not guessing which one you mean:\n"
                f"{listed}{more}"
                f"\n\nSay **{'approve' if decision == 'approved' else 'reject'} all**, or "
                f"decide on the one you want in the cockpit.")

    targets = [a for a in (pend if do_all else pend[:1]) if a.get("run_id")]
    done, pins = [], {}
    if decision == "approved":
        now, unseen = time.time(), []
        for a in targets:
            rid = a["run_id"]
            pin = _CHAT_SHOWN.get(rid)
            if pin and now - pin[1] < CHAT_PIN_SECS:
                pins[rid] = pin[0]
                continue
            try:
                d = run_detail(rid) or {}
            except Exception:
                d = {}
            h = d.get("proposal_hash")
            if not h:
                done.append((a, {"error": "could not read the proposal, so not approving"}))
                continue
            _CHAT_SHOWN[rid] = (h, now)
            unseen.append(_chat_preview(a, d, h))
        if unseen:
            again = "approve all" if do_all else "approve"
            return (("This is what you would approve:\n" if len(unseen) == 1 else
                     "These are what you would approve:\n") + "\n".join(unseen) +
                    f"\n\nNothing is approved yet. Say **{again}** again to approve exactly "
                    f"{'this version' if len(unseen) == 1 else 'these versions'}.")
        targets = [a for a in targets if a["run_id"] in pins]

    for a in targets:
        rid = a["run_id"]
        try:
            res = eng.resume_run(rid, decision, pins.get(rid))
        except Exception as e:
            res = {"error": str(e)[:120]}
        _CHAT_SHOWN.pop(rid, None)
        done.append((a, res))
    _bump_runs()
    verb = "Approved" if decision == "approved" else "Rejected"
    lines, changed = [], False
    for a, res in done:
        st = res.get("status") or res.get("error") or "?"
        out = res.get("outcome") or res.get("error") or ""
        changed = changed or "changed since it was shown" in out
        p = a.get("proposal") or {}
        lines.append(f"- {p.get('action','action')} → **{st}**" + (f" — {out[:90]}" if out else ""))
    tail = ("\n\nA proposal changed after I showed it, so it was not approved. Say **approve** "
            "to see the new version.") if changed else ""
    return f"{verb} {len(done)} item(s):\n" + "\n".join(lines) + tail


ACTION_INTENTS = {
    "diagnostics": lambda p: _handle_diagnostics(),
    # Voice keeps these (operator decision on #967, 2026-10-01). Captures still stop at the gate.
    "gate_decision": lambda p: _handle_gate_decision(p.get("decision", "approved"),
                                                     p.get("all", False)),
    "pipeline_capture": lambda p: _handle_pipeline_capture(p.get("goal", "")),
    "action_capture": lambda p: _handle_pipeline_capture(p.get("request", "")),
}


# #973 (PRD cockpit-chat-tools 5.1). The chat gives information; which intents act depends on who
# is asking, and that is decided by the caller (the endpoint, a flag, the Zulip session), never by
# the message text. The typed cockpit box and Zulip's free text answer questions only. Voice keeps
# approve/reject and capture, because the operator has no button to press while talking. A list of
# what is allowed, so an intent added next month is information-only until someone decides otherwise.
SURFACE_INTENTS = {
    "cockpit": ("chat", "diagnostics"),
    "zulip": ("chat", "diagnostics"),
    "voice": ("chat", "diagnostics", "gate_decision", "pipeline_capture", "action_capture"),
}
CHAT_POINTER = ("I only answer questions here. To approve or reject, use the Approve or Reject "
                "button on the run. To start work, use ⚑ Capture.")

# R2a (#796). Through the tailnet proxy chat answers questions and nothing else. It is a list of
# what is allowed, not of what is refused, so an intent added next month is refused from the phone
# until someone decides it belongs here.
REMOTE_CHAT_INTENTS = ("chat", "diagnostics")
REMOTE_REFUSAL = ("From here I can answer questions. To start or approve work, use the Approve "
                  "buttons or the laptop.")

# #970 (PRD cockpit-chat-tools 4). The only tools the chat model can call, by name. Every one is
# read-only (#968, #969). A list of what is allowed, like REMOTE_CHAT_INTENTS: a tool added to
# chat_tools or chat_pm_tools next month is out of reach from chat until it is named here.
CHAT_TOOL_ALLOWLIST = (
    "pipeline_runs", "pipeline_run_detail", "approval_queue", "autopilot_runs",
    "autopilot_status", "autopilot_config", "system_health",
    "pm_search", "pm_get", "pm_tree", "pm_ready", "pm_project_summary", "pm_my_queue",
)
CHAT_NO_LOOKUP = "(I couldn't look anything up live just now, so this is from a snapshot.)"


def _chat_tools() -> dict:
    """The allowlisted tools, as runtime.run_tools takes them. A name on the list that no module
    offers, or that isn't marked read-only, is left out rather than guessed at."""
    import chat_tools
    import chat_pm_tools
    out = {}
    for mod in (chat_tools, chat_pm_tools):
        for name, t in mod.TOOLS.items():
            if name in CHAT_TOOL_ALLOWLIST and t.get("read_only") is True:
                out[name] = {"description": t["description"], "input_schema": t["input_schema"],
                             "call": lambda args, _m=mod, _n=name: _m.call(_n, args)}
    return out


def _chat_stopped_note(res: dict, max_turns: int, timeout_s: float) -> str:
    """What the operator is told when a tool turn hit a bound, so a cut answer never reads as a
    whole one and a turn with nothing to show still says something."""
    why = {"turns": f"I stopped after {max_turns} steps of looking things up",
           "time": f"I stopped after {timeout_s:g} seconds of looking things up"}[res["stopped"]]
    if res.get("reply"):
        return f"{res['reply']}\n\n({why}, so this may be incomplete.)"
    return f"{why} before I had an answer. Try a narrower question, or ask about one run or task."


def chat_reply(message: str, history=None, session_id: str = "cockpit", remote: bool = False,
               surface: str = "cockpit") -> dict:
    """Conversational turn grounded in live pipeline state + memory. Recalls relevant past
    conversations (cross-session, unified with the console) and the recent in-session history, then
    persists both sides of the turn. Memory is fail-open. `surface` is who is asking (see
    SURFACE_INTENTS); an unknown one is treated as the typed cockpit. A question goes to the model
    with CHAT_TOOL_ALLOWLIST as its only tools (#970); `tools` in the result names the ones it
    called."""
    import runtime
    import memory_chat
    # memory opt-out / forget — honored BEFORE the model runs (mirrors the console)
    intent = memory_chat.classify_memory_intent(message)
    if remote and intent in ("wipe", "forget_last"):
        return {"reply": REMOTE_REFUSAL, "remembered": False, "refused": True}
    if intent == "wipe":
        memory_chat.forget(session_id, scope="session")
        return {"reply": "Done — I've wiped this conversation from my memory.", "remembered": False}
    if intent == "forget_last":
        n = memory_chat.forget(session_id, scope="last")
        return {"reply": f"Forgotten — dropped the last {max(n, 0)} turn(s) from memory.",
                "remembered": False}
    # V2c · the action router, ahead of the model and after the memory commands.
    #
    # Order is the whole point. "Run diagnostics", "work on your backlog", "approve" are commands,
    # and a chat model handed a command answers it: it describes the diagnostic, or says it would be
    # happy to approve that for you. The console put this in front of the model for that reason and
    # it goes in front of the model here. It sits below the memory commands because "forget that"
    # is also a command and it is the one the operator is most entitled to have obeyed literally.
    #
    # An action turn is still remembered. "What did you just do" is a fair question and the answer
    # has to be in the same place as the rest of the conversation.
    i_action, payload = intents.classify(message)
    if remote and i_action not in REMOTE_CHAT_INTENTS:
        return {"reply": REMOTE_REFUSAL, "remembered": False, "refused": True, "intent": i_action}
    allowed = SURFACE_INTENTS.get(surface, SURFACE_INTENTS["cockpit"])
    if i_action != "chat" and i_action not in allowed:
        # A command on an information-only surface: the pointer, and never the model, so "approve"
        # can't come back as "Sure, approved!".
        if intent == "off_record":
            return {"reply": CHAT_POINTER, "remembered": False, "refused": True, "intent": i_action}
        memory_chat.remember(session_id, "user", message)
        memory_chat.remember(session_id, "assistant", CHAT_POINTER)
        return {"reply": CHAT_POINTER, "remembered": True, "refused": True, "intent": i_action}
    if i_action in ACTION_INTENTS:
        try:
            reply = ACTION_INTENTS[i_action](payload)
        except Exception as e:                  # a handler fault must not swallow the turn
            reply = f"⚠ {i_action} failed: {type(e).__name__}: {str(e)[:200]}"
        if intent == "off_record":
            return {"reply": reply, "remembered": False, "intent": i_action}
        memory_chat.remember(session_id, "user", message)
        memory_chat.remember(session_id, "assistant", reply)
        return {"reply": reply, "remembered": True, "intent": i_action}

    # long-term: semantically relevant turns from PAST conversations (any session but this one)
    mems = memory_chat.recall(message, k=5, exclude_session=session_id)
    mem_block = ""
    if mems:
        mem_block = "\n\nRECALLED MEMORY (relevant past conversations — treat as things you remember):\n" + \
            "\n".join(f"- {m['role']}: {m['content'][:200]}" for m in mems)
    # short-term: the recent in-session turns the client sends (continuity within this chat)
    hist_block = ""
    for t in (history or [])[-6:]:
        role = (t.get("role") or "").upper()
        hist_block += f"\n{role}: {(t.get('content') or '')[:400]}"
    tail = mem_block + (f"\n\nRECENT CONVERSATION:{hist_block}" if hist_block else "")
    intro = ("You are NOMAD, the orchestrator of a self-hosted pipeline (Capture→Clarify→Route→"
             "Process→Human Gate→Execute→Log&Learn) over a 5-lane inbox (comms/research/support/ads/"
             "dev). Answer the operator concisely. If they're handing you a task to act on, tell them "
             "to capture it (the ⚑ Capture button) so it enters the gate. Don't invent runs or claim "
             "you executed anything.")

    # #970: the tool turn (PRD 6, R1 to R4, R6). The model reads live state through the read-only
    # tools instead of a pasted snapshot. A question that needs no lookup is answered with none.
    tool_calls, reply = [], None
    try:
        res = runtime.run_tools(
            intro + " Your tools read the live pipeline, the approval queue, the autopilot, service "
            "health and the task board, and nothing you can call changes anything. Look things up "
            "rather than guess; a question that needs no lookup needs no tool. If a tool returns an "
            "error, say what failed and don't fill the gap." + tail,
            message, _chat_tools(), alias="balanced", max_turns=runtime.TOOL_MAX_TURNS,
            timeout_s=runtime.TOOL_TIMEOUT_S)
        tool_calls = res.get("tool_calls") or []
        if res.get("stopped"):
            reply = _chat_stopped_note(res, runtime.TOOL_MAX_TURNS, runtime.TOOL_TIMEOUT_S)
        elif res.get("reply"):
            reply = res["reply"]
    except Exception:
        reply = None                        # R5: no SDK, or it failed to start; fall back below
    if reply is None:
        # R5: no tools, today's pasted context, and the reply says it couldn't look things up.
        # a named run: the recent-runs summary carries status and proposal but never the error, so
        # "why did run X fail" could only be answered with a guess. Pull the actual record instead.
        try:
            run_block = _run_context(*_resolve_run_ids(message))
        except Exception:
            run_block = ""                      # a chat turn must not die because a lookup did
        system = (intro + " Answer about the CURRENT pipeline state shown below.\n\n"
                  f"LIVE PIPELINE STATE:\n{_pipeline_context()}{run_block}" + tail)
        try:
            reply = runtime.run(system, message, alias="balanced", max_tokens=400,
                                timeout_s=runtime.TOOL_TIMEOUT_S)
            reply = f"{reply}\n\n{CHAT_NO_LOOKUP}"
        except Exception as e:
            if isinstance(e, TimeoutError) or "timed out" in str(e).lower():
                reply = (f"(chat timed out: no answer within {runtime.TOOL_TIMEOUT_S:g} seconds. "
                         "Try again, or ask a narrower question.)")
            else:
                reply = f"(chat unavailable: {str(e)[:120]})"
    # 'off the record' → answer but store NOTHING; otherwise persist both sides for future recall
    tools_used = [c["name"] for c in tool_calls]      # what the answer was read from
    if intent == "off_record":
        return {"reply": reply, "remembered": False, "tools": tools_used}
    memory_chat.remember(session_id, "user", message)
    memory_chat.remember(session_id, "assistant", reply)
    return {"reply": reply, "remembered": True, "intent": "chat", "tools": tools_used}


def _delivery(run_id: str) -> list:
    """Outbox rows for this run: what was attempted, and how strong the evidence is that it left.

    `status` is empty on rows written before the column existed. That is reported as-is rather than
    guessed at, because those rows were stamped `delivered_at` whether or not the send worked and
    calling them delivered now would invent the fact the column exists to stop us inventing.

    `provider_id` is Gmail's own message id and is what separates `sent` from `accepted`: one is a
    receipt from the other end, the other is n8n saying it took the request. It is passed through so
    an operator can go and find the message rather than take this system's word for it.
    """
    try:
        rows = db.list("outbox", 20, where=f"(run_id,eq,{run_id})")
    except Exception:
        return []
    return [{"outbox_id": r.get("Id"), "to": r.get("to"),
             "status": r.get("status") or "unrecorded",
             "provider_id": r.get("provider_id") or "",
             "delivered_at": r.get("delivered_at"), "error": r.get("error") or ""}
            for r in rows]


# What a run actually filed, per sink table: the row's own title column, and the body column that
# holds the work. `send_message` is deliberately not here — the outbox has `_delivery`, because a
# mail's evidence is about whether it left, and a brief's is about whether it exists to read.
ARTIFACT_COLS = {"knowledge": ("topic", "brief"), "content": ("topic", "content")}
ARTIFACT_MAX = 8000


def _artifacts(run_id: str) -> list:
    """The rows a run filed to its sink, read back from NocoDB rather than inferred from status.

    A research run reads `executed` because `save_brief` returned, which is the same gap S6 found in
    the outbox: the tool returning is not the artifact existing. The console's answer was to hand the
    brief straight back in the chat, so the operator always saw the thing itself. Under the gate the
    brief is written after approval, minutes later and somewhere else, so the drill-down has to go
    and fetch it or the operator is trusting a status word again.

    The body is capped and says when it was cut. Quietly truncating a brief would leave the operator
    reading what looks like a complete document that stops early, which is worse than no brief."""
    out = []
    for table, (title_col, body_col) in ARTIFACT_COLS.items():
        try:
            rows = db.list(table, 10, where=f"(run_id,eq,{run_id})", sort="-Id")
        except Exception:
            continue                       # a missing sink table must not break the whole drill-down
        for r in rows:
            body = str(r.get(body_col) or "")
            out.append({"sink": table, "id": r.get("Id"), "title": r.get(title_col) or "",
                        "created_at": r.get("created_at"), "chars": len(body),
                        "body": body[:ARTIFACT_MAX],
                        "truncated": len(body) > ARTIFACT_MAX})
    return out


# The text a proposal exists to deliver. A dispatch_build's is `plan`; `task` is its one-line title.
_MAIN_ARGS = ("plan", "body", "brief", "content")


def arg_fields(args) -> list[dict]:
    """Every key of a proposal's args, in reading order, for the run detail to draw.

    The detail used to show the first of body/brief/content/task/prompt and nothing else, so a
    dispatch_build was approved on its one-line `task` while the plan the builder actually runs sat
    unseen. Nothing is dropped or cut here: an operator approves the whole of `args`, so the whole of
    it is shown. `subject` leads so a mail still reads subject-then-body; the main text follows,
    then the rest in stored order. `long` marks what gets a wrapped block instead of one line."""
    if not isinstance(args, dict):
        return [{"key": "args", "text": json.dumps(args, indent=2, default=str), "long": True}]
    order = [k for k in ("subject", *_MAIN_ARGS) if k in args]
    order += [k for k in args if k not in order]
    out = []
    for k in order:
        v = args[k]
        text = v if isinstance(v, str) else json.dumps(v, indent=2, ensure_ascii=False, default=str)
        out.append({"key": k, "text": text,
                    "long": k in _MAIN_ARGS or "\n" in text or len(text) > 120})
    return out


# wp id → (read at, facts). A cockpit with a detail open reads /run every tick, and a slow or
# down OpenProject would otherwise stall every one of those reads, so a miss is cached too.
_WP_FACTS: dict = {}
_WP_TTL = 60


def _wp_facts(wp_id: int) -> dict:
    hit = _WP_FACTS.get(wp_id)
    if hit and time.monotonic() - hit[0] < _WP_TTL:
        return hit[1]
    try:
        wp = eng.op.get(wp_id)
        link = wp["_links"].get("project") or {}
        pid = int(link["href"].rstrip("/").split("/")[-1])
        facts = {"wp_subject": wp.get("subject"), "project_name": link.get("title"),
                 "board": eng.op.call("GET", f"/projects/{pid}").get("identifier")}
    except Exception as e:
        facts = {"pm_error": str(e)[:120]}
    _WP_FACTS[wp_id] = (time.monotonic(), facts)
    return facts


def project_warning(payload: dict) -> str:
    """The gate's warning for a build whose project the dispatcher did not resolve (#851), or ''.

    The engine asks the dispatcher's registry while drafting and stores the answer as
    `project_check`. A run drafted before that check existed has none, and says so rather than
    passing as fine. Every gate surface (cockpit row, run detail, Zulip, chat) shows this one line,
    so they cannot disagree about it."""
    if payload.get("action") != "dispatch_build":
        return ""
    chk = payload.get("project_check")
    proj = str((payload.get("args") or {}).get("project") or "")
    if isinstance(chk, dict) and chk.get("ok") is True:
        return ""
    if isinstance(chk, dict) and chk.get("ok") is False:
        why = ("matches more than one registry project" if chk.get("reason") == "ambiguous"
               else "is not a project in the dispatcher's registry")
        cands = [str(c) for c in chk.get("candidates") or []]
        pick = (f" Candidates: {', '.join(cands[:12])}{' …' if len(cands) > 12 else ''}."
                if cands else "")
        return (f"Project '{proj}' {why}, so this build fails if approved.{pick} "
                f"Reject it and run the goal again naming one registry id.")
    err = (chk or {}).get("error") if isinstance(chk, dict) else ""
    return (f"Project '{proj}' was not checked against the registry"
            f"{f' ({err})' if err else ''}. The dispatcher still refuses a name it cannot "
            f"resolve when the build runs.")


def run_project(payload: dict, lane) -> dict | None:
    """Which project a run belongs to, so a gate message can say what it is about (#776).

    A run started from an OpenProject task names its work package; the board and the title are read
    from OpenProject, fail-open, because the gate must still post when the board is down. An older
    mission-control task has only its NocoDB id. A dev run names the repo it will edit. None means
    the run belongs to no project, which the relay says in words rather than leaving a blank."""
    out = {}
    ref, pm = payload.get("task_ref"), payload.get("task_pm")
    if ref and pm == engine_mod.OP:
        out = {"wp_id": int(ref), "wp_url": f"{pm_follow.PUBLIC_URL}/work_packages/{int(ref)}",
               **_wp_facts(int(ref))}
    elif ref:
        out = {"task_ref": ref}
    # The project the build will actually run in: args.project, which the engine rewrites to the
    # registry id when it resolves. `target` is the Manager's own wording, kept for older runs.
    repo = (payload.get("args") or {}).get("project") or payload.get("target")
    if lane == "dev" and repo:
        out["repo"] = repo
    return out or None


def run_detail(run_id: str) -> dict:
    """Full detail for one run: the routing (goal/intent/target), the full proposal payload, and
    the episodic log entries (provenance) for this run_id."""
    row = db.find("comms", "run_id", run_id) or {}
    try:
        payload = json.loads(row.get("payload") or "{}")
    except Exception:
        payload = {}
    log = []
    try:
        for e in db.list("episodic", 200):
            if e.get("run_id") == run_id:
                log.append({"agent": e.get("agent"), "what": e.get("what"),
                            "created_at": e.get("created_at")})
    except Exception:
        pass
    log.sort(key=lambda x: x.get("created_at") or "")
    return {"run_id": run_id, "lane": row.get("lane"), "status": row.get("status"),
            "created_at": row.get("created_at"),
            "from_agent": row.get("from_agent"), "goal": payload.get("goal"),
            "intent": payload.get("intent"), "target": payload.get("target"),
            "action": payload.get("action"), "args": payload.get("args", {}),
            "arg_fields": arg_fields(payload.get("args", {})),
            "error": payload.get("error"), "detector": payload.get("detector"),
            "project": run_project(payload, row.get("lane")),
            # Whether a build's project resolved in the dispatcher's registry (#851). '' when fine.
            "project_warning": project_warning(payload),
            # What the specialist looked up while drafting, so an operator can see where the
            # proposal's facts came from instead of taking the prose on faith.
            "lookups": payload.get("lookups", []),
            # Whether the thing actually left. A run reads `executed` because the tool returned,
            # and until this was recorded that was the only evidence an operator had. Filtered at
            # NocoDB, not scanned: it returns oldest-first, so a blind `limit` drops the newest rows.
            "delivery": _delivery(run_id),
            # What it filed, for the lanes whose output is a document rather than a message. Read
            # back from the sink, not inferred from `executed`.
            "artifacts": _artifacts(run_id),
            "proposal_hash": proposal_hash(payload) if row else None, "log": log}


def _gpu() -> dict | None:
    """The card, read here rather than borrowed from the console.

    `nvidia-smi` exists in this container only because compose gives the service an nvidia device
    reservation, the same one the console has. A reservation makes the device visible; it does not
    hold VRAM, so this costs nothing against the one resource that is actually scarce here.

    None means no reading was possible, which is the truthful answer when the binary is missing.
    It is not the same as an idle card, and the caller must not render it as one."""
    try:
        q = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
        out = subprocess.run(["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        f = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]

        def num(x):                      # laptop GPUs report some fields as "[N/A]"
            try:
                return float(x)
            except Exception:
                return None
        return {"name": f[0], "util": num(f[1]), "mem_used": num(f[2]), "mem_total": num(f[3]),
                "temp": num(f[4]), "power": num(f[5]), "power_limit": num(f[6])}
    except Exception:
        return None


def _proc_telemetry() -> dict:
    """Stdlib CPU%/MEM% from /proc, plus the GPU read directly. No console, no psutil."""
    out = {"cpu_percent": None, "mem_percent": None, "gpu": _gpu(), "source": "proc"}
    try:
        def _cpu_idle_total():
            with open("/proc/stat") as f:
                v = [float(x) for x in f.readline().split()[1:]]
            return v[3] + v[4], sum(v)            # idle+iowait, total
        i0, t0 = _cpu_idle_total(); time.sleep(0.2); i1, t1 = _cpu_idle_total()
        dt = (t1 - t0) or 1
        out["cpu_percent"] = round(100 * (1 - (i1 - i0) / dt), 1)
    except Exception:
        pass
    try:
        mt = ma = None
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):     mt = float(line.split()[1])
                elif line.startswith("MemAvailable:"): ma = float(line.split()[1])
                if mt and ma:
                    break
        if mt and ma:
            out["mem_percent"] = round(100 * (1 - ma / mt), 1)
            out["mem_total_gb"] = round(mt / 1e6, 1)
            out["mem_used_gb"] = round((mt - ma) / 1e6, 1)
    except Exception:
        pass
    return out


def _telemetry() -> dict:
    """CPU/MEM/GPU for the cockpit strip, read here first.

    This used to ask the console and only fall back to /proc, which made the v2 cockpit's telemetry
    strip a v1 dependency: stop the console and the GPU reading goes to None. Now the local read
    leads, and the console is consulted only when this container has no card visible to it, so an
    engine running without the nvidia reservation still shows a GPU while the console is alive. When
    the console goes, the strip keeps working."""
    local = _proc_telemetry()
    if local.get("gpu"):
        return local
    try:
        with urllib.request.urlopen(CONSOLE_URL + "/api/system", timeout=3) as r:
            d = json.loads(r.read()); d["source"] = "console"; return d
    except Exception:
        return local


def _extract_hook(body: dict):
    """Pull (run_id, status) out of a NocoDB after-update webhook payload (shape varies)."""
    data = body.get("data", body)
    rows = data.get("rows") or data.get("records") or []
    row = rows[0] if rows else (data.get("row") or data)
    if isinstance(row, list):
        row = row[0] if row else {}
    return row.get("run_id"), (row.get("status") or "").strip()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Gate poller + the two Postgres LISTEN bridges are blocking psycopg loops → run as threads.
    threading.Thread(target=_gate_loop, daemon=True).start()      # backup
    threading.Thread(target=_gate_listen, daemon=True).start()    # push (LISTEN/NOTIFY)
    threading.Thread(target=_runs_listen, daemon=True).start()    # live cockpit (SSE refresh)
    threading.Thread(target=_sweep_loop, daemon=True).start()     # fail abandoned runs
    print(f"NOMAD v2 engine (ASGI) on :{PORT} (gate: push+poll)", flush=True)
    yield


app = FastAPI(title="nomad-v2-engine", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                        "static")), name="static")
# Generated images (mcp_image saves PNGs here; the content table stores /images/<file> paths).
_IMG_DIR = os.environ.get("NOMAD_IMAGE_DIR",
                          os.path.join(os.path.dirname(os.path.abspath(__file__)), "generated_images"))
os.makedirs(_IMG_DIR, exist_ok=True)
app.mount("/images", StaticFiles(directory=_IMG_DIR), name="images")


# ── the login, for when the engine is reachable from outside ────────────────────────────────────
#
# Ported from the console with the failure direction reversed and the writes taken away.
#
# The console decides a request is external by spotting a proxy header (cf-connecting-ip,
# x-forwarded-for, …) and asks for a password only then. A proxy that sets none of them is therefore
# served with no password at all: the detection fails open. That was a risk on a dashboard. Here the
# same process hands out a shell on /ws/terminal, runs Claude Code in your repos on /api/dispatch,
# and approves irreversible actions on /resume, so the mode is explicit instead and the header sniff
# is only a backstop.
#
#   NOMAD_AUTH_MODE=on    every request needs Basic Auth. This is what publish.sh turns on, and it
#                         does not depend on any proxy setting any header.
#   NOMAD_AUTH_MODE=off   local use, no login. The default. A request that arrives carrying a proxy
#                         header is refused outright rather than prompted, because if the engine is
#                         not expecting to be exposed and something proxied reaches it, the
#                         configuration is wrong and the safe answer is no. An unwired branch must
#                         refuse, never pass.
#
# What is taken away matters more than the password does. When the engine is exposed it is
# **read-only**: /ws/terminal and /api/dispatch are closed outright, and so is every other write.
# The point of the tunnel is showing NOMAD to someone, which means handing them the URL and the
# password, and a shared password in front of a shell is not a boundary.
#
# Read-only is a method rule rather than a list of dangerous routes, because a list has to enumerate
# every door and the second door is the one nobody rereads. /resume approves a run; so does typing
# "approve" into /chat, since the intent router's gate handler calls resume_run directly. Naming
# /resume and stopping there would have left the gate open through the chat box. A method rule also
# closes a route added next month by default instead of leaving it open by omission.
#
# Nothing is lost by it. Approvals already go through Zulip over Tailscale, and a visitor watching
# the run feed sees real work move while the operator drives it from the tailnet.
#
#   NOMAD_AUTH_MODE=tailnet  the cockpit behind `tailscale serve` on 8449 (#795, PRD R2). Local
#                         requests work as with `off`. A proxied one must carry the operator's
#                         Tailscale-User-Login, which `tailscale serve` strips from the client and
#                         sets itself. It may write only to TAILNET_WRITES, and only with the
#                         cockpit's tailnet Origin, because any page open in the phone's browser can
#                         POST to a tailnet address. A Cloudflare header is refused outright: the
#                         identity header means something only when Tailscale set it, and a tunnel
#                         passes whatever the client sends.
#
# /health is exempt in every mode so nomad-watch keeps reporting.
AUTH_MODE = os.environ.get("NOMAD_AUTH_MODE", "off").strip().lower()
AUTH_USER = os.environ.get("NOMAD_AUTH_USER", "")
AUTH_PASS = os.environ.get("NOMAD_AUTH_PASS", "")
AUTH_OPEN_PATHS = ("/health",)
CLOSED_WHEN_EXPOSED = ("/api/dispatch", "/ws/terminal")
READ_ONLY_METHODS = ("GET", "HEAD")
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
TAILSCALE_HEADERS = ("tailscale-user-login", "tailscale-user-name", "tailscale-headers-info")
CLOUDFLARE_HEADERS = ("cf-connecting-ip", "cf-ray")
TAILNET_OPERATOR = os.environ.get("NOMAD_TAILNET_OPERATOR", "").strip().lower()
TAILNET_ORIGIN = os.environ.get("NOMAD_TAILNET_ORIGIN", "").strip().rstrip("/")
# /chat is here because R2a limits it to questions once it is inside; see REMOTE_CHAT_INTENTS.
# The autopilot's writes (#883, operator decision 2026-09-28): Run and Stop must work from the phone,
# and so must the budget switch and caps (#884). Exactly these routes, matched whole; the engine
# still checks the project id and the stop mode, and the supervisor the config fields.
# #890 adds the per-project ON/OFF switch (/api/autopilot/projects/<id>/autopilot), same id rule.
# #1407 (operator OK 2026-10-16): answering an agent's question or approval from the phone; the
# bridge still checks the ask id and refuses one that is no longer pending.
TAILNET_WRITES = ("/resume", "/retry", "/run-next-task", "/chat", "/api/asks/decide",
                  "/api/autopilot/pass", "/api/autopilot/config")
TAILNET_WRITE_PATTERNS = (
    re.compile(r"/api/autopilot/projects/[A-Za-z0-9][A-Za-z0-9._-]{0,63}/(?:start|stop|autopilot)"),
    # Answering a task in Awaiting approval from the phone (#911).
    re.compile(r"/api/autopilot/tasks/[0-9]{1,9}/decide"),)


def _tailnet_write_allowed(path: str) -> bool:
    return path in TAILNET_WRITES or any(p.fullmatch(path) for p in TAILNET_WRITE_PATTERNS)


def _via_proxy(headers) -> bool:
    """True when a request came through a proxy rather than from this machine."""
    return any(headers.get(h) for h in PROXY_HEADERS + TAILSCALE_HEADERS)


def _tailnet_check(headers, path: str, method: str) -> tuple:
    """The `tailnet` mode's half of _auth_check, for a request that came through a proxy."""
    if any(headers.get(h) for h in CLOUDFLARE_HEADERS):
        return 403, "This engine is on the tailnet, not on a public tunnel."
    if not (TAILNET_OPERATOR and TAILNET_ORIGIN):
        # Fails closed, like `on` without credentials.
        return 503, ("NOMAD_AUTH_MODE=tailnet but NOMAD_TAILNET_OPERATOR/NOMAD_TAILNET_ORIGIN are "
                     "not set, so nothing is served through the proxy.")
    login = (headers.get("tailscale-user-login") or "").strip().lower()
    if login != TAILNET_OPERATOR:           # a name Tailscale set, not a secret
        return 403, "Only the operator's tailnet login may use the cockpit."
    if path in CLOSED_WHEN_EXPOSED:
        return 403, "This route is closed through the tailnet proxy. Use the laptop."
    if method.upper() in READ_ONLY_METHODS:
        return 0, ""
    if not _tailnet_write_allowed(path):
        return 403, ("Through the tailnet the cockpit can only approve, reject, retry, ask, and run "
                     "or stop the autopilot.")
    if (headers.get("origin") or "").rstrip("/") != TAILNET_ORIGIN:
        return 403, "Refused: this write did not come from the cockpit's own page."
    return 0, ""


def _auth_check(headers, path: str, method: str = "GET") -> tuple:
    """(status, message) when a request must be refused, or (0, "") when it may proceed.

    Takes a plain mapping rather than a Request so the HTTP middleware and the websocket handlers
    share one implementation. The console has two copies of this rule, and the copy guarding the
    shell is the one nobody rereads."""
    if path in AUTH_OPEN_PATHS:
        return 0, ""
    if AUTH_MODE == "on":
        if path in CLOSED_WHEN_EXPOSED:
            return 403, ("This route is closed while the engine is externally reachable. "
                         "Reach it over Tailscale instead.")
        # Exposed means read-only. Naming the dangerous routes instead was the first version of this
        # and it was wrong: /resume approves a run, and so does typing "approve" into /chat, because
        # the intent router's gate handler calls resume_run directly. A closed list has to enumerate
        # every door, and the second door is the one nobody rereads. CLAUDE.md already says a resume
        # URL is a bearer capability and must never be published; a shared demo password does not
        # change what it is.
        #
        # Method, not a path list, so a route added next month is closed by default rather than open
        # by omission. An unwired branch must refuse, never pass. Approvals keep going through Zulip
        # over Tailscale, which is where they already live, and a visitor watching the feed sees real
        # runs move while the operator drives from the tailnet.
        if method.upper() not in READ_ONLY_METHODS:
            return 403, ("The engine is read-only while it is externally reachable. Anything that "
                         "executes, spends or approves stays on the tailnet.")
        if not (AUTH_USER and AUTH_PASS):
            # Fails closed. A login asked for and not configured has to lock the door, not leave it
            # open with a note on it.
            return 503, ("NOMAD_AUTH_MODE=on but NOMAD_AUTH_USER/NOMAD_AUTH_PASS are not set, "
                         "so nothing is served.")
        hdr = headers.get("authorization", "")
        if not hdr.startswith("Basic "):
            return 401, "Authentication required"
        try:
            user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
            # Both comparisons always run: `&`, not `and`. A wrong user and a wrong password should
            # take the same time as each other.
            ok = (secrets.compare_digest(user, AUTH_USER)
                  & secrets.compare_digest(pw, AUTH_PASS))
        except Exception:
            ok = False
        return (0, "") if ok else (401, "Authentication required")
    if AUTH_MODE == "tailnet":
        return _tailnet_check(headers, path, method) if _via_proxy(headers) else (0, "")
    if _via_proxy(headers):
        return 403, ("This engine is not configured for external access. Set NOMAD_AUTH_MODE=on "
                     "with NOMAD_AUTH_USER and NOMAD_AUTH_PASS before exposing it.")
    return 0, ""


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    status, msg = _auth_check(request.headers, request.url.path, request.method)
    if status:
        headers = {"WWW-Authenticate": 'Basic realm="NOMAD"'} if status == 401 else {}
        return Response(msg, status_code=status, headers=headers)
    return await call_next(request)


async def _ws_refused(websocket: WebSocket) -> bool:
    """Starlette's HTTP middleware never sees the websocket scope, so the check is re-applied here.

    A close before `accept` is answered as an HTTP error at the handshake, so the browser gets a
    failed connection rather than an open socket that says nothing."""
    # "WEBSOCKET" is not a read-only method, so every socket closes when the engine is exposed, not
    # only the one with the shell on it. /ws/wake streams a stranger's audio into nomad-voice.
    status, _ = _auth_check(websocket.headers, websocket.url.path, "WEBSOCKET")
    if status:
        await websocket.close(code=1008)      # policy violation
        return True
    return False


@app.get("/projects")
async def projects():
    """Discovered Nomad.md projects (proxied from the Builder dispatcher) — for the terminal picker."""
    def _f():
        try:
            import requests
            return requests.get(f"{DISPATCH_URL}/projects",
                                headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=5).json()
        except Exception:
            return {"projects": []}
    return await asyncio.to_thread(_f)


def _registry_detail() -> list | None:
    """The dispatcher's /projects detail rows, or None when it can't be asked."""
    try:
        import requests
        r = requests.get(f"{DISPATCH_URL}/projects", headers={"X-Nomad-Token": DISPATCH_TOKEN},
                         timeout=5)
        r.raise_for_status()
        detail = r.json().get("detail")
        return detail if isinstance(detail, list) else None
    except Exception:
        return None


@app.get("/owui/models")
async def owui_models(request: Request):
    """The Open WebUI pipe's dropdown: projects, + New project, General (local). #1114."""
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    return owui.models(await asyncio.to_thread(_registry_detail))


def _project_context(pid: str) -> dict | None:
    """The dispatcher's /project-context for one registry id; None when it doesn't know it."""
    import requests
    r = requests.get(f"{DISPATCH_URL}/project-context", params={"id": pid},
                     headers={"X-Nomad-Token": DISPATCH_TOKEN}, timeout=5)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


OWUI_TURNS = "owui_turns"
_owui_table = {"ok": False}


def _owui_log(row: dict):
    """One routed turn into NocoDB, for reviewing routing quality later (§6.2 step 5, §9)."""
    if not _owui_table["ok"]:
        cols = [{"column_name": k, "title": k, "uidt": u} for k, u in (
            ("chat_id", "SingleLineText"), ("entry", "SingleLineText"),
            ("project", "SingleLineText"), ("local_only", "Checkbox"),
            ("routed", "SingleLineText"), ("routed_by", "SingleLineText"),
            ("reason", "SingleLineText"), ("served", "SingleLineText"),
            ("fallbacks", "SingleLineText"), ("needs_tools", "Checkbox"),
            ("note", "LongText"), ("error", "LongText"), ("route_ms", "Number"),
            ("total_ms", "Number"), ("chars", "Number"), ("at", "SingleLineText"))]
        db.create_table(OWUI_TURNS, cols)
        _owui_table["ok"] = True
    db.create(OWUI_TURNS, {**row, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})


OWUI_CHATS = "owui_chats"
_owui_chats = {"ok": False, "cache": {}}


def _owui_bound(chat_id: str):
    """The entry an Open WebUI chat is bound to (§6.5), or None. Bindings don't change once
    set, except by M3's + New project rename, which goes through _owui_bind, so they cache."""
    cache = _owui_chats["cache"]
    if chat_id not in cache:
        _owui_chats_table()
        row = db.find(OWUI_CHATS, "chat_id", chat_id)
        if not row:
            return None
        cache[chat_id] = (row.get("Id"), row.get("entry"))
    return cache[chat_id][1]


def _owui_bind(chat_id: str, entry: str):
    _owui_chats_table()
    at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    have = _owui_chats["cache"].get(chat_id)
    if have and have[0]:
        db.update(OWUI_CHATS, have[0], {"entry": entry, "at": at})
        rid = have[0]
    else:
        rid = (db.create(OWUI_CHATS, {"chat_id": chat_id, "entry": entry, "at": at}) or {}).get("Id")
    _owui_chats["cache"][chat_id] = (rid, entry)


def _owui_chats_table():
    if not _owui_chats["ok"]:
        db.create_table(OWUI_CHATS, [{"column_name": k, "title": k, "uidt": "SingleLineText"}
                                     for k in ("chat_id", "entry", "at")])
        _owui_chats["ok"] = True


OWUI_NEW_PROJECTS = "owui_new_projects"
_owui_np = {"ok": False}
OWUI_PRD_TOKENS = 4000
OWUI_PRD_TIMEOUT_S = 300


def _owui_np_table():
    if not _owui_np["ok"]:
        db.create_table(OWUI_NEW_PROJECTS, [
            {"column_name": k, "title": k, "uidt": t}
            for k, t in (("chat_id", "SingleLineText"), ("stage", "SingleLineText"),
                         ("project", "SingleLineText"), ("state", "LongText"),
                         ("at", "SingleLineText"))])
        _owui_np["ok"] = True


def _owui_np_load(chat_id: str):
    """A + New project chat's state (owui_newproject, #1151), or None for a new chat."""
    _owui_np_table()
    row = db.find(OWUI_NEW_PROJECTS, "chat_id", chat_id)
    return json.loads(row["state"]) if row and row.get("state") else None


def _owui_np_save(chat_id: str, state: dict):
    _owui_np_table()
    fields = {"stage": state.get("stage", ""), "project": state.get("id", ""),
              "state": json.dumps(state),
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    row = db.find(OWUI_NEW_PROJECTS, "chat_id", chat_id)
    if row:
        db.update(OWUI_NEW_PROJECTS, row["Id"], fields)
    else:
        db.create(OWUI_NEW_PROJECTS, {"chat_id": chat_id, **fields})


OWUI_TOOL_TURNS = 10
OWUI_TOOL_TIMEOUT_S = 180


def _owui_tools(info: dict) -> dict:
    """The chat's owui_tools (#1146): green writes on its project's board, gated starts through
    the engine. Every run started here is a gate run, so the cockpit's counts move with it."""
    import owui_tools
    from pm_openproject import OpenProject

    def started(st):
        _bump_runs()
        return st

    def run_next(board):
        st = eng.run_next_task(None, OpenProject(project=board).project()["id"])
        pm_board.invalidate_projects()
        return started(st)

    return owui_tools.build(owui_tools.Actions(
        board=info.get("board") or "",
        op=lambda b: OpenProject(project=b) if b else OpenProject(),
        capture=lambda goal: started(eng.start_run(goal)),
        run_task=lambda i: started(eng.run_task(i)), run_next=run_next))


def _owui_draft(alias, system, user):
    import llm
    return llm.chat(alias, system, user, max_tokens=OWUI_PRD_TOKENS, timeout=OWUI_PRD_TIMEOUT_S,
                    extra={"disable_fallbacks": True} if alias == "private" else None)


def _owui_profiles() -> list:
    """The dispatcher's project types (GET /profiles, #1373); [] when it can't be asked."""
    try:
        import requests
        r = requests.get(f"{DISPATCH_URL}/profiles", headers={"X-Nomad-Token": DISPATCH_TOKEN},
                         timeout=5)
        r.raise_for_status()
        got = r.json()
        return got["profiles"] if got.get("ok") and isinstance(got.get("profiles"), list) else []
    except Exception:
        return []


def _owui_upsert_project(row: dict):
    """The mission-control row (§6.4 step 3.6), as nomad-console/sync_projects.py writes it."""
    have = db.find("projects", "title", row["title"])
    if have:
        db.update("projects", have["Id"], {k: v for k, v in row.items() if k != "title"})
    else:
        db.create("projects", row)


def _owui_refresh(chat_id: str, pid: str) -> str:
    """The new project in this chat and in the dropdown (§6.4 step 3.7)."""
    import owui
    if chat_id:
        _owui_bind(chat_id, owui.PROJECT_PREFIX + pid)
    pm_board.invalidate_projects()
    rows = _registry_detail()
    if rows is not None and pid not in {r.get("id") for r in rows}:
        return ("the dispatcher doesn't list it yet: ~/nomad may be on another branch than "
                "master, so it shows up once that checkout is back on master")
    return ""


def _owui_setup(state: dict, save, chat_id: str):
    import owui_setup
    from pm_openproject import OpenProject
    return owui_setup.run(owui_setup.Setup(
        dispatch=_dispatcher, op=lambda b: OpenProject(project=b), plan=_owui_draft,
        upsert_project=_owui_upsert_project, refresh=_owui_refresh), state, save, chat_id)


# ── create a project from answers given at once (#1374) ────────────────────────────────
# The dispatcher's POST /create-project and the create_project MCP tool end here, in the same
# module and with the same setup as the chat. A run's state is saved per step in
# owui_new_projects under new:<id>, so the same call again resumes where it stopped. The steps
# take minutes (the board seed alone can take 15), so the run goes on in a thread and
# GET /owui/create-project/<id> reports it.
CREATE_PREFIX = "new:"
_creating = {}                      # id -> {"running": bool, "lines": [...]} since the engine started
_creating_lock = threading.Lock()


def _create_status(pid: str, state: dict | None) -> dict:
    run = _creating.get(pid) or {}
    st = state or {}
    return {"ok": True, "id": pid, "stage": st.get("stage") or "", "steps": st.get("steps") or [],
            "running": bool(run.get("running")), "lines": run.get("lines") or []}


def _create_run(pid: str, inputs: dict, state: dict):
    from nomad_projects import create
    key, run = CREATE_PREFIX + pid, _creating[pid]
    try:
        for piece in create.create(inputs, state, taken=None, save=lambda st: _owui_np_save(key, st),
                                   setup=lambda st, save: _owui_setup(st, save, "")):
            run["lines"].append(piece.strip())
    except Exception as e:
        run["lines"].append(f"⚠ {type(e).__name__}: {str(e)[:200]}")
    finally:
        run["running"] = False


def owui_create(inputs: dict) -> tuple:
    """(status code, body). Checks the answers before anything starts: a refused one comes back
    at once, and nothing is made. Otherwise the steps run in a thread."""
    from nomad_projects import create
    inputs = inputs if isinstance(inputs, dict) else {}
    pid = create.slug(str(inputs.get("name") or ""))
    if not create.ID.match(pid):
        return 400, {"ok": False, "error": "A project needs a name that makes a usable id "
                                           "(letters, digits and dashes, 2-40)."}
    with _creating_lock:
        if (_creating.get(pid) or {}).get("running"):
            return 200, _create_status(pid, _owui_np_load(CREATE_PREFIX + pid))
        state = _owui_np_load(CREATE_PREFIX + pid) or {}
        if state.get("stage") not in ("setup", "done"):
            rows = _registry_detail()
            st, why = create.prepare(inputs, taken=None if rows is None else {r.get("id") for r in rows},
                                     profiles=_owui_profiles())
            if why:
                return 400, {"ok": False, "id": pid, "error": why}
            state = st
            _owui_np_save(CREATE_PREFIX + pid, state)
        if state["stage"] == "done":
            return 200, _create_status(pid, state)
        _creating[pid] = {"running": True, "lines": []}
    threading.Thread(target=_create_run, args=(pid, inputs, state), daemon=True).start()
    return 202, _create_status(pid, state)


def _owui_rows(table: str, field: str, value: str) -> list:
    """Every NocoDB row of `table` whose `field` equals `value`; none when the table isn't there."""
    where = urllib.parse.quote(f"({field},eq,{value})", safe="(),")
    try:
        return db.list(table, 500, where=where)
    except KeyError:                        # no such table yet: no rows
        return []


def _owui_initiative(pid: str, name: str):
    """The Initiative in nomad-portfolio for a project: the id its + New project run saved, else
    the one Initiative of that name. None when there is none or the name is ambiguous."""
    import owui_delete
    for r in _owui_rows(OWUI_NEW_PROJECTS, "project", pid):
        if str(r.get("chat_id") or "").startswith(owui_delete.STATE_PREFIX):
            continue
        try:
            found = ((json.loads(r.get("state") or "{}")).get("roadmap") or {}).get("initiative")
        except ValueError:
            found = None
        if found:
            return int(found)
    from pm_openproject import OpenProject
    named = [w["id"] for w in OpenProject(project=owui_delete.PORTFOLIO).project_wps()
             if w.get("subject") == name and ((w.get("_links") or {}).get("type") or {}).get("title") == "Initiative"]
    return named[0] if len(named) == 1 else None


def _owui_gate(pid: str, listing: str) -> str:
    """File the delete at the human gate, as a run the cockpit and the chat's `approve` both see."""
    import owui_delete
    st = eng.file_gate(f"Delete project {pid} from an Open WebUI chat", owui_delete.ACTION,
                       {"project": pid, "summary": "repo to ~/archive, registry entry, board and stream "
                                                   "archived, NocoDB rows removed"},
                       target=pid, preview=listing)
    _bump_runs()
    return st["run_id"]


def _owui_forget(chat_id: str, pid: str) -> str:
    """The deleted project out of the dropdown and out of every cache (§6.6 step 6)."""
    import owui
    for cid, (_rid, entry) in list(_owui_chats["cache"].items()):
        if entry == owui.PROJECT_PREFIX + pid:
            _owui_chats["cache"].pop(cid, None)
    pm_board.invalidate_projects()
    rows = _registry_detail()
    if rows is not None and pid in {r.get("id") for r in rows}:
        return "the dispatcher still lists it: ~/nomad may be on another branch than master"
    return ""


def _owui_drop_state(pid: str):
    import owui_delete
    for r in _owui_rows(OWUI_NEW_PROJECTS, "project", pid):     # a create run's too (#1374)
        if str(r.get("chat_id") or "").startswith((owui_delete.STATE_PREFIX, CREATE_PREFIX)):
            db.delete(OWUI_NEW_PROJECTS, r["Id"])


def _owui_delete(text: str, bound: str, chat_id: str):
    """`delete project <id>` in an Open WebUI chat (owui_delete, #1200)."""
    import owui_delete
    from pm_openproject import OpenProject
    key = owui_delete.STATE_PREFIX + chat_id
    d = owui_delete.Delete(
        dispatch=_dispatcher, op=lambda b: OpenProject(project=b), rows=_owui_rows,
        drop_row=db.delete, initiative=_owui_initiative, file_gate=_owui_gate,
        gate_status=lambda rid: (db.find("comms", "run_id", rid) or {}).get("status"),
        refresh=_owui_forget, drop_state=_owui_drop_state)
    return owui_delete.turn(text, bound, chat_id, d, lambda: _owui_np_load(key),
                            lambda st: _owui_np_save(key, st))


def _owui_deps():
    import chat_pm_tools
    import llm
    import memory_chat
    import owui_chat
    import runtime
    return owui_chat.Deps(
        registry=_registry_detail, fetch_context=_project_context, search=chat_pm_tools.pm_search,
        complete=lambda alias, system, user, timeout: llm.chat(
            alias, system, user, max_tokens=120, timeout=timeout,
            extra={"disable_fallbacks": True, "temperature": 0}),
        stream=llm.stream,
        recall=lambda q, s: memory_chat.recall(q, k=5, exclude_session=s),
        remember=memory_chat.remember, log=_owui_log, bound=_owui_bound, bind=_owui_bind,
        tools=_owui_tools, complete_tools=llm.complete,
        # The container runs on LiteLLM (NOMAD_RUNTIME=litellm), so cloud tool turns there go to
        # a local model; on a host with the SDK they go to Claude.
        run_tools=(lambda system, user, tools, alias: runtime.run_tools(
            system, user, tools, alias=alias, max_turns=OWUI_TOOL_TURNS,
            timeout_s=OWUI_TOOL_TIMEOUT_S)) if runtime.USE_SDK else None,
        gate=_handle_gate_decision, np_load=_owui_np_load, np_save=_owui_np_save,
        draft=_owui_draft, setup=_owui_setup, delete=_owui_delete, profiles=_owui_profiles)


@app.post("/owui/chat")
async def owui_chat_turn(request: Request):
    """One Open WebUI turn, streamed as JSON lines (owui_chat.turn). #1117."""
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    import owui_chat
    body = await _json(request)
    events = owui_chat.turn(body, _owui_deps())
    return StreamingResponse((json.dumps(e) + "\n" for e in events),
                             media_type="application/x-ndjson")


@app.post("/owui/context")
async def owui_context_for_filter(request: Request):
    """The project context the Open WebUI filter prepends for a model the operator picked
    (owui_chat.context, §6.5). Body: {chat_id, entry, model}."""
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    import owui_chat
    body = await _json(request)
    return await asyncio.to_thread(owui_chat.context, body, _owui_deps())


def _owui_dispatch(body: dict) -> dict:
    """The Dispatch panel's calls (owui_dispatch, #1441). Body: {op: items|status|dispatch,
    entry, ids?, id?}. The board is the chat's project's, or nomad-work for General."""
    import owui_chat
    import owui_dispatch
    from pm_openproject import OpenProject
    entry = str(body.get("entry") or "general")
    info = owui_chat.entry_info(entry, _registry_detail() if entry.startswith("p-") else [])
    board = info.get("board") or owui_dispatch.DEFAULT_BOARD
    op = OpenProject(project=board)

    def run_status(rid):
        return (db.find("comms", "run_id", rid) or {}).get("status") or ""

    kind = body.get("op")
    if kind == "items":
        return owui_dispatch.items(op, run_status, board)
    if kind == "status":
        return owui_dispatch.status(op, run_status, list(body.get("ids") or []))
    if kind == "dispatch":
        def run_task(i):
            st = eng.run_task(i)
            if isinstance(st, dict) and st.get("run_id"):
                _bump_runs()
                pm_board.invalidate_projects()
            return st
        return owui_dispatch.dispatch(op, run_task, run_status, body.get("id"))
    return {"ok": False, "error": "op must be items, status or dispatch"}


@app.post("/owui/dispatch")
async def owui_dispatch_call(request: Request):
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    return await asyncio.to_thread(_owui_dispatch, await _json(request))


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


@app.post("/owui/create-project")
async def owui_create_project(request: Request):
    """Set a project up from answers given at once (nomad_projects.create.INPUTS, #1374): the
    dispatcher's POST /create-project and the create_project MCP tool call this. 202 when the
    steps started, 200 when they already run or are done, 400 with the refusal."""
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    code, body = await asyncio.to_thread(owui_create, await _json(request))
    return JSONResponse(body, status_code=code)


@app.get("/owui/create-project/{pid}")
async def owui_create_project_status(pid: str, request: Request):
    status, msg = owui.check(request.headers.get("x-nomad-token"))
    if status:
        return JSONResponse({"ok": False, "error": msg}, status_code=status)
    state = await asyncio.to_thread(_owui_np_load, CREATE_PREFIX + pid)
    if state is None and pid not in _creating:
        return JSONResponse({"ok": False, "id": pid, "error": "no create run for that id"},
                            status_code=404)
    return JSONResponse(_create_status(pid, state))


# ── reads ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.get("/cockpit", response_class=HTMLResponse)
async def cockpit(request: Request):
    # R2a: through the proxy chat only answers questions, so the controls that start work go.
    if _via_proxy(request.headers):
        return HTMLResponse(COCKPIT.replace("</head><body>", "</head><body class=remote>", 1))
    return HTMLResponse(COCKPIT)


# Last time each background thread finished real work. The container being up says nothing about
# whether these threads are still running, and a dead sweep thread is silent: runs abandoned in
# `new` would simply stop being failed, with nobody waiting on them. /health reports the age so
# nomad-watch can alarm on staleness instead of on reachability.
_BEATS = {"sweep": time.time()}


@app.get("/health")
async def health():
    import runtime
    now = time.time()
    return {"status": "ok", "service": "nomad-v2-engine", "runtime": runtime.active(),
            "gate": "push+poll", "sweep_secs": engine_mod.SWEEP_SECS,
            "sweep_age_s": int(now - _BEATS["sweep"])}


GPU_HOT_C = float(os.environ.get("NOMAD_GPU_HOT_C", "85"))


@app.get("/briefing")
async def briefing():
    """Things worth speaking up about, in the console's alert shape so the relay can just switch.

    It returns one kind of alert, and that is deliberate rather than unfinished. The console's
    briefing raised three, and the other two are already covered by something that does it better:
    nomad-watch probes all 17 services on a timer and posts to the same Zulip channel, and the relay
    raises its own alert on a failed run. The relay already discards the `approvals:` alerts, because
    the v2 gate speaks for itself. Rebuilding any of that here would only double-post.

    A card that cannot be read reports nothing rather than a reassuring zero: `_gpu` returns None
    when there is no reading, and silence about an unknown temperature is honest where a green light
    would not be."""
    alerts = []
    g = await asyncio.to_thread(_gpu)
    if g and g.get("temp") and g["temp"] >= GPU_HOT_C:
        alerts.append({"key": f"gpu_hot:{int(g['temp'])}", "severity": "warn",
                       "text": f"GPU temperature is {g['temp']:.0f}°C — running hot."})
    return {"alerts": alerts, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/api/chat")
async def api_chat(request: Request):
    """The console's chat contract, so a client written against v1 can point here unchanged.

    nomad-voice speaks `{messages:[…], model, session_id} → {reply}` because it was written against
    the console. Its own `/chat` takes a single `message` plus `history`, so rather than edit the
    voice service and every other v1-shaped caller, the engine accepts both shapes. The last user
    message is the turn; everything before it is history. `model` is ignored: lane and alias choice
    belongs to the engine, and honoring a caller-supplied model would let a client route around it."""
    req = await _json(request)
    msgs = [m for m in (req.get("messages") or []) if m.get("content")]
    turn = next((m for m in reversed(msgs) if (m.get("role") or "user") == "user"), None)
    if not turn:
        return {"reply": ""}
    history = [m for m in msgs if m is not turn and (m.get("role") or "") != "system"]
    # nomad-voice is this endpoint's caller, so its turns are the voice surface (#973).
    return await asyncio.to_thread(chat_reply, (turn.get("content") or "").strip(), history,
                                   req.get("session_id") or "voice", False, "voice")


@app.get("/runs")
async def runs():
    return {"runs": await asyncio.to_thread(recent_runs)}


@app.get("/telemetry")
async def telemetry():
    return await asyncio.to_thread(_telemetry)


@app.get("/run")
async def run(id: str = ""):
    return await asyncio.to_thread(run_detail, id) if id else {"error": "id required"}


@app.get("/cockpit/run/{run_id}", response_class=HTMLResponse)
async def cockpit_run(run_id: str):
    """One run as a page, for the phone (#799). /run stays the JSON for API callers."""
    if not RUN_ID_RE.match(run_id):
        return HTMLResponse(run_missing_page(run_id), status_code=404)
    d = await asyncio.to_thread(run_detail, run_id)
    if not d.get("status"):
        return HTMLResponse(run_missing_page(run_id), status_code=404)
    return HTMLResponse(run_page(d))


# ── pending asks (#1363): the cockpit's answer channel for the agent bridge's inline asks ─────────
# Auth is the middleware's, like every other cockpit action: POST is refused while the engine is
# exposed (`on`) and allowed on the tailnet allowlist with the operator's login and Origin.
@app.get("/cockpit/asks", response_class=HTMLResponse)
async def cockpit_asks_page():
    return HTMLResponse(cockpit_asks.PAGE)


@app.get("/api/asks")
async def api_asks(state: str = ""):
    status, out = await asyncio.to_thread(cockpit_asks.list_asks, state)
    return JSONResponse(out, status_code=status)


@app.post("/api/asks/decide")
async def api_asks_decide(request: Request):
    try:
        req = await request.json()
    except Exception:
        req = None
    if not isinstance(req, dict):
        return JSONResponse({"ok": False, "error": "JSON body required"}, status_code=400)
    status, out = await asyncio.to_thread(cockpit_asks.decide, req)
    return JSONResponse(out, status_code=status)


async def _sse_gen():
    """SSE: 'bump' whenever the runs feed changes; ': ping' heartbeat. Polls the runs version at
    0.5 s (imperceptible for a cockpit refresh; the gate itself stays sub-second via LISTEN)."""
    yield "retry: 3000\n\ndata: hello\n\n"
    last = _runs_ver
    while True:
        for _ in range(50):                      # ~25 s, checking every 0.5 s
            await asyncio.sleep(0.5)
            if _runs_ver != last:
                break
        if _runs_ver != last:
            last = _runs_ver
            yield "data: bump\n\n"
        else:
            yield ": ping\n\n"


@app.get("/events")
async def events():
    return StreamingResponse(_sse_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


# ── writes (blocking engine/LLM/NocoDB work → offloaded to threads) ──
@app.post("/capture")
async def capture(request: Request):
    req = await _json(request)
    st = await asyncio.to_thread(eng.start_run, req["goal"]); _bump_runs()
    return st


@app.post("/resume")
async def resume(request: Request):
    req = await _json(request)
    r = await asyncio.to_thread(eng.resume_run, req["run_id"], req.get("decision", "rejected"),
                                req.get("expect_hash"))
    _bump_runs()
    return r


@app.post("/generate-image")
async def generate_image_direct(request: Request):
    """Ungated, LOCAL-ONLY image generation for in-repo Claude Code (the nomad-image MCP bridge).
    Forces the ComfyUI provider — free + reversible (file left uncommitted), consistent with the
    edit-uncommitted guardrail. Cloud/spend backends (OpenAI/Firefly) stay behind the human gate
    via the normal /capture pipeline. `save_hint` ("<project>/<subdir>") routes the file into a repo; `style` names a preset in
    prompts/image_styles.json (e.g. "nomad-blueprint", the Nomad Lab LinkedIn look)."""
    req = await _json(request)
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt required"}
    run_id = "direct-" + str(int(time.time()))
    return await asyncio.to_thread(generate_image_impl, prompt, run_id, "comfyui",
                                   req.get("save_hint"), req.get("style"))


@app.post("/retry")
async def retry(request: Request):
    req = await _json(request)
    _resolved.discard(req.get("run_id"))         # let the gate act on it again
    r = await asyncio.to_thread(eng.retry_run, req["run_id"]); _bump_runs()
    return r


@app.post("/withdraw")
async def withdraw(request: Request):
    """{run_id, reason}: close a stray proposal without deciding it. Unlike rejecting, it leaves the
    task's status alone (#990). Local only: it is not in TAILNET_WRITES."""
    req = await _json(request)
    r = await asyncio.to_thread(eng.withdraw_run, req["run_id"], req.get("reason", ""))
    _bump_runs()
    return r


@app.post("/chat")
async def chat(request: Request):
    req = await _json(request)
    msg = (req.get("message") or "").strip()
    low = msg.lower()
    remote = _via_proxy(request.headers)
    session = req.get("session_id") or "cockpit"
    prefixed = low.startswith(("project:", "capture:")) or msg.startswith("⚑") or req.get("capture")
    if remote and prefixed:
        return {"reply": REMOTE_REFUSAL, "refused": True}
    # #973. The surface comes from the caller: nomad-relay's session is Zulip, the cockpit page
    # flags a spoken turn as voice, and everything else is the typed box. Only Zulip keeps the
    # explicit capture:/project: commands; the cockpit's ⚑ Capture button posts to /capture.
    surface = "zulip" if session == "zulip" else ("voice" if req.get("voice") else "cockpit")
    if prefixed and surface != "zulip":
        return {"reply": CHAT_POINTER, "refused": True, "intent": "capture"}
    if low.startswith("project:"):
        title = msg.split(":", 1)[1].strip()
        p = await asyncio.to_thread(eng.plan_project, title, "")
        ms = "; ".join(f"{m['title']} ({len(m['tasks'])} tasks)" for m in p["milestones"])
        return {"planned": True, "goal_id": p["goal_id"],
                "reply": f"Planned “{p['title']}” — {len(p['milestones'])} milestones, "
                         f"{p['task_count']} tasks:\n{ms}\n\nSay “run next task” or POST /run-next-task "
                         f"{{goal_id:{p['goal_id']}}} to work them through the gate."}
    if req.get("capture") or low.startswith("capture:") or msg.startswith("⚑"):
        goal = msg.split(":", 1)[1].strip() if low.startswith("capture:") else msg.lstrip("⚑ ").strip()
        st = await asyncio.to_thread(eng.start_run, goal); _bump_runs()
        return {"captured": True, "run_id": st["run_id"], "lane": st["lane"],
                "reply": f"Captured. Routed to {st['lane']} — awaiting your approval."}
    return await asyncio.to_thread(chat_reply, msg, req.get("history"), session, remote, surface)


@app.get("/history")
async def chat_history(session_id: str = "cockpit", limit: int = 60):
    """This session's stored transcript, oldest first, so a reload rebuilds the conversation.
    Every turn was already in Qdrant; the cockpit just had no way to ask for it."""
    import memory_chat
    turns = await asyncio.to_thread(memory_chat.history, session_id, min(max(limit, 1), 200))
    return {"session_id": session_id, "turns": turns}


@app.post("/plan-project")
async def plan_project(request: Request):
    req = await _json(request)
    return await asyncio.to_thread(eng.plan_project, req.get("title", ""), req.get("description", ""))


@app.post("/run-next-task")
async def run_next_task(request: Request):
    """{goal_id} picks under that Feature; {project} (an OpenProject project id) picks across that
    whole board, for the cockpit's per-project "next" (#793). Either way the pick goes to the gate."""
    req = await _json(request)
    try:
        r = await asyncio.to_thread(eng.run_next_task, req.get("goal_id"), req.get("project"))
    except PMError as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)
    if req.get("project") is not None:
        pm_board.invalidate_projects()      # the pick left Ready or Backlog: recount the row
    _bump_runs()
    return r


@app.post("/run-backlog")
async def run_backlog(request: Request):
    """Self-development sweep, the target `nomadselfdevcron` points at now.

    It used to fire at crew's `/run-backlog` on :8001, which ran the work and left uncommitted edits
    behind with no gate in front of it. Here every run it starts stops at `awaiting-approval`, so a
    07:00 cron queues proposals for the operator rather than acting while nobody is watching."""
    req = await _json(request)
    r = await asyncio.to_thread(eng.run_backlog, req.get("limit", 3))
    _bump_runs()
    return r


@app.get("/project-goals")
async def project_goals():
    return {"projects": await asyncio.to_thread(eng.project_goals)}


@app.get("/project")
async def project_status(goal_id: int):
    return await asyncio.to_thread(eng.project_status, goal_id)


@app.post("/task-action")
async def task_action(request: Request):
    req = await _json(request)
    r = await asyncio.to_thread(eng.task_action, req.get("task_id"), req.get("action"))
    _bump_runs()
    return r


# ── OpenProject follow + board (PM-01 P4) ───────────────────────────
# OpenProject posts signed webhooks to /pm-hook; the relay reads the events back from /pm-events
# and posts to Zulip. The relay holds no OpenProject token, so its replies come back through
# /pm-comment. See pm_follow.py.
_follow = None


def _dispatcher(route: str, body: dict, timeout: float) -> dict:
    """POST a dispatcher route and return its JSON; an outage comes back as an error body."""
    req = urllib.request.Request(
        DISPATCH_URL + route, method="POST",
        headers={"Content-Type": "application/json", "X-Nomad-Token": DISPATCH_TOKEN},
        data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"dispatcher returned {e.code}"}
    except Exception as e:
        return {"ok": False, "error": f"dispatcher link error: {str(e)[:200]}"}


def _autopilot_on_task(wp_id) -> bool:
    """True when an autopilot worker holds this work package. A supervisor that can't be read
    blocks nothing: the mention runs."""
    code, st = _autopilot("GET", "/status")
    if code != 200:
        return False
    return any(str(w.get("task")) == str(wp_id)
               for p in (st.get("projects") or {}).values() for w in p.get("workers") or [])


def _pm_follow():
    global _follow
    if _follow is None:
        # A bot mentioned by the operator runs an agent session (pm_mention.py).
        mentions = pm_mention.Mentions(
            eng.op, _dispatcher, pm_follow.OPERATOR_ID, busy=_autopilot_on_task,
            log=lambda **e: eng._log(e["run_id"], e["agent"], e["what"], e["why"],
                                     e["outcome"], e["links"]))
        _follow = pm_follow.Follow(eng.op, mentions=mentions)
    return _follow


@app.post("/pm-hook")
async def pm_hook(request: Request):
    body = await request.body()
    secret = pm_follow.read_secret()
    if not secret:
        return JSONResponse({"error": "no webhook secret configured; run nomad-pm/seed.sh"}, status_code=503)
    if not pm_follow.verify(secret, body, request.headers.get("x-op-signature")):
        return JSONResponse({"error": "bad signature"}, status_code=401)
    try:
        payload = json.loads(body)
    except ValueError:
        return JSONResponse({"error": "not json"}, status_code=400)
    ev = await asyncio.to_thread(_pm_follow().ingest, payload)
    pm_board.invalidate_projects()      # any WP change may move a project's counts
    eng.op.invalidate_wps()             # ...and the board reads behind /project (#1110)
    if ev:
        _bump_runs()        # wakes the relay's SSE listener, so Zulip hears about it now
    return {"event": ev["seq"] if ev else None}


@app.get("/pm-events")
async def pm_events(after: int = 0, boot: str = ""):
    return _pm_follow().since(after, boot)


@app.post("/pm-comment")
async def pm_comment(request: Request):
    req = await _json(request)
    try:
        return await asyncio.to_thread(_pm_follow().post_comment, req.get("wp_id"), req.get("text"))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except PMError as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)


@app.get("/pm-summary")
async def pm_summary(project: str = "", fresh: bool = False):
    op = eng.op if not project or project == eng.op.project_ident else OpenProject(project=project)
    try:
        return await asyncio.to_thread(pm_board.summary, op, None, fresh)
    except PMError as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)


@app.get("/pm-projects")
async def pm_projects(fresh: bool = False):
    """Every OpenProject project with its open tasks by status and priority (cockpit PRD R9-R10).
    Cached 60 s; with OpenProject down it serves the last counts marked stale, with their age."""
    try:
        return await asyncio.to_thread(pm_board.projects, eng.op, fresh)
    except PMError as e:
        return JSONResponse({"error": str(e)[:300]}, status_code=502)


@app.post("/nocodb-hook")
async def nocodb_hook(request: Request):
    req = await _json(request)
    print("NOCODB-HOOK:", json.dumps(req)[:500], flush=True)
    run_id, status = _extract_hook(req)
    if status in ("approved", "rejected") and run_id:
        # db_origin: the operator flipped this row in NocoDB itself, behind NocoDB's own auth.
        r = await asyncio.to_thread(
            functools.partial(eng.resume_run, run_id, status, db_origin=True)); _bump_runs()
        return {"gate": status, **r}
    return {"ignored": True, "status": status, "run_id": run_id}


# ── local voice proxies (3D) ────────────────────────────────────────
@app.post("/api/stt")
async def api_stt(request: Request):
    try:
        audio = await request.body()
        ctype = request.headers.get("content-type", "audio/webm")
        return await asyncio.to_thread(_voice_stt, audio, ctype)
    except Exception as e:
        return JSONResponse({"error": f"voice stt: {str(e)[:120]}"}, status_code=502)


@app.post("/api/tts")
async def api_tts(request: Request):
    try:
        text = (await _json(request)).get("text", "")
        wav = await asyncio.to_thread(_voice_tts, text)
        return Response(content=wav, media_type="audio/wav")
    except Exception as e:
        return JSONResponse({"error": f"voice tts: {str(e)[:120]}"}, status_code=502)


@app.get("/api/voice-config")
def api_voice_config():
    """What the page needs to set voice up, rather than the page guessing at it.

    `rt_url` is the one address the browser dials itself, and it has to be one the browser can
    reach. Live voice is WebRTC: only the offer goes through this URL, and the audio afterwards is
    peer-to-peer between the browser and nomad-voice, so it cannot be proxied the way `/ws/wake`
    and `/api/tts` are. That is why this is the exception to everything else being same-origin."""
    return {"wake": "openwakeword",
            "label": os.environ.get("WAKE_LABEL", "Hey Jarvis"),
            "rt_url": os.environ.get("NOMAD_VOICE_RT_URL", "http://localhost:8200").rstrip("/")}


@app.post("/api/detect")
async def api_detect(request: Request):
    """Does this text read as AI-written? Ported from the console so the answer outlives it."""
    body = await _json(request)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "no text"}, status_code=400)
    status, out = await asyncio.to_thread(
        _detect, text, bool(body.get("lint")), bool(body.get("external")))
    return JSONResponse(out, status_code=status)


def _knowledge_list(limit: int, q: str) -> dict:
    """Filed briefs, newest first, optionally narrowed by a substring of the topic."""
    where = f"(topic,like,%{q}%)" if q else None
    try:
        rows = db.list("knowledge", max(1, min(limit, 200)), where=where, sort="-Id")
    except Exception as e:
        return {"error": f"knowledge unavailable: {str(e)[:160]}", "briefs": []}
    return {"briefs": [{"id": r.get("Id"), "topic": r.get("topic") or "",
                        "run_id": r.get("run_id") or "",
                        "created_at": r.get("created_at"),
                        "chars": len(str(r.get("brief") or ""))} for r in rows]}


def _knowledge_one(bid: int) -> dict:
    try:
        rows = db.list("knowledge", 1, where=f"(Id,eq,{bid})")
    except Exception as e:
        return {"error": f"knowledge unavailable: {str(e)[:160]}"}
    if not rows:
        return {"error": f"no brief {bid}"}
    r = rows[0]
    return {"id": r.get("Id"), "topic": r.get("topic") or "", "run_id": r.get("run_id") or "",
            "created_at": r.get("created_at"), "brief": str(r.get("brief") or "")}


@app.get("/api/knowledge")
async def api_knowledge(id: int = 0, q: str = "", limit: int = 50):
    """The Knowledge Base, read-only, replacing the console's Notion one.

    The console filed briefs into a Notion database and that write refuses now, correctly: Notion is
    the rollback source and a mirror still being written to is not one. The v2 sink is the NocoDB
    `knowledge` table, which the research lane has written through `save_brief` since 2A. What was
    missing was any way to read it back, so a filed brief existed and nothing could show it.

    Read-only on purpose. Filing stays behind the gate, in the pipeline, where the research lane
    puts it; a write route here would be a second way in that skips the approval the first one has.
    """
    if id:
        out = await asyncio.to_thread(_knowledge_one, id)
        return JSONResponse(out, status_code=404 if out.get("error", "").startswith("no brief")
                            else 200)
    return await asyncio.to_thread(_knowledge_list, limit, q.strip())


# nomad-watch's state file, mounted read-only. The engine reports what the one prober found; it
# does not probe. Two target lists would disagree the moment they drifted, and the drift would show
# up as a service the console called down and the cockpit called up.
WATCH_STATE = os.environ.get("NOMAD_WATCH_STATE", "/watch-state/state.json")
WATCH_STALE_S = int(os.environ.get("NOMAD_WATCH_STALE_S", "900"))   # 3 missed 5-minute passes


def _services() -> dict:
    """Service health, as of nomad-watch's last pass, with the age of that pass attached.

    A stale file reports every service as `up: None`, not as the value it last held. The whole
    reason nomad-watch exists is that `active` is not the same as working, and a two-hour-old file
    saying everything was fine is exactly the reassuring-but-dead reading it was built to catch. A
    consumer that has to handle unknown cannot accidentally paint it green.

    `down_since` comes from the prober rather than from this read, so a service that has been down
    for a day says so instead of looking like it just failed."""
    try:
        with open(WATCH_STATE) as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {"services": [], "source": "nomad-watch",
                "error": f"no watch state at {WATCH_STATE} — is nomad-watch running?"}
    except Exception as e:
        return {"services": [], "source": "nomad-watch",
                "error": f"watch state unreadable: {str(e)[:160]}"}
    raw.pop("_meta", None)                      # bookkeeping, not a target
    checked = max((v.get("checked") or "" for v in raw.values()), default="")
    # Age comes from the file's mtime, not from `checked`. nomad-watch stamps a naive local
    # timestamp and this reads it from a container whose clock is UTC, so parsing that string gave
    # an age offset by the timezone — it read -7088s here, and a staleness check that can go
    # negative is not checking anything. The file is rewritten every pass, so mtime is the same
    # fact with no timezone in it. `checked` is kept for display only.
    try:
        age = time.time() - os.path.getmtime(WATCH_STATE)
    except Exception:
        age = None
    stale = age is None or age > WATCH_STALE_S
    out = []
    for name, v in sorted(raw.items()):
        out.append({"name": name,
                    "up": None if stale else bool(v.get("ok")),
                    "last_up": bool(v.get("ok")),
                    "detail": str(v.get("detail") or ""),
                    "fails": v.get("fails", 0),
                    "down_since": v.get("since"),
                    "checked": v.get("checked")})
    d = {"services": out, "source": "nomad-watch", "checked_at": checked,
         "age_s": None if age is None else int(age), "stale": stale}
    if stale:
        d["error"] = ("nomad-watch has not reported for "
                      + (f"{int(age)}s" if age is not None else "an unknown time")
                      + " — these readings are not current")
    return d


@app.get("/api/services")
async def api_services():
    """What nomad-watch last saw. Ported from the console, which probed 17 targets itself.

    The engine does not take that job over. nomad-watch is a oneshot on a timer precisely so a
    wedged watchdog cannot be the failure it is meant to report, and a second prober inside a
    long-lived server would give that property back up. This reads its state and says how old it is.
    """
    return await asyncio.to_thread(_services)


def _dispatch(body: dict) -> tuple:
    """POST to the dispatcher, stdlib, matching the engine's other outbound calls."""
    req = urllib.request.Request(
        DISPATCH_URL + "/dispatch", method="POST",
        headers={"Content-Type": "application/json", "X-Nomad-Token": DISPATCH_TOKEN},
        data=json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=920) as r:
            return 200, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or b"{}")
            return e.code, body or {"ok": False, "error": f"dispatcher returned {e.code}"}
        except Exception:
            return e.code, {"ok": False, "error": f"dispatcher returned {e.code}"}
    except Exception as e:
        # 502, not 200. The console answered 200 with an error body, so a caller that checked only
        # the status code read a dispatcher outage as a build that ran.
        return 502, {"ok": False, "error": f"dispatcher link error: {str(e)[:200]}"}


@app.post("/api/dispatch")
async def api_dispatch(request: Request):
    """Hand a task to the Builder in a repo, through the dispatcher. Ported from the console.

    The dispatcher makes uncommitted edits and every route but `/health` needs the token, so this
    is a proxy and not a reimplementation: the authority stays in one place. A long build is the
    normal case, hence the fifteen minute ceiling."""
    body = await _json(request)
    if not (body.get("project") and body.get("task")):
        return JSONResponse({"ok": False, "error": "project and task are required"},
                            status_code=400)
    status, out = await asyncio.to_thread(_dispatch, body)
    return JSONResponse(out, status_code=status)


# ── policies: the cockpit's Policies area, proxied to the dispatcher (#1396) ────────────────
#
# The policy files live in the repo and this container has neither the repo nor git, so the
# dispatcher runs nomad_policy/api.py and these routes only forward. Every save lands on a
# policy/* branch, never on master. None of the POSTs is in TAILNET_WRITES: the phone can read
# the policy layer but not change it. A guardrail save also carries the cockpit token, which
# only this route sends; a caller with just the dispatch token, as agents have, is refused.
POLICY_GETS = ("", "get", "effective")
POLICY_POSTS = ("simulate", "tag-dry-run", "tag-save", "tag-accept", "validate", "save", "attach",
                "guardrail-save")


def _cockpit_token() -> str:
    """Read at each guardrail save, not at import: the dispatcher creates the file on its first
    start, which can come after this container's. '' means the dispatcher refuses the save."""
    env = os.environ.get("NOMAD_COCKPIT_TOKEN")
    if env:
        return env
    try:
        return open("/term-secret/cockpit-token").read().strip()
    except Exception:
        return ""


def _policies(method: str, route: str, query: str = "", body: dict = None,
              base: str = "/policies") -> tuple:
    """(status, body) from the dispatcher's <base><route>; an outage is a 502."""
    headers = {"Content-Type": "application/json", "X-Nomad-Token": DISPATCH_TOKEN}
    if base == "/policies" and route == "/guardrail-save":
        headers["X-Nomad-Cockpit"] = _cockpit_token()
    req = urllib.request.Request(
        f"{DISPATCH_URL}{base}{route}" + (f"?{query}" if query else ""), method=method,
        headers=headers, data=None if body is None else json.dumps(body).encode())
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}") or {"ok": False, "error": f"dispatcher returned {e.code}"}
        except Exception:
            return e.code, {"ok": False, "error": f"dispatcher returned {e.code}"}
    except Exception as e:
        return 502, {"ok": False, "error": f"dispatcher link error: {str(e)[:200]}"}


@app.get("/cockpit/policies", response_class=HTMLResponse)
async def cockpit_policies_page(request: Request):
    """The Policies page (#1397). From a proxy (the phone on the tailnet) it is read-only, like the
    tailnet itself: no policy POST is on TAILNET_WRITES."""
    if _via_proxy(request.headers):
        return HTMLResponse(cockpit_policies.PAGE.replace("</head><body>", "</head><body class=remote>", 1))
    return HTMLResponse(cockpit_policies.PAGE)


@app.get("/api/policies")
@app.get("/api/policies/{name}")
async def api_policies_get(request: Request, name: str = ""):
    """The policy list, one policy (?id=) or a project's effective access (?project=)."""
    if name not in POLICY_GETS:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    status, out = await asyncio.to_thread(_policies, "GET", f"/{name}" if name else "",
                                          request.url.query)
    return JSONResponse(out, status_code=status)


@app.post("/api/policies/{name}")
async def api_policies_post(request: Request, name: str):
    """Simulate, tag dry run, save or attach on a branch, and the operator's guardrail save."""
    if name not in POLICY_POSTS:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    body = await _json(request)
    status, out = await asyncio.to_thread(_policies, "POST", f"/{name}", "", body)
    return JSONResponse(out, status_code=status)


# ── profiles: the cockpit's Standards area, proxied to the dispatcher (#1377) ────────────────
#
# nomad_projects/profile_api.py on the host, like the policies above: list, get, validate and dry
# run, and save (#1379), which commits on branch profile/<id> and never on master. The
# dispatcher's own GET /profiles stays the + New project type menu; the cockpit's list is
# /profiles/list. No profile POST is in TAILNET_WRITES, so the phone cannot save; it gets the dry
# run of master's version through GET (#1378), which forwards only the id.
PROFILE_GETS = ("list", "get", "dry-run")
PROFILE_POSTS = ("validate", "dry-run", "save")


@app.get("/cockpit/standards", response_class=HTMLResponse)
async def cockpit_standards_page(request: Request):
    """The Standards page (#1378). From a proxy it is read-only: no profile POST is on TAILNET_WRITES."""
    if _via_proxy(request.headers):
        return HTMLResponse(cockpit_standards.PAGE.replace("</head><body>", "</head><body class=remote>", 1))
    return HTMLResponse(cockpit_standards.PAGE)


@app.get("/api/profiles/{name}")
async def api_profiles_get(request: Request, name: str):
    """Every profile with the editor's choices (list), one profile's text (get?id=), or what a new
    project of a type gets as on master (dry-run?id=)."""
    if name not in PROFILE_GETS:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    query = request.url.query
    if name == "dry-run":
        query = urllib.parse.urlencode({"id": request.query_params.get("id", "")})
    status, out = await asyncio.to_thread(_policies, "GET", f"/{name}", query, None, "/profiles")
    return JSONResponse(out, status_code=status)


@app.post("/api/profiles/{name}")
async def api_profiles_post(request: Request, name: str):
    """Check a profile's text, dry-run what a new project of the type gets, or save it on its branch."""
    if name not in PROFILE_POSTS:
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    body = await _json(request)
    status, out = await asyncio.to_thread(_policies, "POST", f"/{name}", "", body, "/profiles")
    return JSONResponse(out, status_code=status)


# ── autopilot: the cockpit's Run/Stop and status line, proxied to the supervisor (#882) ──────
#
# The supervisor (autopilot/supervisor.py) is a host-native daemon on 127.0.0.1:8092. Its POSTs need
# X-Nomad-Token, and like the dispatcher's token that one stays here: the page talks to the engine,
# the engine adds the token. Nothing below re-implements the supervisor's rules; it checks what it
# cheaply can (the project id, the stop mode) so a typo is a 400/404 from here instead of a round
# trip, and passes everything else through.
#
# Auth needs nothing extra. The middleware's method rule already makes every POST here a 403 while
# the engine is published (`on`). In `tailnet` mode the four writes below are on the tailnet
# allowlist (TAILNET_WRITES and TAILNET_WRITE_PATTERNS, #883), so the operator's phone can press
# Run and Stop and change the budget, with the same login and Origin checks as an approval.
#
# The token is read on each call rather than at import, because the supervisor writes
# ~/.config/nomad/autopilot-token on its first start, which may well be after the engine's.

AUTOPILOT_TIMEOUT = float(os.environ.get("NOMAD_AUTOPILOT_TIMEOUT", "4"))
AUTOPILOT_STOP_MODES = ("finish", "kill")
# The cockpit's answer to a task in Awaiting approval (docs/prd/autopilot-approval.md 5.3).
AUTOPILOT_DECISIONS = {"approve": "Approved", "reject": "Changes requested"}
COCKPIT_PREFIX = "**Operator, via the cockpit:**\n\n"
AUTOPILOT_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")   # the supervisor's rule
AUTOPILOT_DOWN = "autopilot service not running"


def _autopilot_token() -> str:
    """NOMAD_AUTOPILOT_TOKEN, else the supervisor's token file. In the container ~/.config/nomad is
    mounted read-only at /term-secret, the same mount the dispatcher's token comes through."""
    env = os.environ.get("NOMAD_AUTOPILOT_TOKEN")
    if env:
        return env
    for cand in ("/term-secret/autopilot-token", "/host/.config/nomad/autopilot-token",
                 os.path.expanduser("~/.config/nomad/autopilot-token")):
        try:
            tok = open(cand).read().strip()
            if tok:
                return tok
        except Exception:
            continue
    return ""


def _autopilot(method: str, path: str, body: dict | None = None) -> tuple:
    """(status, json) from the supervisor. Never raises: a supervisor that is down or wedged is a
    503 the cockpit can print, and short timeouts keep a wedged one from wedging the page."""
    headers = {"Content-Type": "application/json"}
    if method == "POST":
        tok = _autopilot_token()
        if not tok:
            return 503, {"ok": False, "error": "autopilot token not available to the engine",
                         "detail": "no NOMAD_AUTOPILOT_TOKEN and no autopilot-token file; start the "
                                   "supervisor once so it writes ~/.config/nomad/autopilot-token"}
        headers["X-Nomad-Token"] = tok
    req = urllib.request.Request(AUTOPILOT_URL + path, method=method, headers=headers,
                                 data=json.dumps(body or {}).encode() if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=AUTOPILOT_TIMEOUT) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Not passed through: a 401 would make the browser think *its* login failed.
            return 502, {"ok": False, "error": "the supervisor refused the engine's autopilot token",
                         "detail": "the token the engine read does not match the supervisor's"}
        try:
            out = json.loads(e.read() or b"{}")
        except Exception:
            out = {}
        if not isinstance(out, dict) or not out:
            out = {"ok": False, "error": f"autopilot supervisor returned {e.code}"}
        return e.code, out
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        reason = getattr(e, "reason", e)
        return 503, {"ok": False, "error": AUTOPILOT_DOWN, "detail": str(reason)[:200]}
    except ValueError as e:          # a body that is not JSON: something else is on the port
        return 502, {"ok": False, "error": f"autopilot supervisor sent a bad reply: {str(e)[:120]}"}


def _registry_ids() -> list | None:
    """Project ids in the dispatcher's registry (projects.yaml), or None when it cannot be asked."""
    try:
        with urllib.request.urlopen(urllib.request.Request(
                DISPATCH_URL + "/projects", headers={"X-Nomad-Token": DISPATCH_TOKEN}),
                timeout=AUTOPILOT_TIMEOUT) as r:
            ids = json.loads(r.read() or b"{}").get("ids")
        return [str(i) for i in ids] if isinstance(ids, list) else None
    except Exception:
        return None


def _autopilot_project_refusal(pid: str) -> tuple | None:
    """(status, json) when `pid` must not reach the supervisor, else None.

    Known means in the registry the dispatcher reads (projects.yaml, which the autopilot's own
    settings live in, PRD R12) or already in the supervisor's state, so a project dropped from the
    registry can still be stopped. If the registry cannot be asked and the supervisor does not know
    the id either, that is a 503 rather than a guess in either direction."""
    if not AUTOPILOT_PROJECT_ID.match(pid):
        return 400, {"ok": False, "error": f"bad project id {pid[:80]!r}"}
    ids = _registry_ids()
    if ids is not None and pid in ids:
        return None
    code, st = _autopilot("GET", "/status")
    if code == 200 and pid in (st.get("projects") or {}):
        return None
    if code == 503:
        return code, st
    if ids is None:
        return 503, {"ok": False, "error": "cannot check the project: the dispatcher's registry "
                                           "did not answer"}
    return 404, {"ok": False, "error": f"unknown project {pid!r}"}


async def _autopilot_reply(method: str, path: str, body: dict | None = None) -> JSONResponse:
    status, out = await asyncio.to_thread(_autopilot, method, path, body)
    return JSONResponse(out, status_code=status)


@app.get("/api/autopilot/status")
async def api_autopilot_status():
    """Every project's autopilot state, the config and spend today, from the supervisor."""
    return await _autopilot_reply("GET", "/status")


@app.get("/api/autopilot/runs")
async def api_autopilot_runs(limit: int = 50):
    """The newest worker runs (the builds approved tasks turn into), for the cockpit's runs table
    next to the engine's own pipeline runs. Read-only, like /status."""
    return await _autopilot_reply("GET", f"/runs?limit={max(1, min(int(limit), 200))}")


@app.get("/api/autopilot/config")
async def api_autopilot_config():
    return await _autopilot_reply("GET", "/config")


@app.post("/api/autopilot/config")
async def api_autopilot_config_set(request: Request):
    """Partial config update (budget mode, daily cap, caps). The supervisor validates the fields."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict) or not body:
        return JSONResponse({"ok": False, "error": "body must be a non-empty JSON object"},
                            status_code=400)
    return await _autopilot_reply("POST", "/config", body)


@app.post("/api/autopilot/pass")
async def api_autopilot_pass():
    """Run a scheduling pass now."""
    return await _autopilot_reply("POST", "/pass")


@app.post("/api/autopilot/projects/{pid}/start")
async def api_autopilot_start(pid: str):
    """Autopilot on for a project, and a pass right away."""
    refused = await asyncio.to_thread(_autopilot_project_refusal, pid)
    if refused:
        return JSONResponse(refused[1], status_code=refused[0])
    return await _autopilot_reply("POST", f"/projects/{pid}/start")


@app.post("/api/autopilot/projects/{pid}/stop")
async def api_autopilot_stop(pid: str, request: Request):
    """Autopilot off. {"mode": "finish"} (the default) lets running workers finish, "kill" stops
    them; a killed worker's branch is kept (PRD 5.10)."""
    body = await _json(request)
    mode = body.get("mode", "finish") if isinstance(body, dict) else None
    if mode not in AUTOPILOT_STOP_MODES:
        return JSONResponse({"ok": False, "error": f"mode must be one of {AUTOPILOT_STOP_MODES}"},
                            status_code=400)
    refused = await asyncio.to_thread(_autopilot_project_refusal, pid)
    if refused:
        return JSONResponse(refused[1], status_code=refused[0])
    return await _autopilot_reply("POST", f"/projects/{pid}/stop", {"mode": mode})


@app.post("/api/autopilot/projects/{pid}/autopilot")
async def api_autopilot_switch(pid: str, request: Request):
    """The project's Autopilot ON/OFF switch (#890), {"on": true|false}. ON: Run keeps going. OFF:
    Run starts the top qualifying task only, and the project turns off when that task is done."""
    body = await _json(request)
    on = body.get("on") if isinstance(body, dict) else None
    if not isinstance(on, bool):
        return JSONResponse({"ok": False, "error": 'body must be {"on": true|false}'},
                            status_code=400)
    refused = await asyncio.to_thread(_autopilot_project_refusal, pid)
    if refused:
        return JSONResponse(refused[1], status_code=refused[0])
    return await _autopilot_reply("POST", f"/projects/{pid}/autopilot", {"on": on})


def _decide_task(wp_id: int, decision: str, text: str) -> tuple:
    """Move a task out of Awaiting approval, with the operator's answer as a comment, both as
    nomad-manager. The autopilot picks it up at its next pass (autopilot/tasks.py)."""
    try:
        wp = eng.op.get(wp_id)
    except PMError as e:
        if "-> 404" in str(e):
            return 404, {"ok": False, "error": f"no task #{wp_id}"}
        return 502, {"ok": False, "error": "OpenProject did not answer", "detail": str(e)[:200]}
    status = _title(wp, "status")
    if status != pm_follow.AWAITING:
        return 409, {"ok": False, "status": status,
                     "error": f"#{wp_id} is {status} now, not Awaiting approval"}
    target = AUTOPILOT_DECISIONS[decision]
    try:
        eng.op.move(wp_id, target, comment=COCKPIT_PREFIX + text if text else "", as_=MANAGER)
    except PMError as e:
        return 502, {"ok": False, "error": f"could not move #{wp_id}", "detail": str(e)[:200]}
    return 200, {"ok": True, "task": wp_id, "status": target}


@app.post("/api/autopilot/tasks/{wp_id}/decide")
async def api_autopilot_decide(wp_id: int, request: Request):
    """{"decision": "approve"|"reject", "text": "..."}: answer a task in Awaiting approval. A
    rejection needs its reason, which becomes the worker's instruction."""
    body = await _json(request)
    body = body if isinstance(body, dict) else {}
    decision, text = body.get("decision"), body.get("text") or ""
    if decision not in AUTOPILOT_DECISIONS:
        return JSONResponse({"ok": False, "error": "decision must be approve or reject"},
                            status_code=400)
    if not isinstance(text, str):
        return JSONResponse({"ok": False, "error": "text must be a string"}, status_code=400)
    text = text.strip()[:5000]
    if decision == "reject" and not text:
        return JSONResponse({"ok": False, "error": "a rejection needs its reason"}, status_code=400)
    status, out = await asyncio.to_thread(_decide_task, wp_id, decision, text)
    return JSONResponse(out, status_code=status)


def _task_question(wp_id: int) -> tuple:
    """What the worker asked at the gate (#1175): the newest comment that is not the operator's own
    answer, read as a short question plus options. Read-only."""
    try:
        wp = eng.op.get(wp_id)
        acts = eng.op.activities(wp_id)
    except PMError as e:
        if "-> 404" in str(e):
            return 404, {"ok": False, "error": f"no task #{wp_id}"}
        return 502, {"ok": False, "error": "OpenProject did not answer", "detail": str(e)[:200]}
    ask = None
    for a in reversed(acts):
        raw = ((a.get("comment") or {}).get("raw") or "").strip()
        if raw and not raw.startswith("**Operator"):
            ask = (a, raw)
            break
    out = {"ok": True, "id": wp_id, "subject": wp.get("subject") or "",
           "status": _title(wp, "status"), "project": _title(wp, "project") or "",
           "stem": "", "options": [], "complex": False, "asked_by": "", "asked_at": ""}
    if ask:
        a, raw = ask
        out.update(parse_question(raw))
        out["asked_by"] = ((a.get("_links") or {}).get("user") or {}).get("title") or ""
        out["asked_at"] = a.get("createdAt") or ""
    return 200, out


@app.get("/api/autopilot/tasks/{wp_id}/question")
async def api_autopilot_question(wp_id: int):
    """The question a task in Awaiting approval is waiting on, for the cockpit's Now queue."""
    status, out = await asyncio.to_thread(_task_question, wp_id)
    return JSONResponse(out, status_code=status)


# ── websocket proxies: the cockpit talks to the engine, the engine talks to the host daemon ──
#
# Both of these exist for the same reason. termd and nomad-voice are host-native units bound to
# localhost, and the token that opens termd must not be handed to a browser. The engine holds the
# credential, the page holds a same-origin socket, and the daemons stay where they are.


async def _ws_relay(websocket: WebSocket, upstream: str):
    """Pump a client socket and an upstream socket at each other until either one stops.

    Text and bytes are both passed through unchanged, because the two callers need different ones:
    the terminal carries text, the wake word carries 16 kHz PCM frames. Whichever side ends first
    ends the other, so a browser tab closing does not leave a mic stream open upstream."""
    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(upstream, max_size=4 * 1024 * 1024) as up:
            async def c2u():
                while True:
                    m = await websocket.receive()
                    if m["type"] == "websocket.disconnect":
                        return
                    if m.get("text") is not None:
                        await up.send(m["text"])
                    elif m.get("bytes") is not None:
                        await up.send(m["bytes"])

            async def u2c():
                async for m in up:
                    if isinstance(m, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(m))
                    else:
                        await websocket.send_text(m)

            t1 = asyncio.create_task(c2u())
            t2 = asyncio.create_task(u2c())
            _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket):
    """The cockpit's interactive claude session, proxied to the native PTY daemon.

    Closed outright while the engine is externally reachable, rather than put behind the shared
    password, because a shared password in front of a shell is not a boundary."""
    if await _ws_refused(websocket):
        return
    await websocket.accept()
    project = websocket.query_params.get("project", "")
    cols = websocket.query_params.get("cols", "80")
    rows = websocket.query_params.get("rows", "24")
    await _ws_relay(websocket, f"{TERM_BASE}/term?project={urllib.parse.quote(project)}"
                               f"&cols={cols}&rows={rows}&token={urllib.parse.quote(TERM_TOKEN)}")


@app.websocket("/ws/wake")
async def ws_wake(websocket: WebSocket):
    """Mic PCM up to nomad-voice's openWakeWord listener, the {wake:true} signal back down.

    The wake word runs on openWakeWord in nomad-voice: local, free, no account, and it fires the
    instant it hears the phrase rather than after a transcription window. It replaced Picovoice
    Porcupine, which was retired in 3a081d5.

    Proxied rather than dialled directly so the page stays same-origin. nomad-voice is a host-native
    unit on localhost and a browser cannot reach it, which is the correct state of affairs."""
    if await _ws_refused(websocket):
        return
    await websocket.accept()
    up = VOICE_URL.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
    await _ws_relay(websocket, up + "/wake")


def gate_scan():
    """THE GATE (poll): find comms rows the operator flipped to approved/rejected and resume
    the paused run. Idempotent; status flip in NocoDB → run resumes. Returns count acted on."""
    acted = 0
    # Filter and sort at the DB. A blind `limit` scan is oldest-first, so once comms outgrows the
    # window the newest approval is the first thing the backup gate stops seeing.
    pending = db.list("comms", 50, where="(status,eq,approved)~or(status,eq,rejected)", sort="-Id")
    for r in pending:
        rid, status = r.get("run_id"), (r.get("status") or "").strip()
        if rid and status in ("approved", "rejected") and rid not in _resolved:
            # db_origin: the status flip IS the operator's decision, made on the row itself.
            eng.resume_run(rid, status, db_origin=True)
            _resolved.add(rid)
            acted += 1
    return acted


def _gate_loop():
    while True:
        try:
            gate_scan()
        except Exception:
            pass
        time.sleep(GATE_POLL_SECS)


def _sweep_loop():
    """Fail runs abandoned in `new`. See Engine.sweep_stalled for why this exists."""
    while True:
        time.sleep(engine_mod.SWEEP_SECS)
        try:
            for r in eng.sweep_stalled():
                print(f"swept stalled run {r['run_id']} (comms#{r['comms_id']}, "
                      f"{r['age_min']} min in 'new')", flush=True)
            _BEATS["sweep"] = time.time()   # only on success; see /health, watched by nomad-watch
        except Exception as e:
            print(f"sweep_stalled failed: {type(e).__name__}: {e}", flush=True)


def _gate_listen():
    """PUSH gate: Postgres LISTEN/NOTIFY. A trigger on the comms table NOTIFYs on status flip;
    we resume the run instantly (sub-second) — bypassing NocoDB's (broken) webhook delivery.
    The poller stays as backup. No psycopg / DB unreachable → silently rely on the poller."""
    import select as _select
    try:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    except Exception:
        return
    while True:
        try:
            conn = psycopg2.connect(
                host=os.environ.get("NOMAD_PG_HOST", "postgres"), port=5432,
                dbname=os.environ.get("NOMAD_PG_DB", "nomad_v2"),
                user=os.environ.get("POSTGRES_USER"), password=os.environ.get("POSTGRES_PASSWORD"))
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            conn.cursor().execute("LISTEN nomad_gate;")
            print("gate LISTEN active (push)", flush=True)
            while True:
                if _select.select([conn], [], [], 60) == ([], [], []):
                    continue
                conn.poll()
                while conn.notifies:
                    n = conn.notifies.pop(0)
                    try:
                        p = json.loads(n.payload)
                        if p.get("run_id") and p.get("status") in ("approved", "rejected"):
                            eng.resume_run(p["run_id"], p["status"])
                    except Exception:
                        pass
        except Exception:
            time.sleep(5)   # reconnect


def _runs_listen():
    """LISTEN on 'nomad_runs' (any comms insert/update via the Postgres trigger) → bump the runs
    version so open SSE streams push a refresh instantly. Fail-open: no psycopg / DB down → the
    engine still bumps on its own start_run/resume_run, and the cockpit keeps a slow fallback poll."""
    import select as _select
    try:
        import psycopg2
        from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT
    except Exception:
        return
    while True:
        try:
            conn = psycopg2.connect(
                host=os.environ.get("NOMAD_PG_HOST", "postgres"), port=5432,
                dbname=os.environ.get("NOMAD_PG_DB", "nomad_v2"),
                user=os.environ.get("POSTGRES_USER"), password=os.environ.get("POSTGRES_PASSWORD"))
            conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
            conn.cursor().execute("LISTEN nomad_runs;")
            print("runs LISTEN active (SSE push)", flush=True)
            while True:
                if _select.select([conn], [], [], 60) == ([], [], []):
                    continue
                conn.poll()
                if conn.notifies:
                    conn.notifies.clear()
                    _bump_runs()
        except Exception:
            time.sleep(5)   # reconnect


def recent_runs(limit=25):
    """Join comms rows into a cockpit-friendly run list (most recent first). For runs paused at
    the gate, include the proposal so the operator can see what they're approving."""
    out = []
    for r in db.list("comms", limit, sort="-Id"):   # newest first, or limit drops the new runs
        item = {"run_id": r.get("run_id"), "lane": r.get("lane"),
                "from_agent": r.get("from_agent"), "type": r.get("type"),
                "status": r.get("status"), "goal_id": r.get("goal_id"),
                "created_at": r.get("created_at")}
        if r.get("status") == "awaiting-approval":
            try:
                p = json.loads(r.get("payload") or "{}")
                a = p.get("args", {})
                # The hash rides with the preview so the page can approve exactly what it drew,
                # rather than whatever the proposal says by the time the button is pressed.
                item["proposal"] = {
                    "action": p.get("action"),
                    "target": a.get("to") or a.get("topic") or a.get("project") or "",
                    "preview": (a.get("body") or a.get("brief") or a.get("content")
                                or a.get("task") or a.get("prompt") or "")[:280],
                    "warning": project_warning(p),
                    "proposal_hash": proposal_hash(p)}
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


COCKPIT = """<!doctype html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>NOMAD v2 · Cockpit</title><link rel=icon type=image/svg+xml href=/static/favicon.svg><link rel=icon type=image/png sizes=96x96 href=/static/favicon-96x96.png><link rel=apple-touch-icon href=/static/apple-touch-icon.png>
<style>
:root{--bg:#05060a;--surface:#0c0d16;--surface-2:#14141f;--line:#1f2030;--line-strong:#2a2a3a;--text:#ccddee;--text-muted:#99aaaa;
--brand:#c9a45c;--attention:#ffb000;--on-attention:#05060a;--ok:#33dd88;--info:#56b6ff;--danger:#e3554f;--warn:#ff8c5a;--paused:#d07bff;
--sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:24px;--sp-6:32px;--r-control:8px;--r-card:10px;--r-tag:999px;--tap:44px;--tabbar:58px}
h1{color:var(--brand)}
body{background:var(--bg);color:var(--text);font-family:ui-monospace,'Share Tech Mono',monospace;margin:0;padding:22px}
h1{font-size:20px;letter-spacing:.28em;margin:0 0 4px} .sub{opacity:.6;font-size:12px;letter-spacing:.15em;margin-bottom:16px}
.stages{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 18px;font-size:12px;opacity:.8}
.stages span{background:var(--surface-2);border:1px solid var(--line-strong);border-radius:10px;padding:3px 9px}
table{width:100%;border-collapse:collapse;font-size:13px} th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1b1b27}
th{color:var(--info);font-weight:400;letter-spacing:.1em;font-size:12px}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;color:#000;font-weight:700}
.new{background:#445;color:#aab}.awaiting-approval{background:#ff5;animation:b 1.1s steps(2) infinite}
.approved{background:var(--ok)}.executing{background:var(--ok);animation:b 1.1s steps(2) infinite}
.executed{background:var(--info)}.rejected,.declined{background:var(--danger);color:#fff}.failed{background:#8b1a16;color:#fff}
@keyframes b{50%{opacity:.4}} .mut{opacity:.5}
.gate{display:inline-flex;flex-wrap:wrap;justify-content:center;align-items:center;gap:2px 5px;border:0;border-radius:7px;padding:4px 11px;margin-left:7px;cursor:pointer;font:inherit;font-size:12px;font-weight:700}
.ok{background:var(--ok);color:#022} .no{background:var(--danger);color:#fff} .retry{background:var(--attention);color:#022} .gate:active{transform:translateY(1px)}
.prop{background:var(--surface)} .prop td{border-bottom:1px solid #1b1b27;padding:4px 10px 9px}
.prop .act{color:var(--info);letter-spacing:.08em} .prop .pv{opacity:.78;white-space:pre-wrap}
.telem{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px;font-size:12px}
.telem .m{background:var(--surface);border:1px solid var(--line);border-radius:8px;padding:4px 10px;display:flex;gap:7px;align-items:center}
.telem .lbl{color:var(--info);letter-spacing:.08em} .telem .bar{width:54px;height:6px;background:#1b1b27;border-radius:4px;overflow:hidden}
.telem .fill{height:100%;background:var(--ok);transition:width .4s} .telem .hot{background:var(--danger)} .telem .warn{background:var(--warn)}
.lanes{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 12px;font-size:12px}
.lanes .chip{background:var(--surface);border:1px solid var(--line-strong);border-radius:12px;padding:3px 11px;cursor:pointer;letter-spacing:.06em;opacity:.62}
.lanes .chip.on{opacity:1;border-color:var(--info)} .lanes .chip .ct{opacity:.6;margin-left:5px}
.lanes button.chip{font:inherit;font-size:12px;color:var(--text)}
.ldot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.l-comms{background:var(--ok)}.l-research{background:var(--info)}.l-support{background:var(--brand)}.l-ads{background:var(--paused)}.l-dev{background:var(--warn)}.l-autopilot{background:#8fe3d0}.l-other{background:#667}
.ap-merge{background:var(--info)}.ap-running{background:var(--ok);animation:b 1.1s steps(2) infinite}.ap-failed{background:#8b1a16;color:#fff}.ap-needs_operator{background:#ff5}.ap-resume{background:var(--paused)}.ap-unknown{background:#445;color:#aab}
.aprow .pv{opacity:.72;font-size:12px} .aprow a{color:#8fe3d0}
.rlink{color:var(--text);cursor:pointer;border-bottom:1px dotted #665} .rlink:hover{color:#fff}
.detail td{background:#0a0b12;border-bottom:1px solid #1b1b27;padding:10px 14px;font-size:12px}
.detail .kv{margin:2px 0} .detail .k{color:var(--info);display:inline-block;min-width:64px}
.detail .pv{white-space:pre-wrap;overflow-wrap:anywhere;margin:2px 0 8px}
.detail .log{margin-top:8px;border-top:1px solid #1b1b27;padding-top:6px}
.detail .le{opacity:.8;margin:2px 0} .detail .le b{color:var(--ok);font-weight:400}
.chat{margin:0 0 18px;border:1px solid var(--line);border-radius:10px;background:#0a0b12;overflow:hidden}
.clog{max-height:200px;overflow-y:auto;padding:10px 12px;font-size:13px;display:none}
.clog .u{color:var(--text);margin:6px 0} .clog .n{color:#9fd}; .clog .n{margin:6px 0;white-space:pre-wrap}
.clog .u b,.clog .n b{opacity:.6;font-weight:400;margin-right:6px;font-size:12px}
.crow{display:flex;gap:8px;padding:10px 12px;border-top:1px solid var(--line)}
.crow input{flex:1;min-width:0;background:var(--bg);border:1px solid var(--line-strong);border-radius:8px;color:var(--text);padding:8px 11px;font:inherit;font-size:16px}
.crow button{background:#334;color:var(--text);border:0;border-radius:8px;padding:8px 13px;cursor:pointer;font:inherit;font-size:12px}
.crow .cap{background:var(--info);color:#022;font-weight:700} .crow button:active{transform:translateY(1px)}
/* #ws.ws-hidden, not .ws-hidden. The panel's own rule is an id and sets display:flex, so a bare
   class never won and the terminal overlay covered the whole cockpit from first paint, at z-index
   60, with CLOSE doing nothing because all it does is add the class that loses. */
#ws.ws-hidden{display:none}
#ws{position:fixed;inset:0;z-index:60;background:var(--bg);display:flex;flex-direction:column}
#wsbar{display:flex;align-items:center;gap:10px;padding:8px 14px;background:var(--brand);color:#000;font-size:13px;letter-spacing:.08em}
#wsbar .wst{font-weight:700} #wsbar .wsp{background:#000;color:var(--brand);padding:2px 9px;border-radius:9px}
#wsbar button{background:#000;color:var(--brand);border:0;border-radius:7px;padding:5px 11px;cursor:pointer;font:inherit;font-size:12px}
#wsterm{flex:1;min-height:0;padding:6px 4px 4px 8px}
#voicebar{align-items:center;gap:10px;margin:8px 0 18px}
#voicebar .vb{background:#334;color:var(--text-muted);border:0;border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit}
#voicebar .vb.brand{background:var(--info);color:#022;padding:7px 14px}
#vstatus{font-size:12px}
.morecard{margin:0 0 var(--sp-4)}
.morecard .ph{color:var(--info);letter-spacing:.12em;font-size:12px;margin:0 0 6px}
.morerow{display:flex;gap:var(--sp-2);flex-wrap:wrap;align-items:center}
.morerow #termproj{flex:1 1 200px;min-width:0}
#termproj{background:var(--surface-2);color:var(--text);border:1px solid var(--line-strong);border-radius:8px;padding:6px;font:inherit;font-size:12px}
#termopen{background:var(--info);border:0;border-radius:8px;color:#022;padding:7px 12px;cursor:pointer;font:inherit}
.projects{margin:0 0 16px}
.projects .ph{color:var(--info);letter-spacing:.12em;font-size:12px;margin:0 0 6px}
.projects .ph{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.projects .ph .sp{flex:1}
.projects select{background:var(--bg);color:var(--text);border:1px solid var(--line-strong);border-radius:7px;padding:6px 8px;font:inherit;font-size:12px;min-height:36px;letter-spacing:0}
.projects .stale{color:var(--warn);font-size:12px;letter-spacing:0;margin:0 0 6px}
/* One OpenProject project (#787). The button drops under the name when the row is narrow. */
.proj{display:flex;flex-wrap:wrap;align-items:center;gap:6px 12px;background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:8px 11px;margin-bottom:7px}
.proj.zero{opacity:.55}
.proj .pn{flex:1 1 220px;min-width:0}
.proj .pt{font-size:13px;overflow-wrap:anywhere} .proj .pid{font-size:12px;color:var(--text-muted);margin-left:6px}
.proj .pc{display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;font-size:12px;align-items:center}
.proj .po{color:var(--text);font-weight:700;margin-right:4px}
.proj .chip{border:1px solid var(--line-strong);border-radius:10px;padding:1px 7px;color:var(--text-muted);white-space:nowrap}
.proj .chip.blk{border-color:var(--warn);color:var(--warn)} .proj .chip.aw{border-color:var(--attention);color:var(--attention)}
.proj .hi{color:var(--warn);display:inline-flex;align-items:center;gap:3px;white-space:nowrap}
/* #890: next to the name, in this order: Open in OpenProject, 'next' (#793), the Autopilot ON/OFF switch, Run/Stop. */
.proj .pctl{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.proj .pnmsg{flex:1 1 100%;font-size:12px;color:var(--warn)} .proj .pnmsg.ok{color:var(--ok)}
.proj .opbtn{display:inline-flex;align-items:center;gap:6px;min-height:44px;padding:0 12px;border-radius:8px;background:#334;color:var(--text);text-decoration:none;font-size:12px;white-space:nowrap}
.pmb{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:8px 11px;margin-bottom:7px;font-size:12px}
.pmb .ph a{color:var(--text-muted);text-decoration:none;margin-left:8px} .pmb h4{margin:8px 0 3px;font-weight:400;color:var(--info);font-size:12px;letter-spacing:.08em}
.pmb table{border-collapse:collapse;width:100%} .pmb td,.pmb th{padding:1px 6px;text-align:left;font-weight:400;border-bottom:1px solid #151622}
.pmb th{opacity:.55;font-size:12px} .pmb td.n{text-align:right;font-variant-numeric:tabular-nums}
.pmb .blk{color:var(--warn)} .pmb .ch{padding-left:14px;opacity:.75}
.pmb .tp{display:flex;gap:3px;align-items:flex-end;height:46px} .pmb .tp div{flex:1;display:flex;flex-direction:column-reverse}
.pmb .tp i{display:block} .pmb .tp .a{background:var(--info)} .pmb .tp .h{background:var(--brand)}
.pmb svg:not(.ic){width:100%;height:54px;background:#08090f;border-radius:5px}
/* Lucide icons (R12): drawn in the text colour, so they follow the theme and the state colours. */
svg.ic{width:16px;height:16px;flex:none;vertical-align:-3px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
button svg.ic+span,svg.ic+b{margin-left:5px}
button.icon{display:inline-flex;align-items:center;justify-content:center;gap:6px}
.sdot{display:inline-block;width:7px;height:7px;border-radius:50%;border:1px solid currentColor;margin-right:6px;vertical-align:1px}
.sdot.on{background:currentColor}
.rpage{display:inline-flex;align-items:center;justify-content:center;min-width:32px;min-height:32px;color:var(--text-muted);vertical-align:middle}
/* AUTOPILOT (#883, #884): the global strip over PROJECTS, and one status line per autopilot project
   on its board's row. Every control is at least 44 px high so it works from the phone. */
.apstrip{background:var(--surface);border:1px solid var(--line);border-radius:9px;padding:9px 11px;margin:0 0 12px;font-size:12px}
.apstrip .ph{color:var(--info);letter-spacing:.12em;display:flex;align-items:center;gap:8px 12px;flex-wrap:wrap;margin:0 0 6px}
.apstrip .sp{flex:1}
.apstrip .tot{display:flex;flex-wrap:wrap;gap:4px 14px;margin:0 0 8px;color:var(--text)}
.apstrip .tot b{color:var(--text);font-weight:700}
.apform{display:flex;flex-wrap:wrap;align-items:flex-end;gap:8px 12px}
.apform label{display:flex;flex-direction:column;gap:3px;font-size:12px;color:var(--text-muted);letter-spacing:.06em}
.apform input{width:96px;min-height:44px;box-sizing:border-box;background:var(--bg);border:1px solid var(--line-strong);border-radius:8px;color:var(--text);padding:6px 9px;font:inherit;font-size:14px}
.apform input:disabled{opacity:.45}
.apseg{display:inline-flex;border:1px solid var(--line-strong);border-radius:8px;overflow:hidden}
.apseg button{min-height:44px;padding:0 13px;border:0;background:var(--surface-2);color:var(--text-muted);font:inherit;font-size:12px;cursor:pointer}
.apseg button[aria-pressed=true]{background:var(--info);color:#022;font-weight:700}
.apbtn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:44px;padding:0 13px;border:0;border-radius:8px;font:inherit;font-size:12px;font-weight:700;cursor:pointer;white-space:nowrap}
.apbtn:disabled{opacity:.5;cursor:default} .apbtn:active{transform:translateY(1px)}
.apbtn.run{background:var(--ok);color:#022} .apbtn.stop{background:var(--danger);color:#fff}
.apbtn.kill{background:#8b1a16;color:#fff} .apbtn.plain{background:#334;color:var(--text);font-weight:400}
.apsw{display:inline-flex;align-items:center;gap:8px;min-height:44px;padding:0 12px 0 8px;border:1px solid var(--line-strong);border-radius:8px;background:var(--surface-2);color:var(--text-muted);font:inherit;font-size:12px;cursor:pointer;white-space:nowrap}
.apsw .knob{position:relative;width:34px;height:20px;border-radius:10px;background:#334;flex:none;transition:background .15s}
.apsw .knob::after{content:'';position:absolute;top:3px;left:3px;width:14px;height:14px;border-radius:50%;background:var(--text-muted);transition:left .15s}
.apsw[aria-checked=true]{color:var(--text);border-color:var(--info)} .apsw[aria-checked=true] .knob{background:var(--info)}
.apsw[aria-checked=true] .knob::after{left:17px;background:#022}
.apsw:disabled{opacity:.45;cursor:default}
.apmsg{color:var(--warn);font-size:12px;margin-top:5px;overflow-wrap:anywhere} .apmsg.ok{color:var(--ok)}
.aps{flex:1 1 100%;min-width:0}
.ap{border-top:1px solid #1b1b27;padding:7px 0 2px;font-size:12px}
.ap .top{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}
.ap .apid{color:var(--text-muted);font-size:12px}
.ap .apd{flex:1 1 160px;min-width:0;overflow-wrap:anywhere;color:var(--text)}
.ap .acts{display:flex;flex-wrap:wrap;gap:6px;margin-left:auto}
.ap .ask{flex:1 1 100%;display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--surface-2);border-radius:8px;padding:6px 8px}
.ap .ask .sp{flex:1 1 24px}
.ap .meta{display:flex;flex-wrap:wrap;gap:3px 12px;margin-top:4px;color:var(--text-muted);font-size:12px}
.ap .meta b{color:var(--text);font-weight:400}
.ap a{color:var(--text)}
.ap .wk{border:1px solid var(--line-strong);border-radius:10px;padding:1px 7px;color:var(--text);text-decoration:none;white-space:nowrap}
.ap .nyou{color:var(--attention);font-weight:700} .ap .nyou a{color:var(--attention)}
.ap .apdb{margin-left:6px}
.ap .apdec{display:flex;flex-wrap:wrap;align-items:center;gap:6px;background:var(--surface-2);border-radius:8px;padding:6px 8px;margin-top:6px}
.ap .apdec label{flex:1 1 100%;font-size:12px;color:var(--text)}
.ap .apdec textarea{flex:1 1 100%;min-height:44px;box-sizing:border-box;background:#0b0b12;color:#e6e6f0;border:1px solid #445;border-radius:6px;padding:6px 8px;font:inherit;font-size:13px}
.ap .last{margin-top:3px;color:var(--text-muted);font-size:12px;overflow-wrap:anywhere}
.ap details{margin-top:3px;font-size:12px} .ap summary{cursor:pointer;color:var(--info);min-height:32px;display:flex;align-items:center}
.ap .ev{color:var(--text-muted);margin:2px 0 2px 10px;overflow-wrap:anywhere} .ap .ev b{color:var(--text);font-weight:400}
.apst{padding:2px 9px;border-radius:9px;font-size:12px;font-weight:700;color:#022;background:#556;white-space:nowrap}
.apst.running{background:var(--ok)} .apst.waiting{background:var(--info)} .apst.needs_you{background:var(--attention);animation:b 1.1s steps(2) infinite}
.apst.budget_paused{background:var(--paused)} .apst.error{background:var(--danger);color:#fff} .apst.idle{background:var(--text-muted)} .apst.off{background:#334;color:var(--text-muted)}
@media (max-width:600px){ .ap .acts{margin-left:0} .apform label{flex:1 1 40%} .apform input{width:100%} .proj .pctl{flex:1 1 100%} }
/* The runs table on a phone (#799): FROM and TYPE go, and the decision buttons wrap under the status,
   so the row fits 390 px instead of pushing the page to 554. The run page has the rest. */
@media (max-width:600px){
  body{padding:14px}
  #runs th:nth-child(3),#runs td:nth-child(3),#runs th:nth-child(4),#runs td:nth-child(4){display:none}
  #runs th,#runs td{padding:7px 5px}
  #runs .gate{margin:6px 6px 0 0;min-height:40px}
}
/* Runs on a phone (#1177): a run is a two-line row, id and status over lane and time. The decision
   buttons are not in the row; they sit in the sticky bar of the run detail, in the thumb zone. */
@media (max-width:600px){
  #runs,#runs tbody{display:block}
  #runs thead{display:none}
  #runs tr.rrow,#runs tr.aprow{display:grid;grid-template-columns:1fr auto;align-items:center;gap:2px 10px;
    padding:6px 4px;border-bottom:1px solid var(--line);min-height:var(--tap)}
  #runs tr.prop{display:block;border-bottom:1px solid var(--line)}
  #runs td{padding:0;border:0}
  #runs td:nth-child(1){grid-column:1;grid-row:1} #runs td:nth-child(5){grid-column:2;grid-row:1;text-align:right}
  #runs td:nth-child(2){grid-column:1;grid-row:2} #runs td:nth-child(6){grid-column:2;grid-row:2;text-align:right}
  #runs td:nth-child(5) .gate{display:none}
  #runs .rlink{min-height:var(--tap);display:inline-flex;align-items:center}
  .lanes .chip{display:inline-flex;align-items:center;min-height:var(--tap);padding:0 14px;font-size:12px}
  .runsheet .gb{flex:1 1 0}
}
.prow{display:flex;flex-direction:column;gap:2px;width:100%;box-sizing:border-box;min-height:var(--tap);text-align:left;
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--sp-3);margin:0 0 var(--sp-2);
  color:var(--text);font:inherit;cursor:pointer}
.prow:hover{border-color:var(--line-strong)} .prow.zero{opacity:.7}
.prow .pr1{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-3)}
.prow .pt{font-size:14px;overflow-wrap:anywhere}
.prow .pr2{display:flex;flex-wrap:wrap;gap:2px var(--sp-3);font-size:12px;color:var(--text-muted)}
.prow .hi{color:var(--text)} .prow .nyou{color:var(--attention);font-weight:700}
.projsheet{padding-bottom:var(--sp-4)}
.rlink{background:none;border:0;border-bottom:1px dotted #665;padding:0;font:inherit;color:var(--text);cursor:pointer}
.rrow{cursor:pointer}
/* Run detail (#1177): replaces the list inside the Runs destination, never grows it. */
.runsheet{padding-bottom:var(--sp-4)}
.rs-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:var(--sp-2)}
.rs-back{display:inline-flex;align-items:center;gap:var(--sp-2);min-height:var(--tap);background:none;border:0;
  color:var(--text);font:inherit;font-size:14px;cursor:pointer;padding:0 var(--sp-2) 0 0}
.rs-title{margin:0 0 var(--sp-2);font-size:18px;font-weight:600;overflow-wrap:anywhere}
.rs-meta{display:flex;flex-wrap:wrap;align-items:center;gap:var(--sp-2) var(--sp-3);color:var(--text-muted);font-size:12px;margin-bottom:var(--sp-3)}
.rs-prop{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-card);padding:var(--sp-3);margin-bottom:var(--sp-3)}
.rs-prop .act{color:var(--info);letter-spacing:.08em} .rs-prop .pv{white-space:pre-wrap;overflow-wrap:anywhere;margin-top:var(--sp-2)}
.runsheet .kv{margin:var(--sp-1) 0;overflow-wrap:anywhere} .runsheet .k{color:var(--info);display:inline-block;min-width:64px}
.runsheet .pv{white-space:pre-wrap;overflow-wrap:anywhere;margin:2px 0 var(--sp-2)}
.gatebar{position:sticky;bottom:0;display:flex;gap:var(--sp-3);padding:var(--sp-3) 0;background:var(--bg);
  border-top:1px solid var(--line);z-index:5}
.gatebar:empty{display:none}
.gb{display:inline-flex;align-items:center;justify-content:center;gap:var(--sp-2);min-height:var(--tap);padding:0 var(--sp-4);
  border:0;border-radius:var(--r-control);font:inherit;font-size:14px;font-weight:700;cursor:pointer}
.gb:disabled{opacity:.45;cursor:not-allowed}
@media (max-width:600px){ .gatebar{bottom:calc(var(--tabbar) + env(safe-area-inset-bottom))} }
/* The confirm sheet: Reject says what will not happen, in words, before it does anything. */
#csheetwrap.cs-hidden{display:none}
#csheetwrap .csheet-bg{position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:80}
.csheet{position:fixed;left:0;right:0;bottom:0;z-index:81;background:var(--surface);border-top:1px solid var(--line-strong);
  border-radius:var(--r-card) var(--r-card) 0 0;padding:var(--sp-4) var(--sp-4) calc(var(--sp-4) + env(safe-area-inset-bottom));
  max-width:520px;margin:0 auto;box-sizing:border-box}
.csheet h3{margin:0 0 var(--sp-2);font-size:16px} .csheet p{margin:0 0 var(--sp-4);color:var(--text);overflow-wrap:anywhere}
.csheet-btns{display:flex;gap:var(--sp-3)} .csheet-btns .gb{flex:1 1 0}
/* The rest of the cockpit on a phone (#946): nothing wider than the screen, 40 px tap targets, and a
   bottom tab bar that jumps between the sections of a page that is otherwise ~11,000 px tall. */
@media (max-width:600px){
  body{padding-bottom:calc(var(--tabbar) + 18px + env(safe-area-inset-bottom))}
  h1{font-size:16px;letter-spacing:.16em}
  .sub{letter-spacing:.06em;line-height:1.7}
  #voicebar{flex-wrap:wrap}
  #termproj{font-size:16px}
  #voicebar button,#termproj,#termopen,#chat button,#chat input,.apbtn,.projects select,.projects button,.ap summary{min-height:var(--tap)}
  /* The mic floats above the tab bar so it stays under the thumb however far the chat has scrolled. */
  #dest-chat{padding-bottom:calc(var(--tap) + var(--sp-4))}
  #dest-chat #mic{position:fixed;right:var(--sp-4);bottom:calc(var(--tabbar) + var(--sp-4) + env(safe-area-inset-bottom));
    z-index:40;width:56px;height:56px;min-height:56px;border-radius:50%;padding:0;display:flex;align-items:center;justify-content:center;
    box-shadow:0 2px 10px rgba(0,0,0,.5)}
  #dest-chat #mic span{display:none}
  #voicebar button{min-width:var(--tap)}
  .crow{flex-wrap:wrap} .crow input{flex:1 1 100%}
  .crow button{flex:1 1 0}
  .rpage{min-width:var(--tap);min-height:var(--tap)}
  .pmb .ph a{display:inline-block;padding:14px 4px;margin:-14px 0 -14px 4px}
  a.wp{display:inline-block;padding:13px 4px;margin:-13px -1px}
  #shellnav{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:50;
    background:var(--surface);border-top:1px solid var(--line-strong);padding:0 0 env(safe-area-inset-bottom)}
  #shellnav button{min-height:var(--tabbar)}
}
/* !important: #voicebar carries an inline display:flex. It also holds the terminal picker. */
body.remote #voicebar,body.remote #ccap{display:none!important}
body.remote #termrow{display:none!important}
/* Now (#1175): one card per thing that waits on the operator. */
.nowstatus{display:flex;flex-wrap:wrap;gap:6px 14px;align-items:center;font-size:13px;margin:0 0 14px}
.nowq{display:flex;flex-direction:column;gap:10px;margin:0 0 20px}
.nq{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--attention);border-radius:var(--r-card);padding:12px 14px}
.nq.fail{border-left-color:var(--danger)} .nq.blocked{border-left-color:var(--warn)}
.nq .nh{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:center;font-size:12px;color:var(--text-muted)}
.nq .ntag{font-weight:700;font-size:12px;padding:2px 9px;border-radius:var(--r-tag);background:var(--attention);color:var(--on-attention)}
.nq.fail .ntag{background:var(--danger);color:#fff} .nq.blocked .ntag{background:var(--warn);color:var(--on-attention)}
.nq .nt{font-size:15px;font-weight:700;margin:8px 0 4px;overflow-wrap:anywhere}
.nq .ns{font-size:13px;line-height:1.5;margin:4px 0;overflow-wrap:anywhere;white-space:pre-wrap}
.nq .nopts{display:flex;flex-direction:column;gap:6px;margin:8px 0}
.nq .nopt{display:flex;gap:8px;align-items:flex-start;text-align:left;min-height:var(--tap);padding:8px 12px;box-sizing:border-box;
  background:var(--surface-2);border:1px solid var(--line-strong);border-radius:var(--r-control);color:var(--text);font:inherit;font-size:13px;cursor:pointer}
.nq .nopt[aria-checked=true]{border-color:var(--brand);color:var(--brand)}
.nq .nopt b{flex:0 0 auto}
.nq .nhint{font-size:12px;color:var(--text-muted);margin:6px 0}
.nq .nact{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px;align-items:center}
.nq-btn{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:var(--tap);padding:0 16px;box-sizing:border-box;
  border:1px solid var(--line-strong);border-radius:var(--r-control);background:var(--surface-2);color:var(--text);font:inherit;font-size:13px;font-weight:700;cursor:pointer;text-decoration:none}
.nq-btn.go{background:var(--ok);border-color:var(--ok);color:var(--on-attention)}
.nq-btn.stop{border-color:var(--danger);color:var(--danger)}
.nq-btn[disabled]{opacity:.45;cursor:default}
.nq textarea{width:100%;box-sizing:border-box;margin-top:8px;min-height:64px;background:var(--bg);color:var(--text);border:1px solid var(--line-strong);
  border-radius:var(--r-control);padding:8px 10px;font:inherit;font-size:16px}
.nq .nmsg{font-size:12px;margin-top:8px;color:var(--warn)} .nq .nmsg.ok{color:var(--ok)}
.nowempty{padding:18px 14px;border:1px dashed var(--line-strong);border-radius:var(--r-card);color:var(--text-muted);text-align:center}
/* The shell (#1174): five destinations, one visible at a time. Below 600 px the nav is a bottom tab bar;
   600 to 1023 px an icon rail; from 1024 px a labelled rail and Chat becomes a dock on the right. */
#shell{display:block}
#shellnav button{position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:3px;
  background:none;border:0;border-left:3px solid transparent;color:var(--text-muted);font:inherit;font-size:12px;letter-spacing:.06em;cursor:pointer}
#shellnav button[aria-current=true]{color:var(--brand)}
#shellnav .nb{position:absolute;top:5px;left:calc(50% + 6px);min-width:17px;padding:1px 4px;box-sizing:border-box;
  border-radius:9px;background:var(--attention);color:var(--on-attention);font-size:12px;font-weight:700;line-height:15px}
#shellnav .nb[hidden]{display:none}
#main>section[hidden]{display:none}
@media (min-width:601px){
  body{padding:0}
  #shell{display:flex;min-height:100vh}
  #shellnav{display:flex;flex-direction:column;flex:0 0 64px;position:sticky;top:0;align-self:flex-start;height:100vh;
    background:var(--surface);border-right:1px solid var(--line-strong);padding-top:12px}
  #shellnav button{min-height:var(--tap);width:100%;padding:10px 0}
  #shellnav button[aria-current=true]{border-left-color:var(--brand)}
  #shellnav span{display:none}
  #shellnav .nb{left:calc(50% + 4px)}
  #main{flex:1;min-width:0;padding:22px}
}
@media (min-width:1024px){
  #shellnav{flex-basis:220px}
  #shellnav button{flex-direction:row;justify-content:flex-start;gap:10px;padding:10px 16px;font-size:13px;letter-spacing:.04em}
  #shellnav span{display:inline}
  #shellnav .nb{position:static;margin-left:auto}
  #shellnav button[data-dest=chat]{display:none}
  #main{padding-right:372px}
  #main>section[data-dest=chat]{position:fixed;top:0;right:0;bottom:0;width:340px;box-sizing:border-box;
    overflow:hidden;padding:22px 16px;background:var(--surface);border-left:1px solid var(--line-strong);
    flex-direction:column}
  #main>section[data-dest=chat]{display:flex!important}
  #dest-chat #voicebar{flex-wrap:wrap;flex:none;margin:0 0 14px!important}
  #dest-chat #chat{flex:1;display:flex;flex-direction:column;min-height:0;margin:0}
  #dest-chat .clog{flex:1;max-height:none;min-height:0}
  #dest-chat .crow{margin-top:auto;flex-wrap:wrap}
  #dest-chat .crow input{flex:1 1 100%}
  #dest-chat .crow button{flex:1 1 0}
}
</style><link rel=stylesheet href="/static/vendor/xterm.css"></head><body>
<!-- Lucide icons v1.48.0, https://lucide.dev. ISC License, Copyright (c) 2026 Lucide Icons and
     Contributors. Permission to use, copy, modify, and/or distribute this software for any purpose
     with or without fee is hereby granted, provided that the above copyright notice and this
     permission notice appear in all copies. Only the icons the cockpit uses are inlined. -->
<svg width=0 height=0 style="position:absolute" aria-hidden=true>
<symbol id="i-mic" viewBox="0 0 24 24"><path d="M12 19v3"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><rect x="9" y="2" width="6" height="13" rx="3"/></symbol>
<symbol id="i-ear" viewBox="0 0 24 24"><path d="M6 8.5a6.5 6.5 0 1 1 13 0c0 6-6 6-6 10a3.5 3.5 0 1 1-7 0"/><path d="M15 8.5a2.5 2.5 0 0 0-5 0v1a2 2 0 1 1 0 4"/></symbol>
<symbol id="i-messages-square" viewBox="0 0 24 24"><path d="M16 10a2 2 0 0 1-2 2H6.828a2 2 0 0 0-1.414.586l-2.202 2.202A.71.71 0 0 1 2 14.286V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/><path d="M20 9a2 2 0 0 1 2 2v10.286a.71.71 0 0 1-1.212.502l-2.202-2.202A2 2 0 0 0 17.172 19H10a2 2 0 0 1-2-2v-1"/></symbol>
<symbol id="i-volume-2" viewBox="0 0 24 24"><path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/><path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/></symbol>
<symbol id="i-volume-x" viewBox="0 0 24 24"><path d="M11 4.702a.7.7 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.7.7 0 0 0 11 19.298z"/><path d="m16.5 14.5 5-5"/><path d="m16.5 9.5 5 5"/></symbol>
<symbol id="i-square-terminal" viewBox="0 0 24 24"><path d="m7 11 2-2-2-2"/><path d="M11 13h4"/><rect width="18" height="18" x="3" y="3" rx="2" ry="2"/></symbol>
<symbol id="i-flag" viewBox="0 0 24 24"><path d="M4 22V4a1 1 0 0 1 .4-.8A6 6 0 0 1 8 2c3 0 5 2 7.333 2q2 0 3.067-.8A1 1 0 0 1 20 4v10a1 1 0 0 1-.4.8A6 6 0 0 1 16 16c-3 0-5-2-8-2a6 6 0 0 0-4 1.528"/></symbol>
<symbol id="i-square" viewBox="0 0 24 24"><rect width="18" height="18" x="3" y="3" rx="2"/></symbol>
<symbol id="i-play" viewBox="0 0 24 24"><path d="M5 5a2 2 0 0 1 3.008-1.728l11.997 6.998a2 2 0 0 1 .003 3.458l-12 7A2 2 0 0 1 5 19z"/></symbol>
<symbol id="i-check" viewBox="0 0 24 24"><path d="M20 6 9 17l-5-5"/></symbol>
<symbol id="i-x" viewBox="0 0 24 24"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></symbol>
<symbol id="i-rotate-cw" viewBox="0 0 24 24"><path d="M21 12a9 9 0 1 1-9-9c2.52 0 4.93 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/></symbol>
<symbol id="i-triangle-alert" viewBox="0 0 24 24"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/></symbol>
<symbol id="i-lock" viewBox="0 0 24 24"><rect width="18" height="11" x="3" y="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></symbol>
<symbol id="i-external-link" viewBox="0 0 24 24"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></symbol>
<symbol id="i-flame" viewBox="0 0 24 24"><path d="M12 3q1 4 4 6.5t3 5.5a1 1 0 0 1-14 0 5 5 0 0 1 1-3 1 1 0 0 0 5 0c0-2-1.5-3-1.5-5q0-2 2.5-4"/></symbol>
<symbol id="i-arrow-up-down" viewBox="0 0 24 24"><path d="m21 16-4 4-4-4"/><path d="M17 20V4"/><path d="m3 8 4-4 4 4"/><path d="M7 4v16"/></symbol>
<symbol id="i-skip-forward" viewBox="0 0 24 24"><path d="M21 4v16"/><path d="M6.029 4.285A2 2 0 0 0 3 6v12a2 2 0 0 0 3.029 1.715l9.997-5.998a2 2 0 0 0 .003-3.432z"/></symbol>
<symbol id="i-chevron-right" viewBox="0 0 24 24"><path d="m9 18 6-6-6-6"/></symbol>
<symbol id="i-ellipsis" viewBox="0 0 24 24"><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/><circle cx="5" cy="12" r="1"/></symbol>
<symbol id="i-arrow-left" viewBox="0 0 24 24"><path d="m12 19-7-7 7-7"/><path d="M19 12H5"/></symbol>
</svg>
<div id=shell>
<nav id=shellnav aria-label="cockpit destinations">
  <button data-dest=now aria-label=Now><svg class=ic aria-hidden=true><use href="#i-flag"/></svg><span>Now</span><b class=nb id=nb-now hidden></b></button>
  <button data-dest=projects aria-label=Projects><svg class=ic aria-hidden=true><use href="#i-square"/></svg><span>Projects</span></button>
  <button data-dest=runs aria-label=Runs><svg class=ic aria-hidden=true><use href="#i-play"/></svg><span>Runs</span><b class=nb id=nb-runs hidden></b></button>
  <button data-dest=chat aria-label=Chat><svg class=ic aria-hidden=true><use href="#i-messages-square"/></svg><span>Chat</span></button>
  <button data-dest=more aria-label=More><svg class=ic aria-hidden=true><use href="#i-ellipsis"/></svg><span>More</span></button>
</nav>
<main id=main>
<h1>NOMAD&nbsp;v2 · COCKPIT</h1><div class=sub>PIPELINE · LIVE FROM NOCODB · <span id=live class=mut><span class=sdot></span>connecting</span> · <span id=summary class=mut></span></div>
<section id=dest-now data-dest=now>
<div id=nowstatus class=nowstatus><span class=mut>loading…</span></div>
<div id=nowq class=nowq aria-live=polite></div>
<div class=stages><span>Capture</span><span>Clarify</span><span>Route</span><span>Process</span>
<span><svg class=ic><use href="#i-flag"/></svg> Human Gate</span><span>Execute</span><span>Log&amp;Learn</span></div>
</section>
<section id=dest-projects data-dest=projects hidden>
<div id=projects class=projects></div>
<div id=projsheet class=projsheet hidden>
<button type=button class=rs-back onclick="location.hash='projects'"><svg class=ic aria-hidden=true><use href="#i-arrow-left"/></svg><span>Projects</span></button>
<div id=projdetail class=projects></div>
<div id=pmboard class=projects></div>
</div>
</section>
<section id=dest-runs data-dest=runs hidden>
<div id=runslist>
<div id=stagebar class=lanes aria-label="Pipeline stage"></div>
<div id=lanebar class=lanes aria-label="Lane"></div>
<table id=runs><thead><tr><th>RUN</th><th>LANE</th><th>FROM</th><th>TYPE</th><th>STATUS</th><th>WHEN</th></tr></thead>
<tbody id=rows><tr><td class=mut colspan=6>loading…</td></tr></tbody></table>
</div>
<div id=runsheet class=runsheet hidden></div>
</section>
<section id=dest-more data-dest=more hidden>
<div id=termrow class=morecard><div class=ph>TERMINAL</div>
<div class=morerow><select id=termproj title="project" aria-label="Project for the terminal"></select>
<button id=termopen class=icon title="open an interactive claude terminal in this project"><svg class=ic aria-hidden=true><use href="#i-square-terminal"/></svg><span>Open terminal</span></button></div></div>
<div id=morepol class=morecard><div class=ph>POLICIES</div>
<div class=morerow><a href="/cockpit/policies" target=_blank rel=noopener style="display:inline-flex;align-items:center;min-height:44px" title="policy list, editor, attach to types and projects, and each project's effective access">Open policies</a></div></div>
<div id=morestd class=morecard><div class=ph>STANDARDS</div>
<div class=morerow><a href="/cockpit/standards" target=_blank rel=noopener style="display:inline-flex;align-items:center;min-height:44px" title="project types: interview, stack, policies, agents, gates, and a dry run of what a new project gets">Open standards</a></div></div>
<div id=moretelem class=morecard><div class=ph>SYSTEM</div><div id=telem class=telem><span class=mut>telemetry…</span></div></div>
<div id=apstrip class=apstrip><div id=aphead class=ph><span>AUTOPILOT</span></div><div id=apsum class=tot><span class=mut>loading…</span></div><div id=apform class=apform></div><div id=apgmsg class=apmsg></div><div id=apother></div></div>
</section>
<section id=dest-chat data-dest=chat hidden>
<div id=voicebar style="display:flex">
  <button id=mic class="icon vb brand" title="push to talk — click, speak, click. Goes through the chat, so spoken commands act."><svg class=ic aria-hidden=true><use href="#i-mic"/></svg><span>Speak</span></button>
  <button id=wake class="icon vb" aria-label="wake word" title="wake word — say &quot;Hey Jarvis&quot;, then keep talking"><svg class=ic aria-hidden=true><use href="#i-ear"/></svg></button>
  <button id=livevoice class="icon vb" aria-label="live voice" title="live voice — real-time, interrupt any time"><svg class=ic aria-hidden=true><use href="#i-messages-square"/></svg></button>
  <button id=spk class="icon vb" aria-label="spoken replies: off" aria-pressed=false title="spoken replies: off"><svg class=ic aria-hidden=true><use href="#i-volume-x"/></svg></button>
  <span id=vstatus class=mut></span>
  <audio id=rt-audio autoplay></audio>
</div>
<div id=chat class=chat>
  <div id=clog class=clog></div>
  <div class=crow>
    <input id=cin placeholder="Ask NOMAD about the pipeline — Capture files a goal" autocomplete=off>
    <button id=csend title="ask">Send</button>
    <button id=ccap class="cap icon" title="capture as a pipeline goal"><svg class=ic aria-hidden=true><use href="#i-flag"/></svg><span>Capture</span></button>
  </div>
</div>
</section>
</main></div>
<div id=csheetwrap class=cs-hidden></div>
<div id=ws class=ws-hidden>
  <div id=wsbar><span class=wst>WORKSPACE</span><span id=wsproj class=wsp>—</span><span id=wsstat class=mut style="font-size:12px"></span><span style="flex:1"></span><button id=wsre>RECONNECT</button><button id=wsclose class=icon><svg class=ic aria-hidden=true><use href="#i-x"/></svg><span>CLOSE</span></button></div>
  <div id=wsterm></div>
</div>
<script src="/static/vendor/xterm.js"></script>
<script src="/static/vendor/addon-fit.js"></script>
<script>
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
// One Lucide icon from the sprite at the top of the body. Decorative: the words beside it, or the
// button's aria-label, carry the meaning.
function ic(name){ return `<svg class=ic aria-hidden=true><use href="#i-${name}"/></svg>`; }
/* The shell (#1174). One destination shows at a time and the hash picks it (#now, #projects, #runs,
   #chat, #more), so Back works and a link can open on a destination. A badge counts what waits on
   the operator: Now sums approvals and needs_you projects, Runs shows approvals. */
/* Declared before the shell below: its first show() runs while this script is still evaluating, and
   a `let` read before its line is a ReferenceError that stops the whole page. */
let sheetRid=null, sheetBuilt=null, runsAll=[];
let projSheet=null, projFilter='all', pmData=null;
function navBadge(id, n){
  const b=document.getElementById(id); if(!b) return;
  b.hidden=!n; b.textContent=n>99?'99+':String(n||'');
  const btn=b.closest('button'), base=btn.querySelector('span').textContent;
  btn.setAttribute('aria-label', n?`${base}, ${n} waiting`:base);
}
(function(){
  const tabs=[...document.querySelectorAll('#shellnav button[data-dest]')];
  const secs=[...document.querySelectorAll('#main>section[data-dest]')];
  const names=tabs.map(t=>t.dataset.dest);
  function show(){
    // #runs/<id> is a run's detail inside the Runs destination.
    const hp=(location.hash||'').replace('#','').split('/');
    // From 1024 px Chat is the dock beside every destination, so #chat there means Now.
    let cur=names.includes(hp[0])?hp[0]:'now';
    if(cur==='chat' && matchMedia('(min-width:1024px)').matches) cur='now';
    runSheet(cur==='runs' && hp[1] ? decodeURIComponent(hp.slice(1).join('/')) : null);
    projSheetSet(cur==='projects' && hp[1] ? decodeURIComponent(hp.slice(1).join('/')) : null);
    secs.forEach(s=>{ s.hidden=s.dataset.dest!==cur; });
    tabs.forEach(t=>t.setAttribute('aria-current', String(t.dataset.dest===cur)));
  }
  tabs.forEach(t=>t.addEventListener('click',()=>{ location.hash=t.dataset.dest; }));
  addEventListener('hashchange',show); addEventListener('resize',show); show();
})();
// A status line with a CSS dot (live or idle) or a warning icon in front of plain text.
function dotted(el, on, text){ el.innerHTML=`<span class="sdot${on?' on':''}"></span>`+esc(text); }
function warned(el, text){ el.innerHTML=ic('triangle-alert')+' '+esc(text); }
async function gate(rid, decision){
  if(decision==='rejected'){
    const r=runsAll.find(x=>x.run_id===rid), p=r&&r.proposal;
    const what=p&&p.action ? p.action+(p.target?' to '+p.target:'') : 'this action';
    if(!await askSheet('Reject run '+rid+'?', 'Nothing will happen: '+what+' is dropped and the run ends as declined. '+
      'This cannot be undone.', 'Reject')) return;
  }
  const btns=document.querySelectorAll(`[data-rid="${rid}"]`); btns.forEach(b=>b.disabled=true);
  const body={run_id:rid,decision};
  // An approval must name the proposal the operator was shown: the hash this page drew the row
  // from, never a fresh read. This used to re-fetch /run here, which sent the hash of whatever the
  // proposal said at click time, so a proposal edited after the page was painted passed the
  // engine's check without anyone having seen it. No drawn hash means no approval.
  if(decision==='approved'){
    const h=shownHash[rid];
    if(!h){ alert('This proposal changed or was not drawn on this page. Read it again, then approve.');
      btns.forEach(b=>b.disabled=false); return; }
    body.expect_hash=h;
  }
  let res=null;
  try{ res=await (await fetch('/resume',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)})).json(); }
  catch(e){ alert('Could not reach the engine. The run was not decided.'); }
  // The engine answers a refusal with a 200 and an `error`, so it has to be read to be seen.
  if(res && res.error) alert(/changed since/.test(res.error)
    ? 'This proposal changed since you opened it. Reload to read the new one.' : res.error);
  tick();
}
async function retry(rid){
  const btns=document.querySelectorAll(`[data-rid="${rid}"]`); btns.forEach(b=>b.disabled=true);
  try{ await fetch('/retry',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({run_id:rid})}); }catch(e){}
  tick();
}
const LANES=['comms','research','support','ads','dev'];
let laneFilter='all';
// Autopilot worker runs (#957) share the table under their own lane; the engine never sets it.
const AP_LANE='autopilot';
function ldot(l){ return `<span class="ldot l-${LANES.includes(l)||l===AP_LANE?l:'other'}"></span>`; }
function setLane(l){ laneFilter=l; tick(); }
function renderLanes(runs){
  const ct={all:runs.length}; LANES.forEach(l=>ct[l]=0); ct[AP_LANE]=0;
  runs.forEach(r=>{ if(ct[r.lane]!=null) ct[r.lane]++; });
  const chip=(l,label)=>`<span class="chip ${laneFilter===l?'on':''}" onclick="setLane('${l}')">`+
    (l==='all'?'':ldot(l))+`${label}<span class=ct>${ct[l]||0}</span></span>`;
  document.getElementById('lanebar').innerHTML =
    chip('all','ALL') + [...LANES, AP_LANE].map(l=>chip(l,l.toUpperCase())).join('');
}
// run_id → proposal_hash of the proposal as this page last drew it. gate() approves with this.
const shownHash={};
/* The stage filter (#1177). Runs only exist from Process on, and the engine records four states, so
   these are the pipeline stages a run can actually be in; Capture, Clarify and Route have no runs. */
const STAGES=[['process','PROCESS'],['gate','HUMAN GATE'],['execute','EXECUTE'],['done','LOG&amp;LEARN']];
let stageFilter='all';
function runStage(r){
  if(r.lane===AP_LANE){ const o=r.outcome; return o==='running'?'execute':o==='needs_operator'?'gate':'done'; }
  const s=r.status;
  return s==='awaiting-approval'?'gate':s==='executing'?'execute':s==='new'?'process':'done';
}
function setStage(s){ stageFilter=s; tick(); }
function renderStages(runs){
  const ct={all:runs.length}; STAGES.forEach(s=>ct[s[0]]=0);
  runs.forEach(r=>{ ct[runStage(r)]++; });
  const chip=(k,label)=>`<button type=button class="chip ${stageFilter===k?'on':''}" aria-pressed="${stageFilter===k}" onclick="setStage('${k}')">`+
    `${label}<span class=ct>${ct[k]||0}</span></button>`;
  document.getElementById('stagebar').innerHTML=chip('all','ALL')+STAGES.map(s=>chip(s[0],s[1])).join('');
}
/* Run detail: a sheet inside Runs, opened by the hash #runs/<id> so Back closes it. The shell calls
   runSheet on every hash change. The decision buttons are the sticky bar at the bottom. */
function openRun(rid){ location.hash='runs/'+encodeURIComponent(rid); }
function rowTap(e,rid){ if(rid && !e.target.closest('a,button')) openRun(rid); }
function runSheet(rid){
  sheetRid=rid||null;
  const sh=document.getElementById('runsheet'), ls=document.getElementById('runslist');
  if(!sh||!ls) return;
  sh.hidden=!sheetRid; ls.hidden=!!sheetRid;
  if(!sheetRid){ sheetBuilt=null; return; }
  if(sheetBuilt!==sheetRid){
    sheetBuilt=sheetRid;
    sh.innerHTML=`<div class=rs-head><button type=button class=rs-back onclick="location.hash='runs'">${ic('arrow-left')}<span>Runs</span></button>`+
      `<a class=rpage href="/cockpit/run/${encodeURIComponent(sheetRid)}" target=_blank rel=noopener aria-label="open the run page in a new tab">${ic('external-link')}</a></div>`+
      `<h2 class=rs-title>${esc(sheetRid)}</h2><div id=rs-meta class=rs-meta></div><div id=rs-prop></div>`+
      `<div id="det-${escA(sheetRid)}" class=mut>loading…</div><div id=gatebar class=gatebar></div>`;
    scrollTo(0,0);
  }
  refreshRunSheet();
}
function gateBarHtml(r){
  const a=escA(r.run_id);
  if(r.status==='awaiting-approval') return `<button type=button class="gb no" data-rid="${a}" onclick="gate(this.dataset.rid,'rejected')">${ic('x')} Reject</button>`+
    `<button type=button class="gb ok" data-rid="${a}" onclick="gate(this.dataset.rid,'approved')"${shownHash[r.run_id]?'':' disabled'}>${ic('check')} Approve</button>`;
  if(r.status==='failed') return `<button type=button class="gb retry" data-rid="${a}" onclick="retry(this.dataset.rid)">${ic('rotate-cw')} Retry</button>`;
  return '';
}
function refreshRunSheet(){
  const meta=document.getElementById('rs-meta'), prop=document.getElementById('rs-prop'), bar=document.getElementById('gatebar');
  if(!meta||!prop||!bar) return;
  const r=runsAll.find(x=>x.run_id===sheetRid);
  if(!r){ meta.innerHTML='<span>Not in the recent list. The detail below is read from the engine.</span>'; prop.innerHTML=''; bar.innerHTML=''; }
  else{
    meta.innerHTML=`<span class="tag ${escA(r.status)}">${esc((r.status||'').toUpperCase())}</span>`+
      `<span>${ldot(r.lane)}${esc(r.lane||'')}</span>`+(r.type?`<span>${esc(r.type)}</span>`:'')+
      (r.from_agent?`<span>from ${esc(r.from_agent)}</span>`:'')+`<span>${esc(whenCell(r.created_at))}</span>`;
    const p=r.proposal;
    prop.innerHTML = r.status==='awaiting-approval' && p
      ? `<div class=rs-prop><span class=act>${ic('flag')} ${esc(p.action)||'action'}${p.target?' → '+esc(p.target):''}</span>`+
        (p.preview?`<div class=pv>${esc(p.preview)}</div>`:'')+
        (p.warning?`<div class=pv style="color:var(--warn)">${ic('triangle-alert')} ${esc(p.warning)}</div>`:'')+`</div>` : '';
    bar.innerHTML=gateBarHtml(r);
  }
  loadDetail(sheetRid);
}
// A confirm sheet that says what happens, with the safe answer focused. Resolves true to go ahead.
function askSheet(title, body, okLabel){
  return new Promise(res=>{
    const w=document.getElementById('csheetwrap');
    w.className='';
    w.innerHTML=`<div class=csheet-bg></div><div class=csheet role=dialog aria-modal=true aria-labelledby=cs-t>`+
      `<h3 id=cs-t>${esc(title)}</h3><p>${esc(body)}</p><div class=csheet-btns>`+
      `<button type=button class="gb cs-no" style="background:var(--surface-2);color:var(--text)">Keep it</button>`+
      `<button type=button class="gb no cs-ok">${esc(okLabel)}</button></div></div>`;
    const done=v=>{ document.removeEventListener('keydown',key); w.className='cs-hidden'; w.innerHTML=''; res(v); };
    const key=e=>{ if(e.key==='Escape') done(false); };
    document.addEventListener('keydown',key);
    w.querySelector('.csheet-bg').onclick=()=>done(false);
    w.querySelector('.cs-no').onclick=()=>done(false);
    w.querySelector('.cs-ok').onclick=()=>done(true);
    w.querySelector('.cs-no').focus();
  });
}
async function loadDetail(rid){
  const el=document.getElementById('det-'+rid); if(!el) return;
  try{ const r=await (await fetch('/run?id='+encodeURIComponent(rid))).json();
    const kv=(k,v)=>v?`<div class=kv><span class=k>${k}</span>${esc(String(v))}</div>`:'';
    let h='';
    // The detail and the row are two reads. If the proposal moved between them, the page is
    // showing two versions at once, so neither may be approved until the next refresh agrees.
    if(r.status==='awaiting-approval' && shownHash[rid] && r.proposal_hash!==shownHash[rid]){
      shownHash[rid]=null;
      h+=`<div class=kv><span class=k>${ic('triangle-alert')} changed</span><span style="color:var(--warn)">This proposal changed after the list was drawn. Approve is off until it refreshes.</span></div>`; }
    h+=kv('goal',r.goal)+kv('intent',r.intent)+kv('target',r.target)+kv('action',r.action);
    if(r.project_warning) h+=`<div class=kv><span class=k>${ic('triangle-alert')} project</span><span style="color:var(--warn)">${esc(String(r.project_warning))}</span></div>`;
    if(r.error) h+=`<div class=kv><span class=k>${ic('triangle-alert')} error</span><span style="color:var(--warn)">${esc(String(r.error))}</span></div>`;
    // Every argument the approval covers, whole. arg_fields() in the engine sets the order.
    (r.arg_fields||[]).forEach(f=>{
      if(!f.long){ h+=`<div class=kv><span class=k>${esc(f.key)}</span>${f.text?esc(f.text):'<span class=mut>empty</span>'}</div>`; return; }
      h+=`<div class=kv><span class=k>${esc(f.key)}</span></div><div class=pv>${esc(f.text)}</div>`;
    });
    // Delivery. The whole point of the outbox is that a run reading `executed` is the tool
    // returning, not the mail arriving, so the difference belongs on the page the operator opens.
    // `sent` carries Gmail's id; `accepted` means n8n took it and nothing more; `unknown` means the
    // wait expired and it may have gone out anyway, which is the one case where retrying can double
    // send. Colour follows that ladder rather than a pass/fail split.
    if(r.delivery && r.delivery.length){
      const COL={sent:'var(--ok)',accepted:'#e8c33a',unknown:'var(--warn)',failed:'#ff5a5a',unrecorded:'#6b6b80'};
      h+='<div class=log>';
      r.delivery.forEach(d=>{
        const st=(d.status||'unrecorded').toLowerCase();
        h+=`<div class=le><b style="color:${COL[st]||'#6b6b80'}">${esc(st.toUpperCase())}</b> · `+
           `${esc(d.to||'')}${d.provider_id?` <span class=mut>id ${esc(d.provider_id)}</span>`:''}`+
           `${d.error?` <span style="color:var(--warn)">${esc(d.error)}</span>`:''}</div>`;
      });
      h+='</div>'; }
    /* What it filed. A research run's whole product is a document, and until this was drawn the
       cockpit's answer to "did the brief get written" was the word `executed`, which is the tool
       returning. The body is the copy that was actually stored, not the draft above it. */
    if(r.artifacts && r.artifacts.length){
      h+='<div class=log>';
      r.artifacts.forEach(a=>{
        h+=`<div class=le><b style="color:var(--ok)">FILED</b> · `+
           `<span class=mut>${esc(a.sink)}#${esc(String(a.id))}</span> ${esc(a.title||'')} `+
           `<span class=mut>${a.chars} chars${a.truncated?', shown truncated':''}</span></div>`;
        /* \\n, not \n. COCKPIT is a plain Python string, so a single backslash-n here is a real
           newline in the served JS, a single-quoted string cannot hold one, and the SyntaxError
           kills the whole script block rather than this line. */
        const cut = a.truncated ? '\\n\\n… truncated, open /api/knowledge?id='+a.id : '';
        if(a.body) h+=`<div class=pv>${esc(a.body)}${esc(cut)}</div>`;
      });
      h+='</div>'; }
    if(r.log && r.log.length){ h+='<div class=log>';
      r.log.forEach(e=>{ h+=`<div class=le><b>${esc(e.agent||'')}</b> · ${esc(e.what||'')} <span class=mut>${(e.created_at||'').slice(11,19)}</span></div>`; });
      h+='</div>'; }
    el.className=''; el.innerHTML=h||'<span class=mut>no detail</span>';
  }catch(e){ el.innerHTML='<span class=mut>detail unavailable</span>'; }
}
/* situational awareness: counts + spoken announcements on state transitions (Jarvis) */
const lastStatus={}; let primed=false;
function announce(all){
  const c={'awaiting-approval':0,'executing':0,'failed':0};
  all.forEach(r=>{ if(c[r.status]!=null) c[r.status]++; });
  const sm=document.getElementById('summary');
  if(sm) sm.innerHTML=`${ic('flag')} ${c['awaiting-approval']} awaiting · ${ic('play')} ${c['executing']} executing · `+
    `<span style="color:${c.failed?'var(--warn)':'inherit'}">${ic('x')} ${c.failed} failed</span>`;
  navBadge('nb-runs', c['awaiting-approval']);
  // speak transitions (only after the first load, only if confirmations are on)
  all.forEach(r=>{
    const prev=lastStatus[r.run_id], now=r.status;
    if(primed && prev && prev!==now){
      if(now==='awaiting-approval') vspeak(`A ${r.lane||''} action is awaiting your approval.`);
      else if(now==='executed') vspeak(`The ${r.lane||''} action was executed.`);
      else if(now==='failed') vspeak(`Heads up — a ${r.lane||''} action failed.`);
    }
    lastStatus[r.run_id]=now;
  });
  primed=true;
}
// WHEN: the local clock time, with the date in front once the run is older than today (#957).
function whenCell(t){
  const d=typeof t==='number' ? new Date(t*1000) : new Date(String(t||'').replace(' ','T'));
  if(isNaN(d)) return '';
  const p=n=>String(n).padStart(2,'0'), hms=`${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  const today=new Date(); today.setHours(0,0,0,0);
  return d<today ? `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${hms}` : hms;
}
function runTime(r){ return r.lane===AP_LANE ? r.started*1000 : Date.parse(String(r.created_at||'').replace(' ','T'))||0; }
// The builds approved tasks turn into run in autopilot workers, not in the engine pipeline, so
// their records come from the supervisor. A supervisor that is down only drops these rows.
async function apRuns(){
  try{ const d=await (await fetch('/api/autopilot/runs?limit=40')).json();
    return (d.runs||[]).map(r=>({...r, lane:AP_LANE})); }catch(e){ return []; }
}
const AP_OUTCOME={merge:'MERGED', failed:'FAILED', needs_operator:'NEEDS YOU', resume:'RESUME',
  running:'RUNNING', unknown:'UNKNOWN'};
function apRunRow(r){
  const board=((apData&&apData.boards)||{})[r.project]||'';
  const tip=[r.branch, r.cost_usd!=null?'$'+Number(r.cost_usd).toFixed(2):''].filter(Boolean).join(' · ');
  const o=String(r.outcome||'unknown'), tag=AP_OUTCOME[o]||o.toUpperCase();
  let row=`<tr class=aprow title="${escA(tip)}"><td>${apTask(r.task, board)}</td>`+
    `<td>${ldot(AP_LANE)}${AP_LANE}</td><td>${esc(r.project||'')}</td><td>build</td>`+
    `<td><span class="tag ap-${escA(o.replace(/[^a-z_]/g,''))}">${esc(tag)}</span></td>`+
    `<td class=mut>${whenCell(r.started)}</td></tr>`;
  if(r.summary && o!=='merge') row+=`<tr class="prop aprow"><td colspan=6><div class=pv>${esc(r.summary)}</div></td></tr>`;
  return row;
}
async function tick(){
  try{ const [d, ap]=await Promise.all([fetch('/runs').then(x=>x.json()), apRuns()]);
    const eng=d.runs||[]; announce(eng); nowRuns=eng; drawNow();
    const all=[...eng, ...ap].sort((a,b)=>runTime(b)-runTime(a)); renderLanes(all); runsAll=eng;
    const inLane = laneFilter==='all' ? all : all.filter(r=>r.lane===laneFilter);
    renderStages(inLane);
    const runs = stageFilter==='all' ? inLane : inLane.filter(r=>runStage(r)===stageFilter);
    document.getElementById('rows').innerHTML = runs.map(r=>{
      if(r.lane===AP_LANE) return apRunRow(r);
      const wait = r.status==='awaiting-approval';
      let acts = wait
        ? `<button class="gate ok" data-rid="${r.run_id}" onclick="gate('${r.run_id}','approved')">${ic('check')} APPROVE</button>`+
          `<button class="gate no" data-rid="${r.run_id}" onclick="gate('${r.run_id}','rejected')">${ic('x')} REJECT</button>` : '';
      if(r.status==='failed') acts=`<button class="gate retry" data-rid="${r.run_id}" onclick="retry('${r.run_id}')">${ic('rotate-cw')} RETRY</button>`;
      const rid=r.run_id||'';
      // The run page (#799) is the same run on its own URL: the one Zulip and OpenProject link to.
      const page=rid?`<a class=rpage href="/cockpit/run/${encodeURIComponent(rid)}" target=_blank rel=noopener title="open the run page in a new tab" aria-label="open the run page in a new tab">${ic('chevron-right')}</a>`:'';
      let row=`<tr class=rrow onclick="rowTap(event,'${escA(rid)}')"><td><button type=button class=rlink onclick="openRun('${escA(rid)}')">${esc(rid)||'—'}</button>${page}</td>`+
        `<td>${ldot(r.lane)}${r.lane||''}</td><td>${r.from_agent||''}</td>`+
        `<td>${r.type||''}</td><td><span class="tag ${r.status}">${(r.status||'').toUpperCase()}</span>${acts}</td>`+
        `<td class=mut>${whenCell(r.created_at)}</td></tr>`;
      const p=r.proposal;
      if(wait) shownHash[rid]=(p&&p.proposal_hash)||null; else delete shownHash[rid];
      if(wait && p){ const tgt=p.target?` → ${esc(p.target)}`:'';
        row+=`<tr class=prop><td colspan=6><span class=act>${ic('flag')} ${esc(p.action)||'action'}${tgt}</span>`+
          (p.preview?`<div class=pv>${esc(p.preview)}</div>`:'')+
          (p.warning?`<div class=pv style="color:var(--warn)">${ic('triangle-alert')} ${esc(p.warning)}</div>`:'')+`</td></tr>`; }
      return row;
    }).join('') ||
      `<tr><td class=mut colspan=6>${all.length?'no runs in this view':'no runs yet — POST a goal to /capture'}</td></tr>`;
    if(sheetRid) refreshRunSheet();
  }catch(e){}
}
tick();
/* live push: SSE bumps on any run change → refresh instantly. Slow poll is just a safety net. */
setInterval(tick,10000);
try{
  const es=new EventSource('/events');
  es.onmessage=e=>{ if(e.data==='bump'||e.data==='hello') tick(); };
  const dot=document.getElementById('live'); if(dot){ es.onopen=()=>dotted(dot,true,'LIVE'); es.onerror=()=>dotted(dot,false,'reconnecting'); }
}catch(e){}

/* cockpit chat: converse about the pipeline (Send) or drop a goal into it (the Capture button).
   Memory: a stable session id + the recent transcript travel with each turn; the engine recalls
   relevant PAST conversations (unified with the console) and persists every turn. */
const clog=document.getElementById('clog'), cin=document.getElementById('cin');
let chatSession=localStorage.getItem('nomad_v2_session');
if(!chatSession){ chatSession='cockpit-'+Math.random().toString(36).slice(2,10); localStorage.setItem('nomad_v2_session',chatSession); }
const chatHist=[];
function clogAdd(who,text){
  clog.style.display='block';
  const d=document.createElement('div'); d.className=who==='you'?'u':'n';
  d.innerHTML=`<b>${who==='you'?'YOU':'NOMAD'}</b>${esc(text)}`;
  clog.appendChild(d); clog.scrollTop=clog.scrollHeight;
}
/* One path in and out of the chat, so voice and typing get the same brain. `csend` takes the text
   rather than reading the box, because the mic has no box. It resolves only after NOMAD has
   finished speaking, which is what lets conversation mode wait its turn instead of talking over
   the reply it just asked for. */
async function csend(msg, capture, speak){
  msg=(msg||'').trim(); if(!msg) return '';
  clogAdd('you', msg); clogAdd('nomad', '…');
  const ph=clog.lastChild;
  try{
    /* #973: the typed chat only answers. Capture goes to /capture, and a spoken turn is flagged
       voice, the one chat surface that may still approve and capture. */
    const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
    let r;
    if(capture){
      r=await (await post('/capture',{goal:msg})).json();
      r.captured=!!r.run_id && !r.error;
      r.reply=r.captured ? `Captured. Routed to ${r.lane} — awaiting your approval.` : 'Capture failed: '+(r.error||'no run');
    } else r=await (await post('/chat',{message:msg, voice:!!speak, session_id:chatSession, history:chatHist.slice(-6)})).json();
    const priv = r.remembered===false ? ' <span class=mut title="not stored to memory" aria-label="not stored to memory">'+ic('lock')+'</span>' : '';
    ph.innerHTML=`<b>NOMAD</b>${esc(r.reply||'(no reply)')}${priv}`;
    chatHist.push({role:'user',content:msg},{role:'assistant',content:r.reply||''});
    /* A spoken turn is spoken back. An action turn moved the pipeline, so refresh the board:
       a capture, or a spoken approve or capture, changes what the board shows. */
    if(speak) await vspeak(r.reply);
    else if(r.captured) vspeak(r.reply);
    if(r.captured || (r.intent && r.intent!=='chat')) tick();
    return r.reply||'';
  }catch(e){ ph.innerHTML='<b>NOMAD</b>'+ic('triangle-alert')+' chat error'; return ''; }
}
function csendMsg(capture){ const m=cin.value; cin.value=''; return csend(m, capture, false); }
document.getElementById('csend').onclick=()=>csendMsg(false);
document.getElementById('ccap').onclick=()=>csendMsg(true);
cin.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); csendMsg(e.shiftKey); } });

/* Rehydrate on load. Every turn was already stored in Qdrant, but chatHist lived only in this
   page, so a reload silently lost the conversation the engine could still remember. */
(async function restoreChat(){
  try{
    const r=await (await fetch('/history?session_id='+encodeURIComponent(chatSession))).json();
    for(const t of (r.turns||[])){
      if(!t.content) continue;
      clogAdd(t.role==='user'?'you':'nomad', t.content);
      chatHist.push({role:t.role, content:t.content});
    }
  }catch(e){}   /* memory down is not a reason to have no chat box */
})();

/* telemetry strip — CPU / RAM / GPU, read by the engine itself (v1 console is a fallback) */
function meter(lbl,pct,extra){
  if(pct==null) return `<div class=m><span class=lbl>${lbl}</span><span class=mut>n/a</span></div>`;
  const cls = pct>=85?'hot':(pct>=65?'warn':'');
  return `<div class=m><span class=lbl>${lbl}</span><div class=bar><div class="fill ${cls}" style="width:${Math.min(100,pct)}%"></div></div>`+
         `<span>${pct.toFixed(0)}%</span>${extra?`<span class=mut>${extra}</span>`:''}</div>`;
}
async function telem(){
  try{ const s=await (await fetch('/telemetry')).json();
    let h = meter('CPU', s.cpu_percent);
    const ram = s.mem_used_gb!=null?`${s.mem_used_gb}/${s.mem_total_gb} GB`:'';
    h += meter('RAM', s.mem_percent, ram);
    const g=s.gpu;
    if(g){ const gx=`${g.name||'GPU'}${g.temp!=null?' · '+g.temp.toFixed(0)+'°C':''}${g.mem_used!=null?' · '+(g.mem_used/1024).toFixed(1)+'GB':''}`;
      h += meter('GPU', g.util, gx); }
    else h += `<div class=m><span class=lbl>GPU</span><span class=mut>n/a</span></div>`;
    document.getElementById('telem').innerHTML = h;
  }catch(e){}
}
telem(); setInterval(telem,4000);

/* ── voice, all of it local: Whisper in, Piper out, openWakeWord in between. ────────────────
   Everything here goes through the chat, which means it goes through the intent router. Speaking
   "run diagnostics" runs them and speaking "approve" reaches the gate, the same as typing it. The
   mic used to post straight to /capture, so a spoken command was filed as a goal to do later. */
let spkOn=false, curA=null;
/* Resolves when Piper has finished, not when it starts. Conversation mode waits on this, and
   without it NOMAD listens to itself and answers its own reply. */
async function vspeak(t){ if(!spkOn||!t) return;
  try{ if(curA){ try{curA.pause();}catch(_){ } }
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
    if(!r.ok) return; const u=URL.createObjectURL(await r.blob()); const a=new Audio(u); curA=a;
    await new Promise(res=>{ a.onended=a.onerror=()=>{ URL.revokeObjectURL(u); res(); }; a.play().catch(res); });
  }catch(e){}
}
const spk=document.getElementById('spk');
spk.onclick=()=>{ spkOn=!spkOn; spk.style.background=spkOn?'var(--ok)':'#334'; spk.style.color=spkOn?'#000':'var(--text-muted)';
  const say='spoken replies: '+(spkOn?'on':'off');
  spk.innerHTML=ic(spkOn?'volume-2':'volume-x'); spk.title=say; spk.setAttribute('aria-label',say);
  spk.setAttribute('aria-pressed',spkOn); };
const mic=document.getElementById('mic'), vs=document.getElementById('vstatus'),
      wakeBtn=document.getElementById('wake'), liveBtn=document.getElementById('livevoice');
const hasMic = !!(navigator.mediaDevices && window.MediaRecorder);

/* One mic stream, shared. Asking for a second one while the first is open is how the wake loop
   and push-to-talk end up fighting over the device and both getting silence. */
let micStream=null;
async function getMic(){ if(!micStream || !micStream.active) micStream=await navigator.mediaDevices.getUserMedia({audio:true}); return micStream; }
function recordClip(stream, ms){
  return new Promise(res=>{
    let r; try{ r=new MediaRecorder(stream); }catch(e){ return res(null); }
    const ch=[]; r.ondataavailable=e=>{ if(e.data && e.data.size) ch.push(e.data); };
    r.onstop=()=>res(new Blob(ch,{type:r.mimeType||'audio/webm'}));
    r.start(); setTimeout(()=>{ try{r.stop();}catch(_){ } }, ms);
  });
}
async function transcribe(blob){
  /* Below about a kilobyte there is no speech in there, only room tone, and Whisper will happily
     hallucinate a sentence out of it. Cheaper to refuse than to act on an invented command. */
  if(!blob || blob.size<1200) return '';
  try{ const d=await (await fetch('/api/stt',{method:'POST',headers:{'Content-Type':blob.type},body:blob})).json();
    return (d.text||'').trim(); }catch(e){ return ''; }
}

/* push-to-talk: click, talk, click. The transcript goes through the chat, so the intent router
   sees it and a spoken command acts instead of being filed as a goal. */
let pttRec=null;
async function toggleMic(){
  if(pttRec){ try{pttRec.stop();}catch(e){} return; }
  let stream; try{ stream=await getMic(); }
  catch(e){ warned(vs,'mic unavailable'); return; }
  const ch=[]; pttRec=new MediaRecorder(stream);
  pttRec.ondataavailable=e=>{ if(e.data && e.data.size) ch.push(e.data); };
  pttRec.onstop=async()=>{
    const r=pttRec; pttRec=null;
    mic.innerHTML=ic('mic')+'<span>Speak</span>'; mic.style.background='var(--info)';
    const blob=new Blob(ch,{type:r.mimeType||'audio/webm'});
    if(blob.size<1200){ vs.textContent=''; return; }
    vs.textContent='transcribing…';
    const t=await transcribe(blob);
    if(!t){ vs.textContent='(didn’t catch that)'; return; }
    vs.textContent=''; await csend(t,false,true);
  };
  pttRec.start(); dotted(mic,true,'recording — click to stop'); mic.style.background='var(--danger)'; vs.textContent='';
}

/* conversation mode: say the wake word once, then keep talking. After each reply NOMAD listens
   for the next turn with no wake word, until a window comes back silent. */
let convoMode=false;
async function onWake(){
  if(pttRec||convoMode) return;
  convoMode=true; if(wakeBtn) wakeBtn.style.background='var(--ok)';
  try{
    let first=true;
    while(convoMode){
      vs.textContent=first?'listening…':'listening — just talk, pause to end';
      first=false;
      let stream; try{ stream=await getMic(); }catch(e){ break; }
      const t=await transcribe(await recordClip(stream,5000));
      if(!t) break;                 /* silence ends it, rather than a command to end it */
      vs.textContent='';
      await csend(t,false,true);    /* resolves after NOMAD has finished speaking */
    }
  }catch(e){}
  finally{
    convoMode=false; if(wakeBtn) wakeBtn.style.background=wakeOn?'var(--info)':'#334';
    vs.textContent=wakeOn?'— say “Hey Jarvis” to talk again —':'';
  }
}

/* wake word. openWakeWord first: it runs on 16 kHz PCM streamed to nomad-voice and fires the
   instant it hears the phrase. If that cannot start, fall back to transcribing short windows and
   matching the phrase, which works everywhere and is slower. A wake word that silently does not
   work is worse than a slow one, so the fallback says which one you got. */
let wakeOn=false, oww=null;
/* Double backslashes: COCKPIT is a plain Python string, so a lone backslash-b here reaches the
   browser as an actual backspace and the pattern silently stops matching word boundaries. */
const WAKE=/\\b(hey\\s+|ok\\s+|okay\\s+)?(nomad|jarvis|computer)\\b[\\s,.:;!?-]*/i;
async function wakeLoop(){
  if(!wakeOn) return;
  if(pttRec||convoMode){ setTimeout(wakeLoop,600); return; }   /* yield the mic */
  let stream; try{ stream=await getMic(); }catch(e){ wakeOn=false; if(wakeBtn) wakeBtn.style.background='#334'; return; }
  const clip=await recordClip(stream,3500);
  if(wakeOn && !convoMode && clip){
    const said=await transcribe(clip), m=said && said.match(WAKE);
    if(m){
      const cmd=said.slice(m.index+m[0].length).trim();
      if(cmd.length>1) await csend(cmd,false,true);
      await onWake();
    }
  }
  if(wakeOn) wakeLoop();
}
function downsample(buf,inRate,outRate){
  if(outRate>=inRate) return buf;
  const ratio=inRate/outRate, len=Math.floor(buf.length/ratio), out=new Float32Array(len);
  for(let i=0;i<len;i++) out[i]=buf[Math.floor(i*ratio)];
  return out;
}
function startOWW(){
  return new Promise(async (resolve,reject)=>{
    let stream; try{ stream=await getMic(); }catch(e){ return reject(e); }
    const ws=new WebSocket((location.protocol==='https:'?'wss':'ws')+'://'+location.host+'/ws/wake');
    ws.binaryType='arraybuffer';
    const AC=window.AudioContext||window.webkitAudioContext;
    let ac; try{ ac=new AC({sampleRate:16000}); }catch(e){ ac=new AC(); }
    const src=ac.createMediaStreamSource(stream), proc=ac.createScriptProcessor(4096,1,1),
          sink=ac.createGain(); sink.gain.value=0;   /* run the processor without echoing to the speakers */
    const inRate=ac.sampleRate;
    proc.onaudioprocess=e=>{
      if(!ws||ws.readyState!==1) return;
      const f=inRate===16000?e.inputBuffer.getChannelData(0):downsample(e.inputBuffer.getChannelData(0),inRate,16000);
      const i16=new Int16Array(f.length);
      for(let i=0;i<f.length;i++){ const x=Math.max(-1,Math.min(1,f[i])); i16[i]=x<0?x*0x8000:x*0x7fff; }
      ws.send(i16.buffer);
    };
    src.connect(proc); proc.connect(sink); sink.connect(ac.destination);
    oww={ws,ac,src,proc,sink};
    ws.onmessage=ev=>{ try{ const d=JSON.parse(ev.data);
      if(d.error){ stopOWW(); if(wakeOn) wakeLoop(); } else if(d.wake){ onWake(); } }catch(_){ } };
    ws.onopen=()=>resolve();
    ws.onerror=()=>{};
    ws.onclose=()=>{ if(oww&&oww.ws===ws) stopOWW(); };
    setTimeout(()=> ws.readyState===1?resolve():reject(new Error('wake ws timeout')), 4000);
  });
}
function stopOWW(){
  if(!oww) return; const o=oww; oww=null;
  try{o.proc.disconnect();}catch(_){ } try{o.sink.disconnect();}catch(_){ }
  try{o.src.disconnect();}catch(_){ } try{o.ac.close();}catch(_){ } try{o.ws.close();}catch(_){ }
}

/* live voice: real-time and interruptible. Audio goes both ways over a peer connection to
   nomad-voice, Silero VAD yields the moment you speak, and the reply comes back from the same
   brain the text chat uses. Signalling is the one cross-origin call on this page, because WebRTC
   media is peer-to-peer and cannot be proxied through the engine the way everything else is. */
let rtPc=null, rtStream=null, rtBase=null;
async function rtConfig(){
  if(rtBase!==null) return rtBase;
  try{ const c=await (await fetch('/api/voice-config')).json(); rtBase=(c&&c.rt_url)||'http://localhost:8200'; }
  catch(e){ rtBase='http://localhost:8200'; }
  return rtBase;
}
function rtHangup(msg, warn){
  if(rtStream){ rtStream.getTracks().forEach(t=>{ try{t.stop();}catch(_){ } }); rtStream=null; }
  if(rtPc){ try{rtPc.close();}catch(_){ } rtPc=null; }
  const a=document.getElementById('rt-audio'); if(a) a.srcObject=null;
  if(liveBtn){ liveBtn.style.background='#334'; liveBtn.style.color='var(--text-muted)'; }
  if(msg){ if(warn) warned(vs,msg); else vs.textContent=msg; }
}
async function rtConnect(){
  const base=await rtConfig();
  let stream;
  try{ stream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}}); }
  catch(e){ rtHangup('microphone blocked',true); return; }
  rtStream=stream;
  const pc=new RTCPeerConnection({iceServers:[{urls:'stun:stun.l.google.com:19302'}]});
  rtPc=pc; vs.textContent='live voice: connecting…';
  stream.getTracks().forEach(t=>pc.addTrack(t,stream));
  pc.ontrack=e=>{ const a=document.getElementById('rt-audio'); if(a) a.srcObject=e.streams[0]; };
  pc.onconnectionstatechange=()=>{ const c=pc.connectionState;
    if(c==='connected') vs.textContent='live voice — talk, interrupt any time';
    else if(c==='failed'||c==='disconnected'||c==='closed'){
      if(rtPc===pc) { if(c==='failed') rtHangup('live voice could not establish audio. Push-to-talk still works.',true); else rtHangup('— live voice ended —'); } } };
  await pc.setLocalDescription(await pc.createOffer({offerToReceiveAudio:true}));
  await Promise.race([                       /* wait for ICE, but never hang on a STUN server */
    new Promise(r=>{ if(pc.iceGatheringState==='complete') return r();
      const h=()=>{ if(pc.iceGatheringState==='complete'){ pc.removeEventListener('icegatheringstatechange',h); r(); } };
      pc.addEventListener('icegatheringstatechange',h); }),
    new Promise(r=>setTimeout(r,2000))]);
  try{
    const res=await fetch(base+'/api/offer',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})});
    if(!res.ok){ rtHangup('voice service error '+res.status,true); return; }
    await pc.setRemoteDescription(await res.json());
  }catch(e){ rtHangup('voice service unreachable ('+base+')',true); }
}

if(hasMic){
  mic.onclick=toggleMic;
  if(wakeBtn) wakeBtn.onclick=async()=>{
    if(wakeOn){ wakeOn=false; convoMode=false; wakeBtn.style.background='#334'; stopOWW(); vs.textContent=''; return; }
    wakeOn=true; wakeBtn.style.background='var(--info)';
    try{ await startOWW(); vs.textContent='wake word armed — say “Hey Jarvis”'; return; }
    catch(e){ vs.textContent='listening for “Hey Jarvis” (local fallback, a touch slower)'; }
    wakeLoop();
  };
  if(liveBtn) liveBtn.onclick=async()=>{
    if(rtPc||rtStream){ rtHangup('— live voice ended —'); return; }
    /* One mic, one listener. Leaving the wake word armed would have both of them holding it. */
    if(wakeOn){ wakeOn=false; convoMode=false; wakeBtn.style.background='#334'; stopOWW(); }
    liveBtn.style.background='var(--ok)'; liveBtn.style.color='#000';
    await rtConnect();
  };
}else{
  mic.style.display='none';
  if(wakeBtn) wakeBtn.style.display='none';
  if(liveBtn) liveBtn.style.display='none';
}

/* project terminal: pick a project → interactive claude session over the engine's own /ws/terminal */
(function(){
  let term, fit, ws, cur=null, manual=false;
  const panel=document.getElementById('ws'), elTerm=document.getElementById('wsterm'),
        elProj=document.getElementById('wsproj'), elStat=document.getElementById('wsstat'),
        sel=document.getElementById('termproj');
  async function loadProjects(){
    try{ const d=await (await fetch('/projects')).json();
      sel.innerHTML=(d.projects||[]).map(p=>`<option>${p}</option>`).join('') || '<option value="">no projects</option>';
    }catch(e){ sel.innerHTML='<option value="">no projects</option>'; }
  }
  function ensure(){ if(term) return;
    term=new window.Terminal({cursorBlink:true,fontFamily:'ui-monospace,"Share Tech Mono",monospace',fontSize:13,
      theme:{background:'#05060a',foreground:'#ffb000',cursor:'#ffb000',selectionBackground:'#33405a'},scrollback:5000});
    fit=new window.FitAddon.FitAddon(); term.loadAddon(fit); term.open(elTerm);
    term.onData(d=>{ if(ws&&ws.readyState===1) ws.send(d); });
    window.addEventListener('resize',doFit);
  }
  function doFit(){ if(!fit||panel.classList.contains('ws-hidden')) return; try{fit.fit();}catch(e){return;}
    if(ws&&ws.readyState===1) ws.send(JSON.stringify({type:'resize',rows:term.rows,cols:term.cols})); }
  function connect(p){ manual=false;
    const proto=location.protocol==='https:'?'wss':'ws';
    const url=`${proto}://${location.host}/ws/terminal?project=${encodeURIComponent(p)}&cols=${(term&&term.cols)||80}&rows=${(term&&term.rows)||24}`;
    dotted(elStat,false,'connecting');
    ws=new WebSocket(url); ws.binaryType='arraybuffer';
    ws.onopen=()=>{ dotted(elStat,true,'LIVE'); setTimeout(doFit,60); term.focus(); };
    ws.onmessage=e=>{ if(typeof e.data==='string') term.write(e.data); else term.write(new Uint8Array(e.data)); };
    ws.onclose=()=>{ dotted(elStat,false,manual?'closed':'disconnected'); };
    ws.onerror=()=>{ dotted(elStat,false,'error'); };
  }
  function open(p){ if(!p) return; ensure(); cur=p; elProj.textContent=p;
    panel.classList.remove('ws-hidden'); setTimeout(()=>{doFit();connect(p);},30); }
  function close(){ manual=true; if(ws){try{ws.close();}catch(e){}} panel.classList.add('ws-hidden'); }
  document.getElementById('termopen').onclick=()=>open(sel.value);
  document.getElementById('wsclose').onclick=close;
  document.getElementById('wsre').onclick=()=>{ if(!cur)return; if(ws){try{ws.close();}catch(e){}} if(term)term.reset(); connect(cur); };
  document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&!panel.classList.contains('ws-hidden')) close(); });
  loadProjects();
})();

/* PROJECTS panel (#787-#789): one row per OpenProject project from /pm-projects, with open tasks
   by status and the high-priority count. Sorting happens here, on the counts already fetched. In
   every sort a project with nothing open goes last, by name. */
const escA=s=>esc(String(s??'')).replaceAll('"','&quot;');
const PROJ_CHIPS=['Ready','In Progress','In Review','Blocked','Awaiting approval','Backlog','New'];
const PROJ_SORTS={prio:'Tasks by priority', open:'Number of tasks', run:'Running status', name:'Project name'};
// Running status order (#958): what is working first, then what waits on the operator, then the rest.
const RUN_ORDER=['running','needs_you','waiting','budget_paused','error','idle','off'];
function projRunRank(r){ const pids=apPidsFor(r.identifier);
  return pids.length ? Math.min(...pids.map(pid=>{ const i=RUN_ORDER.indexOf(apProj(pid).state); return i<0?RUN_ORDER.length:i; }))
    : RUN_ORDER.length+1; }
let projData=null;
function projSortGet(){ try{ const v=localStorage.getItem('nomad.projSort'); if(v in PROJ_SORTS) return v; }catch(e){} return 'prio'; }
function projSortSet(v){ try{ localStorage.setItem('nomad.projSort',v); }catch(e){} drawProjects(); }
function projSorted(list, how){
  const byName=(a,b)=>a.name.localeCompare(b.name,undefined,{sensitivity:'base'});
  const p=r=>r.by_priority||{};
  const cmp={
    name: byName,
    open: (a,b)=>b.open-a.open||byName(a,b),
    run: (a,b)=>projRunRank(a)-projRunRank(b)||byName(a,b),
    prio: (a,b)=>(p(b).Immediate-p(a).Immediate)||(p(b).High-p(a).High)||(p(b).Normal-p(a).Normal)||(p(b).Low-p(a).Low)||byName(a,b),
  }[how]||byName;
  // By running status a project with no open task still sorts by its state: autopilot can be on it.
  if(how==='run') return [...list].sort(cmp);
  return [...list.filter(r=>r.open>0).sort(cmp), ...list.filter(r=>!(r.open>0)).sort(byName)];
}
function projRow(r){
  const st=r.by_status||{}, pr=r.by_priority||{}, hi=(pr.Immediate||0)+(pr.High||0);
  const split=[pr.Immediate?pr.Immediate+' Immediate':'', pr.High?pr.High+' High':''].filter(Boolean).join(', ');
  const chips=PROJ_CHIPS.filter(k=>st[k]).map(k=>
    `<span class="chip${k==='Blocked'?' blk':k==='Awaiting approval'?' aw':''}">${esc(k)} ${st[k]}</span>`).join('');
  return `<div class="proj${r.open?'':' zero'}" data-ident="${escA(r.identifier)}">`+
    `<div class=pn><span class=pt>${esc(r.name)}</span><span class=pid>${esc(r.identifier)}</span>`+
      `<div class=pc><span class=po>${r.open} open</span>`+
      (hi?`<span class=hi title="${escA(split)}">${ic('flame')}${hi} high</span>`:'')+chips+`</div></div>`+
    `<div class=pctl><a class=opbtn href="${escA(r.url)}" target=_blank rel=noopener title="Open ${escA(r.name)} in OpenProject">`+
      `${ic('external-link')}<span>Open in OpenProject</span></a>`+projNextBtn(r)+apRowCtl(r.identifier)+`</div>`+
    (projNextMsg[r.id]?`<div class="pnmsg${projNextMsg[r.id].ok?' ok':''}">${esc(projNextMsg[r.id].text)}</div>`:'')+
    apRowLines(r.identifier)+
  `</div>`;
}
/* 'next' per project (#793): POST /run-next-task {project}. The engine picks across that board,
   Ready before Backlog, then priority, then WP number, and sends the pick to the gate. /pm-projects
   says whether anything is pickable, so the button is off, with the reason, when nothing is. */
const projNextBusy=new Set(), projNextMsg={};
function projNextBtn(r){
  const n=r.pickable||0, held=r.pickable_blocked||0;
  const why=n?'':held?`Nothing to pick: ${held} Ready or Backlog task${held>1?'s have':' has'} an open blocker`:
    'Nothing to pick: no Task or Bug in Ready or Backlog';
  const t=n?`Send the top Ready or Backlog task of ${r.name} to the gate (${n} pickable)`:why;
  const dis=!n||projNextBusy.has(r.id)?' disabled':'';
  return `<button class="apbtn plain pnext" data-id="${escA(r.id)}" onclick="projNext(+this.dataset.id)" title="${escA(t)}" aria-label="${escA('next: '+t)}"${dis}>`+
    `${ic('skip-forward')}<span>${projNextBusy.has(r.id)?'Picking…':'next'}</span></button>`;
}
async function projNext(id){
  if(projNextBusy.has(id)) return;
  projNextBusy.add(id); delete projNextMsg[id]; drawProjects();
  let d=null;
  try{ d=await (await fetch('/run-next-task',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({project:id})})).json(); }
  catch(e){ d={error:'could not reach the engine'}; }
  projNextBusy.delete(id);
  projNextMsg[id]= d&&d.error ? {text:'next: '+d.error, ok:false} :
    d&&d.done ? {text:'next: '+(d.note||'nothing to pick'), ok:false} :
    {text:`next: #${d.task_id} ${d.task_title||''} is at the gate${d.lane?' ('+d.lane+')':''}. Approve or reject it there.`, ok:true};
  pollProjects();
}
function drawProjects(){
  const box=document.getElementById('projects'), d=projData;
  if(projSheet){ drawProjDetail(); apDrawOther(); return; }
  if(!d){ box.innerHTML=''; apDrawOther(); return; }
  const how=projSortGet();
  const opts=Object.entries(PROJ_SORTS).map(([k,v])=>`<option value=${k}${k===how?' selected':''}>${v}</option>`).join('');
  const age=d.age>=120?Math.round(d.age/60)+' min':d.age+' s';
  box.innerHTML=`<div class=ph><span>PROJECTS · OpenProject · ${d.projects.length}</span><span class=sp></span>`+
      `<label>${ic('arrow-up-down')} <select aria-label="Sort projects" onchange="projSortSet(this.value)">${opts}</select></label></div>`+
    (d.stale?`<div class=stale>${ic('triangle-alert')} OpenProject unreachable. These counts are ${age} old.</div>`:'')+
    projFilterBar(d.projects)+
    (projFilter==='all'?d.projects:d.projects.filter(r=>projState(r.identifier)===projFilter))
      .length ? projSorted(projFilter==='all'?d.projects:d.projects.filter(r=>projState(r.identifier)===projFilter), how).map(projListRow).join('')
      : '<div class=nowempty>No project is in this state.</div>';
  apDrawOther();
}
/* Projects (#1176): a list with a state filter; a row opens the project's detail at #projects/<id>,
   where the controls, the status lines and the board numbers live. The full card (projRow) is the
   detail, so Run, Stop, Autopilot and the decisions are the same code as before. */
const PROJ_STATE_LABEL={running:'RUNNING',needs_you:'NEEDS YOU',waiting:'WAITING',budget_paused:'PAUSED',error:'ERROR',idle:'IDLE',off:'OFF'};
function projState(ident){
  let best='off', rank=RUN_ORDER.length;
  apPidsFor(ident).forEach(pid=>{ const s=apProj(pid).state, i=RUN_ORDER.indexOf(s); if(i>=0 && i<rank){ rank=i; best=s; } });
  return best;
}
function projFilterBar(list){
  const ct={all:list.length}; RUN_ORDER.forEach(s=>ct[s]=0);
  list.forEach(r=>{ ct[projState(r.identifier)]++; });
  const chip=(k,label)=>`<button type=button class="chip ${projFilter===k?'on':''}" aria-pressed="${projFilter===k}" onclick="projFilterSet('${k}')">${label}<span class=ct>${ct[k]||0}</span></button>`;
  return `<div class=lanes>`+chip('all','ALL')+RUN_ORDER.filter(s=>ct[s]||projFilter===s).map(s=>chip(s,PROJ_STATE_LABEL[s])).join('')+`</div>`;
}
function projFilterSet(k){ projFilter=k; drawProjects(); }
function projListRow(r){
  const pr=r.by_priority||{}, hi=(pr.Immediate||0)+(pr.High||0), s=projState(r.identifier);
  const ny=apPidsFor(r.identifier).reduce((n,pid)=>n+apNeedsYou(apProj(pid)).length,0);
  return `<button type=button class="prow${r.open?'':' zero'}" data-ident="${escA(r.identifier)}" onclick="projOpen(this.dataset.ident)">`+
    `<span class=pr1><span class=pt>${esc(r.name)}</span><span class="apst ${s}">${PROJ_STATE_LABEL[s]}</span></span>`+
    `<span class=pr2><span>${r.open} open</span>`+(hi?`<span class=hi>${ic('flame')}${hi} high</span>`:'')+
    (ny?`<span class=nyou>${ny} need${ny>1?'':'s'} you</span>`:'')+`<span class=mut>${esc(r.identifier)}</span></span></button>`;
}
function projOpen(ident){ location.hash='projects/'+encodeURIComponent(ident); }
// Called by the shell on every hash change, while the script may still be evaluating: it only flips
// state and visibility, and draws on the next microtask, after every `let` is initialised.
function projSheetSet(ident){
  const was=projSheet; projSheet=ident||null;
  const sh=document.getElementById('projsheet'), box=document.getElementById('projects');
  if(!sh||!box) return;
  sh.hidden=!projSheet; box.hidden=!!projSheet;
  if(was===projSheet) return;
  Promise.resolve().then(()=>{ drawProjects(); drawBoard(); if(projSheet){ pollBoard(); scrollTo(0,0); } });
}
function drawProjDetail(){
  const el=document.getElementById('projdetail'), d=projData;
  const r=d&&d.projects.find(x=>x.identifier===projSheet);
  el.innerHTML = !d ? '<span class=mut>loading…</span>' :
    !r ? `<div class=nmsg>${ic('triangle-alert')} No project "${esc(projSheet)}" in OpenProject.</div>` :
    (d.stale?`<div class=stale>${ic('triangle-alert')} OpenProject unreachable. These counts are old.</div>`:'')+projRow(r);
}
async function pollProjects(){
  try{
    const r=await fetch('/pm-projects'); const d=await r.json();
    if(r.ok && Array.isArray(d.projects)) projData=d;
  }catch(e){}
  if(!apHold()) drawProjects();   // #998: not under an open stop question
}
pollProjects(); setInterval(pollProjects, 30000);

/* AUTOPILOT (#883, #884, PRD 5.10). /api/autopilot/status every 10 s. Each autopilot project gets a
   status line on its board's row in PROJECTS (the supervisor's `boards` says which row; a board may
   carry several projects, civis carries civis-poc and civis-site). A project whose board has no row
   (OpenProject down, or a board the panel does not list) gets its line in the strip instead, so Run
   and Stop are never out of reach. The strip holds the budget switch, the caps and the totals. */
const AP_STATES={off:'off',running:'running',waiting:'waiting',needs_you:'needs you',
  budget_paused:'budget paused',error:'error',idle:'idle'};
let apData=null, apErr='';
const apBusy=new Set(), apAsk=new Set(), apKill=new Map(), apOpen=new Set(), apMsgs={};
let apFormMsg={text:'',ok:false};
function apUsd(v){ return v==null||isNaN(v)?'—':'$'+Number(v).toFixed(2); }
function apAge(sec){ sec=Math.max(0,Math.round(sec)); return sec<60?sec+' s':sec<3600?Math.round(sec/60)+' min':
  (sec/3600).toFixed(sec<36000?1:0)+' h'; }
function apDayStart(now){ const d=new Date(now*1000); d.setHours(0,0,0,0); return d.getTime()/1000; }
// Done today: the merge queue's `task N: merged <sha> -> Done` events since local midnight. The
// event log keeps the last 50 per project, so a very busy day can count low; the digest has it all.
function apDoneToday(p, now){ const t0=apDayStart(now);
  return (p.events||[]).filter(e=>e.kind==='merge' && e.t>=t0 && /^task \\S+: merged\\b/.test(e.msg||'')).length; }
// The tasks waiting on the operator: all of the detail in `needs_you`, the "needs you:" part while
// running (scheduler.outcome writes both as "#id reason").
function apNeedsYou(p){
  const det=p.detail||''; let part='';
  if(p.state==='needs_you') part=det;
  else { const m=det.match(/needs you: ([^;]*)/); if(m) part=m[1]; }
  return [...new Set((part.match(/#(\\d+)/g)||[]).map(x=>x.slice(1)))];
}
// Of those, the ones in Awaiting approval, which the operator answers with Approve or Reject
// (docs/prd/autopilot-approval.md 5.3). Blocked ones only get their link.
function apAwaiting(p){
  const det=p.detail||'', out=new Set(), re=/#(\\d+)([^#;,]*)/g; let m;
  while((m=re.exec(det))) if(/awaiting approval/i.test(m[2])) out.add(m[1]);
  return out;
}
function apPidsFor(board){ const b=(apData&&apData.boards)||{};
  return Object.keys(b).filter(pid=>b[pid]===board).sort(); }
function apOpBase(){ const r=projData&&projData.projects&&projData.projects.find(x=>x.url);
  return r ? r.url.split('/projects/')[0] : ''; }
// Links go to the project-scoped page: OpenProject answers /work_packages/<id> with a redirect built
// from its own host name (localhost:8097), which Rails refuses as "unsafe" when the browser came in
// over the tailnet, so the short link is a 500.
function apTask(id, board){ const base=apOpBase(), t='#'+esc(String(id));
  const path=board ? '/projects/'+encodeURIComponent(board)+'/work_packages/'+id+'/activity' : '/work_packages/'+id;
  return base ? `<a class=wp href="${escA(base+path)}" target=_blank rel=noopener>${t}</a>` : t; }
function apProj(pid){ return ((apData&&apData.projects)||{})[pid]||{state:'off',enabled:false,autopilot:false,detail:'',workers:[],events:[]}; }
function apRunnable(pid){ return ((apData&&apData.runnable)||[]).includes(pid); }
// #890: the Autopilot ON/OFF switch, saved by the supervisor. It sets what the next Run does.
const AP_NOT_SET_UP='not set up for autopilot';
function apSwitch(pid){
  const p=apProj(pid), on=!!p.autopilot, a=escA(pid), dis=apBusy.has(pid)?' disabled':'';
  const t=(on?'Autopilot ON: Run keeps working the board until you stop it.':
    'Autopilot OFF: Run starts the top-priority task only, then the project stops by itself.')+
    (p.enabled?' A run already going keeps its mode; this applies to the next Run.':'');
  return `<button class=apsw role=switch aria-checked=${on} data-pid="${a}" data-on="${on?'1':''}" onclick="apSwitchSet(this.dataset.pid,!this.dataset.on)" title="${escA(t)}"${dis}>`+
    `<span class=knob></span><span>Autopilot ${on?'ON':'OFF'}</span></button>`;
}
function apCtlOff(why){ const t=escA(why);
  return `<button class=apsw role=switch aria-checked=false disabled title="${t}"><span class=knob></span><span>Autopilot OFF</span></button>`+
    `<button class="apbtn run" disabled title="${t}">${ic('play')}<span>Run</span></button>`; }
// Run/Stop (and Kill for a stopped project's last workers); the switch too when `sw` and runnable.
function apActs(pid, sw){
  const p=apProj(pid), runnable=apRunnable(pid), dis=apBusy.has(pid)?' disabled':'';
  const workers=p.workers||[], a=escA(pid);
  let acts=sw&&runnable?apSwitch(pid):'';
  if(p.enabled) acts+=`<button class="apbtn stop" data-pid="${a}" onclick="apStop(this.dataset.pid)"${dis}>${ic('square')}<span>Stop</span></button>`;
  else {
    if(runnable) acts+=`<button class="apbtn run" data-pid="${a}" onclick="apRun(this.dataset.pid)" title="${p.autopilot?'Run: keep working the board until stopped':'Run: the top-priority task only, then stop'}"${dis}>${ic('play')}<span>Run</span></button>`;
    if(workers.length) acts+=`<button class="apbtn kill" data-pid="${a}" onclick="apKillAsk(this.dataset.pid)"${dis}>${ic('x')}<span>Kill ${workers.length} worker${workers.length>1?'s':''}</span></button>`;
  }
  return acts;
}
// The row's own controls: the one runnable project on this board. None: disabled, with the reason.
// Several (civis carries civis-poc and civis-site): each status line below carries its own.
function apHeadPid(board){ const r=apPidsFor(board).filter(apRunnable); return r.length===1?r[0]:null; }
function apRowCtl(board){
  if(!apData) return apCtlOff(apErr?'autopilot status unavailable: '+apErr:'autopilot status loading');
  const r=apPidsFor(board).filter(apRunnable);
  if(!r.length) return apCtlOff(AP_NOT_SET_UP);
  return r.length===1 ? apActs(r[0], true) : '';
}
function apLine(pid, ctl){
  const d=apData, now=Date.now()/1000;
  const p=apProj(pid);
  const runnable=apRunnable(pid), busy=apBusy.has(pid), dis=busy?' disabled':'';
  const workers=p.workers||[], evs=p.events||[], a=escA(pid);
  const st=AP_STATES[p.state]?p.state:'error';
  const acts=ctl===false?'':apActs(pid, true);
  // #998: Let them finish first and green (the default), Cancel, then Kill on the far side. Kill
  // only arms the confirm row; nothing sends mode kill on one click.
  const nw=`${workers.length} worker${workers.length===1?'':'s'}`;
  const ask=apKill.has(pid) ? `<div class=ask><span>Kill ${nw} of ${esc(pid)}? Their runs end now and can't be resumed; branches are kept.</span>`+
    `<button class="apbtn run" data-pid="${a}" onclick="apKillOff(this.dataset.pid)">Cancel</button><span class=sp></span>`+
    `<button class="apbtn kill" data-pid="${a}" onclick="apKillConfirm(this.dataset.pid)"${dis}>${ic('x')}<span>Confirm kill</span></button></div>` :
    apAsk.has(pid) ? `<div class=ask><span>Stop ${esc(pid)}. ${nw} running:</span>`+
    `<button class="apbtn run" data-pid="${a}" onclick="apStopMode(this.dataset.pid,'finish')"${dis}>Let them finish</button>`+
    `<button class="apbtn plain" data-pid="${a}" onclick="apAskOff(this.dataset.pid)">Cancel</button><span class=sp></span>`+
    `<button class="apbtn kill" data-pid="${a}" onclick="apKillAsk(this.dataset.pid)"${dis}>${ic('x')}<span>Kill them</span></button></div>` : '';
  const board=(d.boards||{})[pid], task=id=>apTask(id, board);
  const wk=workers.map(w=>{ const age=w.started?' '+apAge(now-w.started):'';
    return `<span class=wk title="${escA((w.branch||'')+' started '+(w.started?new Date(w.started*1000).toLocaleTimeString():'?'))}">${task(w.task)}${esc(age)}</span>`; }).join(' ');
  const spend=((d.budget||{}).by_project||{})[pid];
  const ny=apNeedsYou(p), aw=apAwaiting(p), dec=apDecide[pid];
  const nyItem=id=>task(id)+(aw.has(id)?
    `<button class="apbtn plain apdb" data-pid="${a}" data-id="${escA(id)}" onclick="apDecideOpen(this.dataset.pid,this.dataset.id,'approve')"${dis}>Approve</button>`+
    `<button class="apbtn plain apdb" data-pid="${a}" data-id="${escA(id)}" onclick="apDecideOpen(this.dataset.pid,this.dataset.id,'reject')"${dis}>Reject</button>`:'');
  const decBox=dec ? `<div class=apdec><label for="apdt-${a}">${dec.decision==='approve'?
      `Approve #${esc(dec.id)}. Your answer, optional (for a question with options, type the option):`:
      `Reject #${esc(dec.id)}. Your reason, required; the worker gets it as its instruction:`}</label>`+
    `<textarea id="apdt-${a}" class=apdtext data-pid="${a}" rows=2 oninput="apDecideText(this)">${esc(dec.text)}</textarea>`+
    `<button class="apbtn ${dec.decision==='approve'?'run':'kill'}" data-pid="${a}" onclick="apDecideSend(this.dataset.pid)"${dis}>${dec.decision==='approve'?'Send approval':'Send rejection'}</button>`+
    `<button class="apbtn plain" data-pid="${a}" onclick="apDecideOff(this.dataset.pid)">Cancel</button></div>` : '';
  const last=evs.length?evs[evs.length-1]:null;
  const recent=evs.slice(-8).reverse();
  const msg=apMsgs[pid];
  return `<div class=ap data-pid="${a}"><div class=top>`+
      `<span class="apst ${st}">${esc(AP_STATES[st])}</span><span class=apid>${esc(pid)}</span>`+
      `<span class=apd>${esc(p.detail||(runnable?'':'not set up for the autopilot (no autopilot: block or no board)'))}</span>`+
      (p.enabled&&p.run_mode==='once'?`<span class=apid title="Autopilot OFF when Run was pressed: one task, then off">one task</span>`:'')+
      (acts?`<span class=acts>${acts}</span>`:'')+`${ask}</div>`+
    `<div class=meta><span>workers <b>${workers.length}</b>${wk?' '+wk:''}</span>`+
      `<span>done today <b>${apDoneToday(p,now)}</b></span><span>spend today <b>${apUsd(spend||0)}</b></span>`+
      (ny.length?`<span class=nyou>needs you: ${ny.map(nyItem).join(', ')}</span>`:'')+`</div>`+decBox+
    (last?`<div class=last>last: ${esc(last.kind)} · ${esc(last.msg)} · ${apAge(now-last.t)} ago</div>`:'')+
    (recent.length>1?`<details data-pid="${a}"${apOpen.has(pid)?' open':''} ontoggle="apToggle(this)"><summary>recent events (${evs.length})</summary>`+
      recent.map(e=>`<div class=ev><b>${esc(new Date(e.t*1000).toLocaleTimeString())}</b> ${esc(e.kind)} · ${esc(e.msg)}</div>`).join('')+`</details>`:'')+
    (msg?`<div class="apmsg${msg.ok?' ok':''}">${esc(msg.text)}</div>`:'')+
  `</div>`;
}
function apRowLines(board){
  if(!apData) return '';
  const pids=apPidsFor(board), head=apHeadPid(board);
  return pids.length ? `<div class=aps>${pids.map(pid=>apLine(pid, pid!==head)).join('')}</div>` : '';
}
// Projects whose board has no row in PROJECTS, drawn in the strip.
function apDrawOther(){
  const box=document.getElementById('apother'); if(!box) return;
  if(!apData){ box.innerHTML=''; return; }
  const rows=new Set(((projData&&projData.projects)||[]).map(r=>r.identifier));
  const b=apData.boards||{};
  const pids=Object.keys(b).filter(pid=>!rows.has(b[pid])).sort();
  box.innerHTML=pids.length?`<div class=aps><div class=last>Not on a PROJECTS row (board unlisted or OpenProject down):</div>${pids.map(pid=>apLine(pid)).join('')}</div>`:'';
}
// The answer box: one per project line, its text kept here so a redraw does not lose it.
const apDecide={};
function apDecideOpen(pid, id, decision){
  const prev=apDecide[pid];
  apDecide[pid]={id, decision, text: prev&&prev.id===id ? prev.text : ''};
  apMsgs[pid]=null; drawProjects();
  const t=typeof document!=='undefined' && document.getElementById('apdt-'+pid); if(t&&t.focus) t.focus();
}
function apDecideText(el){ const d=apDecide[el.dataset.pid]; if(d) d.text=el.value; }
function apDecideOff(pid){ delete apDecide[pid]; drawProjects(); }
function apDecideBusy(){ const el=typeof document!=='undefined' && document.activeElement;
  return !!(el && el.classList && el.classList.contains('apdtext')); }
async function apDecideSend(pid){
  const d=apDecide[pid]; if(!d) return;
  const text=(d.text||'').trim();
  if(d.decision==='reject' && !text){ apMsgs[pid]={ok:false,text:'Write the reason: it is what the worker gets.'}; drawProjects(); return; }
  apBusy.add(pid); apMsgs[pid]=null; drawProjects();
  const res=await apPost(`/tasks/${encodeURIComponent(d.id)}/decide`, {decision:d.decision, text});
  apBusy.delete(pid);
  if(res.ok){ delete apDecide[pid];
    apMsgs[pid]={ok:true,text:`#${d.id} moved to ${res.data.status}; the autopilot picks it up at its next pass.`}; }
  else apMsgs[pid]={ok:false,text:res.error};
  await apPoll();
}
function apToggle(el){ const pid=el.dataset.pid; if(el.open) apOpen.add(pid); else apOpen.delete(pid); }
function apAskOff(pid){ apAsk.delete(pid); drawProjects(); }
async function apPost(path, body){
  let r, text='';
  try{ r=await fetch('/api/autopilot'+path,{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body||{})}); text=await r.text(); }
  catch(e){ return {ok:false,error:'Could not reach the engine.'}; }
  let d={}; try{ d=JSON.parse(text); }catch(e){ d={}; }
  // The auth middleware answers a refused write in plain text, the proxy in JSON.
  if(!r.ok || d.ok===false) return {ok:false,error:(d.error?d.error+(d.detail?': '+d.detail:''):text||('HTTP '+r.status))};
  return {ok:true,data:d};
}
async function apAct(pid, path, body, done){
  apBusy.add(pid); apMsgs[pid]=null; drawProjects();
  const res=await apPost(path, body);
  apBusy.delete(pid); apAsk.delete(pid);
  apMsgs[pid]=res.ok?{ok:true,text:done}:{ok:false,text:res.error};
  await apPoll();
  if(apHold()) drawProjects();   // the poll held back for another line's question; show this result anyway
}
function apRun(pid){ return apAct(pid, `/projects/${encodeURIComponent(pid)}/start`, null,
  apProj(pid).autopilot?'Autopilot on; a pass is running.':'Running the top task only; the project stops when it is done.'); }
function apSwitchSet(pid, on){ return apAct(pid, `/projects/${encodeURIComponent(pid)}/autopilot`, {on:!!on},
  on?'Autopilot ON: Run keeps working the board.':'Autopilot OFF: Run does one task, then stops.'); }
function apStop(pid){
  const p=((apData&&apData.projects)||{})[pid]||{};
  // Nothing running: nothing to ask. Otherwise finish (the default) or kill, PRD 5.10.
  if(!(p.workers||[]).length) return apStopMode(pid,'finish');
  apAsk.add(pid); drawProjects();
}
function apStopMode(pid, mode){
  return apAct(pid, `/projects/${encodeURIComponent(pid)}/stop`, {mode},
    mode==='kill'?'Stop sent (kill): workers killed, their branches kept.':'Stop sent (finish): running workers finish; none were killed.');
}
// #998: kill is the one stop that can't be undone, so both kill buttons only arm this confirm. A
// confirm that comes too soon after arming is the same click landing twice, and is ignored.
const AP_KILL_ARM_MS=600;
function apKillAsk(pid){ apKill.set(pid, Date.now()); apMsgs[pid]=null; drawProjects(); }
function apKillOff(pid){ apKill.delete(pid); drawProjects(); }   // back to the stop question, if Stop opened it
function apKillConfirm(pid){
  const t=apKill.get(pid);
  if(t==null || Date.now()-t<AP_KILL_ARM_MS) return;
  apKill.delete(pid);
  return apStopMode(pid,'kill');
}
// A poll does not redraw the project lines while a stop or kill question is open, or for a while
// after the pointer went over an autopilot button, so the button under a click never moves (#998).
const AP_HOVER_HOLD_MS=30000;
let apHoverAt=0;
function apHold(){ return apAsk.size>0 || apKill.size>0 || (apHoverAt>0 && Date.now()-apHoverAt<AP_HOVER_HOLD_MS); }
if(typeof document!=='undefined' && document.addEventListener){
  document.addEventListener('pointerover', e=>{
    const t=e.target, on=!!(t && t.closest && t.closest('.ap .acts, .ap .ask, .pctl'));
    if(on){ if(!apHoverAt) apHoverAt=Date.now(); } else apHoverAt=0; });
}
// The strip. The form is redrawn only while nobody is editing it, so a poll never eats a keystroke.
let apFormDirty=false;
function apFormBusy(){ const f=document.getElementById('apform'); return apFormDirty || (f && f.contains(document.activeElement)); }
function apDrawStrip(){
  const head=document.getElementById('aphead'), sum=document.getElementById('apsum'),
        form=document.getElementById('apform'), gm=document.getElementById('apgmsg');
  gm.className='apmsg'+(apFormMsg.ok?' ok':''); gm.textContent=apFormMsg.text;
  const d=apData;
  if(!d){ head.innerHTML='<span>AUTOPILOT</span>';
    sum.innerHTML=apErr?`<span class=mut>${ic('triangle-alert')} ${esc(apErr)}</span>`:'<span class=mut>loading…</span>';
    form.innerHTML=''; return; }
  const cfg=d.config||{}, bud=d.budget||{}, projs=d.projects||{};
  const hb=d.heartbeat_age_s, fresh=hb!=null && hb<180;
  head.innerHTML=`<span>AUTOPILOT</span><span class=mut style="letter-spacing:0"><span class="sdot${fresh?' on':''}"></span>`+
    `${fresh?'supervisor up':'supervisor quiet for '+apAge(hb||0)}</span><span class=sp></span>`+
    `<button class="apbtn plain" onclick="apPass(this)">${ic('rotate-cw')}<span>Pass now</span></button>`;
  const all=Object.values(projs), now=Date.now()/1000;
  const nw=all.reduce((n,p)=>n+(p.workers||[]).length,0);
  const byState={}; all.forEach(p=>{ if(p.enabled||p.state!=='off') byState[p.state]=(byState[p.state]||0)+1; });
  const states=Object.entries(byState).map(([k,v])=>`${v} ${esc(AP_STATES[k]||k)}`).join(', ')||'nothing on';
  const done=all.reduce((n,p)=>n+apDoneToday(p,now),0);
  const unlimited=(bud.mode||cfg.budget_mode)==='unlimited';
  sum.innerHTML=`<span>workers <b>${nw}</b> / ${cfg.global_cap??'—'}</span><span>projects: ${states}</span>`+
    `<span>done today <b>${done}</b></span><span>spent today <b>${apUsd(bud.spent_usd)}</b></span>`+
    `<span>remaining <b>${unlimited?'no limit':apUsd(bud.remaining_usd)}</b>${unlimited?'':' of '+apUsd(bud.cap_usd??cfg.daily_cap_usd)}</span>`+
    (bud.allowed===false?`<span class=nyou>${ic('triangle-alert')} ${esc(bud.detail||'budget spent')}</span>`:'')+
    (d.runnable_error?`<span class=nyou>${ic('triangle-alert')} ${esc(d.runnable_error)}</span>`:'');
  if(apFormBusy()) return;
  const mode=cfg.budget_mode==='unlimited'?'unlimited':'cap';
  form.innerHTML=`<label>BUDGET<span class=apseg role=group aria-label="budget mode">`+
      `<button type=button aria-pressed=${mode==='cap'} onclick="apMode('cap')">Daily cap</button>`+
      `<button type=button aria-pressed=${mode==='unlimited'} onclick="apMode('unlimited')">No limit</button></span></label>`+
    `<label>DAILY CAP $<input id=apcap type=number inputmode=decimal min=0 step=1 value="${escA(cfg.daily_cap_usd)}"${mode==='unlimited'?' disabled':''} oninput="apFormDirty=true"></label>`+
    `<label>GLOBAL CAP<input id=apglobal type=number inputmode=numeric min=0 step=1 value="${escA(cfg.global_cap)}" oninput="apFormDirty=true"></label>`+
    `<label>PER PROJECT<input id=apper type=number inputmode=numeric min=0 step=1 value="${escA(cfg.per_project_cap)}" oninput="apFormDirty=true"></label>`+
    `<button class="apbtn run" onclick="apSave(this)">Save caps</button>`;
}
async function apConfig(patch, btn){
  if(btn) btn.disabled=true;
  const res=await apPost('/config', patch);
  if(btn) btn.disabled=false;
  apFormMsg=res.ok?{ok:true,text:'Saved. It applies on the next pass.'}:{ok:false,text:res.error};
  if(res.ok){ apFormDirty=false; if(document.activeElement) document.activeElement.blur(); }
  await apPoll();
}
function apMode(m){ apFormDirty=false; return apConfig({budget_mode:m}); }
function apSave(btn){
  const cfg=(apData&&apData.config)||{}, patch={};
  const num=(id,int)=>{ const el=document.getElementById(id); if(!el||el.disabled||el.value==='') return undefined;
    const v=Number(el.value); return int?Math.trunc(v):v; };
  const cap=num('apcap',false), g=num('apglobal',true), pp=num('apper',true);
  if(cap!==undefined && cap!==cfg.daily_cap_usd) patch.daily_cap_usd=cap;
  if(g!==undefined && g!==cfg.global_cap) patch.global_cap=g;
  if(pp!==undefined && pp!==cfg.per_project_cap) patch.per_project_cap=pp;
  if([cap,g,pp].some(v=>v!==undefined && (isNaN(v)||v<0))){ apFormMsg={ok:false,text:'Caps must be numbers, 0 or more.'}; apDrawStrip(); return; }
  if(!Object.keys(patch).length){ apFormDirty=false; apFormMsg={ok:true,text:'Nothing changed.'}; apDrawStrip(); return; }
  return apConfig(patch, btn);
}
async function apPass(btn){
  btn.disabled=true; const res=await apPost('/pass'); btn.disabled=false;
  apFormMsg=res.ok?{ok:true,text:'Pass queued.'}:{ok:false,text:res.error};
  await apPoll();
}
async function apPoll(){
  try{
    const r=await fetch('/api/autopilot/status'); const d=await r.json();
    if(r.ok && d.ok!==false){ apData=d; apErr=''; }
    else { apData=null; apErr=(d.error||('HTTP '+r.status))+(d.detail?': '+d.detail:''); }
  }catch(e){ apData=null; apErr='Could not reach the engine.'; }
  apDrawStrip(); if(!apDecideBusy() && !apHold()) drawProjects();
  drawNow();
}
apPoll(); setInterval(apPoll, 10000);

/* Now (#1175): the queue of what waits on the operator. Engine runs at the gate and failed runs come
   from the runs feed, tasks in Awaiting approval (and blocked ones) from the autopilot poll. A task's
   question is read from its ticket the first time it shows, and kept until it leaves the queue.
   Short questions and options are shown whole; a long or structured comment says "read the ticket". */
let nowRuns=[];
const nowTasks={};   // task id -> {q, loading, sel, rej, text, busy, msg}
/* Agent asks (#1363): a question or a permission ask from an OWUI agent run, waiting in the bridge.
   Answered here, the run continues the same way as when the operator answers in the chat. */
let nowAsks=[];
const nowAskS={};    // ask id -> {sel:{q:[option index]}, other:{q:text}, note, busy, msg}
function nowAge(ms){ return ms?apAge((Date.now()-ms)/1000)+' ago':''; }
function nowHold(){ const a=document.activeElement; return !!(a && a.closest && a.closest('#nowq') && a.tagName==='TEXTAREA'); }
async function nowLoadAsks(){
  try{ const r=await fetch('/api/asks?state=pending'); const d=await r.json();
    nowAsks=(r.ok&&d.ok!==false&&Array.isArray(d.asks))?d.asks.filter(a=>a&&a.ask_id&&a.state==='pending'):[]; }
  catch(e){ nowAsks=[]; }
  drawNow();
}
function nowAskQs(a){ return Array.isArray(a.questions)?a.questions:[]; }
function nowItems(){
  const waiting=[], failed=[], seen=new Set();
  nowAsks.slice().sort((a,b)=>(+a.created||0)-(+b.created||0)).forEach(a=>waiting.push({kind:'ask',a}));
  nowRuns.filter(r=>r.status==='awaiting-approval' && r.lane!==AP_LANE)
    .sort((a,b)=>runTime(a)-runTime(b)).forEach(r=>waiting.push({kind:'run',r}));
  const projs=(apData&&apData.projects)||{};
  Object.keys(projs).sort().forEach(pid=>{
    const p=projs[pid], aw=apAwaiting(p);
    apNeedsYou(p).forEach(id=>{ if(seen.has(id)) return; seen.add(id);
      waiting.push({kind:'task', id, pid, awaiting:aw.has(id)}); });
  });
  nowRuns.filter(r=>r.status==='failed').sort((a,b)=>runTime(b)-runTime(a)).slice(0,5)
    .forEach(r=>failed.push({kind:'fail',r}));
  return waiting.concat(failed);
}
async function nowLoadQ(id){
  const s=nowTasks[id]||(nowTasks[id]={}); if(s.q||s.loading) return;
  s.loading=true;
  try{ const r=await fetch('/api/autopilot/tasks/'+encodeURIComponent(id)+'/question'); const d=await r.json();
    s.q=(r.ok&&d.ok!==false)?{...d,options:d.options||[]}:{error:d.error||('HTTP '+r.status)}; }
  catch(e){ s.q={error:'Could not read the ticket.'}; }
  s.loading=false; drawNow();
}
function nowRunCard(r){
  const p=r.proposal||{}, rid=r.run_id||'', a=escA(rid);
  const what=p.action?esc(p.action)+(p.target?' → '+esc(p.target):''):esc(r.type||'action');
  const pv=p.preview?`<div class=ns>${esc(String(p.preview).slice(0,320))}${String(p.preview).length>320?'…':''}</div>`:'';
  const warn=p.warning?`<div class=nmsg>${ic('triangle-alert')} ${esc(p.warning)}</div>`:'';
  return `<article class=nq data-run="${a}"><div class=nh><span class=ntag>${ic('flag')} Approve?</span>${ldot(r.lane)}${esc(r.lane||'')}`+
    `<span>${esc(rid)}</span><span>${nowAge(runTime(r))}</span></div>`+
    `<div class=nt>${what}</div>${pv}${warn}`+
    `<div class=nact><button class="nq-btn stop" data-rid="${a}" onclick="gate(this.dataset.rid,'rejected')">${ic('x')} Reject</button>`+
    `<button class="nq-btn go" data-rid="${a}" onclick="gate(this.dataset.rid,'approved')">${ic('check')} Approve</button>`+
    `<a class=nq-btn href="/cockpit/run/${encodeURIComponent(rid)}" target=_blank rel=noopener>Open run</a></div></article>`;
}
function nowFailCard(r){
  const rid=r.run_id||'', a=escA(rid);
  return `<article class="nq fail" data-run="${a}"><div class=nh><span class=ntag>${ic('x')} Failed</span>${ldot(r.lane)}${esc(r.lane||'')}`+
    `<span>${esc(rid)}</span><span>${nowAge(runTime(r))}</span></div>`+
    `<div class=nt>${esc(r.type||'run')} failed</div>`+
    `<div class=nact><button class="nq-btn" data-rid="${a}" onclick="retry(this.dataset.rid)">${ic('rotate-cw')} Retry</button>`+
    `<a class=nq-btn href="/cockpit/run/${encodeURIComponent(rid)}" target=_blank rel=noopener>Open run</a></div></article>`;
}
function nowTaskCard(it){
  const id=it.id, s=nowTasks[id]||{}, q=s.q, a=escA(id), board=((apData&&apData.boards)||{})[it.pid];
  const link=apTask(id, board).replace('class=wp','class=wp');
  let head=`<div class=nh><span class=ntag>${it.awaiting?ic('flag')+' Needs you':ic('triangle-alert')+' Blocked'}</span>${link}<span>${esc(it.pid)}</span></div>`;
  if(!it.awaiting){
    return `<article class="nq blocked" data-task="${a}">${head}<div class=nhint>Blocked: open the ticket to unblock it.</div></article>`;
  }
  let body='';
  if(!q) body=`<div class=nhint>${s.loading?'Reading the ticket…':'…'}</div>`;
  else if(q.error) body=`<div class=nmsg>${esc(q.error)} Open the ticket.</div>`;
  else {
    body=(q.subject?`<div class=nt>${esc(q.subject)}</div>`:'')+(q.stem?`<div class=ns>${esc(q.stem)}</div>`:'');
    if(q.options.length) body+=`<div class=nopts role=radiogroup aria-label="Options">`+q.options.map((o,i)=>
      `<button class=nopt role=radio aria-checked=${s.sel===i} data-id="${a}" data-i="${i}" onclick="nowPick(this.dataset.id,+this.dataset.i)">`+
      `<b>${esc(o.key)}</b><span>${esc(o.text)}</span></button>`).join('')+`</div>`;
    if(q.complex) body+=`<div class=nhint>${ic('triangle-alert')} This one is long or detailed. Read it in the ticket before you answer.</div>`;
  }
  const needPick=q&&q.options&&q.options.length&&s.sel==null;
  const dis=s.busy?' disabled':'';
  const rej=s.rej?`<textarea id="nr-${a}" data-id="${a}" placeholder="Why? The worker gets this as its instruction." oninput="nowText(this)">${esc(s.text||'')}</textarea>`+
    `<div class=nact><button class="nq-btn stop" data-id="${a}" onclick="nowSend(this.dataset.id,'reject')"${dis}>Send rejection</button>`+
    `<button class=nq-btn data-id="${a}" onclick="nowRej(this.dataset.id,false)">Cancel</button></div>`:'';
  const acts=s.rej?'':`<div class=nact><button class="nq-btn stop" data-id="${a}" onclick="nowRej(this.dataset.id,true)"${dis}>${ic('x')} Reject</button>`+
    `<button class="nq-btn go" data-id="${a}" onclick="nowSend(this.dataset.id,'approve')"${dis||(needPick?' disabled':'')}>${ic('check')} Approve${q&&q.options&&s.sel!=null?' option '+esc(q.options[s.sel].key):''}</button>`+
    (needPick?`<span class=nhint>Pick an option first</span>`:'')+`</div>`;
  const msg=s.msg?`<div class="nmsg${s.msg.ok?' ok':''}">${esc(s.msg.text)}</div>`:'';
  return `<article class=nq data-task="${a}">${head}${body}${rej}${acts}${msg}</article>`;
}
function nowAskAnswer(q,qi,s){
  const opts=q.options||[], o=((s.other||{})[qi]||'').trim();
  const sel=((s.sel||{})[qi]||[]).map(i=>opts[i]&&opts[i].label).filter(Boolean);
  if(!q.multiSelect && o) return o;                 // one answer: what was typed wins over a pick
  return (o?sel.concat(o):sel).join(', ');
}
function nowAskCard(a){
  const id=a.ask_id, x=escA(id), s=nowAskS[id]||{}, dis=s.busy?' disabled':'', isQ=a.kind==='question';
  const head=`<div class=nh><span class=ntag>${ic('flag')} ${isQ?'Question':'Approve?'}</span><span>agent chat</span>`+
    `<span>${esc(a.tool||'')}</span><span>${nowAge((+a.created||0)*1000)}</span></div>`;
  let body='';
  if(isQ) body=nowAskQs(a).map((q,qi)=>{
    const opts=q.options||[], sel=(s.sel||{})[qi]||[], multi=!!q.multiSelect;
    let h=`<div class=nt>${esc((q.header?q.header+': ':'')+(q.question||''))}</div>`;
    if(multi && opts.length) h+=`<div class=nhint>Pick one or more.</div>`;
    if(opts.length) h+=`<div class=nopts role=${multi?'group':'radiogroup'} aria-label="${escA(q.header||'Options')}">`+opts.map((o,oi)=>
      `<button class=nopt role=${multi?'checkbox':'radio'} aria-checked=${sel.includes(oi)} data-id="${x}" data-q="${qi}" data-o="${oi}" onclick="nowAskPick(this)"${dis}>`+
      `<b>${esc(o.label)}</b>${o.description&&o.description!==o.label?`<span>${esc(o.description)}</span>`:''}</button>`).join('')+`</div>`;
    return h+`<textarea data-id="${x}" data-q="${qi}" aria-label="${opts.length?'Other answer':'Answer'}" placeholder="${opts.length?'Other (optional)':'Your answer'}" oninput="nowAskText(this)">${esc((s.other||{})[qi]||'')}</textarea>`;
  }).join('');
  else body=`<div class=nt>${esc(a.tool||'tool')}${a.summary?': '+esc(String(a.summary).slice(0,320)):''}</div>`+
    (a.reason?`<div class=ns>${esc(a.reason)}</div>`:'')+
    `<textarea data-id="${x}" data-q="note" aria-label="Deny note" placeholder="Note if you deny (optional): the agent gets it" oninput="nowAskText(this)">${esc(s.note||'')}</textarea>`;
  const acts=`<div class=nact><button class="nq-btn stop" data-id="${x}" onclick="nowAskSend(this.dataset.id,'deny')"${dis}>${ic('x')} Deny</button>`+
    `<button class="nq-btn go" data-id="${x}" onclick="nowAskSend(this.dataset.id,'approve')"${dis}>${ic('check')} ${isQ?'Submit':'Approve'}</button></div>`;
  const msg=s.msg?`<div class="nmsg${s.msg.ok?' ok':''}">${esc(s.msg.text)}</div>`:'';
  return `<article class=nq data-ask="${x}">${head}${body}${acts}${msg}</article>`;
}
function drawNow(){
  const box=document.getElementById('nowq'); if(!box) return;
  const items=nowItems();
  navBadge('nb-now', items.length);
  items.filter(i=>i.kind==='task' && i.awaiting).forEach(i=>nowLoadQ(i.id));
  const ids=new Set(items.filter(i=>i.kind==='task').map(i=>i.id));
  Object.keys(nowTasks).forEach(k=>{ if(!ids.has(k) && !(nowTasks[k].msg)) delete nowTasks[k]; });
  const asks=new Set(nowAsks.map(a=>a.ask_id));
  Object.keys(nowAskS).forEach(k=>{ if(!asks.has(k)) delete nowAskS[k]; });
  const st=document.getElementById('nowstatus');
  if(st){ const projs=Object.values((apData&&apData.projects)||{});
    const running=projs.filter(p=>p.state==='running').length;
    st.innerHTML=`<span><span class="sdot on"></span>${items.length?items.length+' waiting on you':'nothing waiting on you'}</span>`+
      `<span class=mut>${apData?running+' project'+(running===1?'':'s')+' running':'autopilot unreachable'}</span>`;
  }
  if(nowHold()) return;
  box.innerHTML=items.length?items.map(i=>i.kind==='ask'?nowAskCard(i.a):i.kind==='run'?nowRunCard(i.r):i.kind==='fail'?nowFailCard(i.r):nowTaskCard(i)).join('')
    :`<div class=nowempty>${ic('check')} Nothing waits on you.</div>`;
}
function nowPick(id,i){ const s=nowTasks[id]||(nowTasks[id]={}); s.sel=s.sel===i?null:i; drawNow(); }
function nowRej(id,on){ const s=nowTasks[id]||(nowTasks[id]={}); s.rej=on; s.msg=null; drawNow(); }
function nowText(el){ const s=nowTasks[el.dataset.id]||(nowTasks[el.dataset.id]={}); s.text=el.value; }
async function nowSend(id, decision){
  const s=nowTasks[id]; if(!s) return;
  let text='';
  if(decision==='reject'){ text=(s.text||'').trim();
    if(!text){ s.msg={ok:false,text:'Write the reason: it is what the worker gets.'}; drawNow(); return; } }
  else if(s.q && s.q.options && s.sel!=null){ const o=s.q.options[s.sel]; text='Option '+o.key+': '+o.text; }
  s.busy=true; s.msg=null; drawNow();
  const res=await apPost('/tasks/'+encodeURIComponent(id)+'/decide', {decision, text});
  s.busy=false;
  if(res.ok){ s.msg={ok:true,text:'#'+id+' moved to '+res.data.status+'; the autopilot picks it up at its next pass.'}; s.rej=false; s.q=null; s.sel=null; s.text=''; }
  else s.msg={ok:false,text:res.error};
  await apPoll(); drawNow();
}
function nowAskPick(el){
  const id=el.dataset.id, qi=+el.dataset.q, oi=+el.dataset.o, a=nowAsks.find(x=>x.ask_id===id); if(!a) return;
  const q=nowAskQs(a)[qi]||{}, s=nowAskS[id]||(nowAskS[id]={}), sel=s.sel||(s.sel={}), cur=sel[qi]||[];
  if(q.multiSelect) sel[qi]=cur.includes(oi)?cur.filter(i=>i!==oi):cur.concat(oi);
  else { sel[qi]=cur.includes(oi)?[]:[oi]; if(s.other) s.other[qi]=''; }
  s.msg=null; drawNow();
}
function nowAskText(el){
  const s=nowAskS[el.dataset.id]||(nowAskS[el.dataset.id]={});
  if(el.dataset.q==='note') s.note=el.value; else (s.other||(s.other={}))[+el.dataset.q]=el.value;
}
async function nowAskSend(id, decision){
  const a=nowAsks.find(x=>x.ask_id===id); if(!a) return;
  const s=nowAskS[id]||(nowAskS[id]={}), body={ask_id:id, decision};
  if(a.kind==='question' && decision==='approve'){
    const ans={};
    for(const [qi,q] of nowAskQs(a).entries()){ const v=nowAskAnswer(q,qi,s);
      if(!v){ s.msg={ok:false,text:'Answer every question first.'}; drawNow(); return; }
      ans[q.question]=v; }
    body.answers=ans;
  } else if(a.kind!=='question' && decision==='deny' && (s.note||'').trim()) body.answers={_note:s.note.trim()};
  s.busy=true; s.msg=null; drawNow();
  let st=0, j={};
  try{ const r=await fetch('/api/asks/decide',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    st=r.status; j=await r.json().catch(()=>({})); }
  catch(e){ j={error:'Could not reach the engine.'}; }
  s.busy=false;
  if(st===200 && j.ok){ nowAsks=nowAsks.filter(x=>x.ask_id!==id); delete nowAskS[id]; }
  else if(st===409 && j.state==='expired') s.msg={ok:false,text:'This ask expired when the bridge restarted. Nothing was applied.'};
  else if(st===409) s.msg={ok:false,text:'Already answered elsewhere (chat or another tab). Nothing was changed.'};
  else s.msg={ok:false,text:j.error||('HTTP '+st)};
  drawNow();
}
nowLoadAsks(); setInterval(nowLoadAsks, 5000);

/* BOARD panel: the OpenProject board in numbers (/pm-summary, PM-01 P4). Per-project detail is
   OpenProject's own overview, one click away. */
function pmBurn(b){
  const n=b.dates.length, max=Math.max(1,...b.scope), W=300, H=54;
  const pts=a=>a.map((v,i)=>`${n<2?0:(i*W/(n-1)).toFixed(1)},${(H-3-v*(H-6)/max).toFixed(1)}`).join(' ');
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio=none><polyline fill=none stroke="#556" stroke-width=1.5 points="${pts(b.scope)}"/>`+
         `<polyline fill=none style="stroke:var(--ok)" stroke-width=2 points="${pts(b.done)}"/></svg>`;
}
async function pollBoard(){
  const id=projSheet; if(!id){ pmData=null; drawBoard(); return; }
  try{ const r=await fetch('/pm-summary?project='+encodeURIComponent(id)); const d=await r.json();
    if(!r.ok) throw new Error(d.error||r.status);
    if(projSheet===id) pmData=d; }
  catch(e){ if(projSheet===id) pmData=null; }
  drawBoard();
}
function drawBoard(){
  const box=document.getElementById('pmboard'); if(!box) return;
  const d=projSheet?pmData:null;
  if(!d){ box.innerHTML=''; return; }
  const types=[...new Set(d.by_status.flatMap(r=>Object.keys(r.types)))];
  const grid = d.by_status.length ? `<table><tr><th>status</th>${types.map(t=>`<th>${esc(t)}</th>`).join('')}<th>all</th></tr>`+
    d.by_status.map(r=>`<tr${r.status==='Blocked'?' class=blk':''}><td>${esc(r.status)}</td>${types.map(t=>`<td class=n>${r.types[t]||''}</td>`).join('')}<td class=n>${r.n}</td></tr>`).join('')+`</table>`
    : '<span class=mut>nothing open</span>';
  const blocked = d.blocked.length ? d.blocked.map(b=>`<div class=blk>#${b.id} ${esc(b.subject||'')} <span class=mut>[${esc(b.lane||'')}] ${esc(b.status)}</span></div>`+
    b.chain.map(c=>`<div class=ch style="padding-left:${6+c.depth*10}px">${ic('arrow-left')} #${c.id} ${esc(c.subject||'')} <span class=mut>${esc(c.status)}</span></div>`).join('')).join('')
    : '<span class=mut>nothing blocked</span>';
  const lanes=[...new Set([...Object.keys(d.wip),...Object.keys(d.cycle)])].sort();
  const lanesT = lanes.length ? `<table><tr><th>lane</th><th>WIP</th><th>cycle median</th><th>done 30d</th></tr>`+
    lanes.map(l=>{ const w=d.wip[l]||{}, c=d.cycle[l]; const n=Object.values(w).reduce((a,b)=>a+b,0);
      return `<tr><td>${esc(l)}</td><td class=n title="${escA(Object.entries(w).map(([k,v])=>k+': '+v).join(', '))}">${n||''}</td>`+
             `<td class=n>${c&&c.median_h!=null?c.median_h+' h':''}</td><td class=n>${c?c.n:''}</td></tr>`; }).join('')+`</table>`
    : '<span class=mut>no lane activity</span>';
  const tmax=Math.max(1,...d.throughput.map(w=>w.agent+w.human));
  const tp = `<div class=tp>`+d.throughput.map(w=>`<div title="week of ${escA(w.week)}: ${w.agent} agent, ${w.human} human">`+
    `<i class=a style="height:${w.agent*46/tmax}px"></i><i class=h style="height:${w.human*46/tmax}px"></i></div>`).join('')+
    `</div><span class=mut>done per week, last ${d.throughput.length}: <span style="color:var(--info)">agent</span> · <span style="color:var(--brand)">human</span></span>`;
  const burn = d.burnup.length ? d.burnup.map(b=>`<div>${esc(b.subject||'')} <span class=mut>${b.done.at(-1)}/${b.scope.at(-1)} · ${esc(b.status)}</span></div>${pmBurn(b)}`).join('')
    : '<span class=mut>no Features with tasks</span>';
  box.innerHTML = `<div class=pmb><div class=ph>BOARD · ${esc(d.project)} · ${d.open} open<a href="${escA(d.url)}" target=_blank rel=noopener>OpenProject ${ic('external-link')}</a></div>`+
    `<h4>OPEN BY STATUS AND TYPE</h4>${grid}<h4>BLOCKED</h4>${blocked}<h4>LANES</h4>${lanesT}`+
    `<h4>THROUGHPUT</h4>${tp}<h4>BURNUP PER FEATURE</h4>${burn}</div>`;
}
setInterval(pollBoard, 30000);
</script></body></html>"""


# ── the run page (#799, PRD R3-R5) ──────────────────────────────────────────────────────────────
# One run on one screen, for the phone the Zulip link opens it on. It is drawn on the server, so it
# reads without JavaScript and in one request; JS is there only to decide and to follow the run
# live. It adds no write route: Approve and Reject post to /resume like the cockpit and the relay,
# which is already on TAILNET_WRITES, and Approve sends the hash of the proposal this page drew.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# The icons this page draws, cut from the cockpit's sprite so the two can never drift apart.
RUN_ICONS = ("arrow-left", "check", "x", "triangle-alert", "external-link", "lock")
_SYMBOLS = {name: sym for sym, name in
            re.findall(r'(<symbol id="i-([a-z0-9-]+)".*?</symbol>)', COCKPIT)}
DECIDED = {"approved": "approved", "executing": "approved, executing", "executed": "executed",
           "failed": "failed", "declined": "declined", "rejected": "rejected", "new": "still drafting"}

RUN_PAGE_CSS = """
*{box-sizing:border-box}
body{background:#05060a;color:#ffb000;font-family:ui-monospace,'Share Tech Mono',monospace;margin:0;
  padding:14px 16px 0;font-size:14px;line-height:1.45;overflow-wrap:anywhere;max-width:760px;margin:0 auto}
a{color:#56b6ff}
.top{display:flex;align-items:center;gap:10px;margin:0 0 12px;font-size:12px;letter-spacing:.12em}
.top a{display:inline-flex;align-items:center;gap:6px;min-height:44px;color:#9aa;text-decoration:none}
.top .sp{flex:1}
h1{font-size:15px;letter-spacing:.2em;margin:0;font-weight:400}
.st{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;margin:0 0 14px}
.tag{padding:2px 9px;border-radius:9px;font-size:12px;color:#000;font-weight:700}
.new{background:#445;color:#aab}.awaiting-approval{background:#ff5}.approved{background:#33dd88}
.executing{background:#ffb000}.executed{background:#56b6ff}.rejected,.declined{background:#e3554f;color:#fff}
.failed{background:#8b1a16;color:#fff}
.mut{opacity:.6}
.f{border-top:1px solid #1b1b27;padding:9px 0}
.k{color:#56b6ff;font-size:11px;letter-spacing:.1em;margin:0 0 3px}
.v{min-width:0}
pre.pv{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:inherit;font-size:13px;color:#e6d6b0;
  background:#0a0b12;border:1px solid #1b1b27;border-radius:7px;padding:9px 10px}
.warn{color:#ff8c5a}
.note{border:1px solid #ff8c5a;color:#ff8c5a;border-radius:8px;padding:9px 11px;margin:10px 0}
.note.done{border-color:#2a2a3a;color:#9aa}
details{border-top:1px solid #1b1b27;padding:9px 0} summary{color:#56b6ff;cursor:pointer;min-height:32px}
details pre.pv{margin-top:6px}
.bar{position:sticky;bottom:0;background:#05060a;border-top:1px solid #2a2a3a;margin:14px -16px 0;
  padding:10px 16px calc(10px + env(safe-area-inset-bottom));display:flex;flex-direction:column;gap:8px}
.bar .btns{display:flex;gap:10px}
.bar button{flex:1;min-height:48px;border:0;border-radius:9px;font:inherit;font-size:14px;font-weight:700;
  display:inline-flex;align-items:center;justify-content:center;gap:8px;cursor:pointer}
.bar button:disabled{opacity:.45;cursor:default}
.ok{background:#33dd88;color:#022} .no{background:#e3554f;color:#fff}
#msg:empty{display:none} #msg{font-size:13px}
.end{height:24px}
svg.ic{width:18px;height:18px;flex:none;vertical-align:-4px;fill:none;stroke:currentColor;stroke-width:2;
  stroke-linecap:round;stroke-linejoin:round}
"""

# Plain JS with no backslashes in it, because this is a Python string (see the note in COCKPIT).
RUN_PAGE_JS = """
const B=document.body.dataset, RID=B.rid, HASH=B.hash;
const msg=document.getElementById('msg');
let busy=false, stale=false;
function btns(){ return document.querySelectorAll('.bar button'); }
function say(t, bad){ if(!msg) return; msg.textContent=t; msg.className=bad?'warn':''; }
function markStale(){
  stale=true; const a=document.getElementById('approve'); if(a) a.disabled=true;
  say('This proposal changed since you opened it. Reload to read the new one.', true);
}
async function decide(decision){
  if(busy) return;
  // R5: approving asks once and names what it approves. Rejecting does not ask.
  if(decision==='approved' && !confirm('Approve '+B.action+' on '+B.project+'?')) return;
  const body={run_id:RID, decision:decision};
  if(decision==='approved'){
    // The hash this page was drawn from, never a fresh read (R4).
    if(!HASH || stale){ markStale(); return; }
    body.expect_hash=HASH;
  }
  busy=true; btns().forEach(b=>b.disabled=true);
  say(decision==='approved' ? 'Approved. Executing, this can take a while.' : 'Rejecting.');
  let res=null;
  try{ res=await (await fetch('/resume',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify(body)})).json(); }
  catch(e){ busy=false; btns().forEach(b=>b.disabled=false);
    say('Could not reach the engine. The run was not decided.', true); return; }
  busy=false;
  // The engine answers a refusal with a 200 and an `error`, so it has to be read to be seen.
  if(res && res.error){
    // Approve stays off, since the page is out of date. Rejecting what changed is still fine.
    if(/changed since/.test(res.error)){ markStale(); const r=document.getElementById('reject');
      if(r) r.disabled=false; return; }
    say(res.error, true); btns().forEach(b=>b.disabled=false); return;
  }
  location.reload();
}
async function refresh(){
  if(busy) return;
  let d=null;
  try{ d=await (await fetch('/run?id='+encodeURIComponent(RID))).json(); }catch(e){ return; }
  if(!d || !d.status) return;
  if(d.status!==B.status){ location.reload(); return; }
  if(d.status==='awaiting-approval' && d.proposal_hash!==HASH && !stale) markStale();
}
document.querySelectorAll('[data-decide]').forEach(b=>b.addEventListener('click',()=>decide(b.dataset.decide)));
try{ const es=new EventSource('/events'); es.onmessage=e=>{ if(e.data==='bump') refresh(); }; }catch(e){}
setInterval(refresh, 15000);
"""


def _e(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def _ago(iso) -> str:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        s = int((datetime.now(timezone.utc) - t).total_seconds())
    except Exception:
        return ""
    if s < 90:
        return "just now"
    if s < 7200:
        return f"{s // 60} min ago"
    return f"{s // 3600} h ago" if s < 172800 else f"{s // 86400} d ago"


def _ic(name: str) -> str:
    return f'<svg class=ic aria-hidden=true><use href="#i-{name}"/></svg>'


def _field(key, value_html) -> str:
    return f"<div class=f><div class=k>{_e(key)}</div><div class=v>{value_html}</div></div>"


def run_project_name(d: dict) -> str:
    """How the confirm dialog names the project: the board, else the repo, else says there is none."""
    p = d.get("project") or {}
    return p.get("board") or p.get("project_name") or p.get("repo") or "no project"


def _project_html(p) -> str:
    if not p:
        return "<span class=mut>no project</span>"
    bits = []
    if p.get("board") or p.get("project_name"):
        bits.append(_e(p.get("board") or "?") + (f" <span class=mut>({_e(p['project_name'])})</span>"
                                                 if p.get("project_name") else ""))
    if p.get("wp_id"):
        wp = f"#{int(p['wp_id'])}" + (f" {_e(p['wp_subject'])}" if p.get("wp_subject") else "")
        bits.append(f'<a href="{_e(p["wp_url"])}" target=_blank rel=noopener>{wp} {_ic("external-link")}</a>'
                    if p.get("wp_url") else wp)
        if p.get("pm_error"):
            bits.append("<span class=warn>the board did not answer</span>")
    elif p.get("task_ref"):
        bits.append(f"mission-control task {_e(p['task_ref'])}")
    if p.get("repo"):
        bits.append(f"repo {_e(p['repo'])}")
    return "<br>".join(bits)


def run_page(d: dict) -> str:
    """The HTML for one run, from what run_detail() returns. Pure, so it is tested without a server.

    Every value on it is escaped here: goal, args and lookups come from untrusted input (mail, web
    pages), and the page they land on has the approve button."""
    rid, status = d.get("run_id") or "", d.get("status") or ""
    waiting = status == "awaiting-approval"
    h = []
    h.append(f"<div class=st><span class='tag {_e(status)}'>{_e(status.upper())}</span>"
             f"<span class=mut>{_e(_ago(d.get('created_at')))}</span>"
             f"<span class=mut>{_e((d.get('created_at') or '')[:16].replace('T', ' '))}</span></div>")
    # The same project warning as every other gate surface (#851), up top so it is read before Approve.
    if waiting and d.get("project_warning"):
        h.append(_field("PROJECT CHECK", f"<span class=warn>{_ic('triangle-alert')} {_e(d['project_warning'])}</span>"))
    h.append(_field("PROJECT", _project_html(d.get("project"))))
    h.append(_field("LANE · ACTION", f"{_e(d.get('lane') or '?')} · {_e(d.get('action') or '?')}"))
    if d.get("goal"):
        h.append(_field("GOAL", f"<pre class=pv>{_e(d['goal'])}</pre>"))
    if d.get("intent") and d.get("intent") != d.get("goal"):
        h.append(_field("INTENT", f"<pre class=pv>{_e(d['intent'])}</pre>"))
    if d.get("target"):
        h.append(_field("TARGET", _e(d["target"])))
    if d.get("error"):
        h.append(_field("ERROR", f"<pre class='pv warn'>{_e(d['error'])}</pre>"))
    # Every key the approval covers, whole (R3). arg_fields() sets the order and says which are long.
    for f in d.get("arg_fields") or []:
        if f.get("long"):
            h.append(_field(f["key"], f"<pre class=pv>{_e(f['text'])}</pre>"))
        else:
            h.append(_field(f["key"], _e(f["text"]) if f.get("text") else "<span class=mut>empty</span>"))
    det = d.get("detector") or {}
    if det.get("checked"):
        bits = [f"verdict {_e(det.get('verdict'))}"]
        if det.get("style_score") is not None:
            bits.append(f"style {_e(det['style_score'])}")
        if det.get("statistical") is not None:
            try:
                bits.append(f"statistical {float(det['statistical']):.3f}")
            except (TypeError, ValueError):
                pass
        if det.get("rewritten"):
            bits.append("rewritten once")
        h.append(_field("AI-TEXT CHECK", " · ".join(bits)))
    looks = d.get("lookups") or []
    if looks:
        body = "".join(f"<pre class=pv>{_e(json.dumps(x, indent=2, ensure_ascii=False, default=str))}</pre>"
                       for x in looks)
        h.append(f"<details><summary>LOOKUPS ({len(looks)})</summary>{body}</details>")
    log = d.get("log") or []
    if log:
        body = "".join(f"<div>{_e(e.get('agent'))} · {_e(e.get('what'))} "
                       f"<span class=mut>{_e((e.get('created_at') or '')[11:19])}</span></div>" for e in log)
        h.append(f"<details><summary>LOG ({len(log)})</summary>{body}</details>")
    if waiting and d.get("proposal_hash"):
        bar = ("<div class=bar><div id=msg role=status></div><div class=btns>"
               f"<button id=approve class=ok data-decide=approved>{_ic('check')}<span>APPROVE</span></button>"
               f"<button id=reject class=no data-decide=rejected>{_ic('x')}<span>REJECT</span></button>"
               "</div><noscript><span class=warn>Approving needs JavaScript.</span></noscript></div>")
    elif waiting:
        bar = f"<div class=note>{_ic('lock')} This proposal has no hash to pin, so it cannot be approved here.</div>"
    else:
        bar = f"<div class='note done'>{_ic('lock')} Decided: {_e(DECIDED.get(status, status))}. Nothing to approve.</div>"
    sprite = "".join(_SYMBOLS[n] for n in RUN_ICONS)
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Run {_e(rid)} · NOMAD</title><style>{RUN_PAGE_CSS}</style></head>"
            f"<body data-rid='{_e(rid)}' data-status='{_e(status)}' data-hash='{_e(d.get('proposal_hash') or '')}' "
            f"data-action='{_e(d.get('action') or 'this action')}' data-project='{_e(run_project_name(d))}'>"
            "<!-- Lucide icons, https://lucide.dev. ISC License, Copyright (c) 2026 Lucide Icons and "
            "Contributors. The same symbols as the cockpit's sprite. -->"
            f"<svg width=0 height=0 style='position:absolute' aria-hidden=true>{sprite}</svg>"
            f"<div class=top><a href='/cockpit'>{_ic('arrow-left')}<span>COCKPIT</span></a><span class=sp></span>"
            f"<h1>RUN {_e(rid)}</h1></div>"
            + "".join(h) + bar + "<div class=end></div>"
            f"<script>{RUN_PAGE_JS}</script></body></html>")


def run_missing_page(rid: str) -> str:
    return ("<!doctype html><html lang=en><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'><title>No such run · NOMAD</title>"
            f"<style>{RUN_PAGE_CSS}</style></head><body>"
            f"<div class=top><a href='/cockpit'>COCKPIT</a></div>"
            f"<p>No run with the id <b>{_e(rid)}</b>.</p></body></html>")


if __name__ == "__main__":
    # Dev/direct run; in the container uvicorn runs `server:app` (see Dockerfile). Threads start
    # via the lifespan handler in both paths.
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
