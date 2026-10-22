"""Which of a project's nomad-pm tasks the autopilot may work, and in what order (PRD 5.2, #865).

One read of the project's board gives every open work package with its status, type, priority,
lane, categories, description and `blocked_by` relations (with each blocker's status). From that:

A task QUALIFIES when all of these hold:
- status is Ready; or New with acceptance criteria (see `has_acceptance`); or In Progress with
  `autopilot: resume` as its latest autopilot comment (an earlier run left work on its branch);
  or Approved / Changes requested: the operator answered a worker's question (see below);
- no open `blocked_by` relation (a closed blocker does not count);
- not Awaiting approval, not Blocked (both are outside the statuses above);
- a buildable type: Task, Bug or Sub-task;
- not in the `manager` lane and not tagged `human` (PRD 9.2, see `is_human`);
- in one of the project's `autopilot.lanes`, when it lists any (#1001; see "Shared boards");
- not already held by one of the project's live workers.

ORDER: `autopilot: resume` and answered (Approved / Changes requested) tasks first, then priority (Immediate, High, Normal, Low), then board
order. Board order is the work package id, ascending: the order tasks were written onto the board,
which is also how `create_plan` lays a plan out.

Every task that does not qualify is returned too, with a `kind` and a human reason, because the
supervisor's `waiting` and `needs_you` states are made from them:
    waiting       an open blocker (`blocked_by #x (Status)`)
    operator      Awaiting approval, or Blocked with no open blocker: only the operator moves it
    person        manager lane or tagged human: the operator's own work, never an agent's
    lane          outside this project's `autopilot.lanes`: another project on the board takes it
    unrouted      outside every lane the board's projects take (reported by one project only)
    criteria      New without acceptance criteria and no draft yet (the autopilot drafts them,
                  PRD 5.2, see autopilot/criteria.py)
    running       held by a live worker
    other         not buildable (Feature, Epic, ...), Backlog, In Review, In Progress without resume

The operator's answer (docs/prd/autopilot-approval.md). A worker that needs a reserved decision
moves its task to Awaiting approval with its question. The operator answers by moving it to
Approved or Changes requested (in OpenProject, by an `approve` / `reject` comment, or from the
cockpit; only the operator's roles have that move). The task then qualifies, and `answer` is every
comment from the operator (or relayed for them with a `**Operator, via ...:**` prefix, the prefix
dropped) since the task last moved to Awaiting approval. The worker gets it in its prompt.

How `human` is represented. OpenProject has no free tags. A task is tagged human when either
- it has the work package Category `human` (case-insensitive). Categories are OpenProject's
  per-project labels; the operator adds a `human` category to a board once; or
- its subject carries the token `[human]`, which any agent can add with `update_fields`,
  since the agents' Member role cannot create categories.

Acceptance criteria heuristic (`has_acceptance`): the description has a label line reading
"Acceptance" (optionally "criteria", "tests" or "checks", and a parenthesis), "Done means" or
"Definition of done", written as a markdown heading, in bold, or followed by a colon, AND the
label has content: text after it on the same line (`**Acceptance criteria:** WSEQ-15.`) or a
non-empty line below it before the next heading. Prose that merely mentions acceptance does not
count, and neither does an empty section.

Criteria drafts (#889, `criteria_draft`). A New task without criteria gets one drafting run
(autopilot/criteria.py), which leaves a comment starting `autopilot: criteria draft`:
    autopilot: criteria draft                  + an acceptance section: the task qualifies (the
                                                 draft is a delegated decision, PRD 5.2)
    autopilot: criteria draft needs operator   writing them needs a reserved decision; the task
                                                 is moved to Awaiting approval (kind operator)
    autopilot: criteria draft failed           no usable draft; the operator writes them (kind
                                                 operator). Never drafted twice.

Shared boards (#1001). Several projects can read one board (BOARDS: content-creation-app and
-backend share `content-creation`). A project's optional `autopilot.lanes` limits it to tasks in
those lanes (case-insensitive); without it the project takes every lane, as before. When every
project on a board lists lanes, a task in none of them would be picked by no one: the first of
those projects by id reports it as `unrouted`, so it shows once, not once per project.
"""
import importlib.util
import os
import re
from dataclasses import dataclass, field

NOMAD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BUILDABLE = {"Task", "Bug", "Sub-task"}
PERSON_LANES = {"manager"}
HUMAN_TAG = "human"
PRIORITY_RANK = {"Immediate": 0, "High": 1, "Normal": 2, "Low": 3}   # lower runs first
DEFAULT_RANK = PRIORITY_RANK["Normal"]
OPERATOR_HOLD = {"Awaiting approval", "Blocked"}
# The operator's answer to Awaiting approval -> what the worker is told happened.
DECIDED = {"Approved": "approved", "Changes requested": "changes requested"}
OPERATOR_ID = int(os.environ.get("NOMAD_PM_OPERATOR_ID") or 4)   # same default as the engine
CLOSED = {"Done", "Won't do", "Duplicate", "Closed", "Rejected"}      # fallback; the client asks OP

# projects.yaml id -> nomad-pm board identifier, where they differ. Same table as
# docs/SDLC.md "Boards"; any id not listed is its own board identifier (what the nomad-pm MCP's
# `projects()` lists). None = the project has no board of its own.
BOARDS = {
    "nomad": "nomad-work",
    "civis-poc": "civis",
    "civis-site": "civis",
    "shift-poc": "shift",
    "crypto-trading-bot": "crypto",
    "content-creation-app": "content-creation",
    "content-creation-backend": "content-creation",
    "nomad-public": None,
}

_ACCEPT_LABEL = re.compile(
    r"^\s*(?P<head>#{1,6}\s*)?(?P<bold>[*_]{1,2})?\s*"
    r"(?:acceptance(?:\s+(?:criteria|tests?|checks?))?|done\s+means|definition\s+of\s+done)"
    r"\s*(?:\([^)]*\))?\s*(?P<c1>:)?\s*(?:[*_]{1,2})?\s*(?P<c2>:)?\s*(?P<rest>.*)$", re.I)
_HEADING = re.compile(r"^\s*(#{1,6}\s|\*\*[^*]+\*\*\s*$)")
_AUTOPILOT = re.compile(r"^\s*autopilot:\s*(.*)", re.I | re.S)
_RESUME = re.compile(r"^(result\s+)?resume\b", re.I)
DRAFT_MARK = "autopilot: criteria draft"
# A comment the engine posted for the operator: a Zulip reply, or the cockpit's Approve / Reject.
_RELAYED = re.compile(r"^\s*\*\*Operator, via [^*:]+:\*\*\s*", re.I)
_STATUS = re.compile(r"^\W*Status\W*\s+(?:changed from\s+\W*(.+?)\W*\s+to|set to)\s+\W*(.+?)\W*$")
_DRAFT = re.compile(r"^\s*autopilot:\s*criteria\s+draft\b\s*(?P<tail>.*)", re.I | re.S)


def board_for(project_id):
    """The nomad-pm board identifier for a projects.yaml id, or None if it has none."""
    return BOARDS.get(project_id, project_id)


def board_projects(board):
    """Every id that may read `board`, sorted: the BOARDS ids mapped to it, plus the board's own
    name (a project id only if projects.yaml has it; callers check with `known`)."""
    ids = {pid for pid, b in BOARDS.items() if b == board}
    if board_for(board) == board:
        ids.add(board)
    return sorted(ids)


def has_acceptance(description):
    """True when the description holds an acceptance-criteria section with content (see the
    module docstring for the exact rule)."""
    lines = (description or "").splitlines()
    for i, line in enumerate(lines):
        m = _ACCEPT_LABEL.match(line)
        if not m or not (m["head"] or m["bold"] or m["c1"] or m["c2"]):
            continue            # a label is a heading, bold, or ends in a colon; not prose
        rest = m["rest"].strip(" *_:")
        if rest:
            return True
        for nxt in lines[i + 1:]:
            if not nxt.strip():
                continue
            if _HEADING.match(nxt):
                break
            return True
    return False


def is_human(subject, categories):
    return (any(c.strip().lower() == HUMAN_TAG for c in categories)
            or f"[{HUMAN_TAG}]" in (subject or "").lower())


def resume_requested(comments):
    """True when the latest comment starting `autopilot:` is a resume (`autopilot: resume ...` or
    `autopilot: result resume ...`). A later `autopilot: started` or result clears it."""
    last = None
    for c in comments:
        m = _AUTOPILOT.match(c or "")
        if m:
            last = m.group(1)
    return bool(last is not None and _RESUME.match(last))


def _moved_to(act):
    """The status an activity moved the task to, or None."""
    for d in act.get("details") or []:
        m = _STATUS.match((d.get("raw") or "").strip())
        if m:
            return m.group(2)
    return None


def operator_answer(activities, operator_id=OPERATOR_ID, manager_id=None):
    """The operator's comments since the task last moved to Awaiting approval, oldest first and
    joined by blank lines. A comment counts when the operator wrote it, or when the engine's
    account (`manager_id`) wrote it with the `**Operator, via ...:**` prefix (the prefix is
    dropped). The prefix from anyone else is plain text: any agent could type it. "" if there are
    none, or the task never went through Awaiting approval."""
    start = None
    for i, a in enumerate(activities):
        if _moved_to(a) == "Awaiting approval":
            start = i
    if start is None:
        return ""
    out = []
    for a in activities[start:]:
        raw = ((a.get("comment") or {}).get("raw") or "").strip()
        if not raw:
            continue
        author = _href_id((a.get("_links") or {}).get("user"))
        relayed = _RELAYED.match(raw) if manager_id and author == manager_id else None
        if relayed:
            out.append(raw[relayed.end():].strip())
        elif author == operator_id:
            out.append(raw)
    return "\n\n".join(t for t in out if t)


# ---- grants (#979, docs/prd/ap-permission-grants.md 5.2 and 5.3) ------------------------------

GRANT_ACTIONS = ("edit", "delete branch", "delete worktree", "merge")
APPROVED_ONLY = ("delete branch", "delete worktree", "merge")   # count only in the Approved answer
_GRANT = re.compile(r"^\s*grant:\s+(edit|delete\s+branch|delete\s+worktree|merge)\s+(\S+)\s*$", re.I)
_GRANT_LINE = re.compile(r"^\s*grant:", re.I)
_GRANT_ANYWHERE = re.compile(r"^\s*grant:", re.I | re.M)
_FENCE = re.compile(r"^\s*(```|~~~)")


@dataclass(frozen=True)
class Grant:
    """One `grant:` line the operator wrote: `action` is one of GRANT_ACTIONS, `arg` exactly as
    written. `activity_id` names the comment; a grant is used once, keyed by `key`."""
    action: str
    arg: str
    activity_id: int = None
    created: str = ""       # the comment's createdAt (ISO 8601)

    @property
    def key(self):
        return f"{self.activity_id}:{self.action}:{self.arg}"

    @property
    def line(self):
        return f"grant: {self.action} {self.arg}"

    def as_dict(self):
        return {"action": self.action, "arg": self.arg, "activity_id": self.activity_id,
                "created": self.created}

    @classmethod
    def of(cls, d):
        return d if isinstance(d, cls) else cls(d["action"], d["arg"], d.get("activity_id"),
                                                 d.get("created") or "")


def grant_lines(text):
    """[(line, Grant or None, reason)] for every `grant:` line of a comment outside fenced code
    and quotes. A line that isn't exactly one of the four forms comes back with None and why."""
    out, fenced = [], False
    for line in (text or "").splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced or line.lstrip().startswith(">") or not _GRANT_LINE.match(line):
            continue
        m = _GRANT.match(line)
        if m:
            out.append((line.strip(), Grant(" ".join(m.group(1).lower().split()), m.group(2)), ""))
        else:
            out.append((line.strip(), None, "not one of `grant: edit <path>`, `grant: delete "
                                             "branch <name>`, `grant: delete worktree <path>`, "
                                             "`grant: merge <branch>`"))
    return out


def _counted_text(a, operator_id, manager_id):
    """The comment text when the operator wrote it (or the engine relayed it for them, prefix
    dropped), else None. The same rule as operator_answer."""
    raw = ((a.get("comment") or {}).get("raw") or "").strip()
    if not raw:
        return None
    author = _href_id((a.get("_links") or {}).get("user"))
    relayed = _RELAYED.match(raw) if manager_id and author == manager_id else None
    if relayed:
        return raw[relayed.end():].strip()
    return raw if author == operator_id else None


def task_grants(activities, approved, operator_id=OPERATOR_ID, manager_id=None):
    """(grants, refused) from the task's comments. Only comments the operator wrote (or the
    engine relayed) count; description text never does. `edit` counts from any of them (the
    caller drops those from before the last run started); `delete ...` and `merge` count only
    inside the Approved answer: `approved` True and posted since the last move to Awaiting
    approval. refused is [(activity_id, line, reason)] for the operator's `grant:` lines that are
    not grants."""
    start = None
    for i, a in enumerate(activities):
        if _moved_to(a) == "Awaiting approval":
            start = i
    grants, refused = [], []
    for i, a in enumerate(activities):
        text = _counted_text(a, operator_id, manager_id)
        if text is None:
            continue
        aid, created = a.get("id"), a.get("createdAt") or ""
        in_answer = approved and start is not None and i >= start
        for line, g, why in grant_lines(text):
            g = g and Grant(g.action, g.arg, aid, created)
            if g is None:
                refused.append(Refused(f"{aid}:{line}", line, why))
            elif g.action in APPROVED_ONLY and not in_answer:
                refused.append(Refused(g.key, line, f"`{g.action}` counts only in an Approved "
                                                    "answer"))
            else:
                grants.append(g)
    return grants, refused


@dataclass(frozen=True)
class Refused:
    """A `grant:` line that opens nothing: `key` is what the ledger remembers it by (a grant's
    own key when the line parsed), so it is reported once and never after it was used."""
    key: str
    line: str
    reason: str

    def as_dict(self):
        return {"key": self.key, "line": self.line, "reason": self.reason}


def criteria_draft(comments):
    """The latest `autopilot: criteria draft` comment as (state, text), or None if there is none.
    state: "drafted" (it holds an acceptance section with content), "reserved" (`... needs
    operator`) or "failed" (anything else, including a draft with no usable section)."""
    last = None
    for c in comments:
        m = _DRAFT.match(c or "")
        if m:
            last = (m["tail"], c)
    if last is None:
        return None
    tail, text = last
    if re.match(r"needs\s+operator\b", tail.strip(" .:-*"), re.I):
        return "reserved", text
    if re.match(r"failed\b", tail.strip(" .:-*"), re.I):
        return "failed", text
    return ("drafted" if has_acceptance(text) else "failed"), text


# ---- the task model -----------------------------------------------------------------------

@dataclass
class Blocker:
    id: int
    status: str
    closed: bool
    subject: str = ""


@dataclass
class Task:
    id: int
    subject: str
    status: str
    type: str
    priority: str = "Normal"
    lane: str = ""
    categories: tuple = ()
    description: str = ""
    blocked_by: list = field(default_factory=list)
    resume: bool = False
    draft: tuple = None     # criteria_draft(comments): (state, text) or None
    answer: str = ""        # operator_answer(activities), for an Approved / Changes requested task
    grants: list = field(default_factory=list)      # [Grant] (task_grants), #979
    refused: list = field(default_factory=list)     # [Refused] grant lines that open nothing

    @property
    def decision(self):
        return DECIDED.get(self.status)

    @property
    def human(self):
        return is_human(self.subject, self.categories)

    @property
    def person_only(self):
        return self.lane in PERSON_LANES or self.human

    @property
    def open_blockers(self):
        return [b for b in self.blocked_by if not b.closed]

    def as_dict(self):
        return {"task": self.id, "subject": self.subject, "status": self.status, "type": self.type,
                "priority": self.priority, "lane": self.lane, "resume": self.resume,
                "promote": self.status == "New", "decision": self.decision, "answer": self.answer,
                "criteria_draft": bool(self.status == "New" and not has_acceptance(self.description)
                                       and self.draft and self.draft[0] == "drafted"),
                **({"grants": [g.as_dict() for g in self.grants]} if self.grants else {}),
                **({"refused": [r.as_dict() for r in self.refused]} if self.refused else {})}


@dataclass
class Skipped:
    task: Task
    kind: str       # waiting | operator | person | lane | unrouted | criteria | running | other
    reason: str

    def as_dict(self):
        return {"task": self.task.id, "subject": self.task.subject, "status": self.task.status,
                "kind": self.kind, "reason": self.reason}


@dataclass
class Selection:
    qualified: list     # [Task], in run order
    skipped: list       # [Skipped], in board order

    def of_kind(self, kind):
        return [s for s in self.skipped if s.kind == kind]


# ---- reading the board ----------------------------------------------------------------------

def _href_id(link):
    h = (link or {}).get("href")
    return int(h.rstrip("/").split("/")[-1]) if h else None


def _title(wp, name):
    return (wp.get("_links", {}).get(name) or {}).get("title")


def _categories(wp):
    links = wp.get("_links", {})
    cats = links.get("categories")
    if isinstance(cats, list):
        return tuple(c.get("title") for c in cats if c.get("title"))
    t = (links.get("category") or {}).get("title")
    return (t,) if t else ()


def _needs_comments(t, held):
    """Only these can be changed by a resume comment, so only these cost an activity read."""
    return (t.id not in held and t.type in BUILDABLE and not t.person_only
            and t.status in ("Ready", "New", "In Progress", *DECIDED))


def fetch(client, board, held=()):
    """Every open work package on `board` as a Task, in board (id) order.

    `client` is a PMClient (live) or anything with the same methods (tests use a recorded one).
    Activities are read only for tasks a resume comment could change (see _needs_comments)."""
    wps = sorted(client.open_work_packages(board), key=lambda w: w["id"])
    by_id = {w["id"]: w for w in wps}
    lane_key = client.lane_key(board)
    blockers = {}
    for b, t in client.block_relations():
        if t in by_id:
            blockers.setdefault(t, []).append(b)
    held = {int(h) for h in held}
    out = []
    for w in wps:
        bl = []
        for b in sorted(set(blockers.get(w["id"], []))):
            other = by_id.get(b) or client.work_package(b)   # a closed or off-board blocker
            st = _title(other, "status") or "?"
            bl.append(Blocker(b, st, client.closed(st), other.get("subject", "")))
        t = Task(id=w["id"], subject=w.get("subject", ""), status=_title(w, "status") or "?",
                 type=_title(w, "type") or "?", priority=_title(w, "priority") or "Normal",
                 lane=((w.get("_links", {}).get(lane_key) or {}).get("title") or "") if lane_key else "",
                 categories=_categories(w),
                 description=(w.get("description") or {}).get("raw") or "", blocked_by=bl)
        if _needs_comments(t, held):
            comments = client.comments(t.id)
            t.resume = resume_requested(comments)
            if t.decision:
                t.answer = operator_answer(client.activities(t.id), manager_id=client.manager_id())
            if any(_GRANT_ANYWHERE.search(c or "") for c in comments):
                t.grants, t.refused = task_grants(client.activities(t.id),
                                                  t.status == "Approved",
                                                  manager_id=client.manager_id())
            if t.status == "New":
                t.draft = criteria_draft(comments)
        out.append(t)
    return out


# ---- qualification and order ----------------------------------------------------------------

def _blockers_text(t):
    return ", ".join(f"#{b.id} ({b.status})" for b in t.open_blockers)


def classify(t, held=(), lanes=(), taken=None):
    """None if the task qualifies, else (kind, reason).

    lanes: the project's `autopilot.lanes` (lower case), () for every lane. taken: every lane the
    board's projects take, given only to the project that reports unrouted tasks (None otherwise)."""
    if t.id in held:
        return "running", "held by a live worker"
    if t.type not in BUILDABLE:
        return "other", f"{t.type} is not buildable; it closes when its children are done"
    if t.lane in PERSON_LANES:
        return "person", f"person-only: {t.lane} lane"
    if t.human:
        return "person", "person-only: tagged human"
    lane = t.lane.strip().lower()
    if lanes and lane not in lanes:
        shown = t.lane or "(none)"
        if taken is not None and lane not in taken:
            return "unrouted", (f"unrouted: lane {shown}, no project on this board takes it "
                                f"(lanes: {', '.join(sorted(taken))})")
        return "lane", f"lane {shown} is not this project's ({', '.join(lanes)})"
    if t.status == "Awaiting approval":
        return "operator", "needs operator: Awaiting approval"
    if t.open_blockers:
        return "waiting", f"blocked_by {_blockers_text(t)}"
    if t.status == "Blocked":
        return "operator", "needs operator: Blocked with no open blocker"
    if t.status == "Ready" or t.decision:
        return None
    if t.status == "New":
        if has_acceptance(t.description):
            return None
        state = t.draft[0] if t.draft else None
        if state == "drafted":
            return None
        if state == "reserved":
            return "operator", "needs operator: acceptance criteria need a reserved decision"
        if state == "failed":
            return "operator", "needs operator: criteria draft failed; write the acceptance criteria"
        return "criteria", "New without acceptance criteria"
    if t.status == "In Progress":
        return None if t.resume else ("other", "In Progress without `autopilot: resume`")
    return "other", f"status {t.status}"


def order_key(t):
    return (0 if t.resume or t.decision else 1, PRIORITY_RANK.get(t.priority, DEFAULT_RANK), t.id)


def select(tasks, held=(), lanes=(), taken=None):
    """lanes and taken as in classify."""
    held = {int(h) for h in held}
    qualified, skipped = [], []
    for t in tasks:
        c = classify(t, held, lanes, taken)
        if c is None:
            qualified.append(t)
        else:
            skipped.append(Skipped(t, *c))
    qualified.sort(key=order_key)
    skipped.sort(key=lambda s: s.task.id)
    return Selection(qualified, skipped)


def needs_operator(selection):
    """Skipped tasks only the operator can move on: kind operator, then unrouted."""
    return selection.of_kind("operator") + selection.of_kind("unrouted")


def outcome_for(selection, workers):
    """(state, detail) for a pass with no work to do, or None to let the supervisor decide.

    - workers running, or a task qualifies: None (running / idle / budget are the supervisor's).
    - some task needs the operator: `needs_you`, naming those tasks. This wins over `waiting`,
      because the operator is the one thing that can move the board on. An unrouted task
      (#1001) counts: only the operator can give it a lane some project takes.
    - some task waits on an open blocker: `waiting`, naming task and blocker.
    Person-only tasks alone do not make `needs_you`: they are the operator's own work, and every
    board with a manager lane would otherwise sit in `needs_you` forever. The Selection still
    lists them (kind `person`) for the cockpit."""
    if workers or selection.qualified:
        return None
    ops = needs_operator(selection)
    if ops:
        return "needs_you", "; ".join(f"#{s.task.id} {s.reason}" for s in ops)
    waits = selection.of_kind("waiting")
    if waits:
        return "waiting", "; ".join(f"#{s.task.id} {s.reason}" for s in waits)
    return None


# ---- the live client --------------------------------------------------------------------------

def _import_openproject():
    """v2/pm_openproject.py, the engine's OpenProject client (auth: the nomad-manager token from
    ~/.config/nomad/openproject-tokens.json), loaded by path so v2/ is not put on sys.path."""
    f = os.path.join(NOMAD, "v2", "pm_openproject.py")
    spec = importlib.util.spec_from_file_location("nomad_pm_openproject", f)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PMClient:
    """Access to nomad-pm for selection, on top of the engine's OpenProject client. Reads,
    plus two writes: `comment`, which the scheduler uses for `autopilot: resume` when it stopped
    a run itself (time limit, kill, a supervisor crash) and for criteria drafts (#889), and
    `transition`, for a draft that needs the operator (New -> Awaiting approval). Promotion New -> Ready and every other
    status move is the worker's job (#866, AUTONOMY.md section 1)."""

    def __init__(self, op_module=None):
        self._op = op_module or _import_openproject()
        self._clients = {}
        self._comments = {}     # (wp id, updatedAt) -> [activity]; a comment bumps updatedAt
        self._updated = {}
        self._manager_id = None

    def _client(self, board):
        if board not in self._clients:
            self._clients[board] = self._op.OpenProject(project=board)
        return self._clients[board]

    def _any(self):
        return next(iter(self._clients.values()), None) or self._client(self._op.PROJECT)

    def open_work_packages(self, board):
        op = self._client(board)
        wps = op._all([{"project": {"operator": "=", "values": [str(op.project()["id"])]}},
                       {"status": {"operator": "o", "values": []}}])
        for w in wps:
            self._updated[w["id"]] = w.get("updatedAt")
        return wps

    def lane_key(self, board):
        return self._client(board)._fields().get("Lane")

    def block_relations(self):
        return self._any().block_relations()

    def work_package(self, wp_id):
        return self._any().get(wp_id)

    def closed(self, status):
        try:
            return self._any().closed(status)
        except Exception:
            return status in CLOSED

    def comment(self, wp_id, text):
        """Post a comment as the dev lane's agent user (the manager if it has no token)."""
        return self._any().comment(wp_id, text, as_="dev")

    def transition(self, wp_id, status):
        """Walk a task to `status` as the dev agent (the criteria drafter's move of a New task to
        Awaiting approval, #889). The reason is its own comment, posted first."""
        return self._any().move(wp_id, status, as_="dev")

    def manager_id(self):
        """The engine account's user id (it posts the operator's relayed replies), or None when
        it can't be read; then no relayed reply counts."""
        if self._manager_id is None:
            try:
                self._manager_id = self._any().user_id_of(self._op.MANAGER)
            except Exception:
                return None
        return self._manager_id

    def activities(self, wp_id):
        key = (wp_id, self._updated.get(wp_id))
        if key not in self._comments or key[1] is None:
            self._comments[key] = self._any().activities(wp_id)
        return self._comments[key]

    def comments(self, wp_id):
        return [(a.get("comment") or {}).get("raw") or "" for a in self.activities(wp_id)]


# ---- the Scheduler plug-in ---------------------------------------------------------------------

class TaskSelection:
    """Mixin for supervisor.Scheduler: fills `known`, `select_tasks` and `outcome` (PRD 5.2, 5.7).
    Compose it ahead of Scheduler (or a subclass adding workers):

        class Autopilot(TaskSelection, WorkerPool, Scheduler): ...

    `pm` is the nomad-pm client (PMClient by default); `registry` maps a project id to its
    projects.yaml entry (autopilot.projects.get by default)."""

    def __init__(self, *a, pm=None, registry=None, **kw):
        super().__init__(*a, **kw)
        self.pm = pm or PMClient()
        self._registry = registry
        self.selections = {}        # project id -> the last pass's Selection

    def _entry(self, project_id):
        if self._registry is None:
            try:
                from . import projects
            except ImportError:
                import projects
            self._registry = projects.get
        return self._registry(project_id)

    def known(self, project_id):
        """A projects.yaml project with an `autopilot:` block and a nomad-pm board."""
        p = self._entry(project_id)
        return bool(p and getattr(p, "autopilot", None) and board_for(project_id))

    def _lanes(self, project_id):
        p = self._entry(project_id)
        return getattr(getattr(p, "autopilot", None), "lanes", ()) or ()

    def lane_routing(self, project_id):
        """(lanes, taken) for select (#1001). taken is set only when this project is the first
        autopilot project on its board and every autopilot project there lists lanes."""
        lanes = self._lanes(project_id)
        if not lanes:
            return (), None
        sharing = [pid for pid in board_projects(board_for(project_id)) if self.known(pid)]
        if not sharing or sharing[0] != project_id:
            return lanes, None
        taken = set()
        for pid in sharing:
            other = self._lanes(pid)
            if not other:
                return lanes, None
            taken.update(other)
        return lanes, taken

    def select_tasks(self, ctx):
        board = board_for(ctx.project_id)
        if not board:
            raise ValueError(f"project {ctx.project_id!r} has no nomad-pm board")
        held = [w.get("task") for w in ctx.project.get("workers", []) if w.get("task") is not None]
        sel = select(fetch(self.pm, board, held), held, *self.lane_routing(ctx.project_id))
        self.selections[ctx.project_id] = sel
        return [t.as_dict() for t in sel.qualified]

    def outcome(self, ctx, workers, tasks):
        sel = self.selections.get(ctx.project_id)
        return outcome_for(sel, workers) if sel else None


try:
    from .supervisor import Scheduler as _Scheduler
except ImportError:
    from supervisor import Scheduler as _Scheduler


class BoardScheduler(TaskSelection, _Scheduler):
    """The base Scheduler with real task selection and no workers yet (#866 adds them)."""


def main(argv=None):
    """Read-only dry run: python3 autopilot/tasks.py <project-id> prints what a pass would pick."""
    import sys
    pid = (argv or sys.argv[1:] or ["runematch"])[0]
    board = board_for(pid)
    sel = select(fetch(PMClient(), board))
    print(f"project {pid} -> board {board}")
    print(f"qualified ({len(sel.qualified)}), in run order:")
    for t in sel.qualified:
        flags = " resume" if t.resume else ""
        print(f"  #{t.id} [{t.type}] {t.status} {t.priority}{flags} · {t.subject}")
    print(f"not qualified ({len(sel.skipped)}):")
    for s in sel.skipped:
        print(f"  #{s.task.id} [{s.task.type}] {s.task.status} · {s.kind}: {s.reason} · {s.task.subject[:60]}")
    print("outcome with no workers:", outcome_for(sel, []))


if __name__ == "__main__":
    main()
