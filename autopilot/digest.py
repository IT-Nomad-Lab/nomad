"""The autopilot's daily digest (PRD 5.7, R8, 9.3; nomad-pm #879).

One message per project per day, at 08:00 local time (NOMAD_AUTOPILOT_DIGEST_AT overrides), and
only when the project has something done or waiting. It goes out through the Alerter, so it
reaches the cockpit, Zulip `#autopilot` (topic = project) and email like any alert.

What a project's digest says:
- merged since the last digest: every `merge` event of kind `merged`, with its commit;
- running: the project's live workers, and what sits in the merge queue;
- needs you: the project state when it is needs_you / budget_paused / error, and every task the
  selection sets aside for the operator (Awaiting approval, Blocked with no open blocker, and the
  scheduler's bench: a run that ended failed / needs_operator / error);
- blocked: every task waiting on an open blocker, naming the blocker;
- person-only: the `manager` lane plus anything tagged `human` (PRD 9.2);
- yesterday's spend: the project's share and the total, from the run logs finished yesterday
  (the same day rule as budget.reconcile; the live spend record is reset at midnight).

Board facts come from the scheduler's last Selection per project (`scheduler.selections`), so the
digest reads nomad-pm no more than the pass already does.

Never twice. The key is `digest:<pid>:<date>`, and Alerter.notify sends a key once per channel.
On top of that a marker file (`digest.json` next to the state file, NOMAD_AUTOPILOT_DIGEST
overrides) records per project the date the digest last went out, so a restart does not rebuild
it. A channel that failed leaves the date unmarked, so the next pass retries it; the ledger keeps
it off the channels that already have it. A project with nothing to say is marked for the day too.

Merges are read from the state store's event log, which keeps only the last MAX_EVENTS per
project. `tick()` runs every pass, so it copies new merge events into the marker as they appear
(a cursor per project) and the digest never depends on the log still holding them at 08:00.

Wiring (supervisor.main):

    dg = digest.Digest(store, alerter, scheduler=sch, budget=bud)
    Supervisor(..., digest=dg)        # run_all calls dg.tick() once per pass

`tick()` never raises; the supervisor guards it anyway.
"""
import contextlib
import json
import logging
import os
import re
import tempfile
import threading
from datetime import datetime, time as dtime, timedelta

try:
    from . import budget as bg
except ImportError:  # imported as a top-level module (tests, scripts)
    import budget as bg

log = logging.getLogger("autopilot.digest")

DEFAULT_AT = os.environ.get("NOMAD_AUTOPILOT_DIGEST_AT", "08:00")
ALERT_STATES = {"needs_you": "needs you", "budget_paused": "budget paused", "error": "error"}
_MERGED = re.compile(r"^task (\S+): merged(?: ([0-9a-f]{7,40}))? ->")
MAX_SUBJECT = 80
MAX_KEPT = 500          # merged entries kept per project if digests stop going out


def parse_at(text):
    h, _, m = (text or "08:00").strip().partition(":")
    return dtime(int(h), int(m or 0))


def _subject(s):
    s = " ".join((s or "").split())
    return s if len(s) <= MAX_SUBJECT else s[:MAX_SUBJECT - 1] + "…"


def spend_on(runs_dir, day):
    """{project: usd} of every run log under runs_dir/<project>/ whose mtime falls on `day`
    (a date, local time) and that has a result event."""
    out = {}
    try:
        pids = sorted(os.listdir(runs_dir))
    except OSError:
        return out
    for pid in pids:
        d = os.path.join(runs_dir, pid)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.endswith(".jsonl"):
                continue
            path = os.path.join(d, name)
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)).date() != day:
                    continue
            except OSError:
                continue
            usd = bg.run_cost(path)
            if usd is not None:
                out[pid] = round(out.get(pid, 0.0) + usd, 6)
    return out


class Marker:
    """digest.json: per project {sent_date, sent_t, cursor, merged: [{task, commit, t}]}."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.RLock()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            log.error("digest marker %s unreadable (%s); starting empty", self.path, e)
            return {}

    def save(self, data):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".digest.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


class Digest:
    """store: state.StateStore; alerter: alerts.Alerter (anything with notify); scheduler: has
    `selections` {pid: tasks.Selection} (optional); budget: budget.Budget, for its runs_dir
    (optional); now: callable returning a local datetime (tests)."""

    def __init__(self, store, alerter, scheduler=None, budget=None, at=DEFAULT_AT,
                 marker_path=None, runs_dir=None, now=datetime.now):
        self.store = store
        self.alerter = alerter
        self.scheduler = scheduler
        self.at = parse_at(at) if isinstance(at, str) else at
        base = os.path.dirname(store.path)
        self.marker = Marker(marker_path or os.environ.get("NOMAD_AUTOPILOT_DIGEST")
                             or os.path.join(base, "digest.json"))
        self.runs_dir = runs_dir or getattr(budget, "runs_dir", None) or os.path.join(base, "runs")
        self.now = now

    # -- per pass --

    def tick(self):
        """Collect new merges; once past the hour, send each project's digest not yet sent
        today. Returns {pid: notify result, or "empty"}. Never raises."""
        try:
            with self.marker._lock:
                return self._tick()
        except Exception:
            log.exception("digest tick failed")
            return {}

    def _tick(self):
        now = self.now()
        today = now.date().isoformat()
        data = self.marker.load()
        projects = data.setdefault("projects", {})
        snap = self.store.snapshot()["projects"]
        self._collect(projects, snap)
        out = {}
        if now.time() >= self.at:
            spend = None
            for pid in self._candidates(projects, snap):
                m = projects.setdefault(pid, {})
                if m.get("sent_date") == today:
                    continue
                if spend is None:
                    spend = spend_on(self.runs_dir, now.date() - timedelta(days=1))
                built = self.build(pid, snap.get(pid) or {}, m, spend, now)
                if built is None:
                    m["sent_date"], out[pid] = today, "empty"
                    continue
                title, body, merged = built
                res = self.alerter.notify(pid, f"digest:{pid}:{today}", title, body) or {}
                out[pid] = res
                if "failed" in res.values():
                    continue                    # retried next pass; sent channels are not resent
                m["sent_date"], m["sent_t"] = today, now.timestamp()
                m["merged"] = [x for x in m.get("merged", []) if x not in merged]
        self.marker.save(data)
        return out

    def _collect(self, projects, snap):
        """Copy merge events newer than each project's cursor into the marker."""
        for pid, p in snap.items():
            m = projects.setdefault(pid, {})
            cursor = m.get("cursor", 0)
            for ev in p.get("events", []):
                t = ev.get("t", 0)
                if t <= cursor:
                    continue
                cursor = max(cursor, t)
                if ev.get("kind") != "merge":
                    continue
                hit = _MERGED.match(ev.get("msg") or "")
                if hit:
                    m.setdefault("merged", []).append(
                        {"task": hit.group(1), "commit": hit.group(2) or "", "t": t})
            if "merged" in m:
                del m["merged"][:-MAX_KEPT]
            m["cursor"] = cursor

    def _candidates(self, projects, snap):
        on = {pid for pid, p in snap.items() if p.get("enabled")}
        return sorted(on | {pid for pid, m in projects.items() if m.get("merged")})

    # -- the message --

    def build(self, pid, proj, mark, spend, now):
        """(title, body, merged entries included) or None when there is nothing to report."""
        sel = (getattr(self.scheduler, "selections", None) or {}).get(pid)
        kind = (lambda k: sel.of_kind(k)) if sel is not None else (lambda k: [])
        subjects = {}
        if sel is not None:
            subjects.update({t.id: t.subject for t in getattr(sel, "qualified", [])})
            subjects.update({s.task.id: s.task.subject for s in getattr(sel, "skipped", [])})

        def name(tid):
            try:
                s = subjects.get(int(tid))
            except (TypeError, ValueError):
                s = None
            return f"#{tid}" + (f" {_subject(s)}" if s else "")

        merged = list(mark.get("merged", []))
        workers = [w.get("task") for w in proj.get("workers", [])]
        queue = [s for s in kind("running") if s.reason == "in the merge queue"]
        ops, waits, person = kind("operator"), kind("waiting"), kind("person")
        state, detail = proj.get("state", "off"), proj.get("detail", "")
        mine, total = spend.get(pid, 0.0), sum(spend.values())

        if not (merged or workers or queue or ops or waits or person or mine
                or state in ALERT_STATES):
            return None

        since = mark.get("sent_t")
        head = ("since the last digest (" + datetime.fromtimestamp(since).strftime("%Y-%m-%d %H:%M")
                + ")") if since else "since the autopilot started"
        lines = [f"State: **{state}**" + (f": {detail}" if detail else "")]
        if sel is None and proj.get("enabled"):
            lines.append("_The board was not read on the last pass; task lists may be missing._")

        def section(title, items):
            lines.extend(["", f"**{title}**"] + [f"- {i}" for i in items or ["none"]])

        section(f"Merged {head}", [name(x["task"]) + (f" `{x['commit'][:10]}`" if x.get("commit") else "")
                                   for x in merged])
        section("Running", [name(t) + " (worker)" for t in workers]
                + [name(s.task.id) + " (merge queue)" for s in queue])
        need = []
        if state in ALERT_STATES:
            need.append(f"project is {ALERT_STATES[state]}" + (f": {detail}" if detail else ""))
        need += [f"{name(s.task.id)}: {s.reason}" for s in ops]
        section("Needs you", need)
        section("Blocked", [f"{name(s.task.id)}: {s.reason}" for s in waits])
        section("Person-only (manager lane or tagged human)",
                [f"{name(s.task.id)}: {s.reason.removeprefix('person-only: ')}" for s in person])
        day = (now.date() - timedelta(days=1)).isoformat()
        lines += ["", f"**Spend {day}:** ${mine:.2f} for {pid}, ${total:.2f} across all projects "
                      "(notional, from the run logs)"]

        bits = [f"{len(merged)} merged"]
        if workers or queue:
            bits.append(f"{len(workers) + len(queue)} running")
        if need:
            bits.append(f"{len(need)} need you")
        if waits:
            bits.append(f"{len(waits)} blocked")
        if person:
            bits.append(f"{len(person)} person-only")
        title = f"{pid} daily digest {now.date().isoformat()}: " + ", ".join(bits)
        return title, "\n".join(lines), merged
