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
        self.sent, self.dms, self.resolved = [], [], []
        self.members = [OP]
        self.n = 0

    def send(self, channel, topic, content):
        self.n += 1
        self.sent.append((self.n, channel, topic, content))
        return self.n

    def dm(self, user_id, content):
        self.dms.append(content)
        return 0

    def resolve(self, mid, topic):
        self.resolved.append((mid, topic))

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


def test_cockpit_only_action_cannot_be_approved_remotely(tmp_path):
    r, eng, chat = make(tmp_path)
    g = gate(r, eng, chat, action="dispatch_build", args={"project": "nomad", "task": "rm stuff"})
    assert "cockpit-only" in g[3]
    r.handle_event(react(g[0], "2705"))
    assert approved(eng) == [] and "cockpit" in chat.sent[-1][3]
    r.handle_event(react(g[0], "274c"))                  # rejecting is always allowed
    assert eng.runs["g1"]["status"] == "declined"


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
