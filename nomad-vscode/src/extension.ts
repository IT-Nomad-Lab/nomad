import * as vscode from "vscode";
import { Api } from "./api";
import { registerCommands } from "./commands";
import { collect, NomadState } from "./state";
import { StatusBar } from "./statusbar";
import { ApprovalsTree, PlatformTree, ProjectTree } from "./trees";

export function activate(ctx: vscode.ExtensionContext) {
  const api = new Api();
  const output = vscode.window.createOutputChannel("NOMAD");
  ctx.subscriptions.push(output);

  let state: NomadState = {
    name: "—", hasMarker: false, git: { isRepo: false }, approvals: [], services: [], loading: false,
  };
  const getState = () => state;

  const projectTree = new ProjectTree(getState);
  const approvalsTree = new ApprovalsTree(getState);
  const platformTree = new PlatformTree(getState);
  const statusBar = new StatusBar();
  ctx.subscriptions.push(statusBar);

  ctx.subscriptions.push(
    vscode.window.registerTreeDataProvider("nomad.project", projectTree),
    vscode.window.registerTreeDataProvider("nomad.approvals", approvalsTree),
    vscode.window.registerTreeDataProvider("nomad.platform", platformTree),
  );

  let inFlight = false;
  const refresh = async () => {
    if (inFlight) return;
    inFlight = true;
    try {
      state = await collect(api);
    } catch (e: any) {
      output.appendLine(`refresh error: ${e?.message || e}`);
    } finally {
      inFlight = false;
    }
    vscode.commands.executeCommand("setContext", "nomad.hasProject", state.hasMarker);
    projectTree.refresh();
    approvalsTree.refresh();
    platformTree.refresh();
    statusBar.update(state);
  };

  registerCommands(ctx, { api, getState, refresh, output });

  // auto-refresh loop (config-driven; 0 disables)
  let timer: NodeJS.Timeout | undefined;
  const arm = () => {
    if (timer) clearInterval(timer);
    const secs = vscode.workspace.getConfiguration("nomad").get<number>("refreshSeconds", 30);
    if (secs && secs > 0) timer = setInterval(refresh, Math.max(5, secs) * 1000);
  };
  ctx.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration("nomad")) { arm(); refresh(); }
    }),
    vscode.workspace.onDidChangeWorkspaceFolders(() => refresh()),
    new vscode.Disposable(() => timer && clearInterval(timer)),
  );

  refresh();
  arm();
}

export function deactivate() {}
