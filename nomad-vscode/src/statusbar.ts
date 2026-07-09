import * as vscode from "vscode";
import { NomadState } from "./state";

/** A glanceable status-bar item: NOMAD · <project> <pct>% · <n>⚑. Click → NOMAD: Actions… */
export class StatusBar {
  private item: vscode.StatusBarItem;
  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    this.item.command = "nomad.actions";
    this.item.name = "NOMAD";
  }

  update(s: NomadState) {
    if (!s.root || !s.hasMarker) { this.item.hide(); return; }
    const parts = [`$(broadcast) ${s.name}`];
    if (s.detail && s.detail.total) parts.push(`${s.detail.pct}%`);
    const n = s.approvals.length;
    if (n) parts.push(`$(warning) ${n}`);
    this.item.text = parts.join(" · ");
    const down = s.services.filter((x) => !x.up).length;
    this.item.tooltip = new vscode.MarkdownString(
      [`**NOMAD** — ${s.name}`,
       s.git.isRepo && `${s.git.branch || "(detached)"}${s.git.dirty ? ` · ✎${s.git.dirty}` : ""}`,
       s.detail && s.detail.total ? `tasks ${s.detail.done}/${s.detail.total}` : "no task plan",
       n ? `**${n} awaiting approval**` : "gate clear",
       down ? `**${down} service(s) offline**` : "platform nominal",
       "\n\nClick for actions"].filter(Boolean).join("  ·  "));
    this.item.backgroundColor = (n || down)
      ? new vscode.ThemeColor("statusBarItem.warningBackground") : undefined;
    this.item.show();
  }

  dispose() { this.item.dispose(); }
}
