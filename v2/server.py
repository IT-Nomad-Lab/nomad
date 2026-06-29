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
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from engine import Engine
from mcp_image import generate_image_impl   # direct local image gen for the nomad-image MCP bridge
from nocodb import NocoDB

HOST = os.environ.get("NOMAD_V2_ENGINE_HOST", "127.0.0.1")   # localhost-only (security review)
PORT = int(os.environ.get("NOMAD_V2_ENGINE_PORT", "8099"))
GATE_POLL_SECS = int(os.environ.get("NOMAD_V2_GATE_POLL", "3"))
VOICE_URL = os.environ.get("NOMAD_VOICE_URL", "http://nomad-voice:8200")   # 3D: local Piper/Whisper
CONSOLE_URL = os.environ.get("NOMAD_CONSOLE_URL", "http://nomad-console:8000")  # telemetry source (CPU/MEM/GPU)
TERM_BASE = os.environ.get("NOMAD_TERM_URL", "ws://host.docker.internal:8091")  # interactive project terminal
DISPATCH_URL = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090")  # project list


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


def chat_reply(message: str, history=None, session_id: str = "cockpit") -> dict:
    """Conversational turn grounded in live pipeline state + memory. Recalls relevant past
    conversations (cross-session, unified with the console) and the recent in-session history, then
    persists both sides of the turn. Memory is fail-open. capture=true is handled by the caller."""
    import runtime
    import memory_chat
    # memory opt-out / forget — honored BEFORE the model runs (mirrors the console)
    intent = memory_chat.classify_memory_intent(message)
    if intent == "wipe":
        memory_chat.forget(session_id, scope="session")
        return {"reply": "Done — I've wiped this conversation from my memory.", "remembered": False}
    if intent == "forget_last":
        n = memory_chat.forget(session_id, scope="last")
        return {"reply": f"Forgotten — dropped the last {max(n, 0)} turn(s) from memory.",
                "remembered": False}
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
    system = (
        "You are NOMAD, the orchestrator of a self-hosted pipeline (Capture→Clarify→Route→Process→"
        "Human Gate→Execute→Log&Learn) over a 5-lane inbox (comms/research/support/ads/dev). Answer "
        "the operator concisely about the CURRENT pipeline state shown below. If they're handing you "
        "a task to act on, tell them to capture it (the ⚑ Capture button or 'capture: <goal>') so it "
        "enters the gate. Don't invent runs or claim you executed anything.\n\n"
        f"LIVE PIPELINE STATE:\n{_pipeline_context()}{mem_block}"
        + (f"\n\nRECENT CONVERSATION:{hist_block}" if hist_block else ""))
    try:
        reply = runtime.run(system, message, alias="balanced", max_tokens=400)
    except Exception as e:
        reply = f"(chat unavailable: {str(e)[:120]})"
    # 'off the record' → answer but store NOTHING; otherwise persist both sides for future recall
    if intent == "off_record":
        return {"reply": reply, "remembered": False}
    memory_chat.remember(session_id, "user", message)
    memory_chat.remember(session_id, "assistant", reply)
    return {"reply": reply, "remembered": True}


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
            "from_agent": row.get("from_agent"), "goal": payload.get("goal"),
            "intent": payload.get("intent"), "target": payload.get("target"),
            "action": payload.get("action"), "args": payload.get("args", {}),
            "error": payload.get("error"), "log": log}


def _proc_telemetry() -> dict:
    """Stdlib CPU%/MEM% from /proc — fallback when the console is unreachable. GPU unavailable
    here (the engine container has no nvidia access), so gpu=None."""
    out = {"cpu_percent": None, "mem_percent": None, "gpu": None, "source": "proc"}
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
    """CPU/MEM/GPU for the cockpit strip. Prefer the console's /api/system (it has psutil + GPU,
    reachable on the docker network with no auth); fall back to a stdlib /proc read."""
    try:
        with urllib.request.urlopen(CONSOLE_URL + "/api/system", timeout=3) as r:
            d = json.loads(r.read()); d["source"] = "console"; return d
    except Exception:
        return _proc_telemetry()


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


@app.get("/projects")
async def projects():
    """Discovered Nomad.md projects (proxied from the Builder dispatcher) — for the terminal picker."""
    def _f():
        try:
            import requests
            return requests.get(f"{DISPATCH_URL}/projects", timeout=5).json()
        except Exception:
            return {"projects": []}
    return await asyncio.to_thread(_f)


async def _json(request: Request) -> dict:
    try:
        return await request.json()
    except Exception:
        return {}


# ── reads ───────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
@app.get("/cockpit", response_class=HTMLResponse)
async def cockpit():
    return HTMLResponse(COCKPIT)


@app.get("/health")
async def health():
    import runtime
    return {"status": "ok", "service": "nomad-v2-engine", "runtime": runtime.active(),
            "gate": "push+poll"}


@app.get("/runs")
async def runs():
    return {"runs": await asyncio.to_thread(recent_runs)}


@app.get("/telemetry")
async def telemetry():
    return await asyncio.to_thread(_telemetry)


@app.get("/run")
async def run(id: str = ""):
    return await asyncio.to_thread(run_detail, id) if id else {"error": "id required"}


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
    r = await asyncio.to_thread(eng.resume_run, req["run_id"], req.get("decision", "rejected"))
    _bump_runs()
    return r


@app.post("/generate-image")
async def generate_image_direct(request: Request):
    """Ungated, LOCAL-ONLY image generation for in-repo Claude Code (the nomad-image MCP bridge).
    Forces the ComfyUI provider — free + reversible (file left uncommitted), consistent with the
    edit-uncommitted guardrail. Cloud/spend backends (OpenAI/Firefly) stay behind the human gate
    via the normal /capture pipeline. `save_hint` ("<project>/<subdir>") routes the file into a repo."""
    req = await _json(request)
    prompt = (req.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt required"}
    run_id = "direct-" + str(int(time.time()))
    return await asyncio.to_thread(generate_image_impl, prompt, run_id, "comfyui",
                                   req.get("save_hint"))


@app.post("/retry")
async def retry(request: Request):
    req = await _json(request)
    _resolved.discard(req.get("run_id"))         # let the gate act on it again
    r = await asyncio.to_thread(eng.retry_run, req["run_id"]); _bump_runs()
    return r


@app.post("/chat")
async def chat(request: Request):
    req = await _json(request)
    msg = (req.get("message") or "").strip()
    low = msg.lower()
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
    return await asyncio.to_thread(chat_reply, msg, req.get("history"),
                                   req.get("session_id") or "cockpit")


@app.post("/plan-project")
async def plan_project(request: Request):
    req = await _json(request)
    return await asyncio.to_thread(eng.plan_project, req.get("title", ""), req.get("description", ""))


@app.post("/run-next-task")
async def run_next_task(request: Request):
    req = await _json(request)
    r = await asyncio.to_thread(eng.run_next_task, req.get("goal_id"))
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


@app.post("/nocodb-hook")
async def nocodb_hook(request: Request):
    req = await _json(request)
    print("NOCODB-HOOK:", json.dumps(req)[:500], flush=True)
    run_id, status = _extract_hook(req)
    if status in ("approved", "rejected") and run_id:
        r = await asyncio.to_thread(eng.resume_run, run_id, status); _bump_runs()
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


# ── interactive project terminal: proxy the cockpit WS to the native PTY daemon ──
@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket):
    await websocket.accept()
    project = websocket.query_params.get("project", "")
    cols = websocket.query_params.get("cols", "80")
    rows = websocket.query_params.get("rows", "24")
    upstream = (f"{TERM_BASE}/term?project={urllib.parse.quote(project)}"
                f"&cols={cols}&rows={rows}&token={urllib.parse.quote(TERM_TOKEN)}")
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


def gate_scan():
    """THE GATE (poll): find comms rows the operator flipped to approved/rejected and resume
    the paused run. Idempotent; status flip in NocoDB → run resumes. Returns count acted on."""
    acted = 0
    for r in db.list("comms", 50):
        rid, status = r.get("run_id"), (r.get("status") or "").strip()
        if rid and status in ("approved", "rejected") and rid not in _resolved:
            eng.resume_run(rid, status)
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
    for r in db.list("comms", limit):
        item = {"run_id": r.get("run_id"), "lane": r.get("lane"),
                "from_agent": r.get("from_agent"), "type": r.get("type"),
                "status": r.get("status"), "goal_id": r.get("goal_id"),
                "created_at": r.get("created_at")}
        if r.get("status") == "awaiting-approval":
            try:
                p = json.loads(r.get("payload") or "{}")
                a = p.get("args", {})
                item["proposal"] = {
                    "action": p.get("action"),
                    "target": a.get("to") or a.get("topic") or a.get("project") or "",
                    "preview": (a.get("body") or a.get("brief") or a.get("content")
                                or a.get("task") or a.get("prompt") or "")[:280]}
            except Exception:
                pass
        out.append(item)
    out.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return out


COCKPIT = """<!doctype html><html><head><meta charset=utf-8><title>NOMAD v2 · Cockpit</title>
<style>
body{background:#05060a;color:#ffb000;font-family:ui-monospace,'Share Tech Mono',monospace;margin:0;padding:22px}
h1{font-size:20px;letter-spacing:.28em;margin:0 0 4px} .sub{opacity:.6;font-size:12px;letter-spacing:.15em;margin-bottom:16px}
.stages{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 18px;font-size:11px;opacity:.8}
.stages span{background:#14141f;border:1px solid #2a2a3a;border-radius:10px;padding:3px 9px}
table{width:100%;border-collapse:collapse;font-size:13px} th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #1b1b27}
th{color:#56b6ff;font-weight:400;letter-spacing:.1em;font-size:11px}
.tag{padding:2px 9px;border-radius:9px;font-size:11px;color:#000;font-weight:700}
.new{background:#445;color:#aab}.awaiting-approval{background:#ff5;animation:b 1.1s steps(2) infinite}
.approved{background:#33dd88}.executing{background:#ffb000;animation:b 1.1s steps(2) infinite}
.executed{background:#56b6ff}.rejected,.declined{background:#e3554f;color:#fff}.failed{background:#8b1a16;color:#fff}
@keyframes b{50%{opacity:.4}} .mut{opacity:.5}
.gate{border:0;border-radius:7px;padding:4px 11px;margin-left:7px;cursor:pointer;font:inherit;font-size:11px;font-weight:700}
.ok{background:#33dd88;color:#022} .no{background:#e3554f;color:#fff} .retry{background:#ffb000;color:#022} .gate:active{transform:translateY(1px)}
.prop{background:#0c0d16} .prop td{border-bottom:1px solid #1b1b27;padding:4px 10px 9px}
.prop .act{color:#56b6ff;letter-spacing:.08em} .prop .pv{opacity:.78;white-space:pre-wrap}
.telem{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 16px;font-size:11px}
.telem .m{background:#0c0d16;border:1px solid #1f2030;border-radius:8px;padding:4px 10px;display:flex;gap:7px;align-items:center}
.telem .lbl{color:#56b6ff;letter-spacing:.08em} .telem .bar{width:54px;height:6px;background:#1b1b27;border-radius:4px;overflow:hidden}
.telem .fill{height:100%;background:#33dd88;transition:width .4s} .telem .hot{background:#e3554f} .telem .warn{background:#ffb000}
.lanes{display:flex;gap:7px;flex-wrap:wrap;margin:0 0 12px;font-size:11px}
.lanes .chip{background:#0c0d16;border:1px solid #2a2a3a;border-radius:12px;padding:3px 11px;cursor:pointer;letter-spacing:.06em;opacity:.62}
.lanes .chip.on{opacity:1;border-color:#56b6ff} .lanes .chip .ct{opacity:.6;margin-left:5px}
.ldot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;vertical-align:middle}
.l-comms{background:#33dd88}.l-research{background:#56b6ff}.l-support{background:#ffb000}.l-ads{background:#d07bff}.l-dev{background:#ff8c5a}.l-other{background:#667}
.rlink{color:#ffb000;cursor:pointer;border-bottom:1px dotted #665} .rlink:hover{color:#fff}
.detail td{background:#0a0b12;border-bottom:1px solid #1b1b27;padding:10px 14px;font-size:12px}
.detail .kv{margin:2px 0} .detail .k{color:#56b6ff;display:inline-block;min-width:64px}
.detail .log{margin-top:8px;border-top:1px solid #1b1b27;padding-top:6px}
.detail .le{opacity:.8;margin:2px 0} .detail .le b{color:#33dd88;font-weight:400}
.chat{margin:0 0 18px;border:1px solid #1f2030;border-radius:10px;background:#0a0b12;overflow:hidden}
.clog{max-height:200px;overflow-y:auto;padding:10px 12px;font-size:13px;display:none}
.clog .u{color:#ffb000;margin:6px 0} .clog .n{color:#9fd}; .clog .n{margin:6px 0;white-space:pre-wrap}
.clog .u b,.clog .n b{opacity:.6;font-weight:400;margin-right:6px;font-size:11px}
.crow{display:flex;gap:8px;padding:10px 12px;border-top:1px solid #1f2030}
.crow input{flex:1;background:#05060a;border:1px solid #2a2a3a;border-radius:8px;color:#ffb000;padding:8px 11px;font:inherit;font-size:13px}
.crow button{background:#334;color:#cde;border:0;border-radius:8px;padding:8px 13px;cursor:pointer;font:inherit;font-size:12px}
.crow .cap{background:#ffb000;color:#022;font-weight:700} .crow button:active{transform:translateY(1px)}
.ws-hidden{display:none}
#ws{position:fixed;inset:0;z-index:60;background:#05060a;display:flex;flex-direction:column}
#wsbar{display:flex;align-items:center;gap:10px;padding:8px 14px;background:#ffb000;color:#000;font-size:13px;letter-spacing:.08em}
#wsbar .wst{font-weight:700} #wsbar .wsp{background:#000;color:#ffb000;padding:2px 9px;border-radius:9px}
#wsbar button{background:#000;color:#ffb000;border:0;border-radius:7px;padding:5px 11px;cursor:pointer;font:inherit;font-size:12px}
#wsterm{flex:1;min-height:0;padding:6px 4px 4px 8px}
#termproj{background:#14141f;color:#ffb000;border:1px solid #2a2a3a;border-radius:8px;padding:6px;font:inherit;font-size:12px}
#termopen{background:#56b6ff;border:0;border-radius:8px;color:#022;padding:7px 12px;cursor:pointer;font:inherit}
.projects{margin:0 0 16px}
.projects .ph{color:#56b6ff;letter-spacing:.12em;font-size:12px;margin:0 0 6px}
.proj{background:#0c0d16;border:1px solid #1f2030;border-radius:9px;padding:8px 11px;margin-bottom:7px}
.proj .top{display:flex;align-items:center;gap:10px;cursor:pointer}
.proj .pt{flex:1;font-size:13px} .proj .pp{font-size:11px;color:#9aa;white-space:nowrap}
.proj .pbar{height:6px;background:#1b1b27;border-radius:4px;overflow:hidden;margin-top:6px}
.proj .pbar i{display:block;height:100%;background:#33dd88;transition:width .4s}
.proj .runnext{background:#ffb000;color:#022;border:0;border-radius:7px;padding:4px 9px;cursor:pointer;font:inherit;font-size:11px;font-weight:700}
.proj .det{margin-top:8px;border-top:1px solid #1b1b27;padding-top:6px;font-size:12px;display:none}
.proj.open .det{display:block}
.proj .ms{margin:4px 0} .proj .ms b{color:#56b6ff;font-weight:400}
.proj .tk{opacity:.85;padding:2px 0 2px 12px} .proj .tk.done{opacity:.5;text-decoration:line-through}
.proj .tk.skip{opacity:.45;font-style:italic} .proj .tk.blk{color:#ff8c5a}
.proj .tk .ts{display:inline-block;min-width:74px;font-size:10px;opacity:.6;text-transform:uppercase}
.proj .tact{background:#ffb000;color:#022;border:0;border-radius:6px;padding:2px 7px;margin-left:6px;cursor:pointer;font:inherit;font-size:10px;font-weight:700}
.proj .tact+.tact{background:#334;color:#cde}
</style><link rel=stylesheet href="/static/vendor/xterm.css"></head><body>
<h1>NOMAD&nbsp;v2 · COCKPIT</h1><div class=sub>PIPELINE · LIVE FROM NOCODB · <span id=live class=mut>○ connecting</span> · <span id=summary class=mut></span></div>
<div class=stages><span>Capture</span><span>Clarify</span><span>Route</span><span>Process</span>
<span>⚑ Human Gate</span><span>Execute</span><span>Log&amp;Learn</span></div>
<div id=telem class=telem><span class=mut>telemetry…</span></div>
<div id=voicebar style="margin:8px 0 18px;display:flex;align-items:center;gap:10px">
  <button id=mic title="speak a goal" style="background:#ffb000;border:0;border-radius:8px;color:#000;padding:7px 14px;cursor:pointer;font:inherit">&#127908; Speak a goal</button>
  <button id=spk title="spoken confirmations" style="background:#334;color:#9aa;border:0;border-radius:8px;padding:7px 12px;cursor:pointer;font:inherit">&#128266;</button>
  <span id=vstatus class=mut style="font-size:12px"></span>
  <span style="flex:1"></span>
  <select id=termproj title="project"></select>
  <button id=termopen title="open an interactive claude terminal in this project">&#9000; Terminal</button>
</div>
<div id=chat class=chat>
  <div id=clog class=clog></div>
  <div class=crow>
    <input id=cin placeholder="Ask NOMAD about the pipeline — or capture: a goal" autocomplete=off>
    <button id=csend title="ask">Send</button>
    <button id=ccap class=cap title="capture as a pipeline goal">⚑ Capture</button>
  </div>
</div>
<div id=projects class=projects></div>
<div id=lanebar class=lanes></div>
<table><thead><tr><th>RUN</th><th>LANE</th><th>FROM</th><th>TYPE</th><th>STATUS</th><th>WHEN</th></tr></thead>
<tbody id=rows><tr><td class=mut colspan=6>loading…</td></tr></tbody></table>
<div id=ws class=ws-hidden>
  <div id=wsbar><span class=wst>WORKSPACE</span><span id=wsproj class=wsp>—</span><span id=wsstat class=mut style="font-size:12px"></span><span style="flex:1"></span><button id=wsre>RECONNECT</button><button id=wsclose>&#10005; CLOSE</button></div>
  <div id=wsterm></div>
</div>
<script src="/static/vendor/xterm.js"></script>
<script src="/static/vendor/addon-fit.js"></script>
<script>
function esc(s){ return (s||'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
async function gate(rid, decision){
  if(decision==='rejected' && !confirm('Reject this run?')) return;
  const btns=document.querySelectorAll(`[data-rid="${rid}"]`); btns.forEach(b=>b.disabled=true);
  try{ await fetch('/resume',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({run_id:rid,decision})}); }catch(e){}
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
function ldot(l){ return `<span class="ldot l-${LANES.includes(l)?l:'other'}"></span>`; }
function setLane(l){ laneFilter=l; tick(); }
function renderLanes(runs){
  const ct={all:runs.length}; LANES.forEach(l=>ct[l]=0);
  runs.forEach(r=>{ if(ct[r.lane]!=null) ct[r.lane]++; });
  const chip=(l,label)=>`<span class="chip ${laneFilter===l?'on':''}" onclick="setLane('${l}')">`+
    (l==='all'?'':ldot(l))+`${label}<span class=ct>${ct[l]||0}</span></span>`;
  document.getElementById('lanebar').innerHTML =
    chip('all','ALL') + LANES.map(l=>chip(l,l.toUpperCase())).join('');
}
const openRuns=new Set();
function toggleDetail(rid){ if(openRuns.has(rid)) openRuns.delete(rid); else openRuns.add(rid); tick(); }
async function loadDetail(rid){
  const el=document.getElementById('det-'+rid); if(!el) return;
  try{ const r=await (await fetch('/run?id='+encodeURIComponent(rid))).json();
    const kv=(k,v)=>v?`<div class=kv><span class=k>${k}</span>${esc(String(v))}</div>`:'';
    let h=kv('goal',r.goal)+kv('intent',r.intent)+kv('target',r.target)+kv('action',r.action);
    if(r.error) h+=`<div class=kv><span class=k>⚠ error</span><span style="color:#ff8c5a">${esc(String(r.error))}</span></div>`;
    const a=r.args||{}; const body=a.body||a.brief||a.content||a.task||a.prompt;
    if(a.subject) h+=kv('subject',a.subject);
    if(body) h+=`<div class=kv><span class=k>draft</span></div><div class=pv>${esc(String(body))}</div>`;
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
  if(sm) sm.innerHTML=`⚑ ${c['awaiting-approval']} awaiting · ▶ ${c['executing']} executing · `+
    `<span style="color:${c.failed?'#ff8c5a':'inherit'}">✗ ${c.failed} failed</span>`;
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
async function tick(){
  try{ const d=await (await fetch('/runs')).json();
    const all=d.runs||[]; renderLanes(all); announce(all);
    const runs = laneFilter==='all' ? all : all.filter(r=>r.lane===laneFilter);
    document.getElementById('rows').innerHTML = runs.map(r=>{
      const wait = r.status==='awaiting-approval';
      let acts = wait
        ? `<button class="gate ok" data-rid="${r.run_id}" onclick="gate('${r.run_id}','approved')">✓ APPROVE</button>`+
          `<button class="gate no" data-rid="${r.run_id}" onclick="gate('${r.run_id}','rejected')">✗ REJECT</button>` : '';
      if(r.status==='failed') acts=`<button class="gate retry" data-rid="${r.run_id}" onclick="retry('${r.run_id}')">↻ RETRY</button>`;
      const rid=r.run_id||'';
      let row=`<tr><td><span class=rlink onclick="toggleDetail('${rid}')">${rid||'—'}</span></td>`+
        `<td>${ldot(r.lane)}${r.lane||''}</td><td>${r.from_agent||''}</td>`+
        `<td>${r.type||''}</td><td><span class="tag ${r.status}">${(r.status||'').toUpperCase()}</span>${acts}</td>`+
        `<td class=mut>${(r.created_at||'').slice(11,19)}</td></tr>`;
      const p=r.proposal;
      if(wait && p){ const tgt=p.target?` → ${esc(p.target)}`:'';
        row+=`<tr class=prop><td colspan=6><span class=act>⚑ ${esc(p.action)||'action'}${tgt}</span>`+
          (p.preview?`<div class=pv>${esc(p.preview)}</div>`:'')+`</td></tr>`; }
      if(openRuns.has(rid)) row+=`<tr class=detail><td colspan=6 id="det-${rid}" class=mut>loading…</td></tr>`;
      return row;
    }).join('') ||
      `<tr><td class=mut colspan=6>${all.length?'no runs in this lane':'no runs yet — POST a goal to /capture'}</td></tr>`;
    openRuns.forEach(rid=>{ if(document.getElementById('det-'+rid)) loadDetail(rid); });
  }catch(e){}
}
tick();
/* live push: SSE bumps on any run change → refresh instantly. Slow poll is just a safety net. */
setInterval(tick,10000);
try{
  const es=new EventSource('/events');
  es.onmessage=e=>{ if(e.data==='bump'||e.data==='hello') tick(); };
  const dot=document.getElementById('live'); if(dot){ es.onopen=()=>dot.textContent='● LIVE'; es.onerror=()=>dot.textContent='○ reconnecting'; }
}catch(e){}

/* cockpit chat: converse about the pipeline (Send) or drop a goal into it (⚑ Capture).
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
async function csendMsg(capture){
  const msg=cin.value.trim(); if(!msg) return;
  clogAdd('you', msg); cin.value=''; clogAdd('nomad', '…');
  const ph=clog.lastChild;
  try{
    const r=await (await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:msg, capture:!!capture, session_id:chatSession, history:chatHist.slice(-6)})})).json();
    const priv = r.remembered===false ? ' <span class=mut title="not stored to memory">🔒</span>' : '';
    ph.innerHTML=`<b>NOMAD</b>${esc(r.reply||'(no reply)')}${priv}`;
    chatHist.push({role:'user',content:msg},{role:'assistant',content:r.reply||''});
    if(r.captured){ vspeak(r.reply); tick(); }
  }catch(e){ ph.innerHTML='<b>NOMAD</b>⚠ chat error'; }
}
document.getElementById('csend').onclick=()=>csendMsg(false);
document.getElementById('ccap').onclick=()=>csendMsg(true);
cin.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); csendMsg(e.shiftKey); } });

/* telemetry strip — CPU / RAM / GPU (from the console, or /proc fallback) */
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

/* 3D voice loop: speak a goal → local Whisper → /capture → local Piper confirmation */
let spkOn=false, curA=null;
async function vspeak(t){ if(!spkOn||!t) return;
  try{ if(curA) curA.pause();
    const r=await fetch('/api/tts',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})});
    if(!r.ok) return; const u=URL.createObjectURL(await r.blob()); curA=new Audio(u); curA.onended=()=>URL.revokeObjectURL(u); curA.play(); }catch(e){}
}
const spk=document.getElementById('spk');
spk.onclick=()=>{ spkOn=!spkOn; spk.style.background=spkOn?'#33dd88':'#334'; spk.style.color=spkOn?'#000':'#9aa'; };
let rec=null,chunks=[],recording=false;
const mic=document.getElementById('mic'), vs=document.getElementById('vstatus');
async function toggleMic(){
  if(recording){ try{rec.stop();}catch(e){} return; }
  let stream; try{ stream=await navigator.mediaDevices.getUserMedia({audio:true}); }
  catch(e){ vs.textContent='⚠ mic unavailable'; return; }
  rec=new MediaRecorder(stream); chunks=[];
  rec.ondataavailable=e=>{ if(e.data && e.data.size) chunks.push(e.data); };
  rec.onstop=async()=>{
    recording=false; mic.innerHTML='&#127908; Speak a goal'; mic.style.background='#ffb000';
    stream.getTracks().forEach(t=>t.stop());
    const blob=new Blob(chunks,{type:(rec&&rec.mimeType)||'audio/webm'});
    if(blob.size<1200){ vs.textContent=''; return; }
    vs.textContent='transcribing…';
    try{
      const d=await (await fetch('/api/stt',{method:'POST',headers:{'Content-Type':blob.type},body:blob})).json();
      const goal=(d.text||'').trim();
      if(!goal){ vs.textContent='(didn’t catch that)'; return; }
      vs.textContent='“'+goal+'” → capturing…';
      const cr=await (await fetch('/capture',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal})})).json();
      const msg='Captured. Routed to '+(cr.lane||'?')+'. Awaiting your approval.';
      vs.textContent=msg; vspeak(msg); tick();
    }catch(e){ vs.textContent='⚠ voice error'; }
  };
  rec.start(); recording=true; mic.textContent='● recording — click to stop'; mic.style.background='#e3554f'; vs.textContent='';
}
if(navigator.mediaDevices && window.MediaRecorder){ mic.onclick=toggleMic; } else { mic.style.display='none'; }

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
    elStat.textContent='● connecting';
    ws=new WebSocket(url); ws.binaryType='arraybuffer';
    ws.onopen=()=>{ elStat.textContent='● LIVE'; setTimeout(doFit,60); term.focus(); };
    ws.onmessage=e=>{ if(typeof e.data==='string') term.write(e.data); else term.write(new Uint8Array(e.data)); };
    ws.onclose=()=>{ elStat.textContent=manual?'○ closed':'○ disconnected'; };
    ws.onerror=()=>{ elStat.textContent='○ error'; };
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

/* PROJECTS panel: decomposed projects with milestone/task progress + run-next-task */
const projOpen = new Set();
async function projRunNext(gid, btn){
  btn.disabled = true; btn.textContent = '…';
  try{ await fetch('/run-next-task',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({goal_id:gid})}); }catch(e){}
  await pollProjects(); tick();
}
async function taskAction(tid, action){
  try{ await fetch('/task-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({task_id:tid,action})}); }catch(e){}
  await pollProjects(); tick();
}
function tkRow(t){
  const s=(t.status||'').toLowerCase();
  const cls = s==='done'?'done':(s==='skipped'?'skip':(s==='blocked'?'blk':''));
  let act='';
  if(s==='blocked') act=` <button class=tact onclick="taskAction(${t.task_id},'retry')">↻ retry</button>`+
                        `<button class=tact onclick="taskAction(${t.task_id},'skip')">⏭ skip</button>`;
  return `<div class="tk ${cls}"><span class=ts>${s||'·'}</span> ${esc(t.title||'')} <span class=mut>[${esc(t.lane||'')}]</span>${act}</div>`;
}
async function projLoadDetail(gid, host){
  const det = host.querySelector('.det'); if(!det) return; det.innerHTML='loading…';
  try{
    const d = await (await fetch('/project?goal_id='+gid)).json();
    det.innerHTML = (d.milestones||[]).map(m=>
      `<div class=ms><b>▸ ${esc(m.title||'')}</b> <span class=mut>${m.pct}%</span>`+
      (m.tasks||[]).map(tkRow).join('')+
      `</div>`).join('') || '<span class=mut>no milestones</span>';
  }catch(e){ det.innerHTML='<span class=mut>detail unavailable</span>'; }
}
function projToggle(gid, host){
  if(projOpen.has(gid)){ projOpen.delete(gid); host.classList.remove('open'); }
  else { projOpen.add(gid); host.classList.add('open'); projLoadDetail(gid, host); }
}
async function pollProjects(){
  try{
    const d = await (await fetch('/project-goals')).json();
    const box = document.getElementById('projects'); const list = d.projects||[];
    if(!list.length){ box.innerHTML=''; return; }
    box.innerHTML = `<div class=ph>PROJECTS · ${list.length}</div>` + list.map(p=>{
      const open = projOpen.has(p.goal_id) ? 'open' : '';
      const complete = p.total>0 && p.done===p.total;
      return `<div class="proj ${open}" data-gid="${p.goal_id}">`+
        `<div class=top>`+
          `<span class=pt onclick="projToggle(${p.goal_id}, this.closest('.proj'))">${esc(p.title||'(untitled)')}</span>`+
          `<span class=pp>${p.done}/${p.total} · ${p.pct}%</span>`+
          (complete?'':`<button class=runnext onclick="projRunNext(${p.goal_id}, this)">▶ next</button>`)+
        `</div>`+
        `<div class=pbar><i style="width:${p.pct}%"></i></div>`+
        `<div class=det></div>`+
      `</div>`;
    }).join('');
    // repopulate details for projects left expanded
    projOpen.forEach(gid=>{ const host=document.querySelector(`.proj[data-gid="${gid}"]`); if(host) projLoadDetail(gid, host); });
  }catch(e){}
}
pollProjects(); setInterval(pollProjects, 8000);
</script></body></html>"""


if __name__ == "__main__":
    # Dev/direct run; in the container uvicorn runs `server:app` (see Dockerfile). Threads start
    # via the lifespan handler in both paths.
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
