"""Drafting missing acceptance criteria for New tasks (PRD 5.2, nomad-pm #889).

A New task without acceptance criteria (Selection kind `criteria`) gets ONE cheap headless
Claude run that reads the task and proposes criteria from its description. The run writes
nothing: its settings (permissions.write_criteria) allow only nomad-pm reads, and it answers in
its final message. The supervisor posts the answer as a comment tagged `autopilot: criteria
draft` (tasks.DRAFT_MARK), which is also the rate limit: a task with such a comment, or with a
draft log on disk, is never drafted again.

What a draft does (PRD 5.2: drafted criteria are "logged as a delegated decision, unless writing
them means a reserved decision"):
  CRITERIA   comment `autopilot: criteria draft` + `Delegated decision: ...` + the criteria.
             The task qualifies on the next pass (tasks.classify), and the worker is told the
             criteria are in that comment. No operator OK is needed.
  RESERVED   comment `autopilot: criteria draft needs operator. <reason>`, and the task moves
             to Awaiting approval: only the operator moves it on.
  anything else, a crash or the time limit
             comment `autopilot: criteria draft failed. <why>`; the task waits on the operator
             (kind operator), and is not drafted again.

Bounds:
  - one live draft per project, one launch per pass, only when the pass may start workers
    (ctx.slots > 0: not budget paused, a slot free);
  - `--max-turns` DRAFT_TURNS, a time limit of DRAFT_SECS, a cheaper model (DRAFT_MODEL);
  - the run's stream-json log is runs/<project>/criteria-<task>-<ts>.jsonl, next to the
    workers' logs, so budget.Budget.reconcile counts its total_cost_usd like any run.

A supervisor restart loses the live-run table, not the logs: a draft log for a task with no
draft comment is posted from (its result, if it has one) once it has ended, never relaunched.

Launch style is the worker's (worker.py): `claude -p <prompt> --settings <file>
--setting-sources "" --permission-mode dontAsk`, own process group, ANTHROPIC_API_KEY blanked.
"""
import glob
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass

try:
    from . import permissions as perms
    from . import tasks as tk
    from . import worker as wk
except ImportError:  # imported as a top-level module
    import permissions as perms
    import tasks as tk
    import worker as wk

log = logging.getLogger("autopilot.criteria")

DRAFT_SECS = float(os.environ.get("NOMAD_AUTOPILOT_CRITERIA_SECS", "600"))
DRAFT_TURNS = int(os.environ.get("NOMAD_AUTOPILOT_CRITERIA_TURNS", "8"))
DRAFT_MODEL = os.environ.get("NOMAD_AUTOPILOT_CRITERIA_MODEL", "sonnet")
MAX_CRITERIA = 10

RESERVED = """\
- story canon and naming (characters, places, product and brand names, lore);
- art direction, and any generated image, model, audio or video that ships;
- money, licences, external services and accounts;
- push, deleting anything, any force operation;
- edits to agent config (AGENTS.md, CLAUDE.md, AUTONOMY.md, .claude/, permission settings);
- architecture changes (new or merged modules, changed layering or public interfaces, a new
  framework or datastore, a schema change that drops data);
- publishing, deploying, or sending anything outside this machine;
- anything irreversible."""


def build_prompt(project_id, task):
    """The drafting prompt. `task` is a tasks.Task."""
    return f"""You draft acceptance criteria for one nomad-pm task of project `{project_id}`. You
cannot write anything: you only read and answer. The NOMAD autopilot posts your answer.

Task #{task.id} ({task.type}, {task.status}): {task.subject}

Description:
<<<
{task.description.strip() or "(empty)"}
>>>

You may `get` #{task.id} and its parent with the nomad-pm tools for context. Do nothing else.

Write 2 to {MAX_CRITERIA} acceptance criteria that say how to check the description's intent is met:
observable, testable, each one line, and derived only from the description and its parent. Do
not add scope. Do not make product decisions the text leaves open.

If you cannot write them without choosing something reserved for the operator, or the
description is too thin to say what "done" is without guessing product behaviour, answer
RESERVED instead. Reserved for the operator:
{RESERVED}

Your final message is exactly one of these two forms, nothing before or after:

CRITERIA
- <criterion>
- <criterion>

RESERVED: <one sentence: which decision is needed and why>
"""


_BULLET = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+(?P<text>\S.*)$")


def parse_answer(text):
    """("drafted", [criteria]) | ("reserved", reason) | ("failed", why) from a final message."""
    text = (text or "").strip()
    if not text:
        return "failed", "the run gave no answer"
    m = re.search(r"^\s*\**RESERVED\**\s*:\s*(?P<why>.+)$", text, re.M | re.S)
    if m:
        why = " ".join(m["why"].split())
        return "reserved", why[:600] or "no reason given"
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().strip("*#: ").upper() == "CRITERIA":
            out = []
            for nxt in lines[i + 1:]:
                b = _BULLET.match(nxt)
                if b:
                    out.append(" ".join(b["text"].split())[:400])
                elif nxt.strip():
                    break
            if out:
                return "drafted", out[:MAX_CRITERIA]
            return "failed", "CRITERIA with no criteria under it"
    return "failed", "the answer was neither CRITERIA nor RESERVED: " + " ".join(text.split())[:200]


def comment_for(state, body):
    """The tracker comment for a parsed answer. Starts with tasks.DRAFT_MARK in every case."""
    if state == "drafted":
        items = "\n".join(f"- {c}" for c in body)
        return (f"{tk.DRAFT_MARK}\n\n"
                "Delegated decision: the autopilot drafted this task's acceptance criteria from "
                "its description (PRD 5.2). A worker builds to them. To change them, edit the "
                "description's acceptance criteria or move the task out of New.\n\n"
                f"**Acceptance criteria**\n{items}\n")
    if state == "reserved":
        return (f"{tk.DRAFT_MARK} needs operator. Writing acceptance criteria for this task "
                f"needs a decision reserved for the operator (PRD 5.6): {body} "
                "Moved to Awaiting approval. Add the criteria to the description, then move it "
                "back to New or Ready.")
    return (f"{tk.DRAFT_MARK} failed. {body}. The autopilot drafts once per task; add "
            "acceptance criteria to the description and the task qualifies.")


@dataclass
class DraftRun:
    project: str
    task: int
    log: str
    proc: object            # subprocess.Popen, or anything with poll() and pid
    started: float
    deadline: float
    killed: bool = False


class CriteriaDrafter:
    """pm: the nomad-pm client (comment, and transition for a reserved draft); claude: the CLI;
    state_dir: where settings and runs/<project>/ logs go (the WorkerPool's, so the budget sees
    them); popen: subprocess.Popen (tests pass a fake)."""

    def __init__(self, pm, claude=None, state_dir=None, run_secs=None, model=None,
                 max_turns=None, popen=None):
        self.pm = pm
        self.claude = claude or wk.CLAUDE
        self.state_dir = state_dir or wk.STATE_DIR
        self.run_secs = DRAFT_SECS if run_secs is None else run_secs
        self.model = DRAFT_MODEL if model is None else model
        self.max_turns = DRAFT_TURNS if max_turns is None else max_turns
        self.popen = popen or subprocess.Popen
        self.runs = {}          # (project, task id) -> DraftRun

    def _runs_dir(self, pid):
        return os.path.join(self.state_dir, "runs", pid)

    def logs(self, pid, tid):
        return sorted(glob.glob(os.path.join(self._runs_dir(pid), f"criteria-{tid}-*.jsonl")))

    def pending(self, pid):
        return sorted(t for (p, t) in self.runs if p == pid)

    # -- launch --

    def launch(self, pid, task):
        settings = perms.write_criteria(self.state_dir)
        d = self._runs_dir(pid)
        os.makedirs(d, exist_ok=True)
        cwd = os.path.join(self.state_dir, "criteria")
        os.makedirs(cwd, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(d, f"criteria-{task.id}-{stamp}.jsonl")
        cmd = [self.claude, "-p", build_prompt(pid, task), *perms.cli_args(settings),
               "--max-turns", str(self.max_turns), "--verbose", "--output-format", "stream-json"]
        if self.model:
            cmd += ["--model", self.model]
        with open(log_path, "wb") as out, open(log_path[:-len(".jsonl")] + ".stderr", "wb") as err:
            proc = self.popen(cmd, cwd=cwd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                              start_new_session=True, env={**os.environ, "ANTHROPIC_API_KEY": ""})
        now = time.time()
        run = DraftRun(pid, task.id, log_path, proc, now, now + self.run_secs)
        self.runs[(pid, task.id)] = run
        return run

    def draft(self, ctx, selection):
        """Start at most one draft for ctx's project: the first `criteria` task with no draft
        comment, no live draft and no earlier draft log. An earlier log that has ended (a draft
        from before a restart) is posted instead of relaunching. Returns the task id started,
        or None."""
        pid = ctx.project_id
        if self.pending(pid):
            return None
        for s in selection.of_kind("criteria"):
            t = s.task
            if t.draft is not None:
                continue
            earlier = self.logs(pid, t.id)
            if earlier:
                self._post_orphan(ctx, t.id, earlier[-1])
                continue
            try:
                self.launch(pid, t)
            except (OSError, ValueError) as e:
                ctx.event("error", f"task {t.id}: criteria draft not started: {e}")
                return None
            ctx.event("criteria", f"task {t.id}: drafting acceptance criteria")
            return t.id
        return None

    def _post_orphan(self, ctx, tid, path):
        """A draft log with no live run and no comment: post it once it has ended."""
        parsed = wk.parse_stream(path)
        if parsed["result"] is None:
            try:
                age = time.time() - os.path.getmtime(path)
            except OSError:
                age = self.run_secs
            if age < self.run_secs:
                return                    # may still be running (started before a restart)
            self._post(ctx, tid, ("failed", "the draft run ended without a result"))
            return
        self._post(ctx, tid, parse_answer(parsed["result"]))

    # -- collect --

    def collect(self, ctx, now=None):
        """Post every draft of ctx's project that ended; kill one past its time limit (it
        counts as failed). Returns [(task id, state)]."""
        now = time.time() if now is None else now
        done = []
        for key, run in list(self.runs.items()):
            if run.project != ctx.project_id:
                continue
            if run.proc.poll() is None and now >= run.deadline:
                run.killed = True
                wk._kill_group(run.proc)
            if run.proc.poll() is None:
                continue
            del self.runs[key]
            parsed = wk.parse_stream(run.log)
            if run.killed:
                answer = ("failed", f"the draft run hit its {int(self.run_secs)} s time limit")
            elif parsed["result"] is None:
                answer = ("failed", "the draft run ended without a result")
            else:
                answer = parse_answer(parsed["result"])
            self._post(ctx, run.task, answer)
            done.append((run.task, answer[0]))
        return done

    def _post(self, ctx, tid, answer):
        state, body = answer
        try:
            self.pm.comment(int(tid), comment_for(state, body))
        except Exception as e:
            ctx.event("error", f"task {tid}: criteria draft not posted: {type(e).__name__}: {e}")
            return
        if state == "reserved":
            move = getattr(self.pm, "transition", None)
            try:
                if move is None:
                    raise AttributeError("the nomad-pm client cannot move tasks")
                move(int(tid), "Awaiting approval")
            except Exception as e:
                ctx.event("error", f"task {tid}: not moved to Awaiting approval: "
                                   f"{type(e).__name__}: {e}")
        ctx.event("criteria", f"task {tid}: criteria draft {state}")
