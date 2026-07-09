import * as vscode from "vscode";
import { Approval, Milestone, Task } from "./api";
import { gitSummary } from "./git";
import { NomadState } from "./state";

type Node = vscode.TreeItem & { children?: Node[] };

const { TreeItem, TreeItemCollapsibleState, ThemeIcon, ThemeColor } = vscode;

function taskIcon(status: string): vscode.ThemeIcon {
  const s = (status || "").toLowerCase();
  if (["done", "executed", "complete", "completed"].includes(s)) return new ThemeIcon("pass", new ThemeColor("charts.green"));
  if (["blocked", "failed", "declined"].includes(s)) return new ThemeIcon("error", new ThemeColor("charts.red"));
  if (["in progress", "executing", "awaiting-approval", "awaiting approval"].includes(s)) return new ThemeIcon("sync", new ThemeColor("charts.yellow"));
  if (["skipped"].includes(s)) return new ThemeIcon("debug-step-over", new ThemeColor("disabledForeground"));
  return new ThemeIcon("circle-outline");
}

function taskContext(status: string): string {
  const s = (status || "").toLowerCase();
  if (["backlog", ""].includes(s)) return "task-backlog";
  if (["blocked", "failed", "declined"].includes(s)) return "task-blocked";
  return "task";
}

/** "This Project" — header (project + git + progress), then milestones → tasks. */
export class ProjectTree implements vscode.TreeDataProvider<Node> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  constructor(private getState: () => NomadState) {}
  refresh() { this._onDidChange.fire(); }
  getTreeItem(n: Node) { return n; }

  getChildren(n?: Node): Node[] {
    if (n) return n.children || [];
    const s = this.getState();
    if (!s.root || !s.hasMarker) return [];   // viewsWelcome takes over
    const nodes: Node[] = [];

    const header = new TreeItem(s.name, TreeItemCollapsibleState.None) as Node;
    header.iconPath = new ThemeIcon("broadcast");
    const bits: string[] = [];
    if (s.status) bits.push(s.status);
    if (s.git.isRepo) bits.push(gitSummary(s.git));
    if (s.detail && s.detail.total) bits.push(`${s.detail.pct}% (${s.detail.done}/${s.detail.total})`);
    header.description = bits.join("  ·  ");
    header.tooltip = new vscode.MarkdownString(
      [`**${s.name}**`, s.lane && `lane: ${s.lane}`, s.stack && `stack: ${s.stack}`,
       s.git.lastCommit && `\n\n$(git-commit) ${s.git.lastCommit}`].filter(Boolean).join("\n\n"));
    header.tooltip.supportThemeIcons = true;
    nodes.push(header);

    if (!s.goal) {
      const plan = new TreeItem("No task plan yet — Plan this project", TreeItemCollapsibleState.None) as Node;
      plan.iconPath = new ThemeIcon("list-tree");
      plan.command = { command: "nomad.planProject", title: "Plan project" };
      nodes.push(plan);
      return nodes;
    }

    for (const m of s.detail?.milestones || []) {
      nodes.push(this.milestoneNode(m));
    }
    if (!(s.detail?.milestones || []).length) {
      const empty = new TreeItem("Plan exists but no milestones returned", TreeItemCollapsibleState.None) as Node;
      empty.iconPath = new ThemeIcon("info");
      nodes.push(empty);
    }
    return nodes;
  }

  private milestoneNode(m: Milestone): Node {
    const item = new TreeItem(m.title || "(milestone)", TreeItemCollapsibleState.Collapsed) as Node;
    item.description = `${m.pct}%`;
    item.iconPath = new ThemeIcon("milestone");
    item.children = (m.tasks || []).map((t) => this.taskNode(t));
    return item;
  }

  private taskNode(t: Task): Node {
    const item = new TreeItem(t.title || "(task)", TreeItemCollapsibleState.None) as Node;
    item.description = [t.status, t.lane].filter(Boolean).join(" · ");
    item.iconPath = taskIcon(t.status);
    item.contextValue = taskContext(t.status);
    (item as any).taskId = t.task_id;
    return item;
  }
}

/** "Approval Queue" — global pending gate items with inline approve/reject. */
export class ApprovalsTree implements vscode.TreeDataProvider<Node> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  constructor(private getState: () => NomadState) {}
  refresh() { this._onDidChange.fire(); }
  getTreeItem(n: Node) { return n; }

  getChildren(): Node[] {
    const s = this.getState();
    if (!s.approvals.length) {
      const none = new TreeItem("Nothing awaiting approval", TreeItemCollapsibleState.None) as Node;
      none.iconPath = new ThemeIcon("check-all", new ThemeColor("charts.green"));
      return [none];
    }
    return s.approvals.map((a: Approval) => {
      const item = new TreeItem(a.action || "action", TreeItemCollapsibleState.None) as Node;
      item.description = a.type || "";
      item.iconPath = new ThemeIcon("warning", new ThemeColor("charts.orange"));
      item.contextValue = "approval";
      item.tooltip = new vscode.MarkdownString(
        [`**${a.action}**`, a.by && `by ${a.by}`, a.preview && `\n\n${a.preview}`].filter(Boolean).join("\n\n"));
      (item as any).runId = a.run_id;
      (item as any).label2 = a.action;
      return item;
    });
  }
}

/** "Platform" — NOMAD service health. */
export class PlatformTree implements vscode.TreeDataProvider<Node> {
  private _onDidChange = new vscode.EventEmitter<void>();
  readonly onDidChangeTreeData = this._onDidChange.event;
  constructor(private getState: () => NomadState) {}
  refresh() { this._onDidChange.fire(); }
  getTreeItem(n: Node) { return n; }

  getChildren(): Node[] {
    const s = this.getState();
    if (!s.services.length) {
      const item = new TreeItem("console unreachable", TreeItemCollapsibleState.None) as Node;
      item.iconPath = new ThemeIcon("debug-disconnect", new ThemeColor("charts.red"));
      item.command = { command: "nomad.openConsole", title: "Open console" };
      return [item];
    }
    return s.services.map((svc) => {
      const item = new TreeItem(svc.name, TreeItemCollapsibleState.None) as Node;
      item.description = svc.up ? `${svc.ms} ms` : "offline";
      item.iconPath = svc.up
        ? new ThemeIcon("pass", new ThemeColor("charts.green"))
        : new ThemeIcon("error", new ThemeColor("charts.red"));
      return item;
    });
  }
}
