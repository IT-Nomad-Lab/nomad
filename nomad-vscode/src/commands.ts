import * as vscode from "vscode";
import { Api } from "./api";
import { NomadState } from "./state";

interface Deps {
  api: Api;
  getState: () => NomadState;
  refresh: () => Promise<void>;
  output: vscode.OutputChannel;
}

async function busy<T>(title: string, fn: () => Promise<T>): Promise<T> {
  return vscode.window.withProgress({ location: vscode.ProgressLocation.Notification, title }, fn);
}

function log(out: vscode.OutputChannel, header: string, body: string) {
  out.appendLine(`\n── ${header} ──`);
  out.appendLine(body.trim());
  out.show(true);
}

export function registerCommands(ctx: vscode.ExtensionContext, d: Deps) {
  const { api, getState, refresh, output } = d;
  const reg = (id: string, fn: (...a: any[]) => any) =>
    ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));

  reg("nomad.refresh", () => refresh());

  reg("nomad.ask", async () => {
    const text = await vscode.window.showInputBox({
      prompt: "Ask NOMAD (chat · intent router · memory)",
      placeHolder: "e.g. run diagnostics · research local TTS engines · what's pending?",
    });
    if (!text) return;
    const r = await busy("NOMAD is thinking…", () => api.chat(text));
    const reply = r.reply || r.error || "(no response)";
    log(output, `you: ${text}`, reply);
    if (r.action) vscode.window.setStatusBarMessage(`NOMAD → ${r.action}`, 4000);
  });

  const dispatch = async (mode: "build" | "plan") => {
    const s = getState();
    if (!s.hasMarker) { vscode.window.showWarningMessage("No Nomad.md project in this workspace."); return; }
    const task = await vscode.window.showInputBox({
      prompt: mode === "build" ? `Dispatch the Builder into ${s.name} (makes uncommitted edits)` : `Plan a task for ${s.name} (read-only)`,
      placeHolder: "describe the task…",
    });
    if (!task) return;
    const r = await busy(mode === "build" ? "Builder working in the repo…" : "Planning…", () => api.dispatch(s.name, task, mode));
    if (r.error || r.ok === false) { log(output, `${mode} failed`, r.error || "dispatch failed"); return; }
    let body = `[${(r.mode || mode).toUpperCase()} · ${r.project || s.name} · ${r.secs ?? "?"}s]\n\n${r.summary || "(done)"}`;
    if (r.mode === "build" && r.changed?.length) body += `\n\n▼ uncommitted (review before committing):\n${r.changed.join("\n")}\n\n${r.diffstat || ""}`;
    log(output, `${mode}: ${task}`, body);
    refresh();
  };
  reg("nomad.dispatchBuild", () => dispatch("build"));
  reg("nomad.dispatchPlan", () => dispatch("plan"));

  reg("nomad.planProject", async () => {
    const s = getState();
    if (!s.hasMarker) { vscode.window.showWarningMessage("No Nomad.md project in this workspace."); return; }
    const ok = await vscode.window.showInformationMessage(
      `Have NOMAD decompose "${s.name}" into milestones + tasks?`, { modal: true }, "Plan");
    if (ok !== "Plan") return;
    const r = await busy("Decomposing the project…", () => api.planProject(s.name));
    log(output, `plan-project: ${s.name}`, r.error ? r.error : JSON.stringify(r, null, 2));
    await refresh();
  });

  reg("nomad.runNextTask", async () => {
    const s = getState();
    if (!s.goal) { vscode.commands.executeCommand("nomad.planProject"); return; }
    const r = await busy("Queuing the next task at the gate…", () => api.runNextTask(s.goal!.goal_id));
    vscode.window.showInformationMessage(r.error ? `Couldn't queue: ${r.error}` : (r.message || r.status || "Next task queued — check the Approval Queue."));
    await refresh();
  });
  // inline play on a backlog task = run the next backlog task through the pipeline
  reg("nomad.taskRun", () => vscode.commands.executeCommand("nomad.runNextTask"));

  const resume = async (node: any, decision: "approved" | "rejected") => {
    const runId = node?.runId;
    if (!runId) { vscode.window.showWarningMessage("No run id on that approval."); return; }
    const r = await busy(decision === "approved" ? "Approving…" : "Rejecting…", () => api.resume(runId, decision));
    vscode.window.showInformationMessage(r.error ? `Gate error: ${r.error}` : `${decision}: ${r.status || node.label2 || "done"}`);
    await refresh();
  };
  reg("nomad.approve", (node) => resume(node, "approved"));
  reg("nomad.reject", (node) => resume(node, "rejected"));

  const taskAct = async (node: any, action: "retry" | "skip") => {
    const taskId = node?.taskId;
    if (!taskId) return;
    const r = await busy(`${action}…`, () => api.taskAction(taskId, action));
    vscode.window.showInformationMessage(r.error ? `Task error: ${r.error}` : `Task ${action}: ${r.status || "ok"}`);
    await refresh();
  };
  reg("nomad.taskRetry", (node) => taskAct(node, "retry"));
  reg("nomad.taskSkip", (node) => taskAct(node, "skip"));

  reg("nomad.openConsole", () => vscode.env.openExternal(vscode.Uri.parse(api.consoleUrl())));
  reg("nomad.openVoice", () => vscode.env.openExternal(vscode.Uri.parse(api.consoleUrl())));

  reg("nomad.actions", async () => {
    const s = getState();
    const picks: Array<vscode.QuickPickItem & { cmd: string }> = [
      { label: "$(comment-discussion) Ask NOMAD…", cmd: "nomad.ask" },
      { label: "$(tools) Dispatch Builder into this repo…", cmd: "nomad.dispatchBuild" },
      { label: "$(list-tree) Plan a task (read-only)…", cmd: "nomad.dispatchPlan" },
      { label: "$(play) Run next task through the pipeline", cmd: "nomad.runNextTask" },
      { label: `$(dashboard) Open command console${s.approvals.length ? ` (${s.approvals.length} pending)` : ""}`, cmd: "nomad.openConsole" },
      { label: "$(refresh) Refresh", cmd: "nomad.refresh" },
    ];
    const pick = await vscode.window.showQuickPick(picks, { placeHolder: `NOMAD · ${s.name}` });
    if (pick) vscode.commands.executeCommand(pick.cmd);
  });
}
