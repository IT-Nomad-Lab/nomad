"""The autopilot's budget switch (PRD 5.9, R9, AC6; nomad-pm #880).

Two modes, set at runtime and kept in the state file's config:
  cap        new workers start only while today's spend is below `daily_cap_usd` (default 500)
  unlimited  no check at all

Spend is the sum of `total_cost_usd` from each finished run's stream-json `result` event. Claude
runs on the subscription, so the figure is notional: it tracks usage, it is not a bill.

What the cap does, and what it does not:
- It stops new workers from starting. Running workers are never killed; they finish, and what
  they cost is added when they are collected, so a day can end a little over the cap.
- It resets at local midnight (the state store rolls the spend record over when the date changes).
- A mode or cap change takes effect on the next scheduling pass, because the supervisor asks
  `check()` at the start of each pass.
- Each pause raises one alert per project: `mark_paused(pid)` is True only the first time a
  project is seen paused in a pause episode. The episode ends when starts are allowed again
  (midnight, a raised cap, or `unlimited`), so a later pause alerts again.

Spend is recorded twice over without double counting: `record()` adds a run as the supervisor
collects it, and `reconcile()` scans the run logs for runs finished today that were never
recorded (a supervisor that died between a run's end and its collection). Both key a run by its
log path, which is kept in today's spend record.

Stdlib only; all state goes through `state.StateStore`, so it survives a restart.
"""
import json
import os
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta

try:
    from . import state as st
except ImportError:  # imported as a top-level module (tests, scripts)
    import state as st

MODES = st.BUDGET_MODES


@dataclass
class Decision:
    """The answer to "may this pass start new workers?"."""
    allowed: bool
    mode: str
    cap_usd: float
    spent_usd: float
    remaining_usd: float | None     # None in unlimited mode
    detail: str                     # the `budget_paused` detail when not allowed, else ""

    def as_dict(self):
        return asdict(self)


def run_cost(path):
    """total_cost_usd of the last `result` event in a stream-json log, or None if the run has
    not written one (still running, or it crashed before the end)."""
    cost = None
    try:
        f = open(path, errors="replace")
    except OSError:
        return None
    with f:
        for line in f:
            line = line.strip()
            if not line.startswith("{") or '"result"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("type") == "result":
                try:
                    cost = float(ev.get("total_cost_usd") or 0)
                except (TypeError, ValueError):
                    cost = 0.0
    return cost


def _key(path):
    return os.path.realpath(path)


def _add(rec, project, usd, key=None):
    """Add one run to today's spend record. Returns what was added (0 if already counted)."""
    counted = rec.setdefault("counted", [])
    if key is not None:
        if key in counted:
            return 0.0
        counted.append(key)
    usd = max(0.0, float(usd or 0))
    rec["usd"] = float(rec.get("usd") or 0) + usd
    by = rec.setdefault("by_project", {})
    if project:
        by[project] = round(float(by.get(project) or 0) + usd, 6)
    return usd


class Budget:
    def __init__(self, store, runs_dir=None):
        self.store = store
        self.runs_dir = runs_dir or os.environ.get("NOMAD_AUTOPILOT_RUNS") or \
            os.path.join(os.path.dirname(store.path), "runs")

    # ---- the switch ----------------------------------------------------------------------------

    def mode(self):
        return self.store.config()["budget_mode"]

    def cap(self):
        return float(self.store.config()["daily_cap_usd"])

    def set_mode(self, mode):
        """"cap" or "unlimited". Persisted; the next pass sees it. Returns the new config."""
        if mode not in MODES:
            raise ValueError(f"budget mode must be one of {MODES}")
        return self.store.update_config({"budget_mode": mode})

    def set_cap(self, usd):
        return self.store.update_config({"daily_cap_usd": usd})

    # ---- spend ---------------------------------------------------------------------------------

    def record(self, project, finished):
        """Add one collected run (a dict from Scheduler.collect / WorkerPool.collect). Uses its
        `log` path to avoid counting a run twice, and falls back to parsing the log when the dict
        carries no cost. Returns the amount added."""
        log = finished.get("log")
        usd = finished.get("cost_usd")
        if usd is None and log:
            usd = run_cost(log)
        key = _key(log) if log else None
        return self.store.update_spend(lambda rec: _add(rec, project, usd, key))

    def record_log(self, project, path):
        """Add one run straight from its log file. 0 if it has no result event yet."""
        usd = run_cost(path)
        if usd is None:
            return 0.0
        return self.store.update_spend(lambda rec: _add(rec, project, usd, _key(path)))

    def reconcile(self):
        """Count every run log under runs/<project>/ that finished today (by mtime, local time)
        and is not counted yet. Returns the amount added."""
        today = self.store.spend_record()["date"]
        found = []
        try:
            projects = sorted(os.listdir(self.runs_dir))
        except OSError:
            return 0.0
        for pid in projects:
            d = os.path.join(self.runs_dir, pid)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(d, name)
                try:
                    mday = date.fromtimestamp(os.path.getmtime(path)).isoformat()
                except OSError:
                    continue
                if mday != today:
                    continue
                usd = run_cost(path)
                if usd is not None:
                    found.append((pid, _key(path), usd))
        if not found:
            return 0.0

        def add_all(rec):
            if rec["date"] != today:      # midnight passed during the scan: nothing is today's
                return 0.0
            return sum(_add(rec, pid, usd, key) for pid, key, usd in found)
        return self.store.update_spend(add_all)

    def spent_today(self):
        return float(self.store.spend_record().get("usd") or 0)

    # ---- the check -----------------------------------------------------------------------------

    def check(self):
        """May new workers start now? Call once at the start of each pass, before any start.
        Never says anything about running workers: they always finish."""
        cfg = self.store.config()
        mode, cap = cfg["budget_mode"], float(cfg["daily_cap_usd"])
        rec = self.store.spend_record()
        spent = float(rec.get("usd") or 0)
        if mode == "unlimited":
            d = Decision(True, mode, cap, spent, None, "")
        elif spent < cap:
            d = Decision(True, mode, cap, spent, round(cap - spent, 6), "")
        else:
            d = Decision(False, mode, cap, spent, 0.0,
                         f"daily cap ${cap:.2f} reached (${spent:.2f} spent today); no new workers "
                         "until midnight or a budget change, running workers finish")
        if d.allowed and rec.get("paused"):
            self.store.update_spend(lambda r: r.pop("paused", None))   # the pause episode ends
        return d

    def mark_paused(self, project):
        """The supervisor calls this when it sets `project` to budget_paused. True the first time
        in this pause episode, which is when it should raise the alert."""
        def mark(rec):
            paused = rec.setdefault("paused", [])
            if project in paused:
                return False
            paused.append(project)
            return True
        return self.store.update_spend(mark)

    def status(self):
        """For /status and the cockpit's global strip."""
        d = self.check()
        rec = self.store.spend_record()
        midnight = (datetime.strptime(rec["date"], "%Y-%m-%d") + timedelta(days=1)).timestamp()
        return {"mode": d.mode, "cap_usd": d.cap_usd, "spent_usd": round(d.spent_usd, 6),
                "remaining_usd": d.remaining_usd, "allowed": d.allowed, "detail": d.detail,
                "date": rec["date"], "by_project": dict(rec.get("by_project") or {}),
                "paused_projects": list(rec.get("paused") or []), "resets_at": midnight}
