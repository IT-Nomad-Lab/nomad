"""NOMAD Console — LCARS command center backend.

Serves the LCARS UI and exposes read-only telemetry (system/GPU, service health,
Notion projects/agents/activity/approvals) plus a NOMAD chat proxy to LiteLLM.

Optional HTTP Basic Auth (set NOMAD_AUTH_USER + NOMAD_AUTH_PASS) — REQUIRED before
exposing this externally (e.g. via a tunnel). /healthz is always open.
"""
import asyncio
import os
import re
import secrets
import subprocess
import time
import urllib.parse
from datetime import datetime, timezone

import httpx
import psutil
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

import memory  # NOMAD long-term conversational memory (Qdrant + embeddings)
import intents  # NOMAD intent router (chat → action)

try:
    from notion_client import Client as NotionClient
except Exception:  # notion optional
    NotionClient = None

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
NOMAD_MODEL = os.environ.get("NOMAD_MODEL", "deep")
BUILD_DATE = "2026-06-01"
START_TS = time.time()
PROJECT_ROOTS = os.environ.get("PROJECT_ROOTS", "/host").split(":")
# Only these docs are ever read from a project dir (never arbitrary files / secrets).
DOC_FILES = ["CLAUDE.md", "claude.md", "Claude.md", "AGENTS.md", "README.md", "README.MD"]
DISPATCH_BASE = os.environ.get("NOMAD_DISPATCH_URL", "http://host.docker.internal:8090")
VOICE_BASE = os.environ.get("NOMAD_VOICE_URL", "http://nomad-voice:8200")
# Browser-reachable origin for the real-time WebRTC client embedded in the console. The console
# proxies /tts /stt server-side (VOICE_BASE), but real-time media is peer-to-peer browser↔voice, so
# the browser signals directly to the host-native voice service. Host-only → localhost by default.
VOICE_RT_URL = os.environ.get("NOMAD_VOICE_RT_URL", "http://localhost:8200").rstrip("/")
TERM_BASE = os.environ.get("NOMAD_TERM_URL", "ws://host.docker.internal:8091")  # interactive project terminal (PTY)


def _read_term_token():
    """Shared secret for the terminal daemon. termd auto-generates it on the host; the console
    reads the same file via its read-only /host mount (HOME is mounted at /host). Lets only the
    console attach to termd (which is otherwise LAN-reachable on 0.0.0.0:8091)."""
    env = os.environ.get("NOMAD_TERM_TOKEN")
    if env:
        return env
    for cand in ("/host/.config/nomad/term-token", os.path.expanduser("~/.config/nomad/term-token")):
        try:
            return open(cand).read().strip()
        except Exception:
            continue
    return ""


TERM_TOKEN = _read_term_token()

# v2 UX parity: the mission-control panels can read NocoDB instead of Notion. Default 'notion'
# so the live console is UNCHANGED until the operator flips NOMAD_SOURCE=nocodb (the cutover).
NOMAD_SOURCE = os.environ.get("NOMAD_SOURCE", "notion").lower()
NC_BASE = os.environ.get("NC_BASE_URL", "http://nocodb:8080").rstrip("/")
NC_TOKEN = os.environ.get("NC_API_TOKEN", "")
ENGINE_URL = os.environ.get("NOMAD_ENGINE_URL", "http://nomad-v2-engine:8099")  # v2 pipeline
CREW_URL = os.environ.get("NOMAD_CREW_URL", "http://crew:8001")                 # engineering crew
_nc_cache = {"base": None, "tables": {}}

AUTH_USER = os.environ.get("NOMAD_AUTH_USER", "")
AUTH_PASS = os.environ.get("NOMAD_AUTH_PASS", "")

_notion = NotionClient(auth=os.environ["NOTION_TOKEN"]) if (NotionClient and os.environ.get("NOTION_TOKEN")) else None
_ds_cache = {}

AGENTS = [
    ("Orchestrator", "Chief of Staff", "deep"),
    ("Planner", "Planner", "balanced"),
    ("Researcher", "Researcher", "longdoc"),
    ("Builder", "Builder / Dev", "code"),
    ("Writer", "Writer / Content", "balanced"),
    ("Comms", "Comms / Ops", "fast"),
    ("Reviewer", "Reviewer / QA", "deep"),
]

# NOMAD v2 lane specialists (mirrors v2/specialists.py LANES) — the roster in NocoDB mode.
V2_LANES = [
    ("Comms", "comms", "Email / outbound", "send_message", "balanced"),
    ("Researcher", "research", "Research / briefs", "save_brief", "balanced"),
    ("Support", "support", "Support / replies", "send_message", "balanced"),
    ("Ads", "ads", "Content / creative", "save_content", "balanced"),
    ("Dev", "dev", "Build / dispatch", "dispatch_build", "balanced"),
]

# NOMAD Engineering Crew (mirrors crew/dev_team.yaml) — the team that builds NOMAD.
DEV_AGENTS = [
    ("Lead Architect", "Design / Eng Manager", "deep"),
    ("Backend Eng", "Python / services", "code"),
    ("Frontend Eng", "Jarvis Ops Screen", "code"),
    ("Integration Eng", "DevOps / wiring", "balanced"),
    ("QA Eng", "Test / verify", "deep"),
    ("Reviewer", "Code review gate", "deep"),
]

SERVICES = [
    ("LiteLLM", f"{LITELLM_BASE}/health/liveliness"),
    ("Ollama", "http://host.docker.internal:11434/api/version"),
    ("Claude bridge", "http://host.docker.internal:8088/v1/models"),
    ("Crew", "http://crew:8001/health"),
    ("n8n", "http://host.docker.internal:5678/healthz"),
    ("Qdrant", "http://qdrant:6333/healthz"),
    ("Voice", f"{VOICE_BASE}/health"),
]


# ── auth middleware ─────────────────────────────────────────────────
# Login is enforced ONLY when the request arrives from outside the host (through a
# tunnel / reverse proxy). The console's published port is bound to 127.0.0.1, so a
# direct hit is local-only and runs login-free. Any tunnel (Cloudflare quick tunnel,
# Tailscale Funnel, nginx, …) injects one of these forwarding headers; a direct
# localhost connection does not. NOMAD_FORCE_AUTH=1 forces login even locally.
PROXY_HEADERS = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "x-real-ip")
FORCE_AUTH = os.environ.get("NOMAD_FORCE_AUTH", "").lower() in ("1", "true", "yes")


def _via_proxy(request: Request) -> bool:
    return any(request.headers.get(h) for h in PROXY_HEADERS)


class BasicAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            return await call_next(request)
        external = FORCE_AUTH or _via_proxy(request)
        if not external:
            return await call_next(request)  # local mode → no login
        # Reached from outside → login REQUIRED. Fail closed if no creds are configured,
        # so the console can never be exposed externally without a password.
        if not (AUTH_USER and AUTH_PASS):
            return Response("External access is disabled. Set NOMAD_AUTH_USER and "
                            "NOMAD_AUTH_PASS in .env to enable authenticated remote access.",
                            status_code=403)
        hdr = request.headers.get("authorization", "")
        ok = False
        if hdr.startswith("Basic "):
            import base64
            try:
                user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pw, AUTH_PASS)
            except Exception:
                ok = False
        if not ok:
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="NOMAD"'})
        return await call_next(request)


app = FastAPI(title="NOMAD Console")
app.add_middleware(BasicAuthMiddleware)


@app.on_event("startup")
async def _init_memory():
    await memory.ensure_collection()


# ── interactive project terminal: proxy the browser WS to the native PTY daemon ──
# Starlette's BaseHTTPMiddleware does NOT cover the websocket scope, so we re-apply the same
# external-access gate here: a tunnelled/forwarded WS must carry valid Basic-Auth; a direct
# localhost connection runs login-free (the console port is 127.0.0.1-bound).
def _ws_authorized(websocket: WebSocket) -> bool:
    external = FORCE_AUTH or any(websocket.headers.get(h) for h in PROXY_HEADERS)
    if not external:
        return True
    if not (AUTH_USER and AUTH_PASS):
        return False
    hdr = websocket.headers.get("authorization", "")
    if not hdr.startswith("Basic "):
        return False
    try:
        import base64
        user, _, pw = base64.b64decode(hdr[6:]).decode().partition(":")
        return secrets.compare_digest(user, AUTH_USER) and secrets.compare_digest(pw, AUTH_PASS)
    except Exception:
        return False


@app.websocket("/ws/terminal")
async def ws_terminal(websocket: WebSocket):
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)   # policy violation (auth)
        return
    await websocket.accept()
    project = websocket.query_params.get("project", "")
    cols = websocket.query_params.get("cols", "80")
    rows = websocket.query_params.get("rows", "24")
    upstream = (f"{TERM_BASE}/term?project={urllib.parse.quote(project)}"
                f"&cols={cols}&rows={rows}&token={urllib.parse.quote(TERM_TOKEN)}")
    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(upstream, max_size=4 * 1024 * 1024) as up:
            async def client_to_pty():
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("text") is not None:
                        await up.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await up.send(msg["bytes"])

            async def pty_to_client():
                async for m in up:               # ends when the PTY/claude session closes
                    if isinstance(m, (bytes, bytearray)):
                        await websocket.send_bytes(bytes(m))
                    else:
                        await websocket.send_text(m)

            # When EITHER side ends (browser closes OR claude exits), tear down BOTH — otherwise the
            # surviving pump task blocks forever waiting on a dead peer (deadlock).
            t1 = asyncio.create_task(client_to_pty())
            t2 = asyncio.create_task(pty_to_client())
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


@app.websocket("/ws/wake")
async def ws_wake(websocket: WebSocket):
    """Proxy the browser's mic PCM stream to nomad-voice's openWakeWord listener; relay the
    {wake:true} signal back. Keeps it same-origin + behind the console's auth gate."""
    if not _ws_authorized(websocket):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    upstream = VOICE_BASE.replace("http://", "ws://").replace("https://", "wss://").rstrip("/") + "/wake"
    from websockets.asyncio.client import connect as ws_connect
    try:
        async with ws_connect(upstream, max_size=4 * 1024 * 1024) as up:
            async def c2u():
                while True:
                    msg = await websocket.receive()
                    if msg["type"] == "websocket.disconnect":
                        return
                    if msg.get("bytes") is not None:
                        await up.send(msg["bytes"])
                    elif msg.get("text") is not None:
                        await up.send(msg["text"])

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


# ── helpers ─────────────────────────────────────────────────────────
def _ds(label):
    db = os.environ.get(f"NOTION_DB_{label}")
    if not (_notion and db):
        return None
    if db not in _ds_cache:
        _ds_cache[db] = _notion.databases.retrieve(db)["data_sources"][0]["id"]
    return _ds_cache[db]


def _title(props):
    for v in props.values():
        if v.get("type") == "title":
            return "".join(t["plain_text"] for t in v["title"]) or "(untitled)"
    return "?"


def _sel(props, name):
    p = props.get(name, {})
    s = p.get("select")
    return s["name"] if s else None


# ── NocoDB read path (v2 UX parity) ─────────────────────────────────
def _nc_req(path):
    import json as _json
    import urllib.request
    r = urllib.request.Request(NC_BASE + path, headers={"xc-token": NC_TOKEN})
    with urllib.request.urlopen(r, timeout=8) as resp:
        return _json.loads(resp.read().decode() or "{}")


def _nc_table(name):
    if not _nc_cache["base"]:
        _nc_cache["base"] = next(b["id"] for b in _nc_req("/api/v2/meta/bases/")["list"]
                                 if b["title"] == "NOMAD v2")
    if name not in _nc_cache["tables"]:
        _nc_cache["tables"] = {t["title"]: t["id"]
                               for t in _nc_req(f"/api/v2/meta/bases/{_nc_cache['base']}/tables")["list"]}
    return _nc_cache["tables"][name]


def _nc_rows(table, where="", limit=50):
    q = f"?limit={limit}" + (f"&where={where}" if where else "")
    return _nc_req(f"/api/v2/tables/{_nc_table(table)}/records{q}").get("list", [])


def _gpu():
    try:
        q = "name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit"
        out = subprocess.run(
            ["nvidia-smi", f"--query-gpu={q}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None
        f = [x.strip() for x in out.stdout.strip().splitlines()[0].split(",")]

        def num(x):  # laptop GPUs report some fields as "[N/A]"
            try:
                return float(x)
            except Exception:
                return None
        return {
            "name": f[0], "util": num(f[1]), "mem_used": num(f[2]),
            "mem_total": num(f[3]), "temp": num(f[4]),
            "power": num(f[5]), "power_limit": num(f[6]),
        }
    except Exception:
        return None


# ── endpoints ───────────────────────────────────────────────────────
@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/version")
def api_version():
    return {"name": "NOMAD", "version": "1.0", "build_date": BUILD_DATE}


@app.get("/api/ping")
def api_ping():
    return {"pong": True, "ts": datetime.now(timezone.utc).isoformat()}


class WhoAmI(BaseModel):
    assistant: str
    role: str


@app.get("/api/whoami", response_model=WhoAmI)
def api_whoami():
    return {"assistant": "NOMAD", "role": "orchestrator"}


@app.get("/api/system")
def api_system():
    vm = psutil.virtual_memory()
    try:
        du = psutil.disk_usage("/")
    except Exception:
        du = None
    return {
        "cpu_percent": psutil.cpu_percent(interval=0.2),
        "cpu_count": psutil.cpu_count(),
        "per_cpu": psutil.cpu_percent(interval=0.0, percpu=True),
        "mem_used_gb": round(vm.used / 1e9, 1),
        "mem_total_gb": round(vm.total / 1e9, 1),
        "mem_percent": vm.percent,
        "disk_percent": du.percent if du else None,
        "disk_used_gb": round(du.used / 1e9, 1) if du else None,
        "disk_total_gb": round(du.total / 1e9, 1) if du else None,
        "uptime_s": int(time.time() - START_TS),
        "gpu": _gpu(),
        "ts": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/services")
async def api_services():
    out = []
    async with httpx.AsyncClient(timeout=4) as c:
        for name, url in SERVICES:
            t0 = time.time()
            try:
                r = await c.get(url)
                up = r.status_code < 500
            except Exception:
                up = False
            out.append({"name": name, "up": up, "ms": int((time.time() - t0) * 1000)})
    return out


@app.get("/api/ollama")
async def api_ollama():
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get("http://host.docker.internal:11434/api/ps")
            models = r.json().get("models", [])
            return [{"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 1)} for m in models]
    except Exception:
        return []


@app.get("/api/projects")
def api_projects():
    if NOMAD_SOURCE == "nocodb":
        try:
            return {"projects": [{"name": r.get("title") or "(untitled)", "status": r.get("status") or "—",
                                  "lane": r.get("lane") or "", "stack": r.get("stack") or "",
                                  "repo": r.get("repo") or ""}
                                 for r in _nc_rows("projects", limit=25)]}
        except Exception as e:
            return {"projects": [], "error": str(e)[:120]}
    ds = _ds("PROJECTS")
    if not ds:
        return {"projects": []}
    try:
        rows = _notion.data_sources.query(data_source_id=ds, page_size=25).get("results", [])
        projects = [{"name": _title(r["properties"]), "status": _sel(r["properties"], "Status") or "—"} for r in rows]
    except Exception as e:
        return {"projects": [], "error": str(e)[:120]}
    return {"projects": projects}


@app.get("/api/goals")
def api_goals():
    if NOMAD_SOURCE == "nocodb":
        try:
            return {"goals": [{"name": r.get("title") or "(untitled)", "priority": r.get("priority") or "—"}
                              for r in _nc_rows("mc_goals", limit=25)]}
        except Exception:
            return {"goals": []}
    ds = _ds("GOALS")
    if not ds:
        return {"goals": []}
    try:
        rows = _notion.data_sources.query(data_source_id=ds, page_size=25).get("results", [])
        goals = [{"name": _title(r["properties"]), "priority": _sel(r["properties"], "Priority") or "—"} for r in rows]
    except Exception:
        goals = []
    return {"goals": goals}


def _recent_activity(limit=12):
    if NOMAD_SOURCE == "nocodb":
        try:
            items = [{"action": r.get("title") or "", "agent": r.get("agent") or "—",
                      "ts": r.get("ts") or ""} for r in _nc_rows("activity", limit=40)]
            # merge the LIVE v2 pipeline log (episodic) so the Mission Log shows new runs
            for r in _nc_rows("episodic", limit=40):
                items.append({"action": ((r.get("what") or "") + " → " + (r.get("outcome") or "")).strip(" →"),
                              "agent": r.get("agent") or "—", "ts": r.get("created_at") or ""})
            items.sort(key=lambda x: x.get("ts") or "", reverse=True)
            return items[:limit]
        except Exception:
            return []
    ds = _ds("ACTIVITY")
    if not ds:
        return []
    try:
        rows = _notion.data_sources.query(
            data_source_id=ds, page_size=limit,
            sorts=[{"property": "Timestamp", "direction": "descending"}],
        ).get("results", [])
    except Exception:
        try:
            rows = _notion.data_sources.query(data_source_id=ds, page_size=limit).get("results", [])
        except Exception:
            rows = []
    items = []
    for r in rows:
        p = r["properties"]
        ts = p.get("Timestamp", {}).get("date") or {}
        items.append({"action": _title(p), "agent": _sel(p, "Agent") or "—",
                      "ts": ts.get("start", "")})
    return items


@app.get("/api/activity")
def api_activity():
    return {"activity": _recent_activity(14)}


def _v2_roster():
    """v2 lane specialists, with a lane marked ACTIVE if it has a live (non-terminal) run in the
    engine's runs feed. Fail-open: engine unreachable → all STANDBY."""
    busy = set()
    try:
        with httpx.Client(timeout=3) as c:
            for r in c.get(f"{ENGINE_URL}/runs").json().get("runs", []):
                if (r.get("status") or "") in ("new", "awaiting-approval", "executing"):
                    busy.add(r.get("lane"))
    except Exception:
        pass
    return {"agents": [
        {"name": name, "role": role, "model": model,
         "status": "ACTIVE" if lane in busy else "STANDBY"}
        for (name, lane, role, _action, model) in V2_LANES
    ]}


@app.get("/api/agents")
def api_agents():
    if NOMAD_SOURCE == "nocodb":
        return _v2_roster()
    # an agent is "active" if it logged activity in the last 10 min
    recent = _recent_activity(30)
    now = datetime.now(timezone.utc)
    active = set()
    for a in recent:
        try:
            t = datetime.fromisoformat(a["ts"].replace("Z", "+00:00"))
            if (now - t).total_seconds() < 600:
                active.add(a["agent"])
        except Exception:
            pass
    return {"agents": [
        {"name": n, "role": role, "model": model,
         "status": "ACTIVE" if n in active else "STANDBY"}
        for (n, role, model) in AGENTS
    ]}


@app.get("/api/devteam")
async def api_devteam():
    """Engineering crew roster + whether the build pipeline (dispatcher) is live."""
    builder_online, buildable = False, 0
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{DISPATCH_BASE}/projects")
            builder_online = r.status_code < 500
            buildable = len(r.json().get("projects", []))
    except Exception:
        pass
    return {
        "agents": [{"name": n, "role": role, "model": m} for (n, role, m) in DEV_AGENTS],
        "builder_online": builder_online,
        "buildable": buildable,
    }


class ApprovalDecision(BaseModel):
    run_id: str
    decision: str   # "approved" | "rejected"


@app.post("/api/approvals/decision")
async def api_approval_decision(d: ApprovalDecision):
    """Approve/reject a pipeline run straight from the dashboard → engine resume (same gate path
    as the v2 cockpit). Approving executes the action; rejecting marks it declined."""
    if d.decision not in ("approved", "rejected"):
        return JSONResponse({"error": "decision must be approved|rejected"}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{ENGINE_URL}/resume",
                             json={"run_id": d.run_id, "decision": d.decision})
            return r.json()
    except Exception as e:
        return JSONResponse({"error": f"engine unreachable: {str(e)[:140]}"}, status_code=502)


@app.post("/api/devteam/run-backlog")
async def api_devteam_run_backlog():
    """Kick the engineering crew to work its Notion backlog (hands-off; empty backlog = no-op)."""
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{CREW_URL}/run-backlog", json={"limit": 3})
            return r.json()
    except Exception as e:
        return JSONResponse({"error": f"crew unreachable: {str(e)[:140]}"}, status_code=502)


@app.get("/api/approvals")
def api_approvals():
    if NOMAD_SOURCE == "nocodb":
        # The actionable queue is the LIVE v2 pipeline gate (runs awaiting-approval), which carry a
        # run_id + proposal — so the panel's approve/reject buttons actually execute via the engine.
        try:
            out = []
            with httpx.Client(timeout=3) as c:
                for r in c.get(f"{ENGINE_URL}/runs").json().get("runs", []):
                    if r.get("status") != "awaiting-approval":
                        continue
                    p = r.get("proposal") or {}
                    act, tgt = (p.get("action") or "action"), (p.get("target") or "")
                    out.append({"run_id": r.get("run_id"), "type": r.get("lane") or "—",
                                "action": act + (f" → {tgt}" if tgt else ""),
                                "by": r.get("from_agent") or "manager",
                                "preview": (p.get("preview") or "")[:160]})
            return {"approvals": out}
        except Exception:
            return {"approvals": []}
    ds = _ds("APPROVALS")
    if not ds:
        return {"approvals": []}
    try:
        rows = _notion.data_sources.query(
            data_source_id=ds, page_size=20,
            filter={"property": "Status", "select": {"equals": "Pending"}},
        ).get("results", [])
    except Exception:
        rows = []
    return {"approvals": [
        {"action": _title(r["properties"]),
         "type": _sel(r["properties"], "Type") or "—",
         "by": _sel(r["properties"], "Requested By") or "—"} for r in rows
    ]}


def _context_blurb():
    parts = []
    try:
        pr = api_projects()["projects"]
        if pr:
            parts.append("Projects: " + "; ".join(f"{p['name']} [{p['status']}]" for p in pr[:8]))
    except Exception:
        pass
    try:
        ap = api_approvals()["approvals"]
        parts.append(f"Pending approvals: {len(ap)}" + (
            " (" + "; ".join(f"{a['type']}: {a['action']}" for a in ap[:5]) + ")" if ap else ""))
    except Exception:
        pass
    try:
        s = api_system()
        g = s.get("gpu")
        gpu = f"GPU {g['name']} {g['util']:.0f}% {g['mem_used']:.0f}/{g['mem_total']:.0f}MiB {g['temp']:.0f}C" if g else "GPU n/a"
        parts.append(f"System: CPU {s['cpu_percent']:.0f}%, RAM {s['mem_percent']:.0f}%, {gpu}")
    except Exception:
        pass
    return "\n".join(parts)


# ── project understanding: read a named project's CLAUDE.md on demand ──
_reg = {"ts": 0, "data": []}


def _frontmatter(path):
    meta = {}
    try:
        lines = open(path, encoding="utf-8", errors="ignore").read().splitlines()
    except Exception:
        return meta
    if not lines or lines[0].strip() != "---":
        return meta
    for ln in lines[1:]:
        if ln.strip() == "---":
            break
        if ":" in ln:
            k, _, v = ln.partition(":")
            meta[k.strip().lower()] = v.strip()
    return meta


def _registry():
    """Projects = dirs under PROJECT_ROOTS containing Nomad.md. Cached 60s."""
    if time.time() - _reg["ts"] < 60 and _reg["data"]:
        return _reg["data"]
    reg = []
    for root in PROJECT_ROOTS:
        if not os.path.isdir(root):
            continue
        for nm in sorted(os.listdir(root)):
            d = os.path.join(root, nm)
            if os.path.isdir(d) and os.path.isfile(os.path.join(d, "Nomad.md")):
                name = _frontmatter(os.path.join(d, "Nomad.md")).get("name") or nm
                aliases = {name.lower(), nm.lower(), nm.split("-")[0].lower()}
                aliases |= {w for w in name.lower().split() if len(w) >= 4}
                reg.append({"name": name, "path": d,
                            "aliases": {a for a in aliases if len(a) >= 4}})
    _reg.update(ts=time.time(), data=reg)
    return reg


def _read_doc(path):
    for f in DOC_FILES:
        fp = os.path.join(path, f)
        if os.path.isfile(fp):
            try:
                return f, open(fp, encoding="utf-8", errors="ignore").read()[:7000]
            except Exception:
                pass
    return None, None


def _project_context(text):
    """If the message names a project, return its doc(s) for NOMAD to reason over."""
    t = " " + re.sub(r"[^a-z0-9 ]", " ", (text or "").lower()) + " "
    blocks = []
    for p in _registry():
        if any(re.search(r"\b" + re.escape(a) + r"\b", t) for a in p["aliases"]):
            fname, content = _read_doc(p["path"])
            if content:
                blocks.append(f"### PROJECT: {p['name']}  (source: {fname} @ {p['path']})\n{content}")
        if len(blocks) >= 2:
            break
    return "\n\n".join(blocks)


SYSTEM_PROMPT = (
    "You are NOMAD — Networked Operations & Management Assistant for Decisions — "
    "the central AI orchestrator of a self-hosted multi-agent system on the "
    "operator's own machine. Speak concisely, with calm and capable authority: "
    "the Star Trek main computer crossed with a sharp chief of staff. You can see "
    "the operator's live projects, agent roster, system telemetry and pending "
    "approvals (provided below). Help them read status, plan, and decide. Never "
    "claim to have taken an irreversible action — those route through the human "
    "approval gate. If data is missing, say so plainly.\n\n"
    "When the operator names a specific project, that project's own documentation "
    "(CLAUDE.md / AGENTS.md / README) is included under REFERENCED PROJECT "
    "DOCUMENTATION below. Read it to understand that project's architecture, "
    "conventions and current state, then carry out the request in that context — "
    "answer, plan, or draft the code/commands. To actually execute changes in a "
    "repo, hand off to the crew/specialist agents; don't claim you ran something.\n\n"
    "The operator can dispatch REAL work to the Builder (Claude Code running inside the "
    "repo) with `/plan <project>: <task>` (read-only plan) or `/build <project>: <task>` "
    "(makes uncommitted edits, never commits/pushes). When asked to implement something, "
    "give the exact `/build` or `/plan` command to run rather than claiming you did it.\n\n"
    "You have PERSISTENT MEMORY across conversations. Relevant excerpts from past "
    "conversations are provided under RECALLED MEMORY when they bear on the current "
    "message — treat them as things you genuinely remember, and weave them in "
    "naturally rather than announcing that you searched a database. They are recalled "
    "by relevance, not recency, and may be old; if memory and live context conflict, "
    "trust the live context. The operator can tell you to forget things ('off the "
    "record', 'forget that', 'wipe your memory') and that is honored automatically.\n\n"
    "=== LIVE OPERATIONAL CONTEXT ===\n{context}\n=== END CONTEXT ==="
)


def _format_memories(mems):
    lines = []
    for m in mems:
        who = "Operator" if m.get("role") == "user" else "You (NOMAD)"
        lines.append(f"- {who}: {m.get('content', '').strip()[:400]}")
    return "\n".join(lines)


# ── action handlers (the "Jarvis does things" layer) ───────────────
async def _handle_diagnostics():
    sysd = api_system()
    services = await api_services()
    approvals = api_approvals().get("approvals", [])
    projects = api_projects().get("projects", [])
    up = [s["name"] for s in services if s["up"]]
    down = [s["name"] for s in services if not s["up"]]
    g = sysd.get("gpu")
    L = ["**NOMAD diagnostic report**", ""]
    L.append(f"Systems online: {len(up)}/{len(services)}" + (f" — {', '.join(up)}" if up else ""))
    if down:
        L.append(f"⚠ OFFLINE: {', '.join(down)}")
    disk = f" · disk {sysd['disk_percent']:.0f}%" if sysd.get("disk_percent") is not None else ""
    L.append(f"CPU {sysd['cpu_percent']:.0f}% · RAM {sysd['mem_percent']:.0f}% "
             f"({sysd['mem_used_gb']}/{sysd['mem_total_gb']} GB){disk}")
    if g and g.get("util") is not None:
        L.append(f"GPU {g['name']}: {g['util']:.0f}% util · {g['mem_used']:.0f}/{g['mem_total']:.0f} MiB · {g['temp']:.0f}°C")
    L.append(f"Active projects: {len(projects)}" + (f" — {', '.join(p['name'] for p in projects[:6])}" if projects else ""))
    L.append(f"Pending approvals: {len(approvals)}" + (" ⚠ awaiting your decision" if approvals else ""))
    hot = g and g.get("temp") and g["temp"] >= 85
    L += ["", "All systems nominal, Commander." if not down and not hot
          else "Attention required — see flagged items above."]
    return "\n".join(L)


def _kb_save(title, body):
    """File a research brief to the Notion Knowledge Base (title row + body blocks)."""
    db = os.environ.get("NOTION_DB_KNOWLEDGE")
    if not (_notion and db):
        return False
    chunks = [body[i:i + 1900] for i in range(0, len(body), 1900)] or [""]
    children = [{"object": "block", "type": "paragraph",
                 "paragraph": {"rich_text": [{"text": {"content": c}}]}} for c in chunks[:8]]
    try:
        _notion.pages.create(
            parent={"database_id": db},
            properties={"Title": {"title": [{"text": {"content": title[:200]}}]},
                        "Type": {"select": {"name": "Brief"}}},
            children=children)
        return True
    except Exception:
        return False


async def _handle_research(topic):
    sysp = ("You are NOMAD's Researcher. Produce a TIGHT, structured brief: key facts, "
            "the main options with trade-offs, and a clear recommendation. Be concrete and "
            "concise (think one screen). If you lack live web access, reason from knowledge "
            "and say so. Use short headers and bullets.")
    payload = {"model": "longdoc", "max_tokens": 1300,
               "messages": [{"role": "system", "content": sysp},
                            {"role": "user", "content": f"Research topic: {topic}"}]}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{LITELLM_BASE}/v1/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {LITELLM_KEY}"})
            brief = r.json()["choices"][0]["message"]["content"]
    except Exception as e:
        return f"⚠ Researcher link error: {str(e)[:160]}"
    saved = _kb_save(f"Research: {topic}", brief)
    tag = "Filed to the Knowledge Base. " if saved else ""
    return f"{tag}Here's the brief on **{topic}**:\n\n{brief}"


def _project_row(name, status="Planning"):
    db = os.environ.get("NOTION_DB_PROJECTS")
    if not (_notion and db):
        return False
    try:
        _notion.pages.create(parent={"database_id": db}, properties={
            "Name": {"title": [{"text": {"content": name}}]},
            "Status": {"select": {"name": status}},
            "Owner": {"rich_text": [{"text": {"content": "Josias"}}]},
        })
        return True
    except Exception:
        return False


async def _handle_start_project(name, request):
    if not name:
        return ("I need a name to start a project — try: "
                "\"start a project called <name>\".")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{DISPATCH_BASE}/new-project",
                             json={"name": name, "description": request})
            res = r.json()
    except Exception as e:
        return f"⚠ Couldn't reach the dispatcher to scaffold the project: {str(e)[:160]}"
    if not res.get("ok"):
        return f"Couldn't create **{name}**: {res.get('error', 'unknown error')}"
    _reg["ts"] = 0  # bust the project registry cache so it appears immediately
    in_notion = _project_row(res["name"])
    note = " It's on the mission-control board" if in_notion else ""
    return (f"Project **{res['name']}** is live, Commander. Scaffolded a git repo at "
            f"`{res['path']}` with a Nomad.md marker, CLAUDE.md, and README.{note}.\n\n"
            f"I can take it from here — say `/plan {res['slug']}: <what to build>` for a "
            f"read-only plan, or `/build {res['slug']}: <task>` to start implementing.")


async def _handle_self_develop():
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post("http://crew:8001/run-backlog", json={"limit": 3})
            res = r.json()
    except Exception as e:
        return f"⚠ Couldn't reach the engineering crew to start self-development: {str(e)[:160]}"
    if not res.get("started"):
        return ("Backlog is empty — nothing queued for me to build. Add Tasks rows with "
                "**Assigned Agent = Engineering Crew**, **Status = Backlog**, and a title like "
                "`[NOMAD] <what to build>`, then tell me to work on my backlog.")
    return (f"Engineering crew engaged, Commander — working {res['queued']} backlog item"
            f"{'s' if res['queued'] > 1 else ''} (up to {res['limit']}). For each I'll "
            f"design → build → prove → review, flip the task to **Review** when approved, and "
            f"leave the diff uncommitted for you. Track progress in the Mission Log and task "
            f"statuses; I never commit on my own.")


async def _handle_pipeline_capture(goal):
    if not goal:
        return "Give me a goal — e.g. `/capture research the best local STT engine`."
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(f"{ENGINE_URL}/capture", json={"goal": goal})
            d = r.json()
    except Exception as e:
        return f"⚠ Couldn't reach the v2 pipeline engine: {str(e)[:160]}"
    if d.get("error"):
        return f"Capture failed: {d['error']}"
    lane = d.get("lane", "?")
    action = (d.get("proposal") or {}).get("action", "?")
    return (f"Captured into the pipeline — routed to **{lane}** ({action}). It's now in your "
            f"**Approval Queue** (top-right panel), awaiting your decision. Click ✓ / ✗, or just "
            f"say or type **“approve”** / **“reject”**. Nothing runs until you do.")


async def _handle_gate_decision(decision, do_all):
    """Approve/reject the pending pipeline run(s) — by voice or text. Targets the most recent unless
    'all' was said. This is what makes verbal approval work."""
    pend = api_approvals().get("approvals", [])
    if not pend:
        return "Nothing is awaiting your approval right now."
    targets = pend if do_all else [pend[0]]      # api_approvals lists newest first
    done = []
    async with httpx.AsyncClient(timeout=40) as c:
        for a in targets:
            rid = a.get("run_id")
            if not rid:
                continue
            try:
                r = await c.post(f"{ENGINE_URL}/resume", json={"run_id": rid, "decision": decision})
                done.append((a, r.json()))
            except Exception as e:
                done.append((a, {"error": str(e)[:80]}))
    verb = "Approved" if decision == "approved" else "Rejected"
    lines = []
    for a, res in done:
        st = res.get("status") or res.get("error") or "?"
        out = res.get("outcome", "")
        lines.append(f"- {a.get('action', 'action')} → **{st}**" + (f" — {out[:70]}" if out else ""))
    remaining = 0 if do_all else max(0, len(pend) - len(done))
    msg = f"{verb} {len(done)} item(s):\n" + "\n".join(lines)
    if remaining:
        msg += f"\n\n{remaining} more still pending — say “approve all”, or click them in the queue."
    return msg


ACTION_INTENTS = {
    "diagnostics": lambda p: _handle_diagnostics(),
    # research/web-search + imperative actions now CAPTURE into the pipeline → they hit the human
    # gate and show up in the Approval Queue (instead of running ungated or just being talked about).
    "research": lambda p: _handle_pipeline_capture("Research and brief me on " + p.get("topic", "")),
    "action_capture": lambda p: _handle_pipeline_capture(p.get("request", "")),
    "gate_decision": lambda p: _handle_gate_decision(p.get("decision", "approved"), p.get("all", False)),
    "start_project": lambda p: _handle_start_project(p.get("name", ""), p.get("request", "")),
    "self_develop": lambda p: _handle_self_develop(),
    "pipeline_capture": lambda p: _handle_pipeline_capture(p.get("goal", "")),
}


@app.post("/api/chat")
async def api_chat(req: Request):
    body = await req.json()
    messages = body.get("messages", [])
    model = body.get("model") or NOMAD_MODEL
    session_id = body.get("session_id") or "default"
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    last_user = last_user if isinstance(last_user, str) else ""

    # ── memory opt-out / forget commands (honored before anything else) ──
    intent = memory.classify_memory_intent(last_user)
    store_this_turn = True
    if intent == "wipe":
        await memory.forget(session_id, scope="session")
        return {"reply": "Done — I've wiped this conversation from my memory. "
                         "Nothing from it will resurface in future recalls.", "model": model}
    if intent == "forget_last":
        n = await memory.forget(session_id, scope="last")
        store_this_turn = False  # don't store the "forget that" exchange itself
        return {"reply": f"Forgotten — dropped the last {max(n, 0)} turn(s) from memory.",
                "model": model}
    if intent == "off_record":
        store_this_turn = False  # answer normally, but persist nothing this turn

    # ── intent router: turn a command into an ACTION, not just chat ──
    action, payload = intents.classify(last_user)
    if action in ACTION_INTENTS:
        reply = await ACTION_INTENTS[action](payload)
        if store_this_turn:
            await memory.remember(session_id, "user", last_user)
            await memory.remember(session_id, "assistant", reply)
        return {"reply": reply, "model": f"nomad/{action}", "action": action,
                "remembered": store_this_turn}

    # ── recall relevant past conversations (cross-session) ──
    mems = await memory.recall(last_user, k=6, exclude_session=session_id) if last_user else []

    proj = _project_context(last_user)
    sys_content = SYSTEM_PROMPT.format(context=_context_blurb() or "(telemetry unavailable)")
    if proj:
        sys_content += "\n\n=== REFERENCED PROJECT DOCUMENTATION ===\n" + proj
    if mems:
        sys_content += "\n\n=== RECALLED MEMORY (from past conversations) ===\n" + _format_memories(mems)
    sys_msg = {"role": "system", "content": sys_content}
    payload = {"model": model, "messages": [sys_msg] + messages, "max_tokens": 1500}
    try:
        async with httpx.AsyncClient(timeout=180) as c:
            r = await c.post(f"{LITELLM_BASE}/v1/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {LITELLM_KEY}"})
            data = r.json()
        reply = data["choices"][0]["message"]["content"]
    except Exception as e:
        return JSONResponse({"reply": f"⚠ NOMAD link error: {str(e)[:200]}", "model": model}, status_code=200)

    # ── persist the exchange (best-effort, fail-open) ──
    if store_this_turn:
        await memory.remember(session_id, "user", last_user)
        await memory.remember(session_id, "assistant", reply)

    return {"reply": reply, "model": data.get("model", model), "remembered": store_this_turn}


@app.get("/api/history")
async def api_history(session_id: str = "default"):
    return {"turns": await memory.history(session_id)}


@app.get("/api/voice-config")
def api_voice_config():
    """Front-end voice config. The wake word now runs on **openWakeWord** in nomad-voice
    (local, free, no account) — the browser streams mic audio to /ws/wake and the UI falls back
    to the local Whisper listen-loop only if that can't start. (Picovoice/Porcupine retired.)"""
    return {"wake": "openwakeword", "label": os.environ.get("WAKE_LABEL", "Hey Jarvis"),
            "rt_url": VOICE_RT_URL}


@app.post("/api/tts")
async def api_tts(req: Request):
    """Proxy to the local Piper TTS service → returns WAV audio for the browser to play."""
    body = await req.json()
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{VOICE_BASE}/tts", json={"text": body.get("text", "")})
        if r.status_code != 200:
            return JSONResponse({"error": "tts failed"}, status_code=r.status_code)
        return Response(content=r.content, media_type="audio/wav")
    except Exception as e:
        return JSONResponse({"error": f"voice service unreachable: {str(e)[:160]}"}, status_code=503)


@app.post("/api/stt")
async def api_stt(req: Request):
    """Proxy raw audio bytes to the local Whisper STT service → returns {text}."""
    data = await req.body()
    ct = req.headers.get("content-type", "application/octet-stream")
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(f"{VOICE_BASE}/stt",
                             files={"file": ("audio.webm", data, ct)})
        return JSONResponse(r.json(), status_code=r.status_code)
    except Exception as e:
        return JSONResponse({"error": f"voice service unreachable: {str(e)[:160]}"}, status_code=503)


@app.get("/api/briefing")
async def api_briefing():
    """Proactive watch: things NOMAD should SPEAK UP about. The frontend polls this and
    has NOMAD announce any alert key it hasn't announced yet. Each alert has a stable
    `key` so the same condition isn't repeated every poll."""
    alerts = []
    try:
        services = await api_services()
        for s in services:
            if not s["up"]:
                alerts.append({"key": f"down:{s['name']}", "severity": "critical",
                               "text": f"{s['name']} is offline."})
    except Exception:
        pass
    try:
        approvals = api_approvals().get("approvals", [])
        if approvals:
            head = approvals[0]
            extra = f" and {len(approvals) - 1} more" if len(approvals) > 1 else ""
            alerts.append({"key": f"approvals:{len(approvals)}", "severity": "warn",
                           "text": f"You have {len(approvals)} pending approval"
                                   f"{'s' if len(approvals) > 1 else ''}: "
                                   f"{head['type']} — {head['action']}{extra}. Awaiting your decision."})
    except Exception:
        pass
    try:
        g = api_system().get("gpu")
        if g and g.get("temp") and g["temp"] >= 85:
            alerts.append({"key": f"gpu_hot:{int(g['temp'])}", "severity": "warn",
                           "text": f"GPU temperature is {g['temp']:.0f}°C — running hot."})
    except Exception:
        pass
    return {"alerts": alerts, "ts": datetime.now(timezone.utc).isoformat()}


@app.post("/api/dispatch")
async def api_dispatch(req: Request):
    """Hand a task to the Builder (Claude Code in the repo) via the dispatcher."""
    body = await req.json()
    try:
        async with httpx.AsyncClient(timeout=920) as c:
            r = await c.post(f"{DISPATCH_BASE}/dispatch", json=body)
            return r.json()
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"dispatcher link error: {str(e)[:200]}"}, status_code=200)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))
