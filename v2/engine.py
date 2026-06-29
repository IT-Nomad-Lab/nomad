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
from specialists import SPECIALISTS, _IMAGE_WORDS   # 3B: config-driven, all lanes (+ visual-ask words)
import memory                                     # 3C: episodic recall
import llm

MANAGER_MODEL = "balanced"

# Where each tool writes a sink row (for log links). dispatch_build has no sink row.
SINK = {"send_message": "outbox", "save_brief": "knowledge", "save_content": "content",
        "generate_image": "content"}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


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
            "dev → the project/repo name." + context,
            f"Goal: {goal_title}")
        lane = m.get("lane") if m.get("lane") in self.specialists else "comms"
        intent = (m.get("intent") or goal_title).strip()
        target = (m.get("target") or "operator").strip()
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
                                                             "target": target})})

        # Process — the routed specialist drafts a proposal via its Skill (reversible, no side effect)
        proposal = specialist.propose(intent=intent, target=target, run_id=run_id)

        # Image asks can name a destination project IN THE PROMPT ("…for project MyApp"). Thread the
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
    def resume_run(self, run_id: str, decision: str) -> dict:
        """Resume a paused run on the gate decision. Idempotent by run_id. decision ∈ approved/rejected."""
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
            '{"milestones":[{"title":"...","tasks":[{"title":"...","lane":"research"}]}]}',
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
        self._log(f"proj-{gid}", "manager", f"planned project: {title}",
                  f"{len(out)} milestones", "decomposed into milestones+tasks", f"mc_goals#{gid}")
        self._advance(gid)   # auto-start: queue the first task to the gate
        return {"goal_id": gid, "title": title, "milestones": out,
                "task_count": sum(len(m["tasks"]) for m in out),
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

    # ── Log & Learn ─────────────────────────────────────────────────
    def _log(self, run_id, agent, what, why, outcome, links):
        self.db.create("episodic", {"run_id": run_id, "agent": agent, "what": what,
                                    "why": why, "outcome": outcome, "links": links,
                                    "created_at": _now()})


if __name__ == "__main__":
    eng = Engine()
    st = eng.start_run("Send a short welcome note to the new operator")
    print("paused at gate:", json.dumps(st, indent=2)[:600])
    print("resume(approved):", eng.resume_run(st["run_id"], "approved"))
