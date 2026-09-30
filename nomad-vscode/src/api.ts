import * as vscode from "vscode";

/** Thin HTTP client for the NOMAD v2 engine (:8099). All calls are fail-soft: on any error they
 *  resolve to a fallback so the UI degrades gracefully instead of throwing.
 *
 *  Everything used to be split between the v1 console (:1701) and the engine. V2c moved the
 *  console-only routes onto the engine so the console can be stopped, and this now talks to one
 *  service. The shape-tolerant reads below are deliberate: the cutover is reversible, and pointing
 *  `nomad.engineUrl` back at :1701 should degrade rather than throw. */
export class Api {
  private cfg() {
    const c = vscode.workspace.getConfiguration("nomad");
    return {
      engine: (c.get<string>("engineUrl") || "http://localhost:8099").replace(/\/$/, ""),
    };
  }

  private async get(base: string, path: string, fallback: any): Promise<any> {
    try {
      const r = await fetch(base + path, { signal: AbortSignal.timeout(8000) });
      if (!r.ok) return fallback;
      return await r.json();
    } catch {
      return fallback;
    }
  }

  private async post(base: string, path: string, body: any, timeoutMs = 20000): Promise<any> {
    try {
      const r = await fetch(base + path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal: AbortSignal.timeout(timeoutMs),
      });
      return await r.json();
    } catch (e: any) {
      return { error: String(e?.message || e) };
    }
  }

  // ── ported from the console in V2c ──

  /** Service health as nomad-watch last recorded it. The engine reports that state rather than
   *  probing, so there is one target list instead of two that drift. A stale file comes back with
   *  `up: null` on every row and a `stale` flag, because a two-hour-old all-clear is the reassuring
   *  reading nomad-watch exists to catch. The array form is the old console response. */
  async services(): Promise<ServiceHealth> {
    const r = await this.get(this.cfg().engine, "/api/services", { services: [] });
    if (Array.isArray(r)) return { services: r, stale: false };
    return { services: r.services || [], stale: !!r.stale, age_s: r.age_s, error: r.error };
  }

  /** Runs sitting at the gate. The console had a dedicated route; the engine's run feed already
   *  carries status and proposal, so this filters rather than asking for a second view of it. */
  async approvals(): Promise<{ approvals: Approval[] }> {
    const r = await this.get(this.cfg().engine, "/runs", { runs: [] });
    const runs = (r.runs || []).filter((x: any) => x.status === "awaiting-approval");
    return {
      approvals: runs.map((x: any) => ({
        run_id: x.run_id,
        type: x.lane || "",
        action: (x.proposal && x.proposal.action) || "action",
        by: x.from_agent,
        preview: (x.proposal && (x.proposal.preview || x.proposal.target)) || "",
      })),
    };
  }

  /** `model` is deliberately not sent. The engine ignores it and picks by lane, and a client that
   *  could name a model would be routing around that choice. */
  chat(text: string): Promise<{ reply?: string; action?: string; error?: string }> {
    return this.post(this.cfg().engine, "/api/chat", {
      messages: [{ role: "user", content: text }], session_id: "vscode",
    }, 180000);
  }

  dispatch(project: string, task: string, mode: "build" | "plan"): Promise<any> {
    return this.post(this.cfg().engine, "/api/dispatch", { project, task, mode }, 900000);
  }

  // ── engine ──
  projectGoals(): Promise<{ projects: ProjectGoal[] }> {
    return this.get(this.cfg().engine, "/project-goals", { projects: [] });
  }
  projectStatus(goalId: number): Promise<ProjectDetail> {
    return this.get(this.cfg().engine, `/project?goal_id=${goalId}`, { milestones: [], done: 0, total: 0, pct: 0 });
  }
  planProject(title: string): Promise<any> {
    return this.post(this.cfg().engine, "/plan-project", { title }, 300000);
  }
  runNextTask(goalId: number): Promise<any> {
    return this.post(this.cfg().engine, "/run-next-task", { goal_id: goalId });
  }
  taskAction(taskId: number, action: "retry" | "skip"): Promise<any> {
    return this.post(this.cfg().engine, "/task-action", { task_id: taskId, action });
  }
  /** Approve or reject at the gate.
   *
   *  An approval has to name the proposal it is approving. `resume_run` requires `expect_hash` and
   *  refuses without it, so this button has never actually been able to approve anything against
   *  the engine — it posted decision-only and got a refusal. It reads the run first and sends the
   *  hash it saw, which is the control working as intended: approving a proposal that changed after
   *  it was shown now fails instead of executing.
   *
   *  A rejection needs no hash. It is the safe direction, and demanding one would mean a run whose
   *  detail cannot be read could not be stopped either. */
  async resume(runId: string, decision: "approved" | "rejected"): Promise<any> {
    const body: any = { run_id: runId, decision };
    if (decision === "approved") {
      const d = await this.get(this.cfg().engine, `/run?id=${encodeURIComponent(runId)}`, {});
      if (!d || !d.proposal_hash) {
        return { error: "could not read the proposal, so not approving it" };
      }
      body.expect_hash = d.proposal_hash;
    }
    return this.post(this.cfg().engine, "/resume", body);
  }

  /** The cockpit. Named for the console it replaced, because the commands that open it are. */
  consoleUrl(): string { return this.cfg().engine; }
}

export interface Approval {
  run_id: string;
  type: string;
  action: string;
  by?: string;
  preview?: string;
}
export interface Service {
  name: string;
  /** null means nobody knows: nomad-watch's last pass is too old to believe. Not the same as down,
   *  and a caller that paints it green is reporting a guess as a reading. */
  up: boolean | null;
  last_up?: boolean;
  detail?: string;
  down_since?: string | null;
  ms?: number;
}
export interface ServiceHealth {
  services: Service[];
  stale: boolean;
  age_s?: number | null;
  error?: string;
}
export interface ProjectGoal { goal_id: number; title: string; milestones: number; done: number; total: number; pct: number; }
export interface Task { task_id: number; title: string; status: string; lane?: string; }
export interface Milestone { milestone_id: number; title: string; status: string; pct: number; tasks: Task[]; }
export interface ProjectDetail { goal_id?: number; title?: string; milestones: Milestone[]; done: number; total: number; pct: number; }
