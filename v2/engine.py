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
import urllib.parse
import uuid

from nocodb import NocoDB
from mcp_comms import send_message_impl
from mcp_research import save_brief_impl
from mcp_content import save_content_impl       # 3B
from mcp_dev import dispatch_build_impl, check_project, project_ids   # 3B; #851
from mcp_image import generate_image_impl        # multi-provider: ComfyUI(local) → OpenAI → Firefly (gated)
from specialists import SPECIALISTS, _IMAGE_WORDS, _prompt   # 3B: config-driven, all lanes (+ visual-ask words)
from specialists import is_ads_audit, is_ad_account_audit   # T3.1: audit-only ads mode
import memory                                     # 3C: episodic recall
import llm
from pm_openproject import (OpenProject, MANAGER, NEXT_STATUSES, _title, parent_id, pick_next,
                            project_id)

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

# Where projects live: the NocoDB mc_goals/milestones/tasks tables, or OpenProject (PM-01 P3).
# A switch like NOMAD_SOURCE, so going back is one env change. Each run remembers which board its
# task came from (`task_pm` in the payload), so flipping the switch does not strand runs in flight.
PM_BACKEND = os.environ.get("NOMAD_PM", "nocodb").strip().lower()
COCKPIT_URL = os.environ.get("NOMAD_COCKPIT_URL", "http://localhost:8099").rstrip("/")
# The cockpit's tailnet address (#795), which the phone can open. An OpenProject card's link goes
# there when it is set, so the card opens the run page from anywhere on the tailnet (#801).
COCKPIT_PUBLIC_URL = os.environ.get("NOMAD_TAILNET_ORIGIN", "").strip().rstrip("/") or COCKPIT_URL
OP = "openproject"

# Where each tool writes a sink row (for log links). dispatch_build has no sink row.
SINK = {"send_message": "outbox", "save_brief": "knowledge", "save_content": "content",
        "generate_image": "content"}


def run_page_url(run_id: str) -> str:
    """The run page for one run (#799): HTML a phone can read, not the JSON of GET /run."""
    return f"{COCKPIT_PUBLIC_URL}/cockpit/run/{urllib.parse.quote(str(run_id), safe='')}"


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


def delete_project_impl(project: str, summary: str = "") -> dict:
    """The gate's yes for `delete project <id>` from an Open WebUI chat (#1200). Approving runs
    nothing: the chat runs the steps and reports each one (owui_delete), so this only records the
    approval. Without the chat's own saved plan, an approved run deletes nothing."""
    return {"ok": True, "summary": f"approved for {project}; the steps run from the chat"}


class Engine:
    def __init__(self, db=None, pm=None, op=None):
        self.db = db or NocoDB()
        self.pm = (pm or PM_BACKEND) if (pm or PM_BACKEND) in ("nocodb", OP) else "nocodb"
        self._op = op
        # specialist registry (by lane) + tool registry (by action) — the engine is generic
        self.specialists = SPECIALISTS
        self.tools = {"send_message": send_message_impl, "save_brief": save_brief_impl,
                      "save_content": save_content_impl, "dispatch_build": dispatch_build_impl,
                      "generate_image": generate_image_impl,
                      "delete_project": delete_project_impl}
        # The dispatcher's registry, asked before the gate (#851). Attributes, like `tools`, so a
        # test can stand in for the dispatcher.
        self.check_project, self.project_ids = check_project, project_ids
        # Auto-advance: after a project task executes, queue the next backlog task to the gate (the
        # project flows; each action still gates). NOMAD_AUTO_ADVANCE=0 → manual ▶ next only.
        self.auto_advance = os.environ.get("NOMAD_AUTO_ADVANCE", "1").lower() not in ("0", "false", "no")

    @property
    def op(self):
        if self._op is None:
            self._op = OpenProject()
        return self._op

    def _mirror(self, fn, *a, **kw):
        """Copy a run step onto the OpenProject board, fail-open. The gate and the comms row are
        the record; the board is a view of them, so an OpenProject outage costs a stale card,
        never a run. The miss goes to the episodic log so a lagging board can be explained."""
        try:
            return fn(*a, **kw)
        except Exception as e:
            try:
                self._log("", "engine", "openproject mirror failed",
                          f"{getattr(fn, '__name__', '?')}{a[:2]}: {str(e)[:160]}",
                          "board may lag the run", "")
            except Exception:
                pass
            return None

    # ── stages up to the gate ───────────────────────────────────────
    def start_run(self, goal_title: str, task_ref=None, task_pm=None) -> dict:
        """Capture → Clarify → Route → Process → pause at the Human Gate. Returns run state.
        `task_ref` links this run to a project task (mission control) so its execution marks the
        task Done and rolls up milestone progress (Phase 1A project orchestration)."""
        run_id = uuid.uuid4().hex[:12]
        # Which board the task sits on rides every payload from Capture on, so a run that dies
        # before the gate (and the sweep that finds it) can still mark its task Blocked.
        task = {"task_ref": task_ref, "task_pm": task_pm} if task_ref else {}

        # Capture
        goal = self.db.create("goals", {"title": goal_title, "status": "new", "created_at": _now()})
        gid = goal["Id"]
        comms = self.db.create("comms", {
            "goal_id": gid, "run_id": run_id, "from_agent": "human", "lane": "inbox",
            "type": "task", "status": "new", "priority": "normal",
            "payload": json.dumps({"goal": goal_title, **task}), "created_at": _now()})
        cid = comms["Id"]

        # Everything from here to the gate can throw: the Manager call, the specialist's Process,
        # a scraper timeout. Without this the comms row is simply left at `new` and no longer
        # belongs to anyone — the run neither reaches the gate nor reports a failure. One did
        # exactly that for two months. The sweep below is the backstop for the cases where no
        # handler gets to run at all; this is the one that can say what actually went wrong.
        try:
            return self._route_and_propose(run_id, cid, gid, goal_title, task_ref, task_pm)
        except Exception as e:
            self._fail(cid, {"goal": goal_title, **task}, e)
            self._log(run_id, "engine", "run failed before the gate", str(e)[:200],
                      "failed", f"comms#{cid}")
            raise

    def file_gate(self, goal_title: str, action: str, args: dict, target: str = "",
                  preview: str = "", lane: str = "dev") -> dict:
        """File a proposal at the Human Gate directly, with no Manager or specialist: for an action
        the operator typed as a command (owui_delete, #1200). It waits as `awaiting-approval` like
        any run and resumes through resume_run with the proposal's hash."""
        run_id = uuid.uuid4().hex[:12]
        goal = self.db.create("goals", {"title": goal_title, "status": "routed", "created_at": _now()})
        proposal = {"action": action, "args": args, "target": target, "preview": preview}
        comms = self.db.create("comms", {
            "goal_id": goal["Id"], "run_id": run_id, "from_agent": "operator", "lane": lane,
            "assigned_agent": lane, "type": "proposal", "status": "awaiting-approval",
            "priority": "normal", "created_at": _now(),
            "payload": json.dumps({**proposal, "goal": goal_title, "intent": goal_title,
                                   "target": target, "task_ref": None})})
        self._log(run_id, lane, f"{action} filed at the gate", goal_title[:160],
                  "awaiting approval", f"comms#{comms['Id']}")
        return {"run_id": run_id, "comms_id": comms["Id"], "goal_id": goal["Id"], "lane": lane,
                "status": "awaiting-approval", "proposal": proposal}

    def _route_and_propose(self, run_id, cid, gid, goal_title, task_ref, task_pm=None):
        task = {"task_ref": task_ref, "task_pm": task_pm} if task_ref else {}
        # Clarify + Route (Manager) — classify the goal to a lane/specialist
        past = memory.recall(self.db, goal_title)        # 3C: provenance-aware routing
        context = (f"\n\nRELEVANT PAST WORK (use it to clarify/route consistently):\n{past}"
                   if past else "")
        ids = self.project_ids()
        repos = (f" The registry ids are: {', '.join(ids)}." if ids else "")
        m = llm.chat_json(
            MANAGER_MODEL,
            "You are NOMAD's Manager. Clarify a goal and ROUTE it to one lane. Lanes: "
            "'comms'=draft/send a message; 'research'=answer a question or gather info; "
            "'support'=reply to a support request; 'ads'=write marketing copy, or audit an ad "
            "account or campaigns (read-only); "
            "'dev'=a coding task in a repo. Fields: lane, intent (one line), target — "
            "comms/support → recipient email or 'operator'; research/ads → the topic; "
            "dev → the project's registry id, copied exactly (e.g. 'nomad', 'civis-poc'): "
            "never a file name, a GitHub owner/repo, a host or a description; if no id fits, "
            "use your best guess and say so in concern." + repos +
            " Also return: confidence (0-1, how sure you are this is "
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
        # T3.1: an ad-account audit goes to the ads lane, and its intent is the goal as typed. The
        # Manager's one-line intent drops the account data the operator pasted, and the audit-only
        # Skill may only report figures that were supplied, so it must see the original text.
        if is_ad_account_audit(goal_title):
            lane = "ads"
        if lane == "ads" and is_ads_audit(goal_title):
            intent = goal_title
        if lane == "dev":
            target = self._repick_project(run_id, cid, goal_title, target, ids)
        specialist = self.specialists[lane]
        self.db.update("goals", gid, {"status": "routed"})
        self.db.update("comms", cid, {"lane": lane, "assigned_agent": lane, "from_agent": "manager",
                                      "payload": json.dumps({"goal": goal_title, "intent": intent,
                                                             "target": target,
                                                             "route_confidence": confidence,
                                                             "route_concern": concern,
                                                             **task})})
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

        # A build's project is checked against the dispatcher's registry BEFORE the gate (#851). The
        # Manager wrote free text there ('claude-bridge', 'IT-Nomad-Lab/nomad', a file name) and the
        # run looked fine until it failed after approval. Warn, don't refuse: the drafted plan is
        # still worth reading, and the dispatcher refuses an unresolved name at execute anyway, so
        # approving one cannot build in a guessed repo. A match is written back as the exact id,
        # so the gate shows the repo the Builder will edit rather than the Manager's wording.
        if proposal.get("action") == "dispatch_build":
            a = proposal.setdefault("args", {})
            chk = self.check_project(a.get("project") or "")
            if chk.get("ok"):
                a["project"] = chk.get("target") or chk["id"]   # 'nomad/claude-bridge' (#906)
            proposal["project_check"] = chk

        # Human Gate — file the action proposal and PAUSE. Keep the routing provenance
        # (goal/intent/target) alongside the proposal so the cockpit drill-down can show *why*
        # this run was routed here, not just the drafted action.
        self.db.update("comms", cid, {"type": "proposal", "status": "awaiting-approval",
                                      "payload": json.dumps({**proposal, "goal": goal_title,
                                                             "intent": intent, "target": target,
                                                             "task_ref": task_ref,
                                                             **({"task_pm": task_pm} if task_pm else {})})})
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
        if row.get("status") in ("executing", "executed", "declined", "failed", "withdrawn"):
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
                self._fail(cid, proposal, f"unknown action {action}", lane=row.get("lane"))
                self._log(run_id, row.get("lane", "?"), action or "?", json.dumps(args)[:160],
                          f"FAILED: unknown action {action}", f"comms#{cid}")
                return {"run_id": run_id, "status": "failed", "error": f"unknown action {action}"}
            self.db.update("comms", cid, {"status": "executing"})   # lock before the side effect
            try:
                res = tool(**args)
            except Exception as e:                                  # tool raised (defensive)
                res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}
            if not res.get("ok"):                                   # tool failed (e.g. dispatcher down)
                self._fail(cid, proposal, res.get("error", ""), lane=row.get("lane"))
                self._log(run_id, row.get("lane", "?"), action, json.dumps(args)[:160],
                          f"FAILED: {res.get('error', '')}", f"comms#{cid}")
                return {"run_id": run_id, "status": "failed", "error": res.get("error")}
            sink = SINK.get(action)                                 # None for dispatch_build
            rid = res.get(f"{sink}_id") if sink else None
            self.db.update("comms", cid, {"status": "executed"})
            self.db.update("goals", row["goal_id"], {"status": "done"}) if row.get("goal_id") else None
            outcome = (f"{action} → {sink}#{rid}" if sink and rid
                       else f"{action} executed: {(res.get('summary') or 'ok')[:120]}")
            if res.get("branch"):                       # builds land on a feature branch
                outcome += f" [branch {res['branch']}]"
            links = f"comms#{cid}" + (f",{sink}#{rid}" if sink and rid else "")
            self._log(run_id, row.get("lane", "?"), action, json.dumps(args)[:160], outcome, links)
            if proposal.get("task_ref"):                # Phase 1A: roll a project task → Done
                self._complete_task(proposal["task_ref"], outcome, proposal.get("task_pm"),
                                    lane=row.get("lane"))
            return {"run_id": run_id, "status": "executed", "action": action,
                    "sink": sink, "id": rid, "outcome": outcome}

        # rejected — no action runs; mark declined (terminal) and log
        self.db.update("comms", cid, {"status": "declined"})
        if proposal.get("task_ref") and proposal.get("task_pm") == OP:
            # Won't do, not Blocked: the operator decided, nothing is waiting on a fix. The project
            # does not advance on a decline, same as the NocoDB board.
            self._mirror(self._op_close, proposal["task_ref"], "Won't do",
                         "The operator rejected the proposal at the gate. Nothing ran.",
                         row.get("lane") or MANAGER, advance=False)
        elif proposal.get("task_ref"):      # a rejected project task → Blocked (project pauses, visible)
            try:
                self.db.update("tasks", int(proposal["task_ref"]), {"status": "Blocked"})
            except Exception:
                pass
        self._log(run_id, row.get("lane", "comms"), proposal.get("action", "action") + " (blocked)",
                  "operator rejected at the gate", "rejected — no action taken", f"comms#{cid}")
        return {"run_id": run_id, "status": "declined"}

    def _repick_project(self, run_id, cid, goal_title, target, ids):
        """Ask the Manager once more when a dev target is not a registry project, before the
        specialist drafts a plan for it. The id list in the routing prompt was not enough: run
        50d61fe163ff (#371) recalled its own failed runs and copied 'claude-bridge' out of them.
        Only the dispatcher decides a match, so a second answer that does not resolve changes
        nothing and the gate shows the #851 warning as before. An unreachable dispatcher (ok None)
        is no reason to second-guess the Manager."""
        chk = self.check_project(target)
        if chk.get("ok") is not False:
            return target
        offered = ", ".join(chk.get("candidates") or ids or [])
        m = llm.chat_json(
            MANAGER_MODEL,
            "You are NOMAD's Manager. You routed a coding task to a project name the dispatcher "
            "does not know. Pick the project from the registry ids below, copied exactly. A folder, "
            "file or service inside a repo belongs to the repo that contains it (claude-bridge, "
            "dispatcher and v2 are all inside 'nomad'). If none fits, answer 'none'. "
            'Return JSON only: {"target": "<registry id or none>"}.'
            + (f" The registry ids are: {', '.join(ids)}." if ids else ""),
            f"Goal: {goal_title}\nYou wrote: {target}\nWhy it failed: {chk.get('error', '')}\n"
            f"Nearest ids: {offered}")
        again = str(m.get("target") or "").strip()
        new = self.check_project(again) if again and again.lower() != "none" else {}
        if new.get("ok") is True:
            self._log(run_id, "manager", "manager re-picked project",
                      f"'{target}' is not a registry project", f"now {new['id']}", f"comms#{cid}")
            return new.get("target") or new["id"]
        self._log(run_id, "manager", "project still unresolved after re-ask",
                  f"'{target}', then '{again or 'none'}'", "the gate will flag it", f"comms#{cid}")
        return target

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
        if proposal.get("task_ref") and proposal.get("task_pm") == OP:
            self._mirror(self.op.move, proposal["task_ref"], "Awaiting approval",
                         f"The operator retried run {run_id}. The same proposal is back at the gate.",
                         as_=row.get("lane") or MANAGER)
        self._log(run_id, row.get("lane", "?"), proposal.get("action", "action") + " (retry)",
                  "operator retried a failed run", "re-queued at the human gate", f"comms#{row['Id']}")
        return {"run_id": run_id, "status": "awaiting-approval", "retried": True}

    def withdraw_run(self, run_id: str, reason: str = "") -> dict:
        """Close a proposal that should never have been filed, and leave its task where it is.

        Rejecting is a decision about the work: it sets an OpenProject task to Won't do. A stray
        proposal is no decision at all (#990: a cron queued a parked task and an operator-only
        one), and by the time anyone sees it the task has usually been moved back by hand. So this
        only closes the run, as `withdrawn`, and says so on the task. Only a run still waiting at
        the gate can be withdrawn."""
        with _CLAIM:
            if run_id in _IN_FLIGHT:
                return {"run_id": run_id, "status": "executing", "note": "already being resolved"}
            _IN_FLIGHT.add(run_id)
        try:
            row = self.db.find("comms", "run_id", run_id)
            if not row:
                return {"run_id": run_id, "error": "run not found"}
            if row.get("status") != "awaiting-approval":
                return {"run_id": run_id, "status": row.get("status"),
                        "note": "only a run awaiting approval can be withdrawn"}
            proposal = json.loads(row.get("payload") or "{}")
            why = (reason or "").strip()[:300] or "no reason given"
            self.db.update("comms", row["Id"], {"status": "withdrawn"})
            if proposal.get("task_ref") and proposal.get("task_pm") == OP:
                self._mirror(self.op.comment, proposal["task_ref"],
                             f"Run {run_id} was withdrawn: {why}. Nothing ran, and this task's "
                             "status was left as it is.", as_=row.get("lane") or MANAGER)
            self._log(run_id, row.get("lane", "?"), proposal.get("action", "action") + " (withdrawn)",
                      why, "withdrawn — no action taken, task left alone", f"comms#{row['Id']}")
            return {"run_id": run_id, "status": "withdrawn"}
        finally:
            with _CLAIM:
                _IN_FLIGHT.discard(run_id)

    # ── Phase 1A: project → milestone → task orchestration ──────────
    def plan_project(self, title: str, description: str = "") -> dict:
        """Decompose a project goal into milestones + tasks (mission control) via the Manager LLM.
        Writes mc_goals → milestones → tasks rows (linked by *_ref). Each task names a lane so it
        can later run through the normal pipeline gate. Returns the persisted plan."""
        plan = llm.chat_json(
            MANAGER_MODEL,
            "You are NOMAD's Manager/Planner. Break a PROJECT into a concrete, ordered plan: 2-4 "
            "milestones, each with 1-4 tasks. Each task is ONE actionable unit assignable to a lane "
            "(comms=send a message; research=gather/answer; support=reply; ads=marketing copy or a "
            "read-only ad account audit; "
            "dev=a coding task in a repo). Give every task a short unique key (t1, t2, ...). In "
            "`after`, list the keys of the tasks that must finish before this one can start; leave "
            "it empty when the order does not matter. Return JSON only: "
            '{"milestones":[{"title":"...","tasks":[{"key":"t1","title":"...","lane":"research",'
            '"after":[]}]}],'
            '"assumptions":["what you assumed that the operator did not say"],'
            '"risk":"the one thing most likely to make this plan wrong"}'
            + _CRITIC_BLOCK,
            f"Project: {title}\n{description}".strip())
        if self.pm == OP:
            return self._op_plan(title, description, plan)
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

    def _advance(self, goal_id, pm=None):
        """Queue the next backlog task of a project to the gate, in the background (it does an LLM
        route+draft) so callers return promptly; the new run surfaces via the SSE/DB trigger.
        `pm` is the board the goal lives on, which is not always the one the switch names now."""
        if not self.auto_advance:
            return
        nxt = self._op_next if (pm or self.pm) == OP else self._nocodb_next

        def _go():
            try:
                nxt(goal_id)
            except Exception:
                pass
        threading.Thread(target=_go, daemon=True).start()

    def project_status(self, goal_id) -> dict:
        """Milestones + tasks for a project goal, with rolled-up progress."""
        if self.pm == OP:
            return self._op_status(goal_id)
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
        if self.pm == OP:
            return self._op_goals()
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

    def run_next_task(self, goal_id=None, project=None, ready_only=False) -> dict:
        """Run the task next_task names through the pipeline (it routes + gates like any run; its
        execution then marks the task Done). One task at a time — each gets a human gate.
        `ready_only` leaves Backlog alone on OpenProject; the NocoDB board has no Ready, so there
        Backlog stays the queue."""
        if self.pm == OP:
            return self._op_next(goal_id, project, ready_only=ready_only)
        if project is not None:
            return {"error": "a board to pick from needs NOMAD_PM=openproject"}
        return self._nocodb_next(goal_id)

    def _nocodb_pick(self, goal_id):
        gid = str(goal_id)
        ms_ids = {str(m["Id"]) for m in self.db.list("milestones", 300) if str(m.get("goal_ref")) == gid}
        for t in sorted(self.db.list("tasks", 1000), key=lambda x: x["Id"]):
            if str(t.get("milestone_ref")) in ms_ids and (t.get("status") or "").lower() in ("backlog", ""):
                return t
        return None

    def _nocodb_next(self, goal_id) -> dict:
        nxt = self._nocodb_pick(goal_id)
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

        A project that fails is reported and stepped over: one broken goal must not stop the sweep.

        On OpenProject it takes Ready tasks only (#990). The cockpit's "next" falls back to Backlog
        because the operator pressed it; nobody presses this one. Backlog is where the operator and
        the autopilot park work, and the 2026-10-02 run queued a parked task and an operator-only
        one from there. A task with an open blocker is never taken either way (pick_next)."""
        n = max(1, min(int(limit or 1), self.BACKLOG_MAX))
        started, skipped = [], []
        for g in self.project_goals():
            if len(started) >= n:
                break
            if g["total"] and g["done"] >= g["total"]:      # nothing left to pick up
                continue
            try:
                r = self.run_next_task(g["goal_id"], ready_only=True)
            except Exception as e:
                skipped.append({"goal_id": g["goal_id"], "error": str(e)[:200]})
                continue
            if r.get("done"):
                continue
            # The run's own goal_id is its capture row; the sweep reports the project it came from.
            started.append({**r, "goal_id": g["goal_id"], "project": g["title"]})
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

    def _complete_task(self, task_ref, outcome="", pm=None, lane=None):
        """Mark a task Done, roll up its milestone, and advance the project (Phase 1A)."""
        if pm == OP:
            self._mirror(self._op_close, task_ref, "Done", f"Executed. {outcome}"[:1000],
                         lane or MANAGER)
            return
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
        if self.pm == OP:
            return self._op_task_action(task_id, action)
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

    # ── PM-01 P3: the same project flow on OpenProject ──────────────
    # Feature = project, Epic = milestone, Task = task. Progress rolls up in OpenProject itself;
    # what the engine still does is close an Epic or Feature once everything under it is closed.

    def _op_plan(self, title, description, plan) -> dict:
        op = self.op
        made, keys, deps, out = [], {}, [], []
        try:
            feat = op.create("Feature", title, lane="manager", description=description)
            made.append(feat["id"])
            for ms in (plan.get("milestones") or []):
                mtitle = (ms.get("title") or "Milestone").strip()
                epic = op.create("Epic", mtitle, parent=feat["id"], lane="manager")
                made.append(epic["id"])
                tasks = []
                for t in (ms.get("tasks") or []):
                    lane = t.get("lane") if t.get("lane") in self.specialists else "research"
                    ttitle = (t.get("title") or "Task").strip()
                    tw = op.create("Task", ttitle, parent=epic["id"], lane=lane)
                    made.append(tw["id"])
                    op.move(tw["id"], "Ready")
                    if str(t.get("key") or "").strip():
                        keys[str(t["key"]).strip()] = tw["id"]
                    deps.append((tw["id"], t.get("after") or []))
                    tasks.append({"task_id": tw["id"], "title": ttitle, "lane": lane})
                out.append({"milestone_id": epic["id"], "title": mtitle, "tasks": tasks})
        except Exception as e:
            # There is no delete for agents, so say exactly what already exists on the board.
            self._log("", "manager", f"plan failed: {title}", str(e)[:200],
                      f"created before the failure: {made or 'nothing'}", "")
            raise RuntimeError(f"OpenProject plan failed after creating {made or 'nothing'}: {e}") from e
        # Dependencies go on after every task exists, so `after` may name a later task. A bad key
        # or a cycle OpenProject refuses costs that one relation, not the plan.
        relations, rel_errors = 0, []
        for tid, after in deps:
            for k in (after if isinstance(after, list) else [after]):
                b = keys.get(str(k).strip())
                if not b or b == tid:
                    rel_errors.append(f"#{tid} after '{k}': no such task")
                    continue
                try:
                    op.blocks(b, tid)
                    relations += 1
                except Exception as e:
                    rel_errors.append(f"#{b} blocks #{tid}: {str(e)[:120]}")
        assumptions = [a for a in (plan.get("assumptions") or []) if str(a).strip()]
        risk = (plan.get("risk") or "").strip()
        n_tasks = sum(len(m["tasks"]) for m in out)
        note = ("Planned by the Manager: " + f"{len(out)} epics, {n_tasks} tasks, {relations} dependencies."
                + ("\n\nAssumptions:\n" + "\n".join(f"- {a}" for a in assumptions) if assumptions else "")
                + (f"\n\nBiggest risk: {risk}" if risk else "")
                + ("\n\nDependencies not set:\n" + "\n".join(f"- {x}" for x in rel_errors) if rel_errors else ""))
        self._mirror(op.comment, feat["id"], note)
        self._log(f"proj-{feat['id']}", "manager", f"planned project: {title}",
                  "; ".join(str(a) for a in assumptions) or "no assumptions declared",
                  f"{len(out)} milestones, {n_tasks} tasks, {relations} dependencies"
                  + (f" · biggest risk: {risk}" if risk else ""), f"op#{feat['id']}")
        self._advance(feat["id"], OP)
        return {"goal_id": feat["id"], "title": title, "milestones": out, "task_count": n_tasks,
                "relations": relations, "relation_errors": rel_errors,
                "assumptions": assumptions, "risk": risk or None,
                "auto_advance": self.auto_advance, "pm": OP}

    def _op_board(self, wp):
        """Every work package on the board `wp` sits on. A Feature on the civis board runs and
        closes against civis, not NOMAD_PM_PROJECT (#791); a WP id is unique across boards, so
        the moves and comments themselves need no board."""
        return self.op.project_wps(project_id(wp))

    def _op_pick(self, goal_id=None, project=None, statuses=NEXT_STATUSES):
        """(the Task or Bug "next" takes, or None; the board by id). Under the Feature `goal_id`,
        or across the whole board `project` (an OpenProject project id) for the cockpit's per-project
        "next". The order is pm_openproject.pick_next's: Ready before Backlog, then priority, then
        WP number."""
        op = self.op
        if project is not None:
            wps = op.project_wps(project)
            cands = wps
        else:
            wps = self._op_board(op.get(goal_id))
            epics, tasks = op.tree(wps, goal_id)
            cands = [t for e in epics for t in tasks[e["id"]]]
        by_id = {w["id"]: w for w in wps}
        return pick_next(cands, lambda i: op.open_blockers(i, by_id), statuses), by_id

    def next_task(self, goal_id=None, project=None) -> dict:
        """What "next" would take in this project, without taking it. The chat's and the
        briefing's "next" read this; run_next_task takes the same item (#792)."""
        if self.pm != OP:
            if project is not None:
                return {"error": "a board to pick from needs NOMAD_PM=openproject"}
            t = self._nocodb_pick(goal_id)
            return ({"task_id": t["Id"], "task_title": t.get("title"), "status": t.get("status")}
                    if t else {"done": True, "note": "no backlog tasks remaining"})
        t, _ = self._op_pick(goal_id, project)
        if not t:
            return {"done": True, "note": "no Ready or Backlog task without an open blocker"}
        return {"task_id": t["id"], "task_title": t["subject"], "status": _title(t, "status"),
                "priority": _title(t, "priority") or "Normal"}

    def _op_next(self, goal_id=None, project=None, ready_only=False) -> dict:
        """Take the task next_task names and send it to the gate."""
        op = self.op
        nxt, by_id = self._op_pick(goal_id, project, ("Ready",) if ready_only else NEXT_STATUSES)
        if not nxt:
            return {"done": True, "note": "no Ready " + ("" if ready_only else "or Backlog ")
                    + "task without an open blocker"}
        if _title(nxt, "status") == "Backlog":
            # The board says why a Backlog task jumped: nothing in the project was Ready.
            op.move(nxt["id"], "Ready", "Picked by \"next\": nothing in this project was Ready, "
                    "so it takes the first Backlog task by priority, then number.")
        return self._op_gate(nxt, by_id)

    def run_task(self, task_id) -> dict:
        """Send one named Task or Bug to the gate, as "next" would (an Open WebUI chat's "build
        that fix", #1146). Only an open task with no open blocker; the run stops at the gate."""
        if self.pm != OP:
            return {"error": "running a named task needs NOMAD_PM=openproject"}
        op = self.op
        wp = op.get(task_id)
        kind, status = _title(wp, "type"), _title(wp, "status")
        if kind not in ("Task", "Bug", "User story"):    # the Dispatch panel sends stories too (#1441)
            return {"error": f"#{wp['id']} is a {kind}; only a Task, Bug or User story can be built"}
        if status not in ("New", "Backlog", "Ready", "Blocked", "Changes requested"):
            return {"error": f"#{wp['id']} is {status}, so it isn't started again"}
        if op.open_blockers(wp["id"]):
            return {"error": f"#{wp['id']} has an open blocker"}
        by_id = {w["id"]: w for w in self._op_board(wp)}
        if status in ("New", "Backlog"):
            op.move(wp["id"], "Ready", "Asked for from an Open WebUI chat.")
        return self._op_gate(wp, by_id)

    def _op_gate(self, nxt, by_id) -> dict:
        op = self.op
        # The claim is strict: if the task cannot move to In Progress, no run starts for it.
        op.move(nxt["id"], "In Progress", "Picked up by the engine. Routing it through the gate.")
        epic = by_id.get(parent_id(nxt))
        for c, kind in ((epic, "Epic"), (by_id.get(parent_id(epic)) if epic else None, "Feature")):
            if c and _title(c, "type") == kind and _title(c, "status") in ("New", "Backlog"):
                self._mirror(op.move, c["id"], "In Progress")
        st = self.start_run(nxt["subject"], task_ref=nxt["id"], task_pm=OP)
        lane = st.get("lane") or MANAGER
        self._mirror(op.set_run, nxt["id"], st["run_id"], run_page_url(st["run_id"]))
        p = st.get("proposal") or {}
        self._mirror(op.move, nxt["id"], "Awaiting approval",
                     f"Routed to {lane}. Proposed `{p.get('action', '?')}` in run {st['run_id']}. "
                     "Waiting for the operator's decision; nothing runs before it.", as_=lane)
        return {"task_id": nxt["id"], "task_title": nxt["subject"], **st}

    def _op_close(self, task_ref, status, comment, lane, advance=True):
        """Close a task (Done or Won't do), close its Epic and Feature once nothing under them is
        open, and on Done queue the next task."""
        op = self.op
        t = op.move(task_ref, status, comment, as_=lane)
        epic_id = parent_id(t)
        if not epic_id:
            return
        wps = self._op_board(t)
        by_id = {w["id"]: w for w in wps}
        epic = by_id.get(epic_id)
        fid = parent_id(epic)

        def all_closed(parent):
            kids = [w for w in wps if parent_id(w) == parent]
            return kids and all(op.closed(_title(k, "status")) for k in kids)

        if epic and not op.closed(_title(epic, "status")) and all_closed(epic_id):
            op.move(epic_id, "Done", "Every task under this epic is closed.")
            wps = self._op_board(t)
        if fid in by_id and not op.closed(_title(by_id[fid], "status")) and all_closed(fid):
            op.move(fid, "Done", "Every epic under this feature is closed.")
        if advance and fid:
            self._advance(fid, OP)

    def _op_task_action(self, task_id, action) -> dict:
        if action == "retry":
            new, why = "Ready", "The operator put this back to Ready."
        elif action == "skip":
            new, why = "Won't do", "Skipped by the operator."
        else:
            return {"error": "action must be retry|skip"}
        try:
            if new == "Ready":
                self.op.move(task_id, new, why)
                t = self.op.get(task_id)
                fid = self._op_feature_of(t)
                if fid:
                    self._advance(fid, OP)
            else:
                self._op_close(task_id, new, why, MANAGER)
        except Exception as e:
            return {"error": str(e)[:200]}
        return {"ok": True, "task_id": int(task_id), "status": new}

    def _op_feature_of(self, t):
        epic_id = parent_id(t)
        return parent_id(self.op.get(epic_id)) if epic_id else None

    def _op_status(self, goal_id) -> dict:
        op = self.op
        feat = op.get(goal_id)
        wps = self._op_board(feat)
        epics, tasks = op.tree(wps, goal_id)
        out, total, done = [], 0, 0
        for e in epics:
            ts = tasks[e["id"]]
            d = sum(1 for t in ts if op.closed(_title(t, "status")))
            total += len(ts); done += d
            out.append({"milestone_id": e["id"], "title": e["subject"], "status": _title(e, "status"),
                        "pct": round(100 * d / len(ts)) if ts else 0,
                        "tasks": [{"task_id": t["id"], "title": t["subject"],
                                   "status": _title(t, "status"), "lane": op.lane_of(t)} for t in ts]})
        return {"goal_id": int(goal_id), "title": feat.get("subject"), "milestones": out,
                "done": done, "total": total, "pct": round(100 * done / total) if total else 0,
                "pm": OP}

    def _op_goals(self) -> list:
        op = self.op
        wps = op.project_wps()
        out = []
        for f in (w for w in wps if _title(w, "type") == "Feature"):
            epics, tasks = op.tree(wps, f["id"])
            if not epics:
                continue
            ts = [t for e in epics for t in tasks[e["id"]]]
            done = sum(1 for t in ts if op.closed(_title(t, "status")))
            # The relay alerts on Blocked tasks every pass; listing them here spares it a /project
            # call, and a board read, per goal (#1130).
            blocked = [{"task_id": t["id"], "title": t["subject"], "status": "Blocked",
                        "lane": op.lane_of(t)} for t in ts if _title(t, "status") == "Blocked"]
            out.append({"goal_id": f["id"], "title": f["subject"], "milestones": len(epics),
                        "done": done, "total": len(ts),
                        "pct": round(100 * done / len(ts)) if ts else 0, "blocked": blocked})
        out.sort(key=lambda x: x["goal_id"], reverse=True)
        return out

    def _fail(self, cid, proposal, error, lane=None):
        """Mark a run failed (terminal) and stash the error in the payload for the cockpit. A run
        for an OpenProject task also puts the task in Blocked, with the error as a comment."""
        self.db.update("comms", cid, {"status": "failed",
                                      "payload": json.dumps({**proposal, "error": str(error)[:300]})})
        if proposal.get("task_ref") and proposal.get("task_pm") == OP:
            self._mirror(self.op.move, proposal["task_ref"], "Blocked",
                         f"The run failed: {str(error)[:500]}\n\nRetry it from the cockpit, or move "
                         "this task back to Ready.", as_=lane or MANAGER)

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
