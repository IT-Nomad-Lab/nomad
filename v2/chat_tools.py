"""The cockpit chat's read-only tools (#968, PRD docs/prd/cockpit-chat-tools.md section 4.1).

Seven tools, each one entry in `TOOLS`: a name, a description for the model, a JSON schema for its
arguments, and a handler. #970 hands the model exactly the entries its allowlist names, so a tool
added here is still out of reach until that list says otherwise. Nothing here is wired into
`chat_reply` yet.

Every handler reads what the cockpit already reads, through the same functions, so the chat and
the page can't disagree: `recent_runs` is what `GET /runs` serves, `run_detail` is `GET /run`,
the supervisor calls are the ones behind `GET /api/autopilot/*`, and `_services` is
`GET /api/services`. None of them writes. The supervisor is reached only through `_supervisor_get`,
which has no way to send anything but GET, and `test_chat_tools.py` checks both.

A tool that fails returns `{"error": ...}` as data, never raises (PRD R6): the model has to be able
to say "the supervisor is down" instead of filling the gap with a guess.

`server` is imported inside each call rather than at the top, because #970 makes `server` import
this module, and the engine's state (`db`, `eng`) lives there.
"""
import re
import time
from datetime import datetime, timezone

RUN_ID_RE = re.compile(r"^[0-9a-f]{6,32}$")
LOG_MAX = 40          # newest episodic entries a run detail carries; the page shows them all


def _srv():
    import server
    return server


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _age_s(stamp) -> int | None:
    """Seconds since a NocoDB timestamp, or None when it can't be read. A naive stamp is UTC,
    which is how NocoDB writes them."""
    if not stamp:
        return None
    try:
        t = datetime.fromisoformat(str(stamp).replace("Z", "+00:00").replace(" ", "T", 1))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(time.time() - t.timestamp())


def _supervisor_get(path: str) -> dict:
    """GET from the autopilot supervisor, through the engine's own proxy call. GET is fixed here:
    there is no argument that turns this into a write."""
    code, out = _srv()._autopilot("GET", path)
    if code != 200:
        err = (out or {}).get("error") or f"autopilot supervisor returned {code}"
        detail = (out or {}).get("detail")
        return {"error": f"{err} ({detail})" if detail else err}
    return out


# ── pipeline ─────────────────────────────────────────────────────────────────────────────────

def pipeline_runs(limit=12, status=None) -> dict:
    """The newest pipeline runs, as `GET /runs` lists them."""
    limit = _clamp(limit, 1, 50, 12)
    # With a status filter, scan further back so "the last 5 failed" isn't cut short by the
    # executed runs in between.
    rows = _srv().recent_runs(50 if status else limit)
    if status:
        rows = [r for r in rows if r.get("status") == status]
    out = []
    for r in rows[:limit]:
        item = {"run_id": r.get("run_id"), "lane": r.get("lane"), "status": r.get("status"),
                "created_at": r.get("created_at")}
        p = r.get("proposal")
        if p:
            item["proposal"] = {"action": p.get("action"), "target": p.get("target")}
        out.append(item)
    return {"runs": out, "count": len(out)}


def pipeline_run_detail(run_id="") -> dict:
    """One run in full, as `GET /run` returns it. A prefix resolves the way the chat already
    resolves one; an ambiguous prefix returns its candidates instead of picking."""
    srv = _srv()
    tok = str(run_id or "").strip().lower()
    if not RUN_ID_RE.match(tok):
        return {"error": f"{str(run_id)[:40]!r} is not a run id (6 to 32 hex characters)",
                "found": False}
    resolved, ambiguous = srv._resolve_run_ids(tok, max_runs=1)
    if ambiguous:
        return {"error": f"'{tok}' matches more than one run; ask which one",
                "found": False, "candidates": ambiguous[0][1]}
    d = srv.run_detail(resolved[0]) if resolved else {}
    if not d.get("status"):
        return {"error": f"no run {tok}", "found": False}
    d = {k: v for k, v in d.items() if k != "arg_fields"}   # the page's layout of `args`
    log = d.get("log") or []
    d["log"] = log[-LOG_MAX:]
    if len(log) > LOG_MAX:
        d["log_truncated"] = f"showing the newest {LOG_MAX} of {len(log)} log entries"
    d["found"] = True
    return d


def approval_queue(limit=20) -> dict:
    """Runs waiting at the gate, newest first: the cockpit's pending rows."""
    limit = _clamp(limit, 1, 50, 20)
    out = []
    for r in _srv()._pending_runs(50)[:limit]:
        p = r.get("proposal") or {}
        out.append({"run_id": r.get("run_id"), "lane": r.get("lane"),
                    "action": p.get("action"), "target": p.get("target"),
                    "preview": p.get("preview"), "warning": p.get("warning") or "",
                    "created_at": r.get("created_at"), "age_s": _age_s(r.get("created_at"))})
    return {"pending": out, "count": len(out)}


# ── autopilot ────────────────────────────────────────────────────────────────────────────────

def autopilot_runs(limit=20, project=None) -> dict:
    """The newest autopilot worker runs, as `GET /api/autopilot/runs` lists them."""
    limit = _clamp(limit, 1, 200, 20)
    # The supervisor can't filter by project, so a filtered ask reads its whole window.
    out = _supervisor_get(f"/runs?limit={200 if project else limit}")
    if "error" in out:
        return out
    runs = out.get("runs") or []
    if project:
        runs = [r for r in runs if r.get("project") == project]
    runs = runs[:limit]
    return {"runs": runs, "count": len(runs)}


def autopilot_status() -> dict:
    """Every project's autopilot state and today's spend, as `GET /api/autopilot/status`."""
    return _supervisor_get("/status")


def autopilot_config() -> dict:
    """Budget mode, daily cap and caps, as `GET /api/autopilot/config`."""
    return _supervisor_get("/config")


# ── the box ──────────────────────────────────────────────────────────────────────────────────

def system_health() -> dict:
    """Service health as nomad-watch last saw it, the engine's own heartbeat, and CPU/RAM/GPU.

    Reads only. It doesn't probe a service: a stale watch file comes back as `up: None` from
    `_services`, never as the value it last held."""
    srv = _srv()
    out = {"services": srv._services()}
    try:
        import runtime
        rt = runtime.active()
    except Exception as e:
        rt = f"unknown ({type(e).__name__})"
    age = int(time.time() - srv._BEATS["sweep"])
    out["engine"] = {"runtime": rt, "sweep_age_s": age,
                     "sweep_overdue": age >= srv.engine_mod.SWEEP_SECS * 3}
    out["telemetry"] = srv._proc_telemetry()
    return out


# ── the entries #970 picks from ──────────────────────────────────────────────────────────────

_NONE = {"type": "object", "properties": {}, "additionalProperties": False}

TOOLS = {t["name"]: t for t in (
    {"name": "pipeline_runs", "read_only": True, "handler": pipeline_runs,
     "description": "List the newest pipeline runs: run id, lane, status, created time, and for a "
                    "run at the gate the proposed action and target.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 12},
         "status": {"type": "string",
                    "description": "only runs with this status, e.g. failed, awaiting-approval"}}}},
    {"name": "pipeline_run_detail", "read_only": True, "handler": pipeline_run_detail,
     "description": "One pipeline run in full: status, goal, proposal, error, delivery, artifacts "
                    "and log. Takes a run id or a unique prefix of at least 6 hex characters.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["run_id"],
                      "properties": {"run_id": {"type": "string"}}}},
    {"name": "approval_queue", "read_only": True, "handler": approval_queue,
     "description": "Runs waiting at the gate for the operator's approval, with the proposed "
                    "action, target, preview, any warning, and age in seconds.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20}}}},
    {"name": "autopilot_runs", "read_only": True, "handler": autopilot_runs,
     "description": "The newest autopilot worker runs: project, task, branch, start and finish, "
                    "outcome and the worker's result line.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 20},
         "project": {"type": "string", "description": "only this project id"}}}},
    {"name": "autopilot_status", "read_only": True, "handler": autopilot_status,
     "description": "Each project's autopilot state (running, needs you, waiting, budget paused, "
                    "error, idle, off) and today's spend.",
     "input_schema": _NONE},
    {"name": "autopilot_config", "read_only": True, "handler": autopilot_config,
     "description": "The autopilot's budget mode, daily cap and caps.",
     "input_schema": _NONE},
    {"name": "system_health", "read_only": True, "handler": system_health,
     "description": "Service health from nomad-watch's last pass (up, down or unknown, with its "
                    "age), the engine's sweep heartbeat, and CPU, RAM and GPU.",
     "input_schema": _NONE},
)}


def call(name: str, args: dict | None = None) -> dict:
    """Run one tool by name. An unknown name, bad arguments or a failing read come back as
    `{"error": ...}`; nothing raises into the chat turn."""
    tool = TOOLS.get(name)
    if not tool:
        return {"error": f"no tool {name!r}"}
    args = args or {}
    if not isinstance(args, dict):
        return {"error": "arguments must be an object"}
    allowed = set(tool["input_schema"]["properties"])
    extra = sorted(set(args) - allowed)
    if extra:
        return {"error": f"{name} takes no argument {', '.join(extra)}"}
    try:
        return tool["handler"](**args)
    except Exception as e:
        return {"error": f"{name} failed: {type(e).__name__}: {str(e)[:200]}"}
