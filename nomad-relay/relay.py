"""nomad-relay: NOMAD's activity in Zulip, and Zulip reactions/DMs back into NOMAD (ADR-008).

Outbound, it watches the engine the same way the cockpit does (SSE /events bump + a slow poll of
/runs), diffs run statuses, and posts transitions: runs at the gate → #gate, failures and blocked
tasks → #alerts, lifecycle → #runs, progress → #projects, summaries → #digest. Console briefing
alerts (service down, GPU hot) → #alerts.

Inbound, it long-polls the bot's Zulip event queue. Only the operator's account counts: their
✅/❌ on a gate message calls the engine's /resume, 🔁 on a failure calls /retry, and their DMs are
commands or a conversation with NOMAD. The engine stays the authority on every decision; this is
one more surface onto the same endpoints the cockpit uses.

Runs host-native (systemd user service), so it reaches the engine on 127.0.0.1:8099, the console
on 127.0.0.1:1701, and Zulip through its tailnet URL. Every loop fails open and reconnects.
"""
import datetime as dt
import hashlib
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import requests

import render as R

ENGINE = os.environ.get("NOMAD_ENGINE_URL", "http://127.0.0.1:8099").rstrip("/")
CONSOLE = os.environ.get("NOMAD_CONSOLE_URL", "http://127.0.0.1:1701").rstrip("/")
ZULIPRC = os.path.expanduser(os.environ.get("NOMAD_ZULIPRC", "~/.config/nomad/zulip-bot.zuliprc"))
OPERATOR = os.environ.get("NOMAD_ZULIP_OPERATOR", "")
OPERATOR_ID = os.environ.get("NOMAD_ZULIP_OPERATOR_ID", "")
# Actions Zulip may approve. The rest (dispatch_build edits a repo) can only be rejected from here.
REMOTE_ACTIONS = {a.strip() for a in os.environ.get(
    "NOMAD_RELAY_REMOTE_ACTIONS", "send_message,save_brief,save_content,generate_image").split(",") if a.strip()}
# The cockpit's tailnet origin (#795), the same value the engine checks writes against. With it set,
# gate, #runs and failure messages link to the run page (#800); unset, they read as before.
COCKPIT = os.environ.get("NOMAD_TAILNET_ORIGIN", "").strip().rstrip("/")
STATE_PATH = os.path.expanduser(os.environ.get("NOMAD_RELAY_STATE", "~/.local/state/nomad-relay/state.json"))
POLL = float(os.environ.get("NOMAD_RELAY_POLL", "60"))
DIGEST_AT = [s.strip() for s in os.environ.get("NOMAD_RELAY_DIGEST", "07:30,19:00").split(",") if s.strip()]
TZ = ZoneInfo(os.environ.get("NOMAD_RELAY_TZ", "America/New_York"))
KEEP = 600          # bounded maps: runs/messages/topics remembered
# A rejected key is permanent; a refused connection is not. The relay used to treat both the same
# way — log it, sleep 5, try again forever — so a key rotation on 2026-09-23 left it logging
# "Invalid API key" while systemctl still said `active`, because the process never exits and
# Restart=always never fires. The relay is also the thing that would have reported the outage.
# So auth failures now exit non-zero: systemd restarts us, the restart re-reads the zuliprc, and a
# rotation heals itself. A key that is genuinely wrong crash-loops into `failed`, which is visible.
AUTH_FAIL_LIMIT = int(os.environ.get("NOMAD_RELAY_AUTH_FAIL_LIMIT", "3"))

# T2.1 morning briefing / Friday review. The calendar+mail read goes through n8n (existing Google
# OAuth2 credentials there, never a new one here). `briefing`/`friday review` (DM) always work as a
# manual, read-only dry run. The *scheduled* #digest post only switches to the enriched briefing
# once NOMAD_RELAY_BRIEFING_ARMED=1 — unset/0 keeps the old plain summary() on the existing slots,
# so nothing new posts to #digest on a cron until a human has read a dry run and armed it by hand.
N8N = os.environ.get("NOMAD_N8N_URL", "http://127.0.0.1:5678").rstrip("/")
BRIEFING_ARMED = os.environ.get("NOMAD_RELAY_BRIEFING_ARMED", "0") == "1"
# Every n8n webhook requires this shared header (n8n credential "NOMAD webhook token",
# 2026-09-23). 5678 was published on 0.0.0.0 when that was added, and a published docker port does
# reach the LAN here; it is 127.0.0.1 now, but the header stays — the gate is not the bind.
N8N_TOKEN = os.environ.get("NOMAD_N8N_WEBHOOK_TOKEN", "")


HEARTBEAT = os.path.expanduser(os.environ.get(
    "NOMAD_RELAY_HEARTBEAT", "~/.local/state/nomad-watch/heartbeat/relay"))


def beat():
    """Touch the file nomad-watch reads. `systemctl is-active` cannot tell a relay that is
    polling the engine from one that is wedged, which is how the last outage hid; a file whose
    mtime stops moving can. Written only after a sync actually succeeded. Best effort — a
    watchdog that could take the relay down would be worse than no watchdog."""
    try:
        os.makedirs(os.path.dirname(HEARTBEAT), exist_ok=True)
        with open(HEARTBEAT, "w") as f:
            f.write(str(int(time.time())))
    except Exception as e:
        log("heartbeat write failed:", e)


def is_auth_error(err):
    """A credential rejection (permanent, restart-heals) vs a connectivity blip (transient)."""
    s = str(err).lower()
    return any(m in s for m in ("invalid api key", "malformed api key", "unauthorized",
                                "account is not active", "invalid subdomain"))


# At boot on 2026-10-01 the relay started before Zulip answered, zulip.Client raised
# `AssertionError: self.zulip_version is not None`, five quick restarts hit systemd's start limit,
# and the unit sat in `failed` for ~24 h (#993). A Zulip that isn't up yet is a blip like any other,
# so startup waits for it. Auth errors still raise: a wrong key should crash-loop into `failed`.
CONNECT_FIRST, CONNECT_CAP = 5, 60


def until_up(connect, what="zulip", sleep=None):
    """Call connect() until it returns, backing off 5, 10, 20… up to 60 s between tries."""
    sleep, delay = sleep or time.sleep, CONNECT_FIRST
    while True:
        try:
            return connect()
        except Exception as e:
            if is_auth_error(e):
                raise
            log(f"{what} not ready ({type(e).__name__}: {e}); retrying in {delay} s")
            sleep(delay)
            delay = min(delay * 2, CONNECT_CAP)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


# ── engine ────────────────────────────────────────────────────────────────────────────────────
class Engine:
    def __init__(self, base=ENGINE, console=CONSOLE, n8n=N8N, http=requests):
        self.base, self.console, self.n8n, self.http = base, console, n8n, http

    def get(self, path, **params):
        r = self.http.get(self.base + path, params=params or None, timeout=15)
        r.raise_for_status()
        return r.json()

    def post(self, path, body, timeout=900):
        # /resume executes the approved action synchronously (a dev build can take minutes).
        r = self.http.post(self.base + path, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def briefing(self):
        """Alerts to speak up about. The engine answers; the v1 console is the fallback.

        This used to call the console only, with nothing behind it, so the Zulip alert stream was a
        v1 dependency: stop the console and it goes quiet, which is the worst way for an alerting
        path to fail. The engine's /briefing returns the same shape. The console stays as a fallback
        until it is decommissioned, and when it goes the except arm simply stops being reached."""
        try:
            r = self.http.get(self.base + "/briefing", timeout=20)
            r.raise_for_status()
            return r.json().get("alerts", [])
        except Exception:
            r = self.http.get(self.console + "/api/briefing", timeout=20)
            r.raise_for_status()
            return r.json().get("alerts", [])

    def briefing_data(self, mode):
        """Calendar + mail-needing-reply, read-only (n8n workflow `nomadbriefingdata`): calendar
        through n8n's existing OAuth2 credential, mail through a gmail.readonly service account.
        Never a write."""
        r = self.http.get(self.n8n + "/webhook/nomad-briefing-data", params={"mode": mode},
                          headers={"X-Nomad-Token": N8N_TOKEN} if N8N_TOKEN else {}, timeout=25)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data[0] if data else {}
        return data if isinstance(data, dict) else {}


# ── zulip ─────────────────────────────────────────────────────────────────────────────────────
class Chat:
    """Thin, locked wrapper: the event loop and the sync loops share one client."""

    def __init__(self, client):
        self.c = client
        self.lock = threading.Lock()
        self.principals = []                    # operator + bot, set once both ids are known
        self.streams = set()                    # project streams this process has made sure of

    def _ok(self, res, what):
        if res.get("result") != "success":
            raise RuntimeError(f"zulip {what}: {res.get('msg')}")
        return res

    def send(self, channel, topic, content):
        with self.lock:
            res = self.c.send_message({"type": "stream", "to": channel, "topic": topic, "content": content})
        return self._ok(res, "send")["id"]

    def stream_for(self, board, fallback):
        """The project's own stream (#1123), created and subscribed on first use. Subscribing is
        idempotent, so an existing stream costs one call per process. If Zulip refuses, the post
        goes to the shared stream instead: a lost message is worse than a misfiled one."""
        name = R.project_stream(board, fallback)
        if name == fallback or name in self.streams:
            return name
        with self.lock:
            res = self.c.add_subscriptions(
                streams=[{"name": name, "description": R.project_stream_description(name)}],
                principals=self.principals or None, announce=False)
        if res.get("result") != "success":
            log("project stream", name, "unavailable, using", fallback + ":", res.get("msg"))
            return fallback
        self.streams.add(name)
        return name

    def dm(self, user_id, content):
        with self.lock:
            res = self.c.send_message({"type": "direct", "to": [user_id], "content": content})
        return self._ok(res, "dm")["id"]

    def resolve(self, message_id, topic):
        if topic.startswith(R.RESOLVED):
            return
        with self.lock:
            res = self.c.update_message({"message_id": message_id, "topic": R.RESOLVED + topic,
                                         "propagate_mode": "change_all",
                                         "send_notification_to_old_thread": False,
                                         "send_notification_to_new_thread": False})
        if res.get("result") != "success":          # permissions vary by realm; cosmetic only
            log("resolve topic skipped:", res.get("msg"))

    def react(self, message_id, emoji_name):
        with self.lock:
            res = self.c.add_reaction({"message_id": message_id, "emoji_name": emoji_name})
        if res.get("result") != "success":          # an acknowledgement only; never fail on it
            log("reaction skipped:", res.get("msg"))

    def raw_content(self, message_id):
        with self.lock:
            res = self.c.call_endpoint(url=f"messages/{message_id}", method="GET",
                                       request={"apply_markdown": "false"})
        self._ok(res, "fetch message")
        return res.get("raw_content") or res["message"]["content"]

    def humans(self):
        """Active, non-bot accounts in the org."""
        with self.lock:
            res = self.c.get_members()
        return [u["user_id"] for u in self._ok(res, "members")["members"]
                if u.get("is_active") and not u.get("is_bot")]

    def whoami(self):
        with self.lock:
            return self._ok(self.c.get_profile(), "profile")["user_id"]

    def user_id(self, email):
        with self.lock:
            res = self.c.call_endpoint(url=f"users/{email}", method="GET")
        return self._ok(res, "user lookup")["user"]["user_id"]


# ── state ─────────────────────────────────────────────────────────────────────────────────────
class State:
    def __init__(self, path=STATE_PATH):
        self.path = path
        self.lock = threading.RLock()
        self.d = {"seeded": False, "runs": {}, "msgs": {}, "topics": {}, "projects": {},
                  "blocked": {}, "alerts": {}, "digest_last": "", "locked": False,
                  "pm": {"boot": "", "after": 0}, "pm_blocked": []}
        try:
            with open(path) as f:
                self.d.update(json.load(f))
        except Exception:
            pass

    def save(self):
        with self.lock:
            for k in ("runs", "msgs", "topics"):
                m = self.d[k]
                if len(m) > KEEP:
                    for old in list(m)[: len(m) - KEEP]:
                        del m[old]
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                json.dump(self.d, f)
            os.replace(tmp, self.path)

    def link(self, message_id, channel, topic, ref):
        with self.lock:
            self.d["msgs"][str(message_id)] = ref
            self.d["topics"][f"{channel}\x1f{topic}"] = {**ref, "message_id": message_id}

    def by_message(self, message_id):
        return self.d["msgs"].get(str(message_id))

    def by_topic(self, channel, topic):
        if topic.startswith(R.RESOLVED):
            topic = topic[len(R.RESOLVED):]
        return self.d["topics"].get(f"{channel}\x1f{topic}")


# ── relay ─────────────────────────────────────────────────────────────────────────────────────
class Relay:
    def __init__(self, engine, chat, state, operator_id, bot_id):
        self.eng, self.chat, self.st = engine, chat, state
        self.operator_id, self.bot_id = operator_id, bot_id
        self.wake = threading.Event()
        self.sync_lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.remote_actions = REMOTE_ACTIONS
        self.cockpit = COCKPIT
        self.in_flight, self.flight_lock = set(), threading.Lock()
        self.strangers = []                               # other humans in the org → act on nothing

    # outbound: runs ──────────────────────────────────────────────────────────────────────────
    def sync_runs(self):
        with self.sync_lock:
            runs = self.eng.get("/runs").get("runs", [])
            seeding = not self.st.d["seeded"]
            for r in reversed(runs):                     # oldest first, so topics read in order
                rid, status = r.get("run_id"), r.get("status")
                if not rid:
                    continue
                known = self.st.d["runs"].get(rid)
                if known and known.get("status") == status:
                    continue
                if seeding and status != "awaiting-approval":
                    self.st.d["runs"][rid] = {"status": status}      # history: remember, don't post
                    continue
                self._transition(rid, known or {}, status)
            self.st.d["seeded"] = True
            self.st.save()

    def _transition(self, rid, known, status):
        d = self.eng.get("/run", id=rid)
        topic = known.get("topic") or R.run_topic(d)
        entry = {"status": status, "topic": topic, "gate_msg": known.get("gate_msg")}
        if status == "awaiting-approval":
            remote_ok = d.get("action") in self.remote_actions and bool(d.get("proposal_hash"))
            content, approvable = R.gate_view(d, remote_ok=remote_ok, cockpit=self.cockpit)
            mid = self.chat.send(R.GATE, topic, content)
            # Pin what was shown: the engine refuses if the proposal hash moved, and the relay refuses
            # if the Zulip message no longer says what it posted.
            self.st.link(mid, R.GATE, topic, {"kind": "gate", "run_id": rid, "approvable": approvable,
                                              "proposal_hash": d.get("proposal_hash"),
                                              "shown": _sha(content)})
            entry["gate_msg"] = mid
        self.st.d["runs"][rid] = entry                   # recorded before the optional posts below
        try:
            board = (d.get("project") or {}).get("board")
            self.chat.send(self.chat.stream_for(board, R.RUNS), topic,
                           R.status_message(d, status, cockpit=self.cockpit))
        except Exception as e:                           # the lifecycle feed is nice-to-have
            log("runs feed post failed:", e)
        if status != "awaiting-approval":
            if status == "failed":
                mid = self.chat.send(R.ALERTS, topic, R.failed_alert(d, cockpit=self.cockpit))
                self.st.link(mid, R.ALERTS, topic, {"kind": "failed", "run_id": rid})
            if entry.get("gate_msg") and status in ("executing", "executed", "failed", "declined"):
                self.chat.send(R.GATE, topic, f"Resolved: {R.STATUS_LINE.get(status, status)}.")
                self.chat.resolve(entry["gate_msg"], topic)
                entry["gate_msg"] = None
        self.st.d["runs"][rid] = entry
        log("run", rid, known.get("status"), "→", status)

    # outbound: projects ──────────────────────────────────────────────────────────────────────
    def sync_projects(self):
        projects = self.eng.get("/project-goals").get("projects", [])
        seeding = not self.st.d.get("projects_seeded")   # first pass: remember, don't alert
        for p in projects:
            gid = str(p.get("goal_id"))
            prev = self.st.d["projects"].get(gid)
            cur = {"done": p.get("done"), "total": p.get("total")}
            if not seeding and prev != cur:
                self.chat.send(R.PROJECTS, R.project_topic(gid), R.progress_message(p, prev))
            # A rejected task goes Blocked without moving the done count, so check every pass.
            self._sync_blocked(gid, p.get("title"), post=not seeding, tasks=p.get("blocked"))
            self.st.d["projects"][gid] = cur
        self.st.d["projects_seeded"] = True
        self.st.save()

    def _sync_blocked(self, gid, title, post=True, tasks=None):
        """`tasks`: the goal's Blocked tasks from /project-goals (#1130). An engine that doesn't
        list them there is asked for the goal's detail instead, one board read per goal."""
        if tasks is None:
            detail = self.eng.get("/project", goal_id=gid)
            tasks = [t for m in detail.get("milestones", []) for t in m.get("tasks", [])]
        now = {str(t["task_id"]): t for t in tasks if t.get("status") == "Blocked"}
        seen = self.st.d["blocked"].get(gid, [])
        for tid, t in now.items():
            # A board task may already have been alerted from its OpenProject webhook (sync_pm).
            if post and tid not in seen and tid not in self.st.d["pm_blocked"]:
                topic = R.project_topic(gid)
                mid = self.chat.send(R.ALERTS, topic, R.blocked_alert(title, t))
                self.st.link(mid, R.ALERTS, topic, {"kind": "blocked", "task_id": tid})
        self.st.d["blocked"][gid] = list(now)

    # outbound: OpenProject (PM-01 P4) ───────────────────────────────────────────────────────
    def sync_pm(self):
        """Webhook events the engine turned into facts → Zulip. A watched work package gets its own
        topic in its board's stream (#projects without one); becoming Blocked or mentioning the operator also goes to #alerts, which
        pushes to the phone. Replies in either topic go back as comments (comment_wp)."""
        pm = self.st.d["pm"]
        try:
            d = self.eng.get("/pm-events", after=pm["after"], boot=pm["boot"])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return                                   # an engine without P4
            raise
        if d.get("boot") != pm["boot"]:
            pm["after"] = 0                              # the engine restarted: its queue is new
        for ev in d.get("events", []):
            if ev["seq"] <= pm["after"]:
                continue
            self._pm_event(ev)
            pm["after"] = ev["seq"]
        pm["boot"] = d.get("boot", "")
        self.st.save()

    def _pm_event(self, ev):
        if ev.get("by_operator"):
            return                                       # they know; they did it
        if ev.get("origin") == "zulip" and not ev.get("to"):
            return                                       # the operator's own reply, echoed back
        wid = str(ev["wp_id"])
        topic, ref = R.wp_topic(wid), {"kind": "wp", "wp_id": wid}
        if ev.get("watched") or ev.get("mention"):
            stream = self.chat.stream_for(ev.get("board"), R.PROJECTS)
            mid = self.chat.send(stream, topic, R.pm_event_message(ev))
            self.st.link(mid, stream, topic, ref)
        known = wid in self.st.d["pm_blocked"] or any(wid in v for v in self.st.d["blocked"].values())
        if ev.get("blocked") and not known:
            mid = self.chat.send(R.ALERTS, topic, R.pm_alert(ev, "blocked"))
            self.st.link(mid, R.ALERTS, topic, ref)
            self.st.d["pm_blocked"] = (self.st.d["pm_blocked"] + [wid])[-KEEP:]
        elif ev.get("mention"):
            mid = self.chat.send(R.ALERTS, topic, R.pm_alert(ev, "mention"))
            self.st.link(mid, R.ALERTS, topic, ref)
        if ev.get("to") and ev["to"] != "Blocked" and wid in self.st.d["pm_blocked"]:
            self.st.d["pm_blocked"].remove(wid)          # unblocked: a later block alerts again

    def comment_wp(self, ref, msg, where):
        """The operator's reply in a work package topic → an OpenProject comment, through the
        engine. ✅ on their message says it landed; a failure comes back as a DM (_guard)."""
        why = self.refusal()
        if why:
            self._reply(where, why)
            return
        self.eng.post("/pm-comment", {"wp_id": int(ref["wp_id"]), "text": msg.get("content", "")}, timeout=30)
        self.chat.react(msg.get("id"), "check")

    # outbound: console alerts ────────────────────────────────────────────────────────────────
    def sync_alerts(self):
        alerts = [a for a in self.eng.briefing() if not a.get("key", "").startswith("approvals:")]
        current = {R.alert_key(a["key"]): a for a in alerts}
        known = self.st.d["alerts"]
        for k, a in current.items():
            if k not in known:
                icon = "🔴" if a.get("severity") == "critical" else "🟠"
                self.chat.send(R.ALERTS, "system", f"{icon} {R.esc(a.get('text'), 300)}")
        for k in list(known):
            if k not in current:
                self.chat.send(R.ALERTS, "system", f"🟢 Recovered: {R.esc(known[k], 200)}")
        self.st.d["alerts"] = {k: a.get("text") for k, a in current.items()}
        self.st.save()

    # outbound: digest ────────────────────────────────────────────────────────────────────────
    def maybe_digest(self, now=None):
        now = now or dt.datetime.now(TZ)
        for slot in DIGEST_AT:
            h, m = (int(x) for x in slot.split(":"))
            at = now.replace(hour=h, minute=m, second=0, microsecond=0)
            key = at.strftime("%Y-%m-%d %H:%M")
            if at <= now < at + dt.timedelta(minutes=30) and self.st.d["digest_last"] < key:
                self.st.d["digest_last"] = key
                self.st.save()
                text = self._scheduled_digest_text(slot, now)
                self.chat.send(R.DIGEST, now.strftime("%Y-%m-%d"), text)

    def _scheduled_digest_text(self, slot, now):
        """Picks what the SCHEDULED #digest post is, for one slot. Unarmed (the default) always
        returns the old plain summary(), so turning this feature on never changes a live cron post
        on its own — it only takes effect once a human reads a `briefing`/`friday review` DM and
        sets NOMAD_RELAY_BRIEFING_ARMED=1."""
        if BRIEFING_ARMED and DIGEST_AT and slot == DIGEST_AT[0]:
            return self.briefing_text("morning")
        if BRIEFING_ARMED and DIGEST_AT and slot == DIGEST_AT[-1] and now.weekday() == 4:
            return self.briefing_text("friday")
        return self.summary(hours=12, title=f"Digest {slot}")

    def briefing_text(self, mode="morning"):
        """Calendar, mail needing a reply (via n8n's existing OAuth2 creds), open tasks and the
        gate queue (via the engine); `mode=friday` adds what ran this week. Read-only: it only
        fetches. A section that fails to load says so by name — it never renders as 'nothing
        today'."""
        try:
            data = self.eng.briefing_data(mode)
            reached = True
        except Exception as e:
            data = {"calendar_ok": False, "calendar_error": f"n8n unreachable: {R.clip(e, 200)}",
                    "mail_ok": False, "mail_error": f"n8n unreachable: {R.clip(e, 200)}"}
            reached = False
        runs = self.eng.get("/runs").get("runs", [])
        pending = [r for r in runs if r.get("status") == "awaiting-approval"]
        try:
            projects = self.eng.get("/project-goals").get("projects", [])
        except Exception:
            projects = []
        sections = [R.calendar_section(data, tz=TZ), R.mail_section(data),
                    R.open_tasks_section(projects), self._board_section(mode), R.gate_queue_section(pending)]
        if mode == "friday":
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7)

            def within(r):
                try:
                    return dt.datetime.fromisoformat(r.get("created_at")) >= cutoff
                except Exception:
                    return False
            executed = [r for r in runs if r.get("status") == "executed" and within(r)]
            failed_n = len([r for r in runs if r.get("status") == "failed" and within(r)])
            sections.append(R.moved_this_week_section(executed, failed_n))
        note = None if reached else ("⚠ Could not reach the n8n briefing source at all — the "
                                     "calendar/mail lines above are that failure, not an empty day.")
        generated = data.get("generated_at") or dt.datetime.now(TZ).isoformat()
        return R.briefing_message(mode, generated, sections, note)

    def _board_section(self, mode):
        try:
            return R.board_section(self.eng.get("/pm-summary"), friday=mode == "friday")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 404:
                return None                              # an engine without P4: leave it out
            return R.board_section(None, error=str(e))
        except Exception as e:
            return R.board_section(None, error=str(e))

    def summary(self, hours=None, title="Status"):
        runs = self.eng.get("/runs").get("runs", [])
        if hours:
            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)

            def recent(r):
                try:
                    return dt.datetime.fromisoformat(r.get("created_at")) >= cutoff
                except Exception:
                    return False
            window = [r for r in runs if recent(r)]
        else:
            window = runs
        counts = {}
        for r in window:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        pending = [r for r in runs if r.get("status") == "awaiting-approval"]
        busy = [r for r in runs if r.get("status") in ("new", "executing")]
        failed = [r for r in runs if r.get("status") == "failed"][:5]
        out = [f"**{title}**"]
        span = f"last {hours} h" if hours else f"last {len(runs)} runs"
        out.append(f"Runs ({span}): " + (", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "none"))
        out.append(f"🛂 Waiting at the gate: **{len(pending)}**")
        out += [f"- `{r['run_id']}` {R.esc(r.get('lane'), 20)}: {R.esc((r.get('proposal') or {}).get('preview'), 80)}"
                for r in pending[:8]]
        if busy:
            out.append(f"▶️ In flight: {len(busy)}")
        if failed:
            out.append("💥 Failed (retry with `retry <run>`):")
            out += [f"- `{r['run_id']}` {r.get('lane')}" for r in failed]
        try:
            projects = self.eng.get("/project-goals").get("projects", [])
            if projects:
                out.append("📈 Projects: " + " · ".join(f"{R.esc(p['title'], 60)} {p['done']}/{p['total']}" for p in projects))
        except Exception:
            pass
        if self.st.d["alerts"]:
            out.append("🔴 Active alerts: " + " · ".join(R.esc(v, 120) for v in self.st.d["alerts"].values()))
        return "\n".join(out)

    # inbound ─────────────────────────────────────────────────────────────────────────────────
    def handle_event(self, ev):
        t = ev.get("type")
        if t == "reaction" and ev.get("op") == "add" and ev.get("user_id") == self.operator_id \
                and ev.get("reaction_type") == "unicode_emoji":
            ref = self.st.by_message(ev.get("message_id"))
            intent = R.reaction_intent(ev.get("emoji_code"))
            if ref and intent:
                self._submit(intent, {**ref, "message_id": ev.get("message_id")}, self._topic_of(ev.get("message_id")))
        elif t == "message":
            msg = ev.get("message", {})
            if msg.get("sender_id") != self.operator_id or msg.get("sender_id") == self.bot_id:
                return                                   # only the operator can steer NOMAD
            if msg.get("type") == "private":
                self.pool.submit(self._guard, self.command, msg.get("content", ""))
            elif msg.get("type") == "stream":
                ref = self.st.by_topic(msg.get("display_recipient"), msg.get("subject", ""))
                if ref and ref.get("kind") == "wp":
                    self.pool.submit(self._guard, self.comment_wp, ref, msg,
                                     (msg.get("display_recipient"), msg.get("subject")))
                    return
                intent = R.topic_reply_intent(msg.get("content"))
                if ref and intent:
                    self._submit(intent, ref, (msg.get("display_recipient"), msg.get("subject")))

    def _topic_of(self, message_id):
        for key, ref in self.st.d["topics"].items():
            if ref.get("message_id") == message_id:
                return tuple(key.split("\x1f", 1))
        return None

    def _submit(self, intent, ref, where):
        self.pool.submit(self._guard, self.act, intent, ref, where)

    def _guard(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:
            log("action error:", e)
            traceback.print_exc()
            try:
                self.chat.dm(self.operator_id, f"⚠️ That didn't go through: `{R.clip(e, 300)}`")
            except Exception:
                pass

    def _reply(self, where, text):
        if where:
            self.chat.send(where[0], where[1].removeprefix(R.RESOLVED), text)
        else:
            self.chat.dm(self.operator_id, text)

    def refusal(self):
        """Why nothing from Zulip may be acted on right now, or None."""
        if self.st.d.get("locked"):
            return "🔒 Locked down. Unlock on the host: `nomad-relay/.venv/bin/python relay.py --unlock`."
        if self.strangers:
            return (f"🔒 The org has other active accounts ({self.strangers}); acting on nothing until "
                    "they're deactivated.")
        return None

    def act(self, intent, ref, where=None):
        """One operator decision → one engine call. State is re-checked against the engine first so
        a stale message can never approve something that has already moved on."""
        why = self.refusal()
        if why:
            self._reply(where, why)
            return
        key = ref.get("run_id") or f"task:{ref.get('task_id')}"
        with self.flight_lock:
            if key in self.in_flight:
                return                                   # a double tap: the first one is handling it
            self.in_flight.add(key)
        try:
            self._act(intent, ref, where)
        finally:
            with self.flight_lock:
                self.in_flight.discard(key)

    def _gate_ref(self, rid):
        mid = (self.st.d["runs"].get(rid) or {}).get("gate_msg")
        ref = self.st.by_message(mid) if mid else None
        return (ref, mid) if ref and ref.get("kind") == "gate" else (None, None)

    def _act(self, intent, ref, where):
        if ref.get("kind") == "wp":
            return                                       # work package topics take replies, not reactions
        if ref.get("kind") == "blocked" or ref.get("task_id"):
            if intent not in ("retry", "skip"):
                return
            r = self.eng.post("/task-action", {"task_id": ref["task_id"], "action": intent}, timeout=60)
            self._reply(where, f"{'🔁 Retrying' if intent == 'retry' else '⏭️ Skipped'} task "
                               f"`{ref['task_id']}`: {r.get('status') or r.get('error') or 'ok'}")
            self.wake.set()
            return
        rid = ref["run_id"]
        status = self.eng.get("/run", id=rid).get("status")
        if intent in ("approve", "reject"):
            if status != "awaiting-approval":
                self._reply(where, f"Run `{rid}` is already `{status}`; nothing to decide.")
                return
            body = {"run_id": rid, "decision": "rejected"}
            if intent == "approve":
                # Approval is only ever of a gate message this relay posted, still unedited, showing
                # the whole proposal, for an action allowed remotely.
                gate, mid = (ref, ref.get("message_id")) if ref.get("kind") == "gate" else self._gate_ref(rid)
                if not gate or not mid:
                    self._reply(where, f"Run `{rid}` has no gate message here; approve it in the cockpit"
                                       + self._at(rid))
                    return
                if not gate.get("approvable") or not gate.get("proposal_hash"):
                    self._reply(where, f"🔒 Run `{rid}` can only be approved in the cockpit" + self._at(rid))
                    return
                if _sha(self.chat.raw_content(mid)) != gate.get("shown"):
                    self._reply(where, f"🔒 The gate message for `{rid}` was edited; approve it in the cockpit"
                                       + self._at(rid))
                    return
                body = {"run_id": rid, "decision": "approved", "expect_hash": gate["proposal_hash"]}
            decision = body["decision"]
            self._reply(where, "✅ Approved. Executing…" if decision == "approved" else "❌ Rejected.")
            r = self.eng.post("/resume", body)
            if decision == "approved":
                msg = f"Result: `{r.get('status')}`"
                if r.get("error"):
                    msg += f"\n{R._fence(R.clip(r['error'], 800))}"
                self._reply(where, msg)
        elif intent == "retry":
            if status != "failed":
                self._reply(where, f"Run `{rid}` is `{status}`; only failed runs can be retried.")
                return
            r = self.eng.post("/retry", {"run_id": rid}, timeout=60)
            self._reply(where, f"🔁 Back at the gate: `{r.get('status') or r.get('error')}`. Watch #gate.")
        self.wake.set()

    def _at(self, rid):
        """": <run page>" to end an "approve it in the cockpit" reply, or "." without a cockpit."""
        url = R.run_url(self.cockpit, rid)
        return f": {url}" if url else "."

    def _run_by_prefix(self, prefix):
        hits = [rid for rid in self.st.d["runs"] if rid.startswith(prefix)]
        if not hits:
            hits = [r["run_id"] for r in self.eng.get("/runs").get("runs", []) if r["run_id"].startswith(prefix)]
        return hits

    def command(self, text):
        verb, arg = R.parse_command(text)
        if verb == "help":
            self.chat.dm(self.operator_id, R.HELP)
        elif verb == "lockdown":
            self.st.d["locked"] = True
            self.st.save()
            self.chat.dm(self.operator_id, "🔒 Locked down. I'll keep posting but act on nothing from Zulip. "
                                           "Unlock on the host: `nomad-relay/.venv/bin/python relay.py --unlock`.")
        elif self.refusal():
            self.chat.dm(self.operator_id, self.refusal())
        elif verb == "status":
            self.chat.dm(self.operator_id, self.summary())
        elif verb == "briefing":
            self.chat.dm(self.operator_id, self.briefing_text("morning"))
        elif verb == "friday_review":
            self.chat.dm(self.operator_id, self.briefing_text("friday"))
        elif verb == "pending":
            pend = [r for r in self.eng.get("/runs").get("runs", []) if r.get("status") == "awaiting-approval"]
            self.chat.dm(self.operator_id, "Nothing waiting at the gate." if not pend else "\n".join(
                f"- `{r['run_id']}` {R.esc(r.get('lane'), 20)}: {R.esc((r.get('proposal') or {}).get('preview'), 100)}"
                for r in pend))
        elif verb == "projects":
            ps = self.eng.get("/project-goals").get("projects", [])
            self.chat.dm(self.operator_id, "\n".join(
                f"- **{R.esc(p['title'], 80)}** (`{p['goal_id']}`): {p['done']}/{p['total']}, {p['pct']}%" for p in ps)
                or "No planned projects.")
        elif verb == "next":
            ps = self.eng.get("/project-goals").get("projects", [])
            match = [p for p in ps if str(p["goal_id"]) == arg or p["title"].lower().startswith(arg.lower())]
            if len(match) != 1:
                self.chat.dm(self.operator_id, f"Which project? Matches: {[p['title'] for p in match] or 'none'}")
                return
            r = self.eng.post("/run-next-task", {"goal_id": match[0]["goal_id"]}, timeout=600)
            self.chat.dm(self.operator_id, f"Queued the next task for **{match[0]['title']}**: "
                                           f"`{r.get('status') or r.get('error') or r}`")
            self.wake.set()
        elif verb in ("approve", "reject", "retry"):
            hits = self._run_by_prefix(arg)
            if len(hits) != 1:
                self.chat.dm(self.operator_id, f"Run `{arg}` matched {len(hits)} runs; use more characters.")
                return
            self.act(verb, {"kind": "run", "run_id": hits[0]})
        elif verb in ("retry_task", "skip_task"):
            self.act(verb.split("_")[0], {"kind": "blocked", "task_id": arg})
        else:
            r = self.eng.post("/chat", {"message": arg, "session_id": "zulip"}, timeout=300)
            self.chat.dm(self.operator_id, r.get("reply") or "(no reply)")
            if r.get("captured") or r.get("planned"):
                self.wake.set()

    # loops ───────────────────────────────────────────────────────────────────────────────────
    def sse_loop(self):
        while True:
            try:
                with requests.get(ENGINE + "/events", stream=True, timeout=(5, 90)) as r:
                    for line in r.iter_lines(decode_unicode=True):
                        if line and line.startswith("data:") and "bump" in line:
                            self.wake.set()
            except Exception:
                time.sleep(5)

    def runs_loop(self):
        while True:
            self.wake.wait(POLL)
            self.wake.clear()
            try:
                self.sync_runs()
                beat()          # only on success: the point is "did its job", not "is running"
            except Exception as e:
                log("runs sync failed:", e)
                time.sleep(5)
            try:
                self.sync_pm()
            except Exception as e:
                log("pm sync failed:", e)

    def check_org(self):
        """Fail closed if anyone besides the operator can log in to this org."""
        others = [u for u in self.chat.humans() if u != self.operator_id]
        if others != self.strangers:
            self.strangers = others
            if others:
                log("other active accounts in the org, acting on nothing:", others)
                self.chat.dm(self.operator_id, self.refusal())

    def slow_loop(self):
        while True:
            for step in (self.check_org, self.sync_projects, self.sync_alerts, self.maybe_digest):
                try:
                    step()
                except Exception as e:
                    log(step.__name__, "failed:", e)
            time.sleep(POLL)

    def event_loop(self, client):
        auth_fails = 0
        while True:
            try:
                q = client.register(event_types=["message", "reaction"], apply_markdown=False)
                if q.get("result") != "success":
                    raise RuntimeError(q.get("msg"))
                qid, last = q["queue_id"], q["last_event_id"]
                auth_fails = 0                      # registering proves the key is good
                log("zulip event queue registered")
                while True:
                    res = client.get_events(queue_id=qid, last_event_id=last)
                    if res.get("result") != "success":
                        if res.get("code") == "BAD_EVENT_QUEUE_ID":
                            break
                        raise RuntimeError(res.get("msg"))
                    for ev in res.get("events", []):
                        last = max(last, ev["id"])
                        self.handle_event(ev)
            except Exception as e:
                log("event loop:", e)
                if is_auth_error(e):
                    auth_fails += 1
                    if auth_fails >= AUTH_FAIL_LIMIT:
                        log(f"{auth_fails} consecutive auth failures — exiting so systemd restarts "
                            f"us and re-reads {ZULIPRC}. If this repeats, the key is wrong, not stale.")
                        raise SystemExit(3)
                else:
                    auth_fails = 0                  # a blip, not a bad key
                time.sleep(5)


def _sha(text):
    # Line-trimmed, so Zulip's whitespace normalization can't read as an edit.
    norm = "\n".join(line.rstrip() for line in (text or "").strip().splitlines())
    return hashlib.sha256(norm.encode()).hexdigest()


def connect(zulip):
    """The startup calls that need Zulip up: client, operator lookup, our own id."""
    chat = Chat(zulip.Client(config_file=ZULIPRC))
    return chat, chat.user_id(OPERATOR), chat.whoami()


def main():
    import zulip
    if "--unlock" in sys.argv:
        st = State()
        st.d["locked"] = False
        st.save()
        return print("unlocked; restart with: systemctl --user restart nomad-relay")
    if not OPERATOR or not OPERATOR_ID:
        raise SystemExit("NOMAD_ZULIP_OPERATOR and NOMAD_ZULIP_OPERATOR_ID are required (bootstrap.py writes them)")
    chat, operator_id, bot_id = until_up(lambda: connect(zulip))
    if str(operator_id) != OPERATOR_ID:            # an email can be reassigned; the id can't
        raise SystemExit(f"{OPERATOR} is user {operator_id}, not the pinned {OPERATOR_ID}; refusing to start")
    chat.principals = [operator_id, bot_id]       # who a new project stream is made for
    relay = Relay(Engine(), chat, State(), operator_id=operator_id, bot_id=bot_id)
    relay.check_org()
    log(f"nomad-relay up: engine {ENGINE}, operator {OPERATOR} ({relay.operator_id})")
    for fn in (relay.sse_loop, relay.runs_loop, relay.slow_loop):
        threading.Thread(target=fn, daemon=True).start()
    relay.wake.set()
    # its own client: long-polls hold a connection
    relay.event_loop(until_up(lambda: zulip.Client(config_file=ZULIPRC)))


if __name__ == "__main__":
    main()
