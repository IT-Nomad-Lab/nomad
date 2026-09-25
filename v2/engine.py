"""NOMAD v2 · Pipeline engine (P1-3).

Drives one unit of work through the explicit state machine (ADR-003):
  Capture → Clarify → Route → Process → Human Gate (pause) → Execute → Log & Learn

NocoDB-on-Postgres is the source of truth, so the engine is stateless between pause and
resume — it reconstructs the run from the `comms` row keyed by `run_id`.

Phase-1 scope: one goal, one specialist (Comms), one tool (`comms.send_message`), one gate,
one memory write-back. The Process draft and the Execute send are stubbed here with a direct
LiteLLM call + a direct outbox insert; P1-4 swaps Execute onto the MCP tool and P1-5 swaps
Process onto the Agent-SDK Comms specialist.
"""
import datetime
import json
import os
import threading
import uuid

from nocodb import NocoDB
from mcp_comms import send_message_impl
from mcp_research import save_brief_impl
from mcp_content import save_content_impl       # 3B
from mcp_dev import dispatch_build_impl          # 3B
from mcp_image import generate_image_impl        # multi-provider: ComfyUI(local) → OpenAI → Firefly (gated)
from specialists import SPECIALISTS, _IMAGE_WORDS, _prompt   # 3B: config-driven, all lanes (+ visual-ask words)
import memory                                     # 3C: episodic recall
import llm

MANAGER_MODEL = "balanced"

# The Manager routes and decomposes; both are judgement calls the operator only sees the RESULT of.
# CRITIC_MODE makes it declare confidence and the assumptions it added, so a bad route or a plan
# built on a wrong premise is visible at the gate instead of after execution. Fail-open: an
# unreadable block returns "" and the Manager behaves exactly as before.
CRITIC = _prompt("CRITIC_MODE")
_CRITIC_BLOCK = ("\n\nHOW TO JUDGE (apply to your own answer):\n" + CRITIC) if CRITIC else ""

# How long a run may sit in `new` before the sweep fails it. Generous on purpose: Process can
# legitimately take minutes on a slow local model or a scraped research goal, and a false sweep
# would kill real work. Anything past this is not slow, it is abandoned.
STALL_MINUTES = int(os.environ.get("NOMAD_STALL_MINUTES", "45"))
SWEEP_SECS = int(os.environ.get("NOMAD_SWEEP_SECS", "600"))

# Where each tool writes a sink row (for log links). dispatch_build has no sink row.
SINK = {"send_message": "outbox", "save_brief": "knowledge", "save_content": "content",
        "generate_image": "content"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def proposal_hash(proposal: dict) -> str:
    """Fingerprint of exactly what would execute. A remote approver passes back the hash of what
    they were shown; if the action or any arg changed since, the approval doesn't apply."""
    import hashlib
    body = json.dumps({"action": proposal.get("action"), "args": proposal.get("args", {})},
                      sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


# Every resume path (cockpit /resume, the LISTEN push, the poller, the Zulip relay) runs in the one
# engine process. The status check and the `executing` lock are two separate NocoDB calls, so
# without this claim two concurrent approvals could both see awaiting-approval and both execute.
_CLAIM = threading.Lock()
_IN_FLIGHT: set = set()


class Engine:
    def __init__(self, db=None):
        self.db = db or NocoDB()
        # specialist registry (by lane) + tool registry (by action) — the engine is generic
        self.specialists = SPECIALISTS
        self.tools = {"send_message": send_message_impl, "save_brief": save_brief_impl,
                      "save_content": save_content_impl, "dispatch_build": dispatch_build_impl,
                      "generate_image": generate_image_impl}
        # Auto-advance: after a project task executes, queue the next backlog task to the gate (the
        # project flows; each action still gates). NOMAD_AUTO_ADVANCE=0 → manual ▶ next only.
        self.auto_advance = os.environ.get("NOMAD_AUTO_ADVANCE", "1").lower() not in ("0", "false", "no")

    # ── stages up to the gate ───────────────────────────────────────
    def start_run(self, goal_title: str, task_ref=None) -> dict:
        """Capture → Clarify → Route → Process → pause at the Human Gate. Returns run state.
        `task_ref` links this run to a project task (mission control) so its execution marks the
        task Done and rolls up milestone progress (Phase 1A project orchestration)."""
        run_id = uuid.uuid4().hex[:12]

        # Capture
        goal = self.db.create("goals", {"title": goal_title, "status": "new", "created_at": _now()})
        gid = goal["Id"]
        comms = self.db.create("comms", {
            "goal_id": gid, "run_id": run_id, "from_agent": "human", "lane": "inbox",
            "type": "task", "status": "new", "priority": "normal",
            "payload": json.dumps({"goal": goal_title}), "created_at": _now()})
        cid = comms["Id"]

        # Everything from here to the gate can throw: the Manager call, the specialist's Process,
        # a scraper timeout. Without this the comms row is simply left at `new` and no longer
        # belongs to anyone — the run neither reaches the gate nor reports a failure. One did
        # exactly that for two months. The sweep below is the backstop for the cases where no
        # handler gets to run at all; this is the one that can say what actually went wrong.
        try:
            return self._route_and_propose(run_id, cid, gid, goal_title, task_ref)
        except Exception as e:
            self._fail(cid, {"goal": goal_title}, e)
            self._log(run_id, "engine", "run failed before the gate", str(e)[:200],
                      "failed", f"comms#{cid}")
            raise

    def _route_and_propose(self, run_id, cid, gid, goal_title, task_ref):
        # Clarify + Route (Manager) — classify the goal to a lane/specialist
        past = memory.recall(self.db, goal_title)        # 3C: provenance-aware routing
        context = (f"\n\nRELEVANT PAST WORK (use it to clarify/route consistently):\n{past}"
                   if past else "")
        m = llm.chat_json(
            MANAGER_MODEL,
            "You are NOMAD's Manager. Clarify a goal and ROUTE it to one lane. Lanes: "
            "'comms'=draft/send a message; 'research'=answer a question or gather info; "
            "'support'=reply to a support request; 'ads'=write marketing copy; "
            "'dev'=a coding task in a repo. Fields: lane, intent (one line), target — "
            "comms/support → recipient email or 'operator'; research/ads → the topic; "
            "dev → the project/repo name. Also return: confidence (0-1, how sure you are this is "
            "the right lane), and concern (one line naming what you assumed or what would make "
            "this route wrong; null if genuinely none)." + _CRITIC_BLOCK + context,
            f"Goal: {goal_title}")
        lane = m.get("lane") if m.get("lane") in self.specialists else "comms"
        intent = (m.get("intent") or goal_title).strip()
        target = (m.get("target") or "operator").strip()
        # Routing doubt is only useful if the human at the gate can see it, so it rides the payload
        # into the gate view rather than sitting in a log nobody opens.
        try:
            confidence = float(m.get("confidence"))
        except (TypeError, ValueError):
            confidence = None
        concern = (m.get("concern") or "").strip() or None
        # Deterministic override: a visual/image ask ALWAYS goes to the ads lane (which proposes
        # generate_image). Naming a destination project ("…save it in <project>") otherwise biases
        # the Manager toward 'dev'. Also ensure the intent keeps a visual word so the ads lane's
        # choose() picks generate_image (not marketing copy).
        if any(w in goal_title.lower() for w in _IMAGE_WORDS):
            lane = "ads"
            if not any(w in intent.lower() for w in _IMAGE_WORDS):
                intent = goal_title
        specialist = self.specialists[lane]
        self.db.update("goals", gid, {"status": "routed"})
        self.db.update("comms", cid, {"lane": lane, "assigned_agent": lane, "from_agent": "manager",
                                      "payload": json.dumps({"goal": goal_title, "intent": intent,
                                                             "target": target,
                                                             "route_confidence": confidence,
                                                             "route_concern": concern})})
        if concern or (confidence is not None and confidence < 0.6):
            self._log(run_id, "manager", f"routed to {lane} with reservations",
                      concern or "low confidence, no reason given",
                      f"confidence={confidence}", f"comms#{cid}")

        # Process — the routed specialist drafts a proposal via its Skill (reversible, no side effect)
        proposal = specialist.propose(intent=intent, target=target, run_id=run_id)

        # Image asks can name a destination project IN THE PROMPT ("…for project Civis"). Thread the
        # ORIGINAL goal text through as save_hint (faithful to what the operator typed, unlike the
        # Manager's paraphrased intent) so generate_image can save the file into that project folder.
        if proposal.get("action") == "generate_image":
            proposal.setdefault("args", {})["save_hint"] = goal_title

        # Human Gate — file the action proposal and PAUSE. Keep the routing provenance
        # (goal/intent/target) alongside the proposal so the cockpit drill-down can show *why*
        # this run was routed here, not just the drafted action.
        self.db.update("comms", cid, {"type": "proposal", "status": "awaiting-approval",
                                      "payload": json.dumps({**proposal, "goal": goal_title,
                                                             "intent": intent, "target": target,
                                                             "task_ref": task_ref})})
        return {"run_id": run_id, "comms_id": cid, "goal_id": gid, "lane": lane,
                "status": "awaiting-approval", "proposal": proposal}

    # ── resume after the gate ───────────────────────────────────────
    def resume_run(self, run_id: str, decision: str, expect_hash: str = None,
                   *, db_origin: bool = False) -> dict:
        """Resume a paused run on the gate decision. Idempotent by run_id, including under
        concurrent calls (one claim per run at a time). decision ∈ approved/rejected.

        An approval REQUIRES `expect_hash`, and only executes if it still matches the stored
        proposal. This was optional, which made it a courtesy rather than a control: a caller
        that skipped the field got the same execution as one that checked. Now an approver has
        to name the exact proposal it is approving, so it cannot approve a run it never read,
        and cannot approve text that changed after it was shown.

        `db_origin` is for the paths where the operator flipped the row in NocoDB directly, so
        the decision and the proposal are the same authoritative record and a hash of it would
        only be checking the row against itself. It is keyword-only and set in-process, never
        from a request body, because a client able to name it would have the bypass.
        """
        with _CLAIM:
            if run_id in _IN_FLIGHT:
                return {"run_id": run_id, "status": "executing", "note": "already being resolved"}
            _IN_FLIGHT.add(run_id)
        try:
            return self._resume_claimed(run_id, decision, expect_hash, db_origin=db_origin)
        finally:
            with _CLAIM:
                _IN_FLIGHT.discard(run_id)

    def _resume_claimed(self, run_id: str, decision: str, expect_hash: str = None,
                        *, db_origin: bool = False) -> dict:
        row = self.db.find("comms", "run_id", run_id)
        if not row:
            return {"run_id": run_id, "error": "run not found"}
        cid = row["Id"]
        # Idempotency for BOTH push + poll: engine-set states (lock/terminal) are never re-processed.
        # These are distinct from the operator's decision states (approved/rejected), so the reject
        # decision is actually handled instead of being mistaken for "already done".
        if row.get("status") in ("executing", "executed", "declined", "failed"):
            return {"run_id": run_id, "status": row["status"], "note": "already resolved (idempotent)"}
        proposal = json.loads(row.get("payload") or "{}")
        args = proposal.get("args", {})

        if decision == "approved" and not db_origin:
            if not expect_hash:
                return {"run_id": run_id, "status": row.get("status"),
                        "error": "expect_hash is required to approve. Read the proposal from "
                                 "GET /run?id=<run_id>, which returns proposal_hash, and send "
                                 "that back. Approving without naming the proposal is not "
                                 "approving."}
            if expect_hash != proposal_hash(proposal):
                return {"run_id": run_id, "status": row.get("status"),
                        "error": "proposal changed since it was shown; review it again before approving"}

        if decision == "approved":
            # Execute — dispatch the proposed action to its MCP tool (the one side-effecting call)
            action = proposal.get("action")
            tool = self.tools.get(action)
            if not tool:
                # Distinct 'failed' terminal — NOT 'declined' (which means the operator rejected),
                # so a real execution failure is visible/auditable in the cockpit. Error is kept in
                # the payload so the drill-down can show it without re-deriving from the log.
                self._fail(cid, proposal, f"unknown action {action}")
                self._log(run_id, row.get("lane", "?"), action or "?", json.dumps(args)[:160],
                          f"FAILED: unknown action {action}", f"comms#{cid}")
                return {"run_id": run_id, "status": "failed", "error": f"unknown action {action}"}
            self.db.update("comms", cid, {"status": "executing"})   # lock before the side effect
            try:
                res = tool(**args)
            except Exception as e:                                  # tool raised (defensive)
                res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
            if not res.get("ok"):                                   # tool failed (e.g. dispatcher down)
                self._fail(cid, proposal, res.get("error", ""))
                self._log(run_id, row.get("lane", "?"), action, json.dumps(args)[:160],
                          f"FAILED: {res.get('error', '')}", f"comms#{cid}")
                return {"run_id": run_id, "status": "failed", "error": res.get("error")}
            sink = SINK.get(action)                                 # None for dispatch_build
            rid = res.get(f"{sink}_id") if sink else None
            self.db.update("comms", cid, {"status": "executed"})
            self.db.update("goals", row["goal_id"], {"status": "done"}) if row.get("goal_id") else None
            outcome = (f"{action} → {sink}#{rid}" if sink and rid
                       else f"{action} executed: {(res.get('summary') or 'ok')[:120]}")
            links = f"comms#{cid}" + (f",{sink}#{rid}" if sink and rid else "")
            self._log(run_id, row.get("lane", "?"), action, json.dumps(args)[:160], outcome, links)
            if proposal.get("task_ref"):                # Phase 1A: roll a project task → Done
                self._complete_task(proposal["task_ref"], outcome)
            return {"run_id": run_id, "status": "executed", "action": action,
                    "sink": sink, "id": rid, "outcome": outcome}

        # rejected — no action runs; mark declined (terminal) and log
        self.db.update("comms", cid, {"status": "declined"})
        if proposal.get("task_ref"):        # a rejected project task → Blocked (project pauses, visible)
            try:
                self.db.update("tasks", int(proposal["task_ref"]), {"status": "Blocked"})
            except Exception:
                pass
        self._log(run_id, row.get("lane", "comms"), proposal.get("action", "action") + " (blocked)",
                  "operator rejected at the gate", "rejected — no action taken", f"comms#{cid}")
        return {"run_id": run_id, "status": "declined"}

    def retry_run(self, run_id: str) -> dict:
        """Put a FAILED run back at the human gate (awaiting-approval) so the operator can approve
        it again — e.g. after the dispatcher/n8n recovered. Only failed runs are retryable; the
        drafted proposal is reused (error cleared). Re-approval re-runs the original action."""
        row = self.db.find("comms", "run_id", run_id)
        if not row:
            return {"run_id": run_id, "error": "run not found"}
        if row.get("status") != "failed":
            return {"run_id": run_id, "status": row.get("status"), "note": "only failed runs retry"}
        proposal = json.loads(row.get("payload") or "{}")
        proposal.pop("error", None)
        self.db.update("comms", row["Id"], {"status": "awaiting-approval", "type": "proposal",
                                            "payload": json.dumps(proposal)})
        self._log(run_id, row.get("lane", "?"), proposal.get("action", "action") + " (retry)",
                  "operator retried a failed run", "re-queued at the human gate", f"comms#{row['Id']}")
        return {"run_id": run_id, "status": "awaiting-approval", "retried": True}

    # ── Phase 1A: project → milestone → task orchestration ──────────
    def plan_project(self, title: str, description: str = "") -> dict:
        """Decompose a project goal into milestones + tasks (mission control) via the Manager LLM.
        Writes mc_goals → milestones → tasks rows (linked by *_ref). Each task names a lane so it
        can later run through the normal pipeline gate. Returns the persisted plan."""
        plan = llm.chat_json(
            MANAGER_MODEL,
            "You are NOMAD's Manager/Planner. Break a PROJECT into a concrete, ordered plan: 2-4 "
            "milestones, each with 1-4 tasks. Each task is ONE actionable unit assignable to a lane "
            "(comms=send a message; research=gather/answer; support=reply; ads=marketing copy; "
            "dev=a coding task in a repo). Return JSON only: "
            '{"milestones":[{"title":"...","tasks":[{"title":"...","lane":"research"}]}],'
            '"assumptions":["what you assumed that the operator did not say"],'
            '"risk":"the one thing most likely to make this plan wrong"}'
            + _CRITIC_BLOCK,
            f"Project: {title}\n{description}".strip())
        g = self.db.create("mc_goals", {"title": title, "description": description})
        gid = g["Id"]
        out = []
        for ms in (plan.get("milestones") or []):
            mtitle = (ms.get("title") or "Milestone").strip()
            mrow = self.db.create("milestones", {"title": mtitle, "status": "Planned",
                                                 "pct_complete": 0, "goal_ref": str(gid)})
            tasks = []
            for t in (ms.get("tasks") or []):
                lane = t.get("lane") if t.get("lane") in self.specialists else "research"
                ttitle = (t.get("title") or "Task").strip()
                trow = self.db.create("tasks", {"title": ttitle, "status": "Backlog",
                                                "assigned_agent": lane, "milestone_ref": str(mrow["Id"])})
                tasks.append({"task_id": trow["Id"], "title": ttitle, "lane": lane})
            out.append({"milestone_id": mrow["Id"], "title": mtitle, "tasks": tasks})
        # A plan's assumptions are worth more than the plan: the operator can correct one they can
        # see, and every task below inherits a premise nobody checked.
        assumptions = [a for a in (plan.get("assumptions") or []) if str(a).strip()]
        risk = (plan.get("risk") or "").strip()
        self._log(f"proj-{gid}", "manager", f"planned project: {title}",
                  "; ".join(str(a) for a in assumptions) or "no assumptions declared",
                  f"{len(out)} milestones, {sum(len(m['tasks']) for m in out)} tasks"
                  + (f" · biggest risk: {risk}" if risk else ""), f"mc_goals#{gid}")
        self._advance(gid)   # auto-start: queue the first task to the gate
        return {"goal_id": gid, "title": title, "milestones": out,
                "task_count": sum(len(m["tasks"]) for m in out),
                "assumptions": assumptions, "risk": risk or None,
                "auto_advance": self.auto_advance}

    def _advance(self, goal_id):
        """Queue the next backlog task of a project to the gate, in the background (it does an LLM
        route+draft) so callers return promptly; the new run surfaces via the SSE/DB trigger."""
        if not self.auto_advance:
            return

        def _go():
            try:
                self.run_next_task(goal_id)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()

    def project_status(self, goal_id) -> dict:
        """Milestones + tasks for a project goal, with rolled-up progress."""
        gid = str(goal_id)
        mss = [m for m in self.db.list("milestones", 300) if str(m.get("goal_ref")) == gid]
        all_tasks = self.db.list("tasks", 1000)
        out, total, done = [], 0, 0
        for m in sorted(mss, key=lambda x: x["Id"]):
            ts = [t for t in all_tasks if str(t.get("milestone_ref")) == str(m["Id"])]
            d = sum(1 for t in ts if self._resolved(t.get("status")))
            total += len(ts); done += d
            out.append({"milestone_id": m["Id"], "title": m.get("title"), "status": m.get("status"),
                        "pct": round(100 * d / len(ts)) if ts else 0,
                        "tasks": [{"task_id": t["Id"], "title": t.get("title"),
                                   "status": t.get("status"), "lane": t.get("assigned_agent")}
                                  for t in sorted(ts, key=lambda x: x["Id"])]})
        g = self.db.find("mc_goals", "Id", goal_id) or {}
        return {"goal_id": goal_id, "title": g.get("title"), "milestones": out,
                "done": done, "total": total,
                "pct": round(100 * done / total) if total else 0}

    def project_goals(self, limit=60) -> list:
        """All projects (mc_goals that HAVE a milestone plan) with rolled-up progress — one pass
        over milestones+tasks so the cockpit panel is one call, not N."""
        goals = self.db.list("mc_goals", limit)
        mss = self.db.list("milestones", 500)
        by_ms = {}
        for t in self.db.list("tasks", 2000):
            by_ms.setdefault(str(t.get("milestone_ref")), []).append(t)
        out = []
        for g in goals:
            gid = str(g["Id"])
            gms = [m for m in mss if str(m.get("goal_ref")) == gid]
            if not gms:
                continue
            total = done = 0
            for m in gms:
                ts = by_ms.get(str(m["Id"]), [])
                total += len(ts)
                done += sum(1 for t in ts if self._resolved(t.get("status")))
            out.append({"goal_id": g["Id"], "title": g.get("title"), "milestones": len(gms),
                        "done": done, "total": total, "pct": round(100 * done / total) if total else 0})
        out.sort(key=lambda x: x["goal_id"], reverse=True)
        return out

    def run_next_task(self, goal_id) -> dict:
        """Run the next Backlog task in this project through the pipeline (it routes + gates like any
        run; its execution then marks the task Done). One task at a time — each gets a human gate."""
        gid = str(goal_id)
        ms_ids = {str(m["Id"]) for m in self.db.list("milestones", 300) if str(m.get("goal_ref")) == gid}
        nxt = None
        for t in sorted(self.db.list("tasks", 1000), key=lambda x: x["Id"]):
            if str(t.get("milestone_ref")) in ms_ids and (t.get("status") or "").lower() in ("backlog", ""):
                nxt = t
                break
        if not nxt:
            return {"done": True, "note": "no backlog tasks remaining"}
        self.db.update("tasks", nxt["Id"], {"status": "In Progress"})
        st = self.start_run(nxt["title"], task_ref=nxt["Id"])
        return {"task_id": nxt["Id"], "task_title": nxt["title"], **st}

    BACKLOG_MAX = 5

    def run_backlog(self, limit=3) -> dict:
        """One backlog task from each project with work left, up to `limit`. The v2 answer to
        crew's `/run-backlog`, which is what `nomadselfdevcron` fires at daily.

        The difference that matters: crew's version ran the work and landed uncommitted edits by
        itself. Every run started here stops at `awaiting-approval` like any other, so a cron firing
        while nobody is awake queues proposals and executes nothing. That is the whole reason this
        is safe to leave on a timer.

        `limit` is clamped, because the thing on the other end of this is a scheduler. A bad payload
        or a loop should not be able to fill the gate faster than an operator can read it. One task
        per project rather than `limit` from the first one, so a long project cannot starve the rest.

        A project that fails is reported and stepped over: one broken goal must not stop the sweep."""
        n = max(1, min(int(limit or 1), self.BACKLOG_MAX))
        started, skipped = [], []
        for g in self.project_goals():
            if len(started) >= n:
                break
            if g["total"] and g["done"] >= g["total"]:      # nothing left to pick up
                continue
            try:
                r = self.run_next_task(g["goal_id"])
            except Exception as e:
                skipped.append({"goal_id": g["goal_id"], "error": str(e)[:200]})
                continue
            if r.get("done"):
                continue
            started.append({"goal_id": g["goal_id"], "project": g["title"], **r})
        return {"started": len(started), "limit": n, "runs": started, "skipped": skipped,
                "note": "each run is awaiting approval; nothing has executed"}

    @staticmethod
    def _resolved(status):
        return (status or "").lower() in ("done", "skipped")   # both count as progress

    def _rollup_milestone(self, mref):
        """Recompute a milestone's pct_complete from its tasks (done or skipped = resolved)."""
        sib = [x for x in self.db.list("tasks", 1000) if str(x.get("milestone_ref")) == str(mref)]
        d = sum(1 for x in sib if self._resolved(x.get("status")))
        pct = round(100 * d / len(sib)) if sib else 0
        self.db.update("milestones", int(mref),
                       {"pct_complete": pct, "status": "Done" if pct == 100 else "In Progress"})

    def _goal_of_milestone(self, mref):
        m = self.db.find("milestones", "Id", int(mref)) or {}
        return int(m["goal_ref"]) if m.get("goal_ref") else None

    def _complete_task(self, task_ref, outcome=""):
        """Mark a task Done, roll up its milestone, and advance the project (Phase 1A)."""
        try:
            tid = int(task_ref)
            self.db.update("tasks", tid, {"status": "Done", "output_link": str(outcome)[:200]})
            t = self.db.find("tasks", "Id", tid) or {}
            if t.get("milestone_ref"):
                self._rollup_milestone(t["milestone_ref"])
                gid = self._goal_of_milestone(t["milestone_ref"])
                if gid:
                    self._advance(gid)
        except Exception:
            pass

    def task_action(self, task_id, action) -> dict:
        """Unblock a stalled project task. 'retry' → Backlog (auto-advance re-queues it); 'skip' →
        Skipped (counts as resolved, project moves on). Both nudge the project forward."""
        t = self.db.find("tasks", "Id", int(task_id))
        if not t:
            return {"error": "task not found"}
        if action == "retry":
            self.db.update("tasks", int(task_id), {"status": "Backlog"})
            new = "Backlog"
        elif action == "skip":
            self.db.update("tasks", int(task_id), {"status": "Skipped",
                                                   "output_link": "skipped by operator"})
            new = "Skipped"
        else:
            return {"error": "action must be retry|skip"}
        if t.get("milestone_ref"):
            self._rollup_milestone(t["milestone_ref"])
            gid = self._goal_of_milestone(t["milestone_ref"])
            if gid:
                self._advance(gid)
        return {"ok": True, "task_id": int(task_id), "status": new}

    def _fail(self, cid, proposal, error):
        """Mark a run failed (terminal) and stash the error in the payload for the cockpit."""
        self.db.update("comms", cid, {"status": "failed",
                                      "payload": json.dumps({**proposal, "error": str(error)[:300]})})

    # ── stalled-run sweep ───────────────────────────────────────────
    def sweep_stalled(self, max_age_min: int = STALL_MINUTES) -> list:
        """Fail any run still in `new` past `max_age_min`, and return what was swept.

        A run only reaches the gate at the END of start_run. Anything that kills the process
        between Capture and the gate — a container restart, an OOM, a hung model call that
        outlives the request — leaves the comms row at `new` with nobody waiting on it. The
        try/except in start_run cannot help there, because no handler runs.

        One such run sat in `new` from 2026-07-16 to 2026-09-22, counted forever as "in flight"
        by the relay digest, and was found by hand. This is the backstop for that.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(minutes=max_age_min)
        swept = []
        for row in self.db.list("comms", limit=200, where="(status,eq,new)") or []:
            created = row.get("created_at")
            if not created:
                continue
            try:
                ts = datetime.datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=datetime.timezone.utc)
            if ts > cutoff:
                continue
            age = int((now - ts).total_seconds() // 60)
            try:
                payload = json.loads(row.get("payload") or "{}")
            except (ValueError, TypeError):
                payload = {}
            self._fail(row["Id"], payload,
                       f"stalled in 'new' for {age} min with no action; swept by sweep_stalled")
            self._log(row.get("run_id") or "", "engine", "swept a stalled run",
                      f"sat in 'new' for {age} min, past the {max_age_min} min limit",
                      "failed", f"comms#{row['Id']}")
            swept.append({"run_id": row.get("run_id"), "comms_id": row["Id"], "age_min": age})
        return swept

    # ── Log & Learn ─────────────────────────────────────────────────
    def _log(self, run_id, agent, what, why, outcome, links):
        self.db.create("episodic", {"run_id": run_id, "agent": agent, "what": what,
                                    "why": why, "outcome": outcome, "links": links,
                                    "created_at": _now()})


if __name__ == "__main__":
    eng = Engine()
    st = eng.start_run("Send a short welcome note to the new operator")
    print("paused at gate:", json.dumps(st, indent=2)[:600])
    print("resume(approved):", eng.resume_run(st["run_id"], "approved",
                                              proposal_hash(st["proposal"])))
