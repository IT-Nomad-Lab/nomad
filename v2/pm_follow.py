"""NOMAD v2 · follow OpenProject from Zulip (PM-01 P4).

OpenProject posts a signed webhook here for every work package journal, after its one-minute
aggregation. This module turns each one into a small event: who changed what on which work
package, whether the operator watches it or was mentioned, and whether it just became Blocked. The
relay reads the events from /pm-events and decides which Zulip channel each one goes to.

The engine holds the OpenProject tokens and the relay holds none. That is why the relay reads
events from here instead of receiving the webhook itself, and why a reply typed in Zulip reaches
OpenProject through post_comment below.

Everything here reads as nomad-manager. A project the manager is not a member of cannot be read,
so its events are counted and dropped. To follow such a project, add nomad-manager to it.
"""
import collections
import datetime as dt
import hashlib
import hmac
import os
import re
import threading
import uuid

from pm_openproject import MANAGER, PMError, _href_id, _title

SECRET_FILE = os.path.expanduser(os.environ.get(
    "NOMAD_PM_HOOK_SECRET_FILE",
    "/term-secret/openproject-webhook.secret" if os.path.exists("/term-secret")
    else "~/.config/nomad/openproject-webhook.secret"))
OPERATOR_ID = int(os.environ.get("NOMAD_PM_OPERATOR_ID") or 0) or None
PUBLIC_URL = os.environ.get("NOMAD_PM_PUBLIC_URL", "http://localhost:8097").rstrip("/")
HOOKS = {"work_package:created", "work_package:updated", "work_package_comment:comment"}
KEEP = 500
WINDOW = dt.timedelta(minutes=10)   # how far back one work package hook looks
ZULIP_PREFIX = "**Operator, via Zulip:**\n\n"

# The operator answers a task in Awaiting approval with a comment that starts with a keyword
# (docs/prd/autopilot-approval.md 5.2). "Approve? Not sure yet" is not one: the keyword has to be
# followed by the end, a space, a colon or a dash.
AWAITING = "Awaiting approval"
DECISIONS = {"approve": "Approved", "approved": "Approved",
             "reject": "Changes requested", "rejected": "Changes requested"}
_KEYWORD = re.compile(r"^(approved?|reject(?:ed)?)(?=$|[\s:\-\u2013\u2014])", re.I)
_ZULIP = re.compile(r"^\s*\*\*Operator, via Zulip:\*\*\s*")

# Activity details come back as plain sentences: "Status changed from Ready to Blocked" or, on
# creation, "Status set to New". Markdown emphasis is tolerated in case the format grows it.
_STATUS = re.compile(r"^\W*Status\W*\s+(?:changed from\s+\W*(.+?)\W*\s+to|set to)\s+\W*(.+?)\W*$")
_MENTION = re.compile(r"<mention\b([^>]*)>(.*?)</mention>", re.S)
_ATTR = re.compile(r'([\w-]+)="([^"]*)"')


def _when(act):
    try:
        return dt.datetime.fromisoformat(act["createdAt"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, ValueError):
        return None


def read_secret(path=None):
    try:
        with open(path or SECRET_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def verify(secret, body: bytes, header) -> bool:
    """OpenProject signs the raw body: X-OP-Signature: sha1=<hex HMAC-SHA1>. No secret means no
    webhook at all, never an unsigned one."""
    if not secret or not header:
        return False
    want = "sha1=" + hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()
    return hmac.compare_digest(want, header.strip())


def status_change(details):
    """(from, to) out of an activity's details, or None. `from` is None on creation."""
    for d in details or []:
        m = _STATUS.match((d.get("raw") or "").strip())
        if m:
            return m.group(1), m.group(2)
    return None


def mentioned_users(raw):
    ids = set()
    for attrs, _ in _MENTION.findall(raw or ""):
        a = dict(_ATTR.findall(attrs))
        if a.get("data-type") == "user" and a.get("data-id", "").isdigit():
            ids.add(int(a["data-id"]))
    return ids


def decision(text):
    """The status a comment asks for (Approved, Changes requested), or None."""
    m = _KEYWORD.match((text or "").lstrip())
    return DECISIONS[m.group(1).lower()] if m else None


def plain(raw):
    """A comment as Zulip should show it: mention tags become their @text."""
    return _MENTION.sub(lambda m: re.sub(r"<[^>]+>", "", m.group(2)), raw or "").strip()


class Follow:
    def __init__(self, op, operator_id=OPERATOR_ID, keep=KEEP, mentions=None):
        self.op = op
        self.mentions = mentions            # pm_mention.Mentions: a bot mentioned runs a session
        self.operator_id = operator_id
        self.boot = uuid.uuid4().hex[:8]    # a restart empties the queue; the relay sees a new boot
        self.seq = 0
        self.events = collections.deque(maxlen=keep)
        self.seen = collections.OrderedDict()      # activity ids already turned into an event
        self.ours = collections.OrderedDict()      # activity ids this engine wrote from Zulip
        self.dropped = 0
        self.lock = threading.Lock()
        self.keep = keep

    def _remember(self, m, key):
        m[key] = True
        while len(m) > self.keep:
            m.popitem(last=False)

    def ingest(self, payload):
        """One webhook body → its events; returns the newest, or None when there is nothing to
        tell. A status change and its comment arrive as two hooks for one aggregated activity, so
        the second is a repeat. A work package hook arrives a minute or more after its change, by
        which time the next change may be in too, so it takes every activity not yet seen: back
        from the newest, stopping at a seen one or at WINDOW before the newest, so the first hook
        on an old work package never replays its history."""
        action = (payload or {}).get("action")
        if action not in HOOKS:
            return None
        hooked = payload.get("activity") if action == "work_package_comment:comment" else None
        wp_id = _href_id(hooked["_links"].get("workPackage")) if hooked else (payload.get("work_package") or {}).get("id")
        if not wp_id:
            return None
        try:
            wp = self.op.get(wp_id)
            acts = [hooked] if hooked else self._unseen(self.op.activities(wp_id))
        except PMError:
            self.dropped += 1       # a project nomad-manager cannot read
            return None
        last = None
        for act in acts:
            last = self._event(wp, act, action) or last
        return last

    def _unseen(self, acts):
        if not acts:
            return []
        newest = _when(acts[-1])
        out = []
        for a in reversed(acts):
            if a["id"] in self.seen or (newest and _when(a) and newest - _when(a) > WINDOW):
                break
            out.append(a)
        return out[::-1]

    def _event(self, wp, act, action):
        with self.lock:
            if act["id"] in self.seen:
                return None
            self._remember(self.seen, act["id"])
        change = status_change(act.get("details"))
        raw = (act.get("comment") or {}).get("raw") or ""
        created = action == "work_package:created" or bool(change and change[0] is None)
        if not (change or raw.strip() or created):
            return None             # a field edit: the relay has nothing useful to say about it
        user_href = (act["_links"].get("user") or {}).get("href")
        author = self._name(user_href)
        status = _title(wp, "status")
        ev = {
            "wp_id": wp["id"], "subject": wp.get("subject"), "type": _title(wp, "type"),
            "project": _title(wp, "project"), "board": self._board(wp),
            "status": status, "lane": self._lane(wp),
            "action": "created" if created else "updated",
            "from": change[0] if change else None, "to": change[1] if change else None,
            "blocked": bool(change and change[1] == "Blocked" and change[0] != "Blocked"),
            "closed": self._closed(status),
            "comment": plain(raw)[:1500], "author": author,
            "by_agent": bool(author and author.endswith("(agent)")),
            "by_operator": bool(self.operator_id and _href_id({"href": user_href}) == self.operator_id),
            "mention": bool(self.operator_id and self.operator_id in mentioned_users(raw)),
            "watched": self._watched(wp["id"]),
            "origin": "zulip" if act["id"] in self.ours else "openproject",
            "activity_id": act["id"], "url": f"{PUBLIC_URL}/work_packages/{wp['id']}/activity",
        }
        ev["decided"] = self._decide(wp, status, raw, ev["by_operator"], act)
        ev["session"] = None
        if self.mentions and change and ev["closed"] and not self._closed(change[0] or ""):
            self.mentions.task_closed(wp["id"])     # drop its session worktree if it is clean
        if self.mentions and raw.strip():
            try:
                ev["session"] = self.mentions.consider(wp, act, raw, ev["by_operator"], ev["closed"])
            except PMError:
                pass
        with self.lock:
            self.seq += 1
            ev["seq"] = self.seq
            self.events.append(ev)
        return ev

    def since(self, after=0, boot=None):
        with self.lock:
            after = int(after or 0) if boot == self.boot else 0
            return {"boot": self.boot, "seq": self.seq, "dropped": self.dropped,
                    "events": [e for e in self.events if e["seq"] > after]}

    def post_comment(self, wp_id, text):
        """The operator's Zulip reply, as a comment by nomad-manager with a byline. The engine
        has no operator token, and should not: nomad-manager cannot delete anything."""
        text = (text or "").strip()
        if not text:
            raise ValueError("empty comment")
        act = self.op.comment(int(wp_id), ZULIP_PREFIX + text[:5000], as_=MANAGER) or {}
        if act.get("id"):
            with self.lock:
                self._remember(self.ours, act["id"])
        return {"ok": True, "wp_id": int(wp_id), "activity_id": act.get("id")}

    def _by_manager(self, act):
        author = _href_id((act.get("_links") or {}).get("user"))
        try:
            manager = self.op.user_id_of(MANAGER)
        except PMError:
            return False
        return bool(author and author == manager)

    def _decide(self, wp, status, raw, by_operator, act):
        """Move a task in Awaiting approval when the operator's comment, or their Zulip reply,
        starts with approve or reject. Returns the new status, or None. Each activity comes
        through here once (self.seen), so a comment moves the task at most once."""
        if status != AWAITING or not raw.strip():
            return None
        # The prefix alone could be typed by anyone, so a relayed reply counts only when the
        # manager posted it, as post_comment does.
        relayed = _ZULIP.match(raw) if self._by_manager(act) else None
        if not (by_operator or relayed):
            return None
        target = decision(plain(raw[relayed.end():] if relayed else raw))
        if not target:
            return None
        try:
            self.op.move(wp["id"], target, as_=MANAGER)
        except PMError:
            return None
        return target

    # ── best-effort lookups: a miss leaves a field empty, never drops the event ──
    def _name(self, href):
        try:
            return self.op.user_name(href)
        except PMError:
            return None

    def _board(self, wp):
        """The board identifier, so the relay can post to that project's stream. None on a miss:
        the relay then falls back to #projects."""
        try:
            return self.op.board_of(wp)
        except (PMError, KeyError):
            return None

    def _lane(self, wp):
        try:
            return self.op.lane_of(wp)
        except (PMError, KeyError):
            return None

    def _closed(self, status):
        try:
            return self.op.closed(status)
        except PMError:
            return False

    def _watched(self, wp_id):
        if not self.operator_id:
            return False
        try:
            return self.operator_id in self.op.watcher_ids(wp_id)
        except PMError:
            return False
