import { execFile } from "child_process";
import { promisify } from "util";

const pexec = promisify(execFile);

export interface GitState {
  isRepo: boolean;
  branch?: string;
  dirty?: number;      // count of changed/untracked files
  ahead?: number;
  behind?: number;
  lastCommit?: string; // "<rel time> · <hash> · <subject>"
}

async function git(cwd: string, args: string[]): Promise<string> {
  const { stdout } = await pexec("git", args, { cwd, timeout: 5000, windowsHide: true });
  return stdout.trim();
}

/** Local git state for the workspace repo. The extension runs in the repo, so this is a cheap
 *  read — no server round-trip. Fail-soft: a non-repo returns { isRepo:false }. */
export async function gitState(cwd: string): Promise<GitState> {
  try {
    await git(cwd, ["rev-parse", "--is-inside-work-tree"]);
  } catch {
    return { isRepo: false };
  }
  const out: GitState = { isRepo: true };
  try { out.branch = await git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"]); } catch { /* detached */ }
  try {
    const porcelain = await git(cwd, ["status", "--porcelain"]);
    out.dirty = porcelain ? porcelain.split("\n").filter((l) => l.trim()).length : 0;
  } catch { /* ignore */ }
  try {
    const lr = await git(cwd, ["rev-list", "--left-right", "--count", "@{u}...HEAD"]);
    const [behind, ahead] = lr.split(/\s+/).map((n) => parseInt(n, 10));
    out.behind = behind || 0;
    out.ahead = ahead || 0;
  } catch { /* no upstream */ }
  try {
    out.lastCommit = await git(cwd, ["log", "-1", "--format=%cr · %h · %s"]);
  } catch { /* empty repo */ }
  return out;
}

/** One-line summary for a status bar / tooltip, e.g. "main ✎3 ↑1". */
export function gitSummary(g: GitState): string {
  if (!g.isRepo) return "";
  const bits = [g.branch || "(detached)"];
  if (g.dirty) bits.push(`✎${g.dirty}`);
  if (g.ahead) bits.push(`↑${g.ahead}`);
  if (g.behind) bits.push(`↓${g.behind}`);
  return bits.join(" ");
}
