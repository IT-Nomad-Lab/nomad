import * as vscode from "vscode";
import * as path from "path";
import { Api, Approval, ProjectDetail, ProjectGoal } from "./api";
import { GitState, gitState } from "./git";

const MARKERS = ["Nomad.md", "nomad.md", "NOMAD.md"];

export interface NomadState {
  root?: string;            // workspace folder path
  name: string;             // project name (Nomad.md `name:` or folder basename)
  status?: string;          // Nomad.md `status:`
  lane?: string;
  stack?: string;
  hasMarker: boolean;
  git: GitState;
  goal?: ProjectGoal;       // matched mission-control goal (if any)
  detail?: ProjectDetail;   // milestones + tasks (if a plan exists)
  approvals: Approval[];    // global pending gate (few; shown for quick action)
  services: Array<{ name: string; up: boolean; ms: number }>;
  loading: boolean;
}

function parseFrontmatter(text: string): Record<string, string> {
  const m = text.match(/^---\s*\n([\s\S]*?)\n---/);
  const out: Record<string, string> = {};
  if (!m) return out;
  for (const line of m[1].split("\n")) {
    const kv = line.match(/^\s*([A-Za-z0-9_-]+)\s*:\s*(.+?)\s*$/);
    if (kv) out[kv[1].toLowerCase()] = kv[2].replace(/^["']|["']$/g, "");
  }
  return out;
}

async function readMarker(root: string): Promise<Record<string, string> | null> {
  for (const name of MARKERS) {
    try {
      const uri = vscode.Uri.file(path.join(root, name));
      const buf = await vscode.workspace.fs.readFile(uri);
      return parseFrontmatter(Buffer.from(buf).toString("utf8"));
    } catch { /* try next */ }
  }
  return null;
}

/** Match this project's name to a mission-control goal. Loose, case-insensitive: a repo "civis"
 *  matches the goal "CIVIS Outreach Playbook", and vice-versa. Longest containment wins. */
function matchGoal(name: string, goals: ProjectGoal[]): ProjectGoal | undefined {
  const n = name.toLowerCase().trim();
  let best: ProjectGoal | undefined;
  for (const g of goals) {
    const t = (g.title || "").toLowerCase();
    if (t === n || t.includes(n) || n.includes(t)) {
      if (!best || (g.title || "").length > (best.title || "").length) best = g;
    }
  }
  return best;
}

export async function collect(api: Api): Promise<NomadState> {
  const folder = vscode.workspace.workspaceFolders?.[0];
  const root = folder?.uri.fsPath;
  const st: NomadState = {
    root, name: root ? path.basename(root) : "—",
    hasMarker: false, git: { isRepo: false }, approvals: [], services: [], loading: false,
  };
  if (!root) return st;

  const fm = await readMarker(root);
  if (fm) {
    st.hasMarker = true;
    if (fm.name) st.name = fm.name;
    st.status = fm.status; st.lane = fm.lane; st.stack = fm.stack;
  }

  // gather in parallel — every call is fail-soft
  const [git, goalsResp, approvalsResp, services] = await Promise.all([
    gitState(root),
    api.projectGoals(),
    api.approvals(),
    api.services(),
  ]);
  st.git = git;
  st.approvals = approvalsResp.approvals || [];
  st.services = services || [];

  st.goal = matchGoal(st.name, goalsResp.projects || []);
  if (st.goal) st.detail = await api.projectStatus(st.goal.goal_id);
  return st;
}
