"""NOMAD v2 · mention a bot in OpenProject to run an agent session (docs/prd/op-mention-agent.md).

The operator writes a comment on a work package that mentions one of the six bots, say
`@Dev fix the null check in the importer`. pm_follow hands every comment here (consider). When the
comment is the operator's, or their Zulip reply, and it mentions a bot, a job is queued; nothing
slow happens in the webhook. A worker thread then, as the mentioned bot:

1. asks the dispatcher which project the work package's board runs in (POST /session/project),
   and replies with the reason or the choices when it can't tell;
2. posts "On it" with the session and the branch;
3. runs the session (POST /session): one Claude session per work package, in its own worktree,
   resumed by every later mention;
4. posts the result (summary, diffstat, changed files, worktree, time, cost) or the error, and
   logs the run to episodic.

One run per work package at a time; later mentions on it wait their turn. At most CAP runs go at
once. Bots never trigger anything, so bots answering bots can't loop.

A run that hits the Claude Max usage limit (#962) is not a failure: the bot replies "Claude usage
limit reached; resuming at HH:MM", the job goes back to the head of its work package's queue with
its session id, and reruns at the reset, which resumes the same session. There is no paid fallback.
"""
import collections
import os
import re
import threading
import time

from pm_openproject import MANAGER, PMError, _href_id, _title

CAP = max(1, int(os.environ.get("NOMAD_MENTION_CAP", "2")))
SESSION_TIMEOUT = int(os.environ.get("NOMAD_SESSION_TIMEOUT", "1800"))
SUMMARY_MAX = 6000          # characters of the agent's summary in the result comment
REFRESH = 600               # seconds between bot map refreshes for an unknown mention

# login -> (short name, the role line the prompt gets)
BOTS = {
    "nomad-dev": ("Dev", "You implement: code, tests and fixes."),
    "nomad-research": ("Research", "You investigate and report. Change files only if asked."),
    "nomad-manager": ("Manager", "You plan: break the work down, write acceptance criteria and "
                                 "propose tasks."),
    "nomad-comms": ("Comms", "You write docs, posts and messages, in the operator's style guide."),
    "nomad-support": ("Support", "You diagnose the problem and propose the fix."),
    "nomad-ads": ("Ads", "You write and review ad and marketing copy."),
}
_BY_NAME = {name.lower(): login for login, (name, _) in BOTS.items()}
_AT = re.compile(r"(?<![\w@])@(" + "|".join(_BY_NAME) + r")\b", re.I)
_PLAN = re.compile(r"\b(?:plan[ -]only|read[ -]only)\b", re.I)
_ZULIP = re.compile(r"^\s*\*\*Operator, via Zulip:\*\*\s*")
_MENTION = re.compile(r"<mention\b([^>]*)>(.*?)</mention>", re.S)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')
_HOME = re.compile(r"^/home/[^/]+")


def mentioned_in_order(raw):
    """The user ids of the mention tags in `raw`, first to last."""
    out = []
    for attrs, _ in _MENTION.findall(raw or ""):
        a = dict(_ATTR.findall(attrs))
        if a.get("data-type") == "user" and a.get("data-id", "").isdigit():
            out.append(int(a["data-id"]))
    return out


def plain(raw):
    return _MENTION.sub(lambda m: re.sub(r"<[^>]+>", "", m.group(2)), raw or "").strip()


def first_bot(raw, ids):
    """The login of the first bot `raw` addresses: a mention tag of a bot user (`ids` maps user
    id -> login), else a plain `@Dev`-style word (a Zulip reply has no tags). None if none."""
    for uid in mentioned_in_order(raw):
        if uid in ids:
            return ids[uid]
    m = _AT.search(plain(raw))
    return _BY_NAME[m.group(1).lower()] if m else None


def _short(path):
    return _HOME.sub("~", path or "")


def _dur(secs):
    secs = int(secs or 0)
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"


class Mentions:
    """`call(route, body, timeout)` posts to the dispatcher and returns its JSON (an error body
    on failure). `log(**episodic)` records a run. `busy(wp_id)` says whether the autopilot has a
    live worker on the work package (PRD 5.6)."""

    def __init__(self, op, call, operator_id, cap=CAP, log=None, busy=None, sync=False):
        self.op, self.call, self.operator_id = op, call, operator_id
        self.cap, self.log, self.busy = cap, log, busy
        self.sync = sync                        # tests: run jobs inline, no threads
        self.lock = threading.Lock()
        self.pending = collections.defaultdict(collections.deque)   # wp id -> jobs
        self.active = set()                     # wp ids queued or running
        self.ready = collections.deque()        # wp ids waiting for a worker
        self.cv = threading.Condition(self.lock)
        self.running = 0
        self.workers = 0
        self._ids, self._ids_at = None, 0.0
        self.recent = collections.deque(maxlen=50)   # what happened, for /pm-mentions
        self.paused = {}                        # wp id -> reset time of its limit-paused job

    # ── who is who ──
    def bot_ids(self, refresh=False):
        """user id -> bot login, looked up once from each bot's own token."""
        if self._ids is None or (refresh and time.time() - self._ids_at > REFRESH):
            ids = {}
            for login in BOTS:
                try:
                    uid = self.op.user_id_of(login)
                except PMError:
                    uid = None
                if uid:
                    ids[uid] = login
            self._ids, self._ids_at = ids, time.time()
        return self._ids

    def _from_operator(self, act, raw, by_operator):
        if by_operator:
            return True
        if not _ZULIP.match(raw):
            return False
        # A Zulip reply is posted by nomad-manager with the prefix; the prefix alone could be typed
        # by anyone, so the author has to be the manager too.
        author = _href_id((act.get("_links") or {}).get("user"))
        manager = next((uid for uid, login in self.bot_ids().items() if login == MANAGER), None)
        return bool(author and author == manager)

    # ── the trigger (webhook thread: no slow work beyond the cached lookups) ──
    def consider(self, wp, act, raw, by_operator, closed=False):
        """Queue a run when this comment is the operator's and addresses a bot. Returns
        {"bot", "state"} (state: queued, waiting or started) or None."""
        if not (raw or "").strip() or not self._from_operator(act, raw, by_operator):
            return None
        ids = self.bot_ids()
        if any(uid not in ids and uid != self.operator_id for uid in mentioned_in_order(raw)):
            ids = self.bot_ids(refresh=True)     # a bot created since the engine started
        bot = first_bot(raw, ids)
        if not bot:
            return None
        text = plain(raw[_ZULIP.match(raw).end():] if _ZULIP.match(raw) else raw)
        job = {"wp_id": wp["id"], "bot": bot, "text": text, "activity_id": act.get("id"),
               "mode": "plan" if _PLAN.search(text) else "build", "closed": closed,
               "at": time.time()}
        print(f"pm_mention: #{wp['id']} activity {act.get('id')} -> {bot} ({job['mode']})",
              flush=True)
        return {"bot": bot, "state": self._enqueue(job)}

    def _enqueue(self, job):
        wp = job["wp_id"]
        with self.cv:
            behind = wp in self.active
            self.pending[wp].append(job)
            if not behind:
                self.active.add(wp)
                self.ready.append(wp)
            ahead = self.running + len(self.ready) - (0 if behind else 1)
            state = "queued" if behind else ("waiting" if ahead >= self.cap else "started")
            if not self.sync:
                while self.workers < self.cap:
                    self.workers += 1
                    threading.Thread(target=self._worker, daemon=True,
                                     name=f"mention-{self.workers}").start()
                self.cv.notify()
        if state != "started":
            note = ("Queued: another run on this task is going; this one resumes the same session "
                    "when it ends." if state == "queued" else
                    f"Waiting: {self.cap} sessions are running; this one starts when one ends.")
            self._later(wp, job["bot"], note)
        if self.sync:
            self.drain()
        return state

    def _later(self, wp, bot, text):
        if self.sync:
            self._say(wp, bot, text)
        else:
            threading.Thread(target=self._say, args=(wp, bot, text), daemon=True).start()

    # ── workers ──
    def _next(self, wp):
        """The next job on `wp`, or None after releasing it (lock held)."""
        if self.pending[wp]:
            return self.pending[wp].popleft()
        self.pending.pop(wp, None)
        self.active.discard(wp)
        return None

    def _worker(self):
        while True:
            with self.cv:
                while not self.ready:
                    self.cv.wait()
                wp = self.ready.popleft()
                self.running += 1
            try:
                self._work(wp)
            finally:
                with self.cv:
                    self.running -= 1

    def _work(self, wp):
        while True:
            with self.cv:
                job = self._next(wp)
            if not job:
                return
            try:
                reset_at = self.run(job)
            except Exception as e:           # a bug here must not kill the worker
                self._say(job["wp_id"], job["bot"], f"The run failed inside the engine: {e}"[:500])
                continue
            if reset_at:
                # Usage limit: the job waits at the head of its queue (the task stays active, so
                # later mentions queue behind it) and reruns at the reset.
                with self.cv:
                    self.pending[wp].appendleft(job)
                    self.paused[wp] = reset_at
                if not self.sync:
                    t = threading.Timer(max(0.0, reset_at - time.time()), self.wake, args=(wp,))
                    t.daemon = True
                    t.start()
                return

    def wake(self, wp):
        """The usage limit on `wp` has reset: its paused job runs again."""
        with self.cv:
            if self.paused.pop(wp, None) is None:
                return
            self.ready.append(wp)
            self.cv.notify()
        if self.sync:
            self.drain()

    def drain(self):
        """Tests: run everything queued, inline."""
        while True:
            with self.cv:
                if not self.ready:
                    return
                wp = self.ready.popleft()
            self._work(wp)

    # ── one run ──
    def _say(self, wp_id, bot, text):
        try:
            self.op.comment(int(wp_id), text, as_=bot)
        except PMError:
            pass

    def _note(self, job, outcome, detail=""):
        self.recent.append({"wp_id": job["wp_id"], "bot": job["bot"], "mode": job["mode"],
                            "outcome": outcome, "detail": detail[:300], "at": int(time.time())})
        print(f"pm_mention: #{job['wp_id']} {job['bot']} {job['mode']}: {outcome}"
              + (f" ({detail[:200]})" if detail else ""), flush=True)

    def run(self, job):
        wp_id, bot = job["wp_id"], job["bot"]
        name = BOTS[bot][0]
        try:
            wp = self.op.get(wp_id)
            board = self.op.board_of(wp)
        except PMError as e:
            self._note(job, "failed", str(e))
            return
        if job.get("closed") or self._closed(wp):
            self._say(wp_id, bot, "This task is closed; reopen it to continue.")
            self._note(job, "closed")
            return
        if self.busy and self.busy(wp_id):
            self._say(wp_id, bot, "The autopilot has a worker on this task right now, so I won't "
                                  "start a second one. Mention me again when it is done.")
            self._note(job, "autopilot")
            return
        key = f"op-{wp_id}"
        where = self.call("/session/project", {"board": board, "comment": job["text"],
                                               "subject": wp.get("subject", ""), "key": key}, 60)
        if not where.get("project"):
            choices = where.get("choices") or []
            tail = (" Add one of these tags to your comment: "
                    + ", ".join(f"`[{c}]`" for c in choices) + ".") if choices else ""
            self._say(wp_id, bot, f"I can't start a session here: "
                                  f"{where.get('error') or 'no project for this board'}.{tail}")
            self._note(job, "no project", where.get("error", ""))
            return
        project, sess = where["project"], where.get("session") or {}
        verb = "Continuing" if sess.get("resumed") else "On it"
        self._say(wp_id, bot, f"{verb}. Session `{(sess.get('session_id') or '')[:8]}`, project "
                              f"`{project}`, branch `{sess.get('branch') or '?'}`"
                              + (", read-only." if job["mode"] == "plan" else "."))
        r = self.call("/session", {"key": key, "project": project, "bot": bot,
                                   "mode": job["mode"], "title": wp.get("subject", ""),
                                   "prompt": self.prompt(wp, board, job)}, SESSION_TIMEOUT + 120)
        if r.get("limited"):
            at = float(r.get("reset_at") or time.time() + 5 * 3600)
            job["limited"], job["session_id"] = True, r.get("session_id") or sess.get("session_id")
            self._say(wp_id, bot, "Claude usage limit reached; resuming at "
                                  f"{r.get('reset_clock') or time.strftime('%H:%M', time.localtime(at))}")
            self._note(job, "limited", f"resumes at {int(at)}")
            if self.log:
                self.log(run_id=key, agent=bot,
                         what=f"mention session on #{wp_id} ({project}, {job['mode']})",
                         why=job["text"][:1000], outcome="paused on the usage limit",
                         links=f"wp#{wp_id} session {job['session_id']}")
            return at
        if len(r.get("summary") or "") > SUMMARY_MAX:
            print(f"pm_mention: #{wp_id} full summary:\n{r['summary']}", flush=True)
        self._say(wp_id, bot, self.result(r, name))
        self._note(job, "done" if r.get("ok") else "failed", r.get("error", ""))
        if self.log:
            self.log(run_id=key, agent=bot,
                     what=f"mention session on #{wp_id} ({project}, {job['mode']})",
                     why=job["text"][:1000], outcome="done" if r.get("ok") else "failed",
                     links=f"wp#{wp_id} session {r.get('session_id') or sess.get('session_id')}")

    # ── a task closed (PRD 5.6) ──
    def task_closed(self, wp_id):
        """Tell the dispatcher; a clean worktree goes, one with changes stays and is named here."""
        if self.sync:
            self._close(wp_id)
        else:
            threading.Thread(target=self._close, args=(wp_id,), daemon=True).start()

    def _close(self, wp_id):
        r = self.call("/session/close", {"key": f"op-{wp_id}"}, 60)
        if not r.get("known"):
            return
        if r.get("removed") is False and r.get("changed"):
            self._say(wp_id, r.get("bot") or "nomad-dev",
                      f"This task is closed. Its session worktree `{_short(r.get('worktree'))}` "
                      f"still has {len(r['changed'])} uncommitted change(s), so I kept it; branch "
                      f"`{r.get('branch')}` stays too.")
        print(f"pm_mention: #{wp_id} closed: worktree "
              + ("removed" if r.get("removed") else f"kept ({r.get('error') or 'changes'})"),
              flush=True)

    def _closed(self, wp):
        try:
            return self.op.closed(_title(wp, "status"))
        except PMError:
            return False

    def prompt(self, wp, board, job):
        name, role = BOTS[job["bot"]]
        desc = ((wp.get("description") or {}).get("raw") or "").strip() or "(none)"
        lines = [
            f"You are {name}, one of NOMAD's agents. {role}",
            f"The operator addressed you in a comment on OpenProject work package #{wp['id']} "
            f"(board `{board}`). Their comment is your instruction.",
            "",
            f"## Work package #{wp['id']}: {wp.get('subject', '')}",
            f"Type: {_title(wp, 'type')} · Status: {_title(wp, 'status')}",
            "",
            "### Description",
            desc[:8000],
        ]
        comments = self._comments(wp["id"], job.get("activity_id"))
        if comments:
            lines += ["", "### Recent comments (oldest first)", *comments]
        lines += ["", "## The operator's instruction", job["text"]]
        if job.get("limited"):
            lines += ["", "(Your last run on this stopped on the Claude usage limit. Continue from "
                          "where you stopped.)"]
        return "\n".join(lines)

    def _comments(self, wp_id, skip):
        try:
            acts = self.op.activities(wp_id)
        except PMError:
            return []
        out = []
        for a in acts:
            raw = (a.get("comment") or {}).get("raw") or ""
            if a.get("id") == skip or not raw.strip():
                continue
            try:
                who = self.op.user_name((a.get("_links", {}).get("user") or {}).get("href"))
            except PMError:
                who = None
            text = plain(raw).replace("\n", " ")
            out.append(f"- {who or 'someone'}: {text[:800]}")
        return out[-10:]

    @staticmethod
    def result(r, name):
        if not r.get("ok"):
            head = f"**Run failed** · {name}: {r.get('error') or 'no answer from the dispatcher'}"
            if r.get("worktree"):
                head += f"\n\nWorktree `{_short(r['worktree'])}`, branch `{r.get('branch')}`."
            return head[:8000]
        cost = r.get("cost_usd")
        parts = [f"**Done** · {_dur(r.get('secs'))}" + (f" · ${cost:.2f}" if cost else "")
                 + (" · read-only" if r.get("mode") == "plan" else ""), ""]
        summary = (r.get("summary") or "").strip() or "(no summary)"
        if len(summary) > SUMMARY_MAX:
            summary = summary[:SUMMARY_MAX] + "\n\n… (trimmed; the full text is in the engine log)"
        parts.append(summary)
        changed = r.get("changed") or []
        parts.append("")
        if changed:
            parts.append(f"**Changed, uncommitted** in `{_short(r.get('worktree'))}` on "
                         f"`{r.get('branch')}`:")
            parts.append("```\n" + ((r.get("diffstat") or "").strip() or "\n".join(changed[:40]))
                         + "\n```")
            new = [c[3:] for c in changed if c.startswith("??")]
            if new:
                parts.append("New files: " + ", ".join(f"`{n}`" for n in new[:20]))
        else:
            parts.append(f"No files changed. Worktree `{_short(r.get('worktree'))}`, branch "
                         f"`{r.get('branch')}`.")
        return "\n".join(parts)[:12000]
