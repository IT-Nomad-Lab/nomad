#!/usr/bin/env python3
"""Smoke test against the REAL local Claude Code (Max login). Costs a few cents of plan quota.

  1. a turn that edits a file          2. a turn that spawns a subagent
  3. a turn that stops at an approval gate, then resumes after "approve"
  plus: session resume on the next turn, and "approve" with nothing pending.

Runs the bridge in-process in a scratch git repo. Exit 0 only if every check passed.
"""
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import runner  # noqa: E402
import server  # noqa: E402
from store import Store  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail else ""), flush=True)


def text(evs):
    return "".join(e.get("text", "") for e in evs if e.get("type") == "delta")


def collect(run, on_gate=None):
    evs = []
    for _, ev in run.stream():
        evs.append(ev)
        if ev.get("type") == "gate" and on_gate:
            on_gate(ev)
    return evs


def main():
    work = tempfile.mkdtemp(prefix="agent-bridge-smoke-")
    subprocess.run(["git", "init", "-q", work], check=True)
    mgr = runner.RunManager(Store(":memory:"))
    app = server.App(store=mgr.store, manager=mgr)
    t0 = time.time()
    import projects
    projects.resolve = lambda project, entries=None: ("smoke", work)   # never touch ~/nomad

    # 1. file edit
    r = mgr.start("smoke-1", work, "smoke", "Create the file smoke.txt in the current directory "
                  "containing exactly: agent-bridge smoke. Use the Write tool, then say done. "
                  "Also remember the secret word MANGO for later.")
    evs = collect(r)
    path = os.path.join(work, "smoke.txt")
    check("1 edit: run finished", evs[-1].get("status") == "done", evs[-1].get("status"))
    check("1 edit: file exists on disk", os.path.isfile(path) and "agent-bridge smoke" in open(path).read())
    check("1 edit: stream showed a Write tool_use and its result", "**Write**" in text(evs) and "✓ done" in text(evs))
    check("1 edit: footer counts tool results", "tool results:" in text(evs))

    # resume: same chat, new turn, context must persist
    r = mgr.start("smoke-1", work, "smoke", "What was the secret word? Answer with the word only.")
    evs = collect(r)
    check("resume: second turn used --resume and kept context", "MANGO" in text(evs).upper(), text(evs)[:120])

    # 2. subagent
    r = mgr.start("smoke-2", work, "smoke", "Use the Task tool to launch a general-purpose subagent "
                  "whose only job is to reply with the single word PINEAPPLE. Then tell me what it replied.")
    evs = collect(r)
    t = text(evs)
    check("2 subagent: Task tool_use shown", "**Task**" in t or "**Agent**" in t)
    check("2 subagent: subagent result reported", "subagent finished" in t)
    check("2 subagent: run finished", evs[-1].get("status") == "done")

    # 3. approval gate: `docker compose up` carries an ask rule in the operator's settings
    check("3 gate: approve with nothing pending says so",
          "nothing is pending" in text(list(app.chat("smoke-3", "general", "approve"))))
    marker = []
    r = mgr.start("smoke-3", work, "smoke", "Run exactly this with Bash and report its first line: "
                  "docker compose up --help")
    waiting = {}
    for _, ev in r.stream():
        if ev.get("type") == "gate":
            waiting = ev
            marker.append(("pending", [p.run_id for p in mgr.gates.pending("smoke-3")], r.status))
            break
    check("3 gate: run stopped at a gate with a run id", bool(waiting.get("run_id")), str(waiting)[:140])
    check("3 gate: it is pending while the process is blocked",
          bool(marker) and marker[0][1] == [r.run_id] and marker[0][2] == "waiting", str(marker))
    if waiting:
        dec = mgr.gates.decide(waiting["run_id"], "approve", chat_id="smoke-3")
        check("3 gate: approve acted on the real pending request", dec.get("ok"), str(dec))
        rest = [ev for _, ev in r.stream(r.delivered)] if False else collect(r)
        t3 = text(rest)
        check("3 gate: run resumed and finished after approve", rest[-1].get("status") == "done", rest[-1].get("status"))
        check("3 gate: command result came from a tool_result", "✓ done" in t3 or "Usage" in t3)
        again = mgr.gates.decide(waiting["run_id"], "approve", chat_id="smoke-3")
        check("3 gate: a second approve finds nothing pending", not again["ok"])

    # 4. fallback: Claude Code marked unavailable -> the same chat flow runs on real Codex CLI
    mgr.be.mark_unavailable("claude", "smoke: simulated quota")
    evs = list(app.chat("smoke-4", "general", "Create the file codex.txt in the current directory "
                        "containing exactly: from codex. Use a shell command, then say done."))
    t4 = text(evs)
    done = evs[-1]
    check("4 fallback: ran on Codex and the label says so", done.get("backend") == "codex"
          and "Codex CLI" in t4, done.get("agent", "")[:100])
    check("4 fallback: Codex edited the file on disk",
          os.path.isfile(os.path.join(work, "codex.txt")) and "from codex" in open(os.path.join(work, "codex.txt")).read())
    check("4 fallback: shell result came from a tool_result", "✓ done" in t4 and "**Bash**" in t4)
    check("4 fallback: uncommitted change listed under Pending (sandbox can't commit)",
          "Commit the changes" in t4, t4[-200:].replace("\n", " "))
    print("   model reported by Codex:", done.get("model") or "(not found)")
    mgr.be.cool.clear()

    mgr.stop(chat_id="smoke-1")
    print(f"\n{sum(results)}/{len(results)} checks passed in {time.time() - t0:.0f}s; scratch repo {work}")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
