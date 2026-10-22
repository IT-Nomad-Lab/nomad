"""The autopilot's memory: which projects are on, what each is doing, the config, recent events.

One JSON file, `~/.local/state/nomad-autopilot/state.json` (NOMAD_AUTOPILOT_STATE overrides it),
so a restart picks up where the supervisor left off (PRD R7). Every write goes to a temp file in
the same directory, is fsynced, then renamed over the old one, so a crash mid-write leaves either
the old file or the new one, never half of each.

A file that will not parse is moved aside as `state.json.corrupt-<epoch>` and the store starts
empty. Losing the on/off switches is recoverable by pressing Run again; refusing to start would
turn one bad write into an outage nobody sees.

Stdlib only. Thread-safe: the HTTP handlers and the scheduling loop share one store.
"""
import copy
import json
import logging
import os
import tempfile
import threading
import time
from datetime import date

log = logging.getLogger("autopilot.state")

DEFAULT_PATH = os.path.expanduser(
    os.environ.get("NOMAD_AUTOPILOT_STATE", "~/.local/state/nomad-autopilot/state.json"))

# PRD 5.7. `off` is the only state a disabled project has; the rest belong to enabled ones.
STATES = ("off", "running", "waiting", "needs_you", "idle", "budget_paused", "error")
STOP_MODES = ("finish", "kill")
RUN_MODES = ("continuous", "once")      # #890: what Run does, fixed when Run is pressed
BUDGET_MODES = ("cap", "unlimited")

DEFAULT_CONFIG = {
    "budget_mode": "cap",       # "cap" | "unlimited" (PRD 5.9)
    "daily_cap_usd": 500.0,     # notional: summed from each run's total_cost_usd
    "per_project_cap": 3,       # workers per project (PRD 9.1)
    "global_cap": 6,            # workers across all projects
}
MAX_EVENTS = int(os.environ.get("NOMAD_AUTOPILOT_MAX_EVENTS", "50"))
SCHEMA = 1


def _now():
    return time.time()


def new_project():
    return {
        # #890. `autopilot` is the operator's switch, `enabled` means running. Switch ON: Run keeps
        # the project going (run_mode "continuous"). Switch OFF: Run takes one task (run_mode
        # "once"), then the project turns itself off. The switch is read when Run is pressed; a run
        # already going keeps its mode until it ends or Stop is pressed.
        "autopilot": False,
        "run_mode": "continuous",
        "once": None,           # run_mode "once": {"task": id, "phase": "working"|"merging"}
        "enabled": False,
        "state": "off",
        "detail": "",
        "last_pass": None,      # epoch of the last pass that finished for this project
        "workers": [],          # live workers, filled by #866: {task, branch, started, ...}
        "fault": "",            # a worker ended `error`: no new workers until Run is pressed again
        "events": [],           # newest last, capped at MAX_EVENTS
        "updated": None,
    }


def validate_config(patch):
    """Check a partial config update. Returns the cleaned patch; raises ValueError on a bad field.
    Unknown keys are refused rather than stored, so a typo does not look like it took effect."""
    clean = {}
    for k, v in patch.items():
        if k not in DEFAULT_CONFIG:
            raise ValueError(f"unknown config key {k!r}")
        if k == "budget_mode":
            if v not in BUDGET_MODES:
                raise ValueError(f"budget_mode must be one of {BUDGET_MODES}")
        elif k == "daily_cap_usd":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
                raise ValueError("daily_cap_usd must be a number >= 0")
            v = float(v)
        else:  # the two caps
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"{k} must be an integer >= 0")
        clean[k] = v
    return clean


class StateStore:
    def __init__(self, path=DEFAULT_PATH, max_events=MAX_EVENTS):
        self.path = path
        self.max_events = max_events
        self._lock = threading.RLock()
        self._data = self._load()

    # ---- persistence -------------------------------------------------------------------------

    def _empty(self):
        return {"schema": SCHEMA, "config": dict(DEFAULT_CONFIG), "projects": {},
                "spend": {"date": date.today().isoformat(), "usd": 0.0},
                # #960: the Max usage limit was hit. {"reset_at", "until", "since", "project",
                # "task"}; no worker starts on any project before `until`.
                "usage_pause": None}

    def _load(self):
        if not os.path.exists(self.path):
            return self._empty()
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("top level is not an object")
        except Exception as e:
            aside = f"{self.path}.corrupt-{int(_now())}"
            log.error("state file %s unreadable (%s: %s); moved to %s, starting empty",
                      self.path, type(e).__name__, e, aside)
            try:
                os.replace(self.path, aside)
            except OSError:
                pass
            return self._empty()
        base = self._empty()
        base["config"].update({k: v for k, v in data.get("config", {}).items()
                               if k in DEFAULT_CONFIG})
        for pid, p in (data.get("projects") or {}).items():
            rec = new_project()
            rec.update({k: v for k, v in p.items() if k in rec})
            # A state file from before #890 has no switch. A project that was running keeps
            # running as it did (ON, continuous); every other project starts OFF.
            if not isinstance(p.get("autopilot"), bool):
                rec["autopilot"] = bool(rec["enabled"])
            if rec["run_mode"] not in RUN_MODES:
                rec["run_mode"] = "continuous"
            if rec["state"] not in STATES:
                rec["state"] = "error"
                rec["detail"] = f"unknown stored state {p.get('state')!r}"
            base["projects"][pid] = rec
        if isinstance(data.get("spend"), dict):
            base["spend"].update(data["spend"])
        if isinstance(data.get("usage_pause"), dict):
            base["usage_pause"] = data["usage_pause"]
        return base

    def save(self):
        """Atomic write: temp file in the same dir, fsync, rename. Held under the lock end to end,
        so two writers cannot land their renames out of order and leave the older state on disk."""
        with self._lock:
            self._write(json.dumps(self._data, indent=2, sort_keys=True))

    def _write(self, blob):
        d = os.path.dirname(self.path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".state.", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(blob)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---- reads -------------------------------------------------------------------------------

    def snapshot(self):
        """A deep copy of everything, safe to serialise or hand to another thread."""
        with self._lock:
            return copy.deepcopy(self._data)

    def config(self):
        with self._lock:
            return dict(self._data["config"])

    def project(self, pid):
        with self._lock:
            p = self._data["projects"].get(pid)
            return copy.deepcopy(p) if p else new_project()

    def enabled_projects(self):
        with self._lock:
            return sorted(pid for pid, p in self._data["projects"].items() if p["enabled"])

    def spent_today(self):
        with self._lock:
            self._roll_spend()
            return self._data["spend"]["usd"]

    # ---- writes (each persists) --------------------------------------------------------------

    def _rec(self, pid):
        return self._data["projects"].setdefault(pid, new_project())

    def _event(self, rec, kind, msg):
        rec["events"].append({"t": _now(), "kind": kind, "msg": msg})
        del rec["events"][:-self.max_events]

    def event(self, pid, kind, msg):
        with self._lock:
            self._event(self._rec(pid), kind, msg)
        self.save()

    def set_state(self, pid, state, detail="", event=True):
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        with self._lock:
            rec = self._rec(pid)
            changed = (rec["state"], rec["detail"]) != (state, detail)
            rec["state"], rec["detail"], rec["updated"] = state, detail, _now()
            if changed and event:
                self._event(rec, "state", f"{state}" + (f": {detail}" if detail else ""))
        self.save()

    def set_autopilot(self, pid, on):
        """The operator's ON/OFF switch (#890). It changes what the next Run does, not a run
        already going."""
        if not isinstance(on, bool):
            raise ValueError("on must be true or false")
        with self._lock:
            rec = self._rec(pid)
            if rec["autopilot"] != on:
                rec["autopilot"] = on
                rec["updated"] = _now()
                self._event(rec, "switch", "autopilot switch " + ("ON: Run keeps going" if on
                                                                  else "OFF: Run does one task"))
        self.save()

    def set_once(self, pid, once):
        """Record the one task a run_mode "once" run holds, and its phase."""
        with self._lock:
            self._rec(pid)["once"] = copy.deepcopy(once)
        self.save()

    def finish_once(self, pid, detail):
        """The one task of a run_mode "once" run is final: the project turns itself off."""
        with self._lock:
            rec = self._rec(pid)
            rec["once"] = None
            if rec["enabled"]:
                rec["enabled"] = False
                rec["state"], rec["detail"] = "off", detail
                self._event(rec, "once", detail)
            else:                # Stop won the race: keep it off, say how the task ended
                rec["detail"] = detail
            rec["last_pass"] = rec["updated"] = _now()
        self.save()

    def start(self, pid):
        """Turn a project's autopilot on. The first pass decides its real state. The switch
        (#890) fixes the run mode now; pressing Run on a project already on changes nothing."""
        with self._lock:
            rec = self._rec(pid)
            was = rec["enabled"]
            if not was:
                rec["run_mode"] = "continuous" if rec["autopilot"] else "once"
                rec["once"] = None
            rec["enabled"] = True
            if rec["fault"]:
                self._event(rec, "start", f"fault cleared: {rec['fault']}")
                rec["fault"] = ""
                if was:
                    rec["state"], rec["detail"], rec["updated"] = "idle", "fault cleared", _now()
            if not was:
                once = rec["run_mode"] == "once"
                rec["state"], rec["detail"], rec["updated"] = "idle", (
                    "one task: waiting for the first pass" if once else "waiting for first pass"), _now()
                self._event(rec, "start", "run once: the top task, then off" if once else "autopilot on")
        self.save()
        return not was

    def stop(self, pid, mode):
        """Turn it off. `mode` is what happens to running workers; the scheduler acts on it."""
        if mode not in STOP_MODES:
            raise ValueError(f"mode must be one of {STOP_MODES}")
        with self._lock:
            rec = self._rec(pid)
            was = rec["enabled"]
            rec["enabled"] = False
            rec["once"] = None
            n = len(rec["workers"])
            detail = f"stopped ({mode})" + (f", {n} worker(s) {'finishing' if mode == 'finish' else 'killed'}"
                                            if n else "")
            rec["state"], rec["detail"], rec["updated"] = "off", detail, _now()
            self._event(rec, "stop", detail)
        self.save()
        return was

    def finish_pass(self, pid, state, detail, workers=None):
        """Record the end of one project's pass: its state and, if given, its worker list."""
        if state not in STATES:
            raise ValueError(f"state must be one of {STATES}")
        with self._lock:
            rec = self._rec(pid)
            if workers is not None:
                rec["workers"] = workers
            rec["last_pass"] = _now()
            # Stopped while the pass ran: Stop wins, the pass result does not flip it back on.
            # A stopped project's own passes (collecting its last workers) say "off" and may
            # only change its detail; "off" never lands on a project that is on.
            if rec["enabled"]:
                if state != "off":
                    if (rec["state"], rec["detail"]) != (state, detail):
                        self._event(rec, "state", state + (f": {detail}" if detail else ""))
                    rec["state"], rec["detail"] = state, detail
            elif state == "off":
                rec["detail"] = detail
            rec["updated"] = _now()
        self.save()

    def set_workers(self, pid, workers):
        """Replace the stored worker list without touching state (crash recovery)."""
        with self._lock:
            rec = self._rec(pid)
            rec["workers"] = workers
            rec["updated"] = _now()
        self.save()

    def set_fault(self, pid, detail):
        """A worker ended `error` (its first shell command was refused: a broken permission
        profile, not a bad task). The project starts nothing new until Run is pressed again."""
        with self._lock:
            rec = self._rec(pid)
            rec["fault"] = detail
            self._event(rec, "fault", detail)
        self.save()

    def update_config(self, patch):
        clean = validate_config(patch)
        with self._lock:
            self._data["config"].update(clean)
            cfg = dict(self._data["config"])
        self.save()
        return cfg

    def _roll_spend(self):
        today = date.today().isoformat()
        if self._data["spend"].get("date") != today:
            self._data["spend"] = {"date": today, "usd": 0.0}

    def update_spend(self, fn):
        """Run fn(spend) on today's spend record under the lock, then persist. The record is
        {"date", "usd", ...}; extra keys a caller adds (autopilot/budget.py keeps per-project
        totals and the runs already counted) survive a restart and are cleared at midnight with
        the rest. Returns what fn returned."""
        with self._lock:
            self._roll_spend()
            out = fn(self._data["spend"])
        self.save()
        return out

    def spend_record(self):
        """A copy of today's spend record, rolled over first if the day changed."""
        with self._lock:
            self._roll_spend()
            return copy.deepcopy(self._data["spend"])

    def usage_pause(self):
        """A copy of the usage-limit pause (#960), or None."""
        with self._lock:
            p = self._data.get("usage_pause")
            return dict(p) if p else None

    def set_usage_pause(self, pause):
        """Record the usage-limit pause, or None to forget it. Persisted, so a restart keeps it."""
        with self._lock:
            self._data["usage_pause"] = dict(pause) if pause else None
        self.save()

    def add_spend(self, usd):
        with self._lock:
            self._roll_spend()
            self._data["spend"]["usd"] += float(usd or 0)
        self.save()


# ---- cost ledger (#1093) ----------------------------------------------------------------------

LEDGER_FILE = "cost-ledger.jsonl"


def append_ledger(state_dir, line):
    """Append one JSON line for a collected run. Never raises: a ledger that cannot be written
    must not stop a run being collected."""
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(os.path.join(state_dir, LEDGER_FILE), "a") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
    except OSError as e:
        log.warning("cost ledger not written: %s", e)


def task_totals(state_dir):
    """{project: {task: {"cost_usd", "turns", "runs"}}} from the ledger.

    A result reports its whole conversation, so after `--resume` two lines of one session_id
    carry the later total, not an increment: the last line per session counts, and the task
    total sums over sessions. A line with no session_id counts on its own."""
    last = {}
    try:
        with open(os.path.join(state_dir, LEDGER_FILE)) as f:
            for n, raw in enumerate(f):
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(e, dict) or e.get("task") is None or e.get("event"):
                    continue
                last[(str(e.get("project")), str(e["task"]), e.get("session_id") or f"#{n}")] = e
    except OSError:
        return {}
    out = {}
    for (pid, tid, _), e in last.items():
        t = out.setdefault(pid, {}).setdefault(tid, {"cost_usd": 0.0, "turns": 0, "runs": 0})
        t["cost_usd"] = round(t["cost_usd"] + float(e.get("total_cost_usd") or 0), 6)
        t["turns"] += int(e.get("num_turns") or 0)
        t["runs"] += 1
    return out


ESCALATE_AFTER = 3          # #1109: failed runs in a row before a task goes to the operator


def failed_streak(state_dir, project, task):
    """Failed runs of one task in a row, newest back, from the ledger. A run with any other
    outcome, or an `escalated` event, ends the streak; a `resume` (timeout, usage limit) is
    neither a failure nor a success, so it is skipped."""
    lines = []
    try:
        with open(os.path.join(state_dir, LEDGER_FILE)) as f:
            for raw in f:
                try:
                    e = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(e, dict) and str(e.get("project")) == str(project)
                        and str(e.get("task")) == str(task)):
                    lines.append(e)
    except OSError:
        return 0
    n = 0
    for e in reversed(lines):
        if e.get("event") == "escalated":
            break
        if e.get("event") or e.get("outcome") == "resume":
            continue
        if e.get("outcome") != "failed":
            break
        n += 1
    return n


def note_escalated(state_dir, project, task):
    """Mark that the task went to the operator, so its failed streak starts again."""
    append_ledger(state_dir, {"at": time.time(), "project": project, "task": task,
                              "event": "escalated"})
