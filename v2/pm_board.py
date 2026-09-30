"""NOMAD v2 · the OpenProject board in numbers, for the cockpit and the briefing (PM-01 P4).

One read of the project's work packages and its blocks relations gives open work by status and
type, the blocked chain and WIP per lane. Burnup, cycle time and throughput need to know when a
task started and closed, and only a work package's activity says that, so those read the activity
of recently closed tasks. Each activity read is cached against the work package's lockVersion,
which changes on every write, so a closed task is read once and then never again.

Per-project charts beyond these are OpenProject's own widgets; the cockpit links to them.
"""
import datetime as dt
import json
import threading
import urllib.parse
import time

from pm_follow import PUBLIC_URL, status_change
from pm_openproject import (CONTAINERS, NEXT_STATUSES, NEXT_TYPES, PMError, _href_id, _title,
                            parent_id)

STATUS_ORDER = ["New", "Backlog", "Ready", "In Progress", "Awaiting approval", "In Review",
                "Blocked", "Done", "Won't do", "Duplicate"]
ACTIVE = {"In Progress", "Awaiting approval", "In Review", "Blocked"}
TTL = 60
CLOSED_CAP = 200         # closed tasks whose history is read, most recently updated first
DAYS = 60                # burnup window
WEEKS = 8                # throughput window

_lock = threading.Lock()
_cache = {}              # project identifier → (time, summary)
_history = {}            # (wp id, lockVersion) → transitions


def _ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _hours(a, b):
    return round((b - a).total_seconds() / 3600, 1)


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else round((xs[n // 2 - 1] + xs[n // 2]) / 2, 1)


def transitions(op, wp):
    """[(time, from, to, user href)] for one work package, oldest first, cached by lockVersion."""
    key = (wp["id"], wp.get("lockVersion"))
    if key not in _history:
        out = []
        for a in op.activities(wp["id"]):
            ch = status_change(a.get("details"))
            if ch:
                out.append((_ts(a["createdAt"]), ch[0], ch[1], (a["_links"].get("user") or {}).get("href")))
        _history[key] = out
    return _history[key]


def lifecycle(op, wp):
    """(started, closed_at, closer href): first move into In Progress, last move into a closed
    status. Either can be None."""
    started = closed_at = closer = None
    for t, _, to, who in transitions(op, wp):
        if to == "In Progress" and started is None:
            started = t
        if op.closed(to):
            closed_at, closer = t, who
        elif closed_at and not op.closed(to):
            closed_at = closer = None      # reopened
    return started, closed_at, closer


def summary(op, now=None, fresh=False):
    ident = op.project_ident
    with _lock:
        hit = _cache.get(ident)
        if hit and not fresh and time.time() - hit[0] < TTL:
            return hit[1]
    data = build(op, now)
    with _lock:
        _cache[ident] = (time.time(), data)
    return data


def build(op, now=None):
    now = now or dt.datetime.now(dt.timezone.utc)
    wps = op.project_wps()
    by_id = {w["id"]: w for w in wps}
    status = {w["id"]: _title(w, "status") for w in wps}
    closed = {i for i, s in status.items() if op.closed(s)}
    tasks = [w for w in wps if _title(w, "type") not in CONTAINERS]

    # open work by status and type
    by_status = {}
    for w in wps:
        if w["id"] in closed:
            continue
        row = by_status.setdefault(status[w["id"]], {})
        row[_title(w, "type")] = row.get(_title(w, "type"), 0) + 1
    statuses = [s for s in STATUS_ORDER if s in by_status] + sorted(set(by_status) - set(STATUS_ORDER))

    # the blocked chain: Blocked items, and open items waiting on an open blocker
    blockers = {}
    for b, t in op.block_relations():
        if t in by_id and b in by_id:
            blockers.setdefault(t, []).append(b)

    def open_blockers(i):
        return [b for b in blockers.get(i, []) if b not in closed]

    def chain(i, depth=1, seen=None):
        seen = seen if seen is not None else {i}
        out = []
        for b in open_blockers(i):
            if b in seen or depth > 6:
                continue
            seen.add(b)
            w = by_id[b]
            out.append({"id": b, "subject": w.get("subject"), "status": status[b], "depth": depth})
            out += chain(b, depth + 1, seen)
        return out

    blocked = []
    for w in wps:
        i = w["id"]
        if i in closed or not (status[i] == "Blocked" or open_blockers(i)):
            continue
        blocked.append({"id": i, "subject": w.get("subject"), "status": status[i], "type": _title(w, "type"),
                        "lane": _lane(op, w), "chain": chain(i)})

    # WIP per lane
    wip = {}
    for w in tasks:
        if status[w["id"]] in ACTIVE:
            row = wip.setdefault(_lane(op, w) or "none", {})
            row[status[w["id"]]] = row.get(status[w["id"]], 0) + 1

    # history of recently closed tasks → cycle time, throughput and the done line of the burnup.
    # Imported work (PM-01 P5) was walked to its status in one sitting by nomad-manager, so its
    # history says nothing about how long the work took or who did it.
    legacy = _legacy_key(op)
    recent = sorted((w for w in tasks if w["id"] in closed and not (legacy and w.get(legacy))),
                    key=lambda w: w.get("updatedAt") or "", reverse=True)
    life = {}
    for w in recent[:CLOSED_CAP]:
        life[w["id"]] = lifecycle(op, w)

    cycle = {}
    week0 = (now - dt.timedelta(days=now.weekday())).date()
    weeks = [week0 - dt.timedelta(weeks=k) for k in range(WEEKS - 1, -1, -1)]
    through = {str(d): {"agent": 0, "human": 0} for d in weeks}
    for w in recent[:CLOSED_CAP]:
        started, closed_at, closer = life[w["id"]]
        if status[w["id"]] != "Done" or not closed_at:
            continue
        if started and closed_at >= now - dt.timedelta(days=30):
            cycle.setdefault(_lane(op, w) or "none", []).append(_hours(started, closed_at))
        wk = str(closed_at.date() - dt.timedelta(days=closed_at.weekday()))
        if wk in through:
            name = _name(op, closer) or ""
            through[wk]["agent" if name.endswith("(agent)") else "human"] += 1
    cycle_out = {lane: {"n": len(v), "median_h": _median(v), "mean_h": round(sum(v) / len(v), 1)}
                 for lane, v in sorted(cycle.items())}

    # burnup per Feature: scope is tasks created by that day, done is tasks closed by that day
    def feature_of(w):
        seen = set()
        p = parent_id(w)
        while p and p in by_id and p not in seen:
            seen.add(p)
            if _title(by_id[p], "type") == "Feature":
                return p
            p = parent_id(by_id[p])
        return None

    per = {}
    for w in tasks:
        f = feature_of(w)
        if f:
            per.setdefault(f, []).append(w)
    today = now.date()
    burnup = []
    for f, ts in sorted(per.items(), key=lambda kv: max(x.get("updatedAt") or "" for x in kv[1]), reverse=True)[:12]:
        created = [_ts(w["createdAt"]).date() for w in ts]
        done = []
        for w in ts:
            if w["id"] in closed:
                c = (life.get(w["id"]) or (None, None, None))[1] or _ts(w.get("updatedAt"))
                done.append(c.date())
        start = max(min(created), today - dt.timedelta(days=DAYS - 1))
        days = [start + dt.timedelta(days=k) for k in range((today - start).days + 1)]
        burnup.append({"id": f, "subject": by_id[f].get("subject"), "status": status[f],
                       "dates": [str(d) for d in days],
                       "scope": [sum(1 for c in created if c <= d) for d in days],
                       "done": [sum(1 for c in done if c <= d) for d in days]})

    return {
        "project": op.project_ident, "generated_at": now.isoformat(timespec="seconds"),
        "url": f"{PUBLIC_URL}/projects/{op.project_ident}",
        "open": sum(sum(r.values()) for r in by_status.values()),
        "by_status": [{"status": s, "types": by_status[s], "n": sum(by_status[s].values())} for s in statuses],
        "blocked": blocked, "wip": wip, "cycle": cycle_out,
        "throughput": [{"week": k, **v} for k, v in through.items()],
        "burnup": burnup,
    }


def _legacy_key(op):
    try:
        return op._fields().get("Legacy ref")
    except Exception:
        return None


def _lane(op, w):
    try:
        return op.lane_of(w)
    except Exception:
        return None


def _name(op, href):
    try:
        return op.user_name(href)
    except Exception:
        return None


# ── every project on one screen (#786, cockpit PRD R9-R10) ──
# Open tasks per project, by status and by priority, for the cockpit's PROJECTS panel. Five reads
# cover every board: one trimmed list of the open work (id, project, status, type), one count per
# project for each of Immediate, High and Low, and the blocks relations for "next" (#793).
# OpenProject can't select a priority, and a full read of ~400 work packages takes seconds, while
# these take ~0.05 s each. Normal is what is left.
COUNTED_TYPES = ("Task", "Bug", "User story", "Sub-task")
PRIORITIES = ("Immediate", "High", "Normal", "Low")
CHIP_STATUSES = ["Ready", "In Progress", "In Review", "Blocked", "Awaiting approval", "Backlog", "New"]
SKIP_PROJECTS = {"nomad-portfolio"}    # Initiatives only, no tasks

_projects = {"at": 0.0, "data": None, "error": None}


def _q(params):
    return "?" + urllib.parse.urlencode({k: json.dumps(v) if isinstance(v, list) else v
                                         for k, v in params.items()})


def build_projects(op):
    """[{id, identifier, name, url, open, by_status, by_priority, pickable, pickable_blocked}] for
    every active project. pickable counts what the per-project "next" (#793) could take: a Task or
    Bug in Ready or Backlog with no open blocker, as pm_openproject.pick_next has it.
    pickable_blocked counts the ones an open blocker holds back, so the cockpit can say why."""
    ps = op.call("GET", "/projects" + _q({"pageSize": 500,
                                          "filters": [{"active": {"operator": "=", "values": ["t"]}}]}))
    projects = {p["id"]: p for p in ps["_embedded"]["elements"] if p["identifier"] not in SKIP_PROJECTS}
    types = [str(op.type_id(t)) for t in COUNTED_TYPES if t in op._named("types")]
    base = [{"status": {"operator": "o", "values": []}}, {"type": {"operator": "=", "values": types}}]

    # Every open WP, containers too: an open Epic can block a Task. One more read gives every
    # blocks relation, so "next" is judged on all boards without a request per task.
    by_status = {pid: {} for pid in projects}
    cands = {pid: [] for pid in projects}
    open_ids = set()
    for w in op._all(base[:1], cap=20000, select="id,project,status,type"):
        open_ids.add(w["id"])
        pid, ty = _href_id(w["_links"]["project"]), w["_links"]["type"]["title"]
        if pid in by_status and ty in COUNTED_TYPES:
            st = w["_links"]["status"]["title"]
            by_status[pid][st] = by_status[pid].get(st, 0) + 1
            if ty in NEXT_TYPES and st in NEXT_STATUSES:
                cands[pid].append(w["id"])
    held = {blocked for blocker, blocked in op.block_relations() if blocker in open_ids}

    prio_ids = {p["name"]: p["id"] for p in op.call("GET", "/priorities")["_embedded"]["elements"]}
    by_prio = {pid: dict.fromkeys(PRIORITIES, 0) for pid in projects}
    for name in ("Immediate", "High", "Low"):
        if name not in prio_ids:
            continue
        d = op.call("GET", "/work_packages" + _q({"pageSize": 0, "groupBy": "project", "filters": base + [
            {"priority": {"operator": "=", "values": [str(prio_ids[name])]}}]}))
        for g in d.get("groups", []):
            links = g.get("_links", {}).get("valueLink") or [{}]
            pid = _href_id(links[0]) if links[0].get("href") else None
            if pid in by_prio:
                by_prio[pid][name] = g.get("count", 0)

    out = []
    for pid, p in projects.items():
        n = sum(by_status[pid].values())
        pr = by_prio[pid]
        pr["Normal"] = max(0, n - pr["Immediate"] - pr["High"] - pr["Low"])
        out.append({"id": pid, "identifier": p["identifier"], "name": p["name"],
                    "url": f"{PUBLIC_URL}/projects/{p['identifier']}/work_packages",
                    "open": n, "by_status": by_status[pid], "by_priority": pr,
                    "pickable": sum(1 for i in cands[pid] if i not in held),
                    "pickable_blocked": sum(1 for i in cands[pid] if i in held)})
    return sorted(out, key=lambda r: r["name"].lower())


def projects(op, fresh=False, now=None):
    """build_projects behind a 60 s cache. When OpenProject is down, the last counts come back
    with stale=True and their age rather than an empty panel. PMError only with nothing cached."""
    now = time.time() if now is None else now
    with _lock:
        hit = dict(_projects)
    if hit["data"] is not None and not fresh and now - hit["at"] < TTL:
        return {"projects": hit["data"], "age": int(now - hit["at"]), "stale": False}
    try:
        data = build_projects(op)
    except PMError as e:
        if hit["data"] is None:
            raise
        return {"projects": hit["data"], "age": int(now - hit["at"]), "stale": True, "error": str(e)[:200]}
    with _lock:
        _projects.update(at=now, data=data)
    return {"projects": data, "age": 0, "stale": False}


def invalidate_projects():
    """A work package changed (/pm-hook): the next read recounts. The old counts stay as the
    fallback for an outage."""
    with _lock:
        _projects["at"] = 0.0
