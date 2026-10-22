import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import render as R  # noqa: E402
import relay  # noqa: E402

OP, BOT, STRANGER = 10, 99, 11


def phash(run):
    return repr((run.get("action"), sorted((run.get("args") or {}).items())))


class FakeEngine:
    def __init__(self):
        self.runs = {}
        self.projects = []
        self.tasks = {}
        self.alerts = []
        self.posts = []
        self.pm = {"boot": "b1", "events": []}

    def add(self, rid, status, lane="comms", **detail):
        self.runs[rid] = {"run_id": rid, "lane": lane, "status": status, "goal": f"goal {rid}",
                          "action": "send_message", "args": {"to": "a@b.c", "body": "hello"}, **detail}

    def get(self, path, **p):
        if path == "/runs":
            return {"runs": [{"run_id": r["run_id"], "lane": r["lane"], "status": r["status"],
                              "created_at": dt.datetime.now(dt.timezone.utc).isoformat()}
                             for r in reversed(list(self.runs.values()))]}
        if path == "/run":
            r = dict(self.runs[p["id"]])
            return {**r, "proposal_hash": phash(r)}
        if path == "/project-goals":
            return {"projects": self.projects}
        if path == "/project":
            return {"milestones": [{"tasks": self.tasks.get(str(p["goal_id"]), [])}]}
        if path == "/pm-events":
            after = p["after"] if p["boot"] == self.pm["boot"] else 0
            return {"boot": self.pm["boot"], "events": [e for e in self.pm["events"] if e["seq"] > after]}
        raise KeyError(path)

    def post(self, path, body, timeout=None):
        self.posts.append((path, body))
        if path == "/resume":
            run = self.runs[body["run_id"]]
            if body.get("expect_hash") and body["expect_hash"] != phash(run):
                return {"status": run["status"], "error": "proposal changed since it was shown"}
            self.runs[body["run_id"]]["status"] = "executed" if body["decision"] == "approved" else "declined"
            return {"status": self.runs[body["run_id"]]["status"]}
        if path == "/retry":
            self.runs[body["run_id"]]["status"] = "awaiting-approval"
            return {"status": "awaiting-approval"}
        if path == "/chat":
            return {"reply": f"echo {body['message']}"}
        return {"status": "ok"}

    def briefing(self):
        return self.alerts


class FakeChat:
    def __init__(self):
        self.sent, self.dms, self.resolved, self.reactions = [], [], [], []
        self.members = [OP]
        self.n = 0

    def send(self, channel, topic, content):
        self.n += 1
        self.sent.append((self.n, channel, topic, content))
        return self.n

    def dm(self, user_id, content):
        self.dms.append(content)
        return 0

    refused = set()                              # boards Zulip won't make a stream for

    def stream_for(self, board, fallback):
        name = R.project_stream(board, fallback)
        return fallback if name in self.refused else name

    def resolve(self, mid, topic):
        self.resolved.append((mid, topic))

    def react(self, mid, name):
        self.reactions.append((mid, name))

    def raw_content(self, mid):
        return next(s[3] for s in self.sent if s[0] == mid)

    def humans(self):
        return self.members

    def on(self, channel):
        return [s for s in self.sent if s[1] == channel]


class Inline:                                           # run pool work synchronously in tests
    def submit(self, fn, *a):
        fn(*a)


def make(tmp_path):
    eng, chat = FakeEngine(), FakeChat()
    r = relay.Relay(eng, chat, relay.State(str(tmp_path / "s.json")), OP, BOT)
    r.pool = Inline()
    return r, eng, chat


def approved(eng):
    return [b["run_id"] for p, b in eng.posts if p == "/resume" and b["decision"] == "approved"
            and b.get("expect_hash")]


def gate(r, eng, chat, rid="g1", **detail):
    r.st.d["seeded"] = True
    eng.add(rid, "awaiting-approval", **detail)
    r.sync_runs()
    return chat.on("gate")[-1]


def react(message_id, code, user=OP):
    return {"type": "reaction", "op": "add", "user_id": user, "message_id": message_id, "emoji_code": code,
            "reaction_type": "unicode_emoji"}


def test_first_sync_posts_only_pending_gate_items(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.add("old1", "executed")
    eng.add("gate1", "awaiting-approval")
    r.sync_runs()
    assert [s[1] for s in chat.sent] == ["gate", "runs"]
    assert "Approval needed" in chat.on("gate")[0][3]


def test_operator_check_reaction_approves_and_resolves(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("g1", "awaiting-approval")
    r.sync_runs()
    gate_mid = chat.on("gate")[0][0]
    r.handle_event(react(gate_mid, "2705"))
    assert approved(eng) == ["g1"]
    r.sync_runs()
    assert chat.resolved and chat.resolved[0][0] == gate_mid


def test_reactions_and_messages_from_others_are_ignored(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("g1", "awaiting-approval")
    r.sync_runs()
    mid = chat.on("gate")[0][0]
    r.handle_event(react(mid, "2705", user=STRANGER))
    r.handle_event({"type": "message", "message": {"sender_id": STRANGER, "type": "private", "content": "approve g1"}})
    r.handle_event({"type": "message", "message": {"sender_id": STRANGER, "type": "stream",
                                                   "display_recipient": "gate", "subject": chat.on("gate")[0][2],
                                                   "content": "approve"}})
    assert eng.posts == []


def test_stale_approval_never_resumes(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("g1", "awaiting-approval")
    r.sync_runs()
    mid = chat.on("gate")[0][0]
    eng.runs["g1"]["status"] = "declined"               # decided elsewhere (cockpit, email)
    r.handle_event(react(mid, "2705"))
    assert not any(p[0] == "/resume" for p in eng.posts)
    assert "already `declined`" in chat.sent[-1][3]


def test_topic_reply_reject(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("g1", "awaiting-approval")
    r.sync_runs()
    topic = chat.on("gate")[0][2]
    r.handle_event({"type": "message", "message": {"sender_id": OP, "type": "stream",
                                                   "display_recipient": "gate", "subject": topic, "content": "Reject"}})
    assert ("/resume", {"run_id": "g1", "decision": "rejected"}) in eng.posts


def test_failure_alerts_and_retry_reaction_requeues(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("f1", "executing")
    r.sync_runs()
    eng.runs["f1"].update(status="failed", error="dispatcher down")
    r.sync_runs()
    alert = chat.on("alerts")[0]
    assert "dispatcher down" in alert[3]
    r.handle_event(react(alert[0], "1f501"))
    assert ("/retry", {"run_id": "f1"}) in eng.posts
    r.sync_runs()
    assert len(chat.on("gate")) == 1                    # back at the gate, posted fresh


def test_dm_commands(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("abcd1234", "awaiting-approval")
    r.sync_runs()
    r.command("approve abcd")
    assert approved(eng) == ["abcd1234"]
    r.command("what are you working on?")
    assert chat.dms[-1] == "echo what are you working on?"
    r.command("help")
    assert "NOMAD commands" in chat.dms[-1]


def test_blocked_task_alert_and_skip(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.projects = [{"goal_id": 8, "title": "NOMAD", "done": 2, "total": 12, "pct": 17}]
    r.sync_projects()                                   # seed: nothing posted
    assert chat.sent == []
    eng.tasks["8"] = [{"task_id": 61, "title": "Wire X", "status": "Blocked", "lane": "dev"}]
    r.sync_projects()
    alert = chat.on("alerts")[0]
    r.handle_event(react(alert[0], "23ed"))
    assert ("/task-action", {"task_id": "61", "action": "skip"}) in eng.posts
    eng.projects[0].update(done=3, pct=25)
    r.sync_projects()
    assert "3/12" in chat.on("projects")[0][3]



def test_blocked_from_project_goals_needs_no_project_call(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.projects = [{"goal_id": 8, "title": "NOMAD", "done": 2, "total": 12, "pct": 17, "blocked": []}]
    r.sync_projects()
    eng.projects[0]["blocked"] = [{"task_id": 61, "title": "Wire X", "status": "Blocked", "lane": "dev"}]
    eng.tasks["8"] = []                                 # /project would say nothing is blocked
    r.sync_projects()
    assert "Wire X" in chat.on("alerts")[0][3]

def test_alerts_post_and_recover_once(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.alerts = [{"key": "down:LiteLLM", "severity": "critical", "text": "LiteLLM is offline."},
                  {"key": "approvals:2", "severity": "warn", "text": "2 pending"}]
    r.sync_alerts()
    r.sync_alerts()
    assert len(chat.on("alerts")) == 1
    eng.alerts = []
    r.sync_alerts()
    assert "Recovered" in chat.on("alerts")[-1][3]


def test_digest_fires_once_per_slot(tmp_path):
    r, eng, chat = make(tmp_path)
    at = dt.datetime(2026, 9, 15, 7, 40, tzinfo=relay.TZ)
    r.maybe_digest(at)
    r.maybe_digest(at + dt.timedelta(minutes=5))
    assert len(chat.on("digest")) == 1


def test_render_rules():
    assert R.reaction_intent("2705") == "approve" and R.reaction_intent("1f44d") is None
    assert R.parse_command("skip task 61") == ("skip_task", "61")
    assert R.parse_command("skip 61")[0] == "chat"
    assert R.parse_command("capture: email bob") == ("chat", "capture: email bob")
    assert R.run_topic({"lane": "research", "goal": "secret deal", "run_id": "abcdef123"}) == "run abcdef123"
    msg = R.gate_message({"lane": "comms", "run_id": "r", "goal": "g", "action": "a",
                          "args": {"body": "has ``` fence"}})
    assert "````text" in msg
    assert "@\u200ball" in R.esc("ping @all", 50) and "\\[x\\]" in R.esc("[x](http://e)", 50)
    assert R.topic_reply_intent("yes") is None


def test_every_argument_is_shown(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, args={"to": "a@b.c", "cc": "boss@corp.com", "body": "hi"})
    assert "cc: boss@corp.com" in g[3]


# ── #776: which project a request is for ─────────────────────────────────────────────────────
WP = {"wp_id": 812, "wp_url": "https://pm.example/work_packages/812", "wp_subject": "Fix the gate",
      "board": "nomad-work", "project_name": "NOMAD work"}


def test_a_gate_for_a_project_task_names_the_board_and_links_the_task(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, project=WP)
    head = g[3].splitlines()[1]
    assert head.startswith("**Project:** `nomad-work` (NOMAD work)")
    assert "[#812 Fix the gate](https://pm.example/work_packages/812)" in head
    assert g[2] == "nomad-work · run g1"
    runs = chat.on("nomad-work")[-1]                     # #1123: the board's own stream
    assert runs[2] == g[2] and "`nomad-work`" in runs[3]


def test_a_gate_with_no_project_says_so(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    assert g[3].splitlines()[1] == "**Project:** no project"
    assert g[2] == "run g1"


def test_a_dev_run_names_its_repo_in_the_message_but_not_the_topic(tmp_path):
    """The repo comes from the Manager reading the goal, so it is untrusted text and stays out of
    the topic, which shows on the lock screen."""
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, lane="dev", project={"repo": "secret-deal"})
    assert "repo `secret-deal`" in g[3] and g[2] == "run g1"


def test_a_board_that_is_not_a_plain_identifier_stays_out_of_the_topic():
    d = {"run_id": "abc", "project": {"board": "Launch **now** @all"}}
    assert R.run_topic(d) == "run abc"
    assert R.run_topic({"run_id": "abc", "project": {"board": "x" * 41}}) == "run abc"


def test_project_text_is_escaped():
    line = R.project_line({"project": {"wp_id": 5, "wp_subject": "[click](http://evil) @all",
                                       "wp_url": "https://pm.example/work_packages/5"}})
    assert "\\[click\\]" in line and "@​all" in line


def test_the_board_being_down_still_posts_the_gate(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, project={"wp_id": 9, "wp_url": "https://pm.example/work_packages/9",
                                    "pm_error": "OpenProject unreachable"})
    assert "[#9](https://pm.example/work_packages/9) · board did not answer" in g[3]
    assert g[2] == "run g1"


def test_an_old_mission_control_task_is_named(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, project={"task_ref": 44})
    assert "mission-control task `44`" in g[3]


def test_approving_in_a_project_topic_still_works(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, project=WP)
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == ["g1"]


def test_a_failure_alert_names_the_project(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    eng.add("f1", "failed", project=WP, error="boom")
    r.sync_runs()
    alert = chat.on("alerts")[-1]
    assert "`nomad-work`" in alert[3] and alert[2] == "nomad-work · run f1"


def test_cockpit_only_action_cannot_be_approved_remotely(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, action="dispatch_build", args={"project": "nomad", "task": "rm stuff"})
    assert "cockpit-only" in g[3]
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == [] and "cockpit" in chat.sent[-1][3]
    r.handle_event(react(g[0], "274c"))                  # rejecting is always allowed
    assert eng.runs["g1"]["status"] == "declined"


def test_an_unresolved_build_project_is_named_at_the_gate():
    """#851: the engine's project warning reaches the Zulip gate message, escaped."""
    d = {"run_id": "g1", "lane": "dev", "goal": "fix it", "action": "dispatch_build",
         "args": {"project": "claude-bridge", "task": "t"},
         "project_warning": "Project 'claude-bridge' is not a project in the dispatcher's "
                            "registry, so this build fails if approved. Candidates: nomad, nomad-crm."}
    msg = R.gate_message(d, remote_ok=False)
    assert "Project not resolved" in msg and "Candidates: nomad, nomad-crm" in msg
    assert "Project not resolved" not in R.gate_message({**d, "project_warning": ""}, False)


def test_truncated_proposal_cannot_be_approved_remotely(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, args={"to": "a@b.c", "body": "x" * 5000})
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == []


def test_edited_gate_message_refuses(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    i = chat.sent.index(g)
    chat.sent[i] = (g[0], g[1], g[2], g[3].replace("a@b.c", "me@x.y"))
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == [] and "edited" in chat.sent[-1][3]


def test_proposal_changed_after_posting_is_refused_by_engine(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    eng.runs["g1"]["args"] = {"to": "attacker@x.y", "body": "hello"}
    r.handle_event(react(g[0], "2705"))
    assert eng.runs["g1"]["status"] == "awaiting-approval"
    assert "proposal changed" in chat.sent[-1][3]


def test_dm_approve_needs_a_gate_message(tmp_path):
    r, eng, chat = make(tmp_path)
    r.st.d["seeded"] = True
    r.st.d["runs"]["zzzz9999"] = {"status": "executed"}
    eng.add("zzzz9999", "awaiting-approval")              # never posted to #gate by this relay
    r.st.d["runs"]["zzzz9999"]["status"] = "awaiting-approval"
    r.command("approve zzzz")
    assert approved(eng) == []


def test_double_tap_resumes_once(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    r.in_flight.add("g1")                                  # the first tap is still executing
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == []


def test_lockdown_and_strangers_stop_everything(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    r.command("lockdown")
    r.handle_event(react(g[0], "2705"))
    r.command("reject g1xx")
    assert eng.posts == [] and r.st.d["locked"]
    r.st.d["locked"] = False
    chat.members = [OP, STRANGER]
    r.check_org()
    r.handle_event(react(g[0], "274c"))
    assert eng.posts == []
    chat.members = [OP]
    r.check_org()
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == ["g1"]


def test_custom_emoji_ignored(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat)
    r.handle_event({**react(g[0], "2705"), "reaction_type": "realm_emoji"})
    assert eng.posts == []


# ── liveness: a bad key must not look like a healthy relay (CLAUDE.md, 2026-09-23) ────────────

def test_auth_errors_are_told_apart_from_blips():
    assert relay.is_auth_error(RuntimeError("Invalid API key"))
    assert relay.is_auth_error(RuntimeError("Account is not active"))
    assert relay.is_auth_error(RuntimeError("zulip user lookup: Malformed API key"))
    assert not relay.is_auth_error(ConnectionError("Connection refused"))
    assert not relay.is_auth_error(RuntimeError("Bad Gateway"))


class _Client:
    """A zulip client whose register() fails a set number of times, then succeeds."""
    def __init__(self, msg, fails=99):
        self.msg, self.fails, self.calls = msg, fails, 0

    def register(self, **_kw):
        self.calls += 1
        if self.calls <= self.fails:
            return {"result": "error", "msg": self.msg}
        return {"result": "success", "queue_id": "q", "last_event_id": 0}

    def get_events(self, **_kw):
        raise SystemExit(0)          # stop the test once the loop is running for real


def _run_event_loop(monkeypatch, tmp_path, client):
    r, _eng, _chat = make(tmp_path)
    monkeypatch.setattr(relay.time, "sleep", lambda _s: None)
    try:
        r.event_loop(client)
    except SystemExit as e:
        return e.code
    return None


def test_repeated_bad_key_exits_nonzero(monkeypatch, tmp_path):
    """Three straight auth failures → exit 3, so systemd restarts and re-reads the zuliprc."""
    c = _Client("Invalid API key")
    assert _run_event_loop(monkeypatch, tmp_path, c) == 3
    assert c.calls == relay.AUTH_FAIL_LIMIT


def test_transient_errors_retry_forever(monkeypatch, tmp_path):
    """A Zulip blip must never take the relay down — it retries past the auth limit and recovers."""
    c = _Client("Bad Gateway", fails=relay.AUTH_FAIL_LIMIT + 4)
    assert _run_event_loop(monkeypatch, tmp_path, c) == 0      # reached get_events → recovered
    assert c.calls == relay.AUTH_FAIL_LIMIT + 5


# ── PM-01 P4: OpenProject events → Zulip, replies → comments ────────────────────────────────────
def pm_ev(eng, wp_id=112, **kw):
    ev = {"seq": len(eng.pm["events"]) + 1, "wp_id": wp_id, "subject": "Wire the API", "type": "Task",
          "project": "Work", "status": "Blocked", "lane": "dev", "action": "updated", "from": "Ready",
          "to": "Blocked", "blocked": True, "comment": "no key yet", "author": "Dev (agent)",
          "by_agent": True, "by_operator": False, "mention": False, "watched": False,
          "origin": "openproject", "url": f"http://localhost:8097/work_packages/{wp_id}/activity", **kw}
    eng.pm["events"].append(ev)
    return ev


def stream(channel, topic, content, sender=OP, mid=500):
    return {"type": "message", "message": {"id": mid, "type": "stream", "sender_id": sender,
                                           "display_recipient": channel, "subject": topic, "content": content}}


def test_pm_blocked_goes_to_alerts_and_watched_to_its_own_topic(tmp_path):
    r, eng, chat = make(tmp_path)
    pm_ev(eng)                                              # blocked, not watched
    r.sync_pm()
    ((_, ch, topic, text),) = chat.sent
    assert (ch, topic) == ("alerts", "wp 112") and "⛔ Blocked" in text and "no key yet" in text
    assert "`dev`" in text
    pm_ev(eng, **{"from": "Blocked"}, to="Ready", blocked=False, status="Ready", watched=True, comment="")
    r.sync_pm()
    assert chat.sent[-1][1:3] == ("projects", "wp 112") and "Blocked → **Ready**" in chat.sent[-1][3]
    assert r.st.d["pm_blocked"] == []                      # unblocked: a new block alerts again
    r.sync_pm()                                            # the cursor held: nothing twice
    assert len(chat.sent) == 2


def test_pm_block_is_alerted_once_across_both_paths(tmp_path):
    """The engine's own board tasks are also watched by sync_projects; one alert, not two."""
    r, eng, chat = make(tmp_path)
    pm_ev(eng, wp_id=61)
    r.sync_pm()
    eng.projects = [{"goal_id": 8, "title": "NOMAD", "done": 0, "total": 3, "pct": 0}]
    r.st.d["projects_seeded"] = True
    eng.tasks["8"] = [{"task_id": 61, "title": "Wire the API", "status": "Blocked", "lane": "dev"}]
    r.sync_projects()
    assert len(chat.on("alerts")) == 1
    r2, eng2, chat2 = make(tmp_path / "other")
    (tmp_path / "other").mkdir(exist_ok=True)
    eng2.projects, eng2.tasks = eng.projects, eng.tasks
    r2.st.d["projects_seeded"] = True
    r2.sync_projects()                                     # the poll saw it first this time
    pm_ev(eng2, wp_id=61)
    r2.sync_pm()
    assert len(chat2.on("alerts")) == 1


def test_pm_mention_skips_and_boot(tmp_path):
    r, eng, chat = make(tmp_path)
    pm_ev(eng, to=None, blocked=False, mention=True, comment="@Admin can you look?")
    pm_ev(eng, by_operator=True, watched=True)                              # they did it
    pm_ev(eng, origin="zulip", to=None, blocked=False, watched=True)       # their reply, echoed
    r.sync_pm()
    assert [(c, t) for _, c, t, _ in chat.sent] == [("projects", "wp 112"), ("alerts", "wp 112")]
    # the comment sits in a code block, where a mention cannot ping anyone
    assert "mentioned you" in chat.sent[1][3] and "```text\n@Admin can you look?\n```" in chat.sent[1][3]
    eng.pm = {"boot": "b2", "events": []}                  # the engine restarted
    pm_ev(eng, wp_id=7, watched=True, blocked=False, to=None)
    r.sync_pm()
    assert chat.sent[-1][2] == "wp 7"


def test_reply_in_a_wp_topic_is_a_comment(tmp_path):
    r, eng, chat = make(tmp_path)
    pm_ev(eng, watched=True)
    r.sync_pm()
    r.handle_event(stream("projects", "wp 112", "use the staging key", mid=501))
    assert ("/pm-comment", {"wp_id": 112, "text": "use the staging key"}) in eng.posts
    assert chat.reactions == [(501, "check")]
    r.handle_event(stream("alerts", "✔ wp 112", "retry", mid=502))       # a word is a comment here
    assert eng.posts[-1] == ("/pm-comment", {"wp_id": 112, "text": "retry"})
    r.handle_event(stream("projects", "wp 112", "not me", sender=STRANGER))
    r.handle_event(react(chat.sent[0][0], "1f501"))                    # reactions do nothing on wp
    assert len(eng.posts) == 2
    r.st.d["locked"] = True
    r.handle_event(stream("projects", "wp 112", "hello", mid=503))
    assert len(eng.posts) == 2 and "Locked down" in chat.sent[-1][3]


def test_pm_events_absent_on_an_old_engine(tmp_path):
    import requests
    r, eng, chat = make(tmp_path)
    resp = requests.Response()
    resp.status_code = 404

    def get(path, **p):
        raise requests.HTTPError("404", response=resp)
    eng.get = get
    r.sync_pm()                                            # quiet, no exception
    assert chat.sent == []


# ── #800: every gate, #runs and failure message links to the run page ───────────────────────
TAILNET = "https://nomad.tail1234.ts.net:8449"
PAGE = f"{TAILNET}/cockpit/run/g1"


def linked(tmp_path):
    r, eng, chat = make(tmp_path)
    r.cockpit = TAILNET
    return r, eng, chat


def test_an_approvable_gate_links_to_the_run_page_and_still_approves(tmp_path):
    """The link is in the text the relay hashed as shown, so a ✅ still passes the edit check."""
    r, eng, chat = linked(tmp_path)
    g = gate(r, eng, chat)
    assert g[3].splitlines()[-1] == f"Open in cockpit: {PAGE}"
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == ["g1"]


def test_a_cockpit_only_gate_says_where_to_approve_it(tmp_path):
    r, eng, chat = linked(tmp_path)
    g = gate(r, eng, chat, action="dispatch_build", args={"project": "nomad", "task": "x"})
    assert g[3].splitlines()[-1].endswith(f"approve it in the cockpit: {PAGE}")
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == [] and chat.sent[-1][3].endswith(f"approved in the cockpit: {PAGE}")


def test_a_gate_too_long_for_zulip_links_to_the_page_with_all_of_it(tmp_path):
    r, eng, chat = linked(tmp_path)
    g = gate(r, eng, chat, args={"to": "a@b.c", "body": "x" * 5000})
    assert g[3].splitlines()[-1].endswith(f"approve it in the cockpit: {PAGE}")


def test_every_runs_message_links_to_the_run_page(tmp_path):
    r, eng, chat = linked(tmp_path)
    gate(r, eng, chat)
    eng.runs["g1"]["status"] = "executed"
    r.sync_runs()
    posts = chat.on("runs")
    assert len(posts) == 2
    assert all(p[3].splitlines()[0].endswith(f"[open in cockpit]({PAGE})") for p in posts)


def test_a_failure_alert_links_to_the_run_page(tmp_path):
    r, eng, chat = linked(tmp_path)
    r.st.d["seeded"] = True
    eng.add("f1", "failed", error="boom")
    r.sync_runs()
    assert chat.on("alerts")[-1][3].splitlines()[-1] == f"Open in cockpit: {TAILNET}/cockpit/run/f1"


def test_without_a_cockpit_address_the_messages_read_as_before(tmp_path):
    r, eng, chat = make(tmp_path)
    r.cockpit = ""                                       # NOMAD_TAILNET_ORIGIN unset
    g = gate(r, eng, chat, action="dispatch_build", args={"project": "nomad", "task": "x"})
    assert "/cockpit/run/" not in g[3]
    assert g[3].splitlines()[-1].endswith("approve it in the cockpit.")
    assert "/cockpit/run/" not in chat.on("runs")[-1][3]
    r.handle_event(react(g[0], "2705"))
    assert chat.sent[-1][3].endswith("can only be approved in the cockpit.")


def test_the_run_id_is_quoted_in_the_link():
    assert R.run_url(TAILNET + "/", "a/b c") == f"{TAILNET}/cockpit/run/a%2Fb%20c"
    assert R.run_url("", "g1") is None and R.run_url(TAILNET, "") is None


# ── #1123: one stream per project ───────────────────────────────────────────────────────────────
def test_project_stream_names():
    assert R.project_stream("runematch", "projects") == "runematch"
    assert R.project_stream(None, "projects") == "projects"
    assert R.project_stream("Bad Name!", "runs") == "runs"           # not a board identifier
    for shared in ("gate", "alerts", "runs", "projects", "digest", "autopilot", "nomad-portfolio"):
        assert R.project_stream(shared, "projects") == "projects"    # never hijack a shared channel


def test_wp_topic_goes_to_its_board_stream_and_replies_still_comment(tmp_path):
    r, eng, chat = make(tmp_path)
    pm_ev(eng, watched=True, blocked=False, to="Ready", status="Ready", board="runematch")
    r.sync_pm()
    assert chat.sent[-1][1:3] == ("runematch", "wp 112")
    r.handle_event(stream("runematch", "wp 112", "use the staging key", mid=601))
    assert ("/pm-comment", {"wp_id": 112, "text": "use the staging key"}) in eng.posts


def test_blocked_still_alerts_the_shared_channel(tmp_path):
    r, eng, chat = make(tmp_path)
    pm_ev(eng, watched=True, board="runematch")
    r.sync_pm()
    assert {s[1] for s in chat.sent} == {"runematch", "alerts"}


def test_refused_or_missing_board_falls_back(tmp_path):
    r, eng, chat = make(tmp_path)
    chat.refused = {"civis"}
    pm_ev(eng, watched=True, blocked=False, to="Ready", status="Ready", board="civis")
    pm_ev(eng, wp_id=113, watched=True, blocked=False, to="Ready", status="Ready")   # no board
    r.sync_pm()
    assert [s[1] for s in chat.sent] == ["projects", "projects"]


def test_run_lifecycle_goes_to_the_board_stream(tmp_path):
    r, eng, chat = make(tmp_path)
    eng.add("b1", "awaiting-approval", project={"board": "nomad-crm"})
    eng.add("n1", "awaiting-approval")
    r.sync_runs()
    feed = {s[2]: s[1] for s in chat.sent if s[1] != "gate"}
    assert feed == {"nomad-crm · run b1": "nomad-crm", "run n1": "runs"}


class _SubClient:
    def __init__(self, ok=True):
        self.ok, self.calls = ok, []

    def add_subscriptions(self, streams, principals=None, announce=False):
        self.calls.append((streams[0]["name"], principals))
        return {"result": "success"} if self.ok else {"result": "error", "msg": "Insufficient permission"}


def test_chat_creates_a_stream_once_and_falls_back_when_refused():
    c = relay.Chat(_SubClient())
    c.principals = [OP, BOT]
    assert c.stream_for("runematch", "projects") == "runematch"
    assert c.stream_for("runematch", "runs") == "runematch"
    assert c.c.calls == [("runematch", [OP, BOT])]                  # subscribed once
    assert c.stream_for(None, "projects") == "projects" and len(c.c.calls) == 1
    no = relay.Chat(_SubClient(ok=False))
    assert no.stream_for("civis", "projects") == "projects"


def test_project_streams_backfill_is_idempotent_and_skips_shared_names():
    import project_streams as ps
    c = _SubClient()
    res = ps.ensure(c, ["runematch", "projects", "civis"], [OP, BOT])
    assert res == {"runematch": "ok", "projects": "skipped: not a usable stream name", "civis": "ok"}
    assert ps.ensure(_SubClient(ok=False), ["civis"], [OP])["civis"].startswith("error")
    assert ps.ensure(c, ["shift"], [OP], dry=True) == {"shift": "would create"} and len(c.calls) == 2


class _ArchClient:
    def __init__(self, known=("civis",), ok=True):
        self.known, self.ok, self.deleted = set(known), ok, []

    def get_stream_id(self, name):
        if name in self.known:
            return {"result": "success", "stream_id": 7}
        return {"result": "error", "msg": "Invalid stream name 'x'"}

    def call_endpoint(self, url, method):
        self.deleted.append((method, url))
        return {"result": "success"} if self.ok else {"result": "error", "msg": "Must be an organization administrator"}


def test_project_streams_archive_deletes_only_project_streams_and_reports_each(  # #1200
):
    import project_streams as ps
    c = _ArchClient()
    res = ps.archive(c, ["civis", "gone", "projects", "autopilot"])
    assert res == {"civis": "ok", "gone": "ok: no such stream",
                   "projects": "skipped: not a project stream", "autopilot": "skipped: not a project stream"}
    assert c.deleted == [("DELETE", "streams/7")]
    assert ps.archive(_ArchClient(ok=False), ["civis"])["civis"].startswith("error: Must be")
    assert ps.archive(_ArchClient(), ["civis"], dry=True) == {"civis": "would archive"}
