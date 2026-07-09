import * as vscode from "vscode";

/** Thin HTTP client for the NOMAD console (:1701) and v2 engine (:8099). All calls are fail-soft:
 *  on any error they resolve to a fallback so the UI degrades gracefully instead of throwing. */
export class Api {
  private cfg() {
    const c = vscode.workspace.getConfiguration("nomad");
    return {
      console: (c.get<string>("consoleUrl") || "http://localhost:1701").replace(/\/$/, ""),
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

  // ── console ──
  services(): Promise<Array<{ name: string; up: boolean; ms: number }>> {
    return this.get(this.cfg().console, "/api/services", []);
  }
  approvals(): Promise<{ approvals: Approval[] }> {
    return this.get(this.cfg().console, "/api/approvals", { approvals: [] });
  }
  chat(text: string): Promise<{ reply?: string; action?: string; error?: string }> {
    return this.post(this.cfg().console, "/api/chat", {
      messages: [{ role: "user", content: text }], session_id: "vscode", model: "deep",
    }, 180000);
  }
  dispatch(project: string, task: string, mode: "build" | "plan"): Promise<any> {
    return this.post(this.cfg().console, "/api/dispatch", { project, task, mode }, 900000);
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
  resume(runId: string, decision: "approved" | "rejected"): Promise<any> {
    return this.post(this.cfg().engine, "/resume", { run_id: runId, decision });
  }

  consoleUrl(): string { return this.cfg().console; }
}

export interface Approval {
  run_id: string;
  type: string;
  action: string;
  by?: string;
  preview?: string;
}
export interface ProjectGoal { goal_id: number; title: string; milestones: number; done: number; total: number; pct: number; }
export interface Task { task_id: number; title: string; status: string; lane?: string; }
export interface Milestone { milestone_id: number; title: string; status: string; pct: number; tasks: Task[]; }
export interface ProjectDetail { goal_id?: number; title?: string; milestones: Milestone[]; done: number; total: number; pct: number; }
