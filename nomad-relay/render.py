"""Pure pieces of the relay: message text, topic names, command parsing, emoji mapping.

No I/O here, so every rule about what the operator sees and what a reaction means is unit-testable
without Zulip or the engine running.
"""
import datetime as _dt
import json
import re
import urllib.parse

# Channels. #gate and #alerts push to the phone; the rest are read on demand (set by bootstrap.py).
GATE, ALERTS, RUNS, PROJECTS, DIGEST = "gate", "alerts", "runs", "projects", "digest"
CHANNELS = {
    GATE: ("Runs waiting for your approval. React ✅ or ❌.", True),
    ALERTS: ("Failures, blocked tasks, services down.", True),
    RUNS: ("Every run's lifecycle, one topic per run.", False),
    PROJECTS: ("Project progress, one topic per project.", False),
    DIGEST: ("Morning and evening summaries.", False),
}

RESOLVED = "✔ "          # Zulip's resolved-topic prefix
TOPIC_MAX = 60

# Reactions are matched on the unicode codepoint, not the emoji name (names differ across sets).
APPROVE = {"2705", "2714"}            # ✅ ✔️
REJECT = {"274c", "2716"}             # ❌ ✖️
RETRY = {"1f501", "1f504"}            # 🔁 🔄
SKIP = {"23ed"}                       # ⏭️


def reaction_intent(emoji_code: str):
    code = (emoji_code or "").lower().split("-")[0]
    if code in APPROVE:
        return "approve"
    if code in REJECT:
        return "reject"
    if code in RETRY:
        return "retry"
    if code in SKIP:
        return "skip"
    return None


def clip(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# Topics carry ids only. Goal and project titles come from untrusted input (email, web pages) and
# topic names are visible in lock-screen previews and the channel list. The one name allowed in is
# the OpenProject board identifier, which the operator chose when making the board (#776). A dev
# run's repo is not: the Manager writes it from the goal text, so it stays inside the message.
_BOARD = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


# Per-project streams (#1123). A board's traffic (wp and run topics, autopilot) goes to #<board>.
# A missing board, one that isn't a safe identifier, or one that would collide with a shared
# channel falls back to the shared stream the caller names.
RESERVED = set(CHANNELS) | {"autopilot", "general", "nomad-portfolio"}


def project_stream(board, fallback: str) -> str:
    board = str(board or "")
    return board if _BOARD.match(board) and board not in RESERVED else fallback


def project_stream_description(board: str) -> str:
    return f"Everything NOMAD does on the {board} board: work packages, runs, autopilot."


def run_topic(detail: dict) -> str:
    rid = f"run {(detail.get('run_id') or '?')[:12]}"
    board = ((detail.get("project") or {}).get("board") or "")
    return f"{board} · {rid}" if _BOARD.match(board) else rid


def project_line(d: dict) -> str:
    """Which project a run is for, as the engine's /run reports it. Says "no project" when there is
    none, so a missing line never reads as a relay that forgot to say."""
    p = d.get("project") or {}
    bits = []
    if p.get("board") or p.get("project_name"):
        bits.append(code(p.get("board") or "?", 40)
                    + (f" ({esc(p['project_name'], 60)})" if p.get("project_name") else ""))
    if p.get("wp_id"):
        wp = f"#{int(p['wp_id'])}" + (f" {esc(p['wp_subject'], 120)}" if p.get("wp_subject") else "")
        bits.append(f"[{wp}]({p['wp_url']})" if p.get("wp_url") else wp)
        if p.get("pm_error"):
            bits.append("board did not answer")
    elif p.get("task_ref"):
        bits.append(f"mission-control task {code(p['task_ref'], 20)}")
    if p.get("repo"):
        bits.append(f"repo {code(p['repo'], 60)}")
    return "**Project:** " + (" · ".join(bits) if bits else "no project")


def project_topic(goal_id) -> str:
    return f"project {goal_id}"


def wp_topic(wp_id) -> str:
    """One topic per OpenProject work package, in #projects and in #alerts (PM-01 P4)."""
    return f"wp {wp_id}"


_ZWSP = "\u200b"


def esc(text, n) -> str:
    """One line of untrusted text, safe for inline markdown: clipped, no mentions, no markup."""
    t = clip(text, n).replace("@", "@" + _ZWSP)
    return re.sub(r"([\\`*_~\[\]()<>#|$>])", r"\\\1", t)


def code(text, n) -> str:
    """Untrusted text for an inline `code` span, where nothing renders but a backtick ends it."""
    return "`" + clip(text, n).replace("`", "'") + "`"


def _fence(text: str, kind: str = "text") -> str:
    """A code block: nothing inside renders (no links, mentions, images, or fake instructions)."""
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{kind}\n{text}\n{ticks}"


def preview(args: dict) -> str:
    a = args or {}
    return str(a.get("body") or a.get("brief") or a.get("content") or a.get("plan")
               or a.get("task") or a.get("prompt") or "").strip()


PREVIEW_KEYS = ("body", "brief", "content", "plan", "task", "prompt")


def _arg_text(v) -> str:
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)


def run_url(cockpit: str | None, run_id) -> str | None:
    """The run page in the cockpit (#799, #800), or None when the relay has no cockpit address.

    `cockpit` is the cockpit's tailnet origin, so the link opens on the phone. The run id is the
    engine's own, but it is quoted anyway: it ends up in a link the operator taps."""
    if not cockpit or not run_id:
        return None
    return f"{cockpit.rstrip('/')}/cockpit/run/{urllib.parse.quote(str(run_id), safe='')}"


def gate_view(d: dict, remote_ok: bool = True, limit: int = 1500, arg_limit: int = 300,
              cockpit: str | None = None):
    """→ (message, complete). `complete` is False when anything the engine would execute had to be
    cut to fit; the relay then refuses remote approval, because you'd be approving text you didn't see.

    With `cockpit` set, the message ends with the run page's link (R6), on every gate message, the
    ones Zulip can approve too. The link is part of the text the relay hashes as "shown", so it
    does not weaken the edit check. The URL stays last on its line so no punctuation joins it."""
    a = d.get("args") or {}
    complete = True
    lines = [f"**🛂 Approval needed** · {code(d.get('lane'), 20)} · run `{d.get('run_id')}`",
             project_line(d),
             f"**Action:** {code(d.get('action'), 40)}",
             "**Goal:**", _fence(clip(d.get("goal") or d.get("intent"), 300))]
    body_key = next((k for k in PREVIEW_KEYS if a.get(k)), None)
    rest = [(k, _arg_text(v)) for k, v in a.items() if k != body_key]
    if rest:
        shown = []
        for k, v in rest:
            if len(v) > arg_limit:
                complete = False
                v = v[:arg_limit] + "…"
            shown.append(f"{k}: {v}")
        lines += ["**Arguments:**", _fence("\n".join(shown))]
    if body_key:
        body = _arg_text(a[body_key]).strip()
        if len(body) > limit:
            complete = False
            body = body[:limit].rstrip() + "\n…(truncated)"
        lines += [f"**{body_key.capitalize()}:**", _fence(body)]
    # The engine's own verdict on a build's project (#851): named here, before the approval, instead
    # of surfacing as "Unknown project" after it.
    if d.get("project_warning"):
        lines.append("⚠️ **Project not resolved:** " + esc(d["project_warning"], 600))
    det = d.get("detector") or {}
    if det.get("checked"):
        bits = [f"verdict **{det.get('verdict')}**"]
        if det.get("style_score") is not None:
            bits.append(f"style {det['style_score']}")
        if det.get("statistical") is not None:
            bits.append(f"statistical {det['statistical']:.3f}")
        if det.get("rewritten"):
            bits.append("rewritten once")
        lines.append("**AI-text check:** " + " · ".join(bits))
    url = run_url(cockpit, d.get("run_id"))
    if not remote_ok:
        lines.append(f"🔒 {code(d.get('action'), 40)} is cockpit-only. React ❌ to reject; "
                     + (f"approve it in the cockpit: {url}" if url else "approve it in the cockpit."))
    elif not complete:
        lines.append(f"🔒 Too long to show in full. React ❌ to reject, or approve it in the cockpit: {url}"
                     if url else "🔒 Too long to show in full, so approve it in the cockpit. React ❌ to reject.")
    else:
        lines.append("React ✅ to approve or ❌ to reject, or reply `approve` / `reject` here.")
        if url:
            lines.append(f"Open in cockpit: {url}")
    return "\n".join(lines), complete and remote_ok


def gate_message(d: dict, remote_ok: bool = True, limit: int = 1500, cockpit: str | None = None) -> str:
    return gate_view(d, remote_ok, limit, cockpit=cockpit)[0]


STATUS_LINE = {
    "new": "🟦 captured, specialist is drafting",
    "awaiting-approval": "🛂 waiting at the gate",
    "executing": "▶️ approved, executing",
    "executed": "✅ executed",
    "failed": "💥 failed",
    "declined": "🚫 declined",
}


def status_message(d: dict, status: str, cockpit: str | None = None) -> str:
    line = STATUS_LINE.get(status, f"status → `{status}`")
    url = run_url(cockpit, d.get("run_id"))
    if url:                                  # on the status line itself, so every #runs post has it
        line += f" · [open in cockpit]({url})"
    if status == "awaiting-approval":       # once per run is enough; the topic carries the rest
        line += "\n" + project_line(d)
    if status == "failed" and d.get("error"):
        line += f"\n{_fence(clip(d['error'], 800))}"
    return line


def failed_alert(d: dict, cockpit: str | None = None) -> str:
    url = run_url(cockpit, d.get("run_id"))
    return (f"**💥 Run failed** · {code(d.get('lane'), 20)} · run `{d.get('run_id')}`\n"
            f"{project_line(d)}\n"
            f"**Action:** {code(d.get('action'), 40)}\n"
            f"{_fence(clip(d.get('error') or 'no error text recorded', 800))}\n"
            "React 🔁 to put it back at the gate, or reply `retry`."
            + (f"\nOpen in cockpit: {url}" if url else ""))


def blocked_alert(project: str, task: dict) -> str:
    return (f"**⛔ Task blocked** in **{esc(project, 80)}**: {esc(task.get('title'), 200)} "
            f"(task {code(task.get('task_id'), 20)}, lane {code(task.get('lane'), 20)})\n"
            "React 🔁 to retry it or ⏭️ to skip it, or DM me `retry task "
            f"{task.get('task_id')}` / `skip task {task.get('task_id')}`.")


def progress_message(p: dict, prev: dict | None) -> str:
    if p.get("total") and p.get("done") == p.get("total"):
        return f"🏁 **Complete**: all {p['total']} tasks done."
    was = f" (was {prev.get('done')}/{prev.get('total')})" if prev else ""
    return f"📈 {p.get('done')}/{p.get('total')} tasks done, {p.get('pct')}%{was}"


# ── OpenProject follow (PM-01 P4): one engine event → one Zulip line ────────────────────────────
def _wp_head(ev: dict) -> str:
    return f"{esc(ev.get('type') or 'Work package', 20)} #{int(ev['wp_id'])} {esc(ev.get('subject'), 120)}"


def _said(ev: dict) -> str:
    c = (ev.get("comment") or "").strip()
    return f"\n{_fence(clip_block(c, 1200))}" if c else ""


def clip_block(text: str, n: int) -> str:
    """Like clip, but keeps line breaks, for a comment shown in a block."""
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def pm_event_message(ev: dict) -> str:
    who = esc(ev.get("author") or "someone", 60)
    if ev.get("action") == "created":
        line = f"🆕 {_wp_head(ev)} · created by {who}"
    elif ev.get("to"):
        line = f"🔀 {_wp_head(ev)} · {esc(ev.get('from') or '?', 30)} → **{esc(ev['to'], 30)}** · by {who}"
    else:
        line = f"💬 {_wp_head(ev)} · {who}"
    return line + _said(ev) + f"\n[open in OpenProject]({ev['url']})" * bool(ev.get("url"))


def pm_alert(ev: dict, why: str) -> str:
    who = esc(ev.get("author") or "someone", 60)
    if why == "blocked":
        lane = f", lane {code(ev['lane'], 20)}" if ev.get("lane") else ""
        head = f"**⛔ Blocked**: {_wp_head(ev)} in {esc(ev.get('project'), 60)}{lane} · by {who}"
    else:
        head = f"**📣 {who} mentioned you** on {_wp_head(ev)}"
    return head + _said(ev) + "\nReply in this topic to comment on it in OpenProject."


def board_section(s, error: str | None = None, friday: bool = False):
    """The OpenProject board in the briefing. None means the engine has no board to show, and
    then the section is left out; a failed read says so by name."""
    if error:
        return f"⚠ **Board:** did not load — {esc(error, 300)}"
    if s is None:
        return None
    active = [f"{r['n']} {esc(r['status'], 30)}" for r in s.get("by_status", [])
              if r["status"] in ("In Progress", "Awaiting approval", "In Review", "Blocked")]
    lines = [f"**Board ({esc(s.get('project'), 40)}):** {s.get('open', 0)} open"
             + (" · " + ", ".join(active) if active else "")]
    blocked = s.get("blocked") or []
    if blocked:
        lines.append(f"⛔ Blocked or waiting ({len(blocked)}):")
        for b in blocked[:8]:
            waits = [c for c in b.get("chain", []) if c.get("depth") == 1]
            on = ", waiting on " + ", ".join(f"#{c['id']} {esc(c.get('subject'), 60)} ({esc(c.get('status'), 20)})"
                                             for c in waits[:3]) if waits else ""
            lane = f" [{esc(b['lane'], 20)}]" if b.get("lane") else ""
            lines.append(f"- #{b['id']} {esc(b.get('subject'), 80)}{lane} · {esc(b.get('status'), 20)}{on}")
    if friday and s.get("throughput"):
        w = s["throughput"][-1]
        lines.append(f"Done this week: {w['agent']} by agents, {w['human']} by people.")
    return "\n".join(lines)


# ── T2.1 briefing: calendar/mail (from n8n) + tasks/gate (from the engine) ─────────────────────
# Read-only. Every "nothing here" line only appears when the upstream call actually said so; a
# failed call always renders as a named failure, never as an empty, cheerful section.
def _local_time(iso: str, tz) -> str:
    """HH:MM in `tz`. Calendar events carry their own offset (e.g. +02:00 for a trip), which is not
    necessarily the digest's own timezone — convert rather than slice the raw string, or a
    9am-there event would misread as 9am-here."""
    try:
        return _dt.datetime.fromisoformat(iso).astimezone(tz).strftime("%H:%M")
    except Exception:
        return iso[11:16] if len(iso) >= 16 else iso


def calendar_section(d: dict, tz=None) -> str:
    if not d.get("calendar_ok"):
        return f"⚠ **Calendar:** did not load — {esc(d.get('calendar_error') or 'unknown error', 300)}"
    events = d.get("calendar") or []
    if not events:
        return "**Calendar:** nothing in this window."
    lines = ["**Calendar:**"]
    for e in events[:12]:
        start = e.get("start") or ""
        when = (start[:10] + " (all day)") if e.get("all_day") else _local_time(start, tz) if tz else \
            (start[11:16] if len(start) >= 16 else start)
        lines.append(f"- {when} {esc(e.get('summary') or '(no title)', 120)}")
    return "\n".join(lines)


def mail_section(d: dict) -> str:
    if not d.get("mail_ok"):
        return f"⚠ **Mail:** did not load — {esc(d.get('mail_error') or 'unknown error', 300)}"
    mail = d.get("mail_needing_reply") or []
    if not mail:
        return "**Mail:** nothing unread needing a reply."
    lines = ["**Mail needing a reply:**"]
    for m in mail[:10]:
        lines.append(f"- {esc(m.get('from') or '?', 60)} — {esc(m.get('subject') or '(no subject)', 100)}")
    return "\n".join(lines)


def open_tasks_section(projects: list) -> str:
    open_ps = [p for p in (projects or []) if p.get("done") != p.get("total")]
    if not open_ps:
        return "**Open tasks:** none — every tracked project is complete."
    lines = ["**Open tasks (by project):**"]
    for p in open_ps[:10]:
        remaining = (p.get("total") or 0) - (p.get("done") or 0)
        lines.append(f"- {esc(p.get('title'), 80)}: {remaining} open ({p.get('done')}/{p.get('total')})")
    return "\n".join(lines)


def gate_queue_section(pending: list) -> str:
    if not pending:
        return "**At the gate:** nothing waiting."
    lines = [f"**At the gate ({len(pending)}):**"]
    for r in pending[:8]:
        lines.append(f"- `{r.get('run_id')}` {esc(r.get('lane'), 20)}: "
                     f"{esc((r.get('proposal') or {}).get('preview'), 80)}")
    return "\n".join(lines)


def moved_this_week_section(executed: list, failed_count: int, window_days: int = 7) -> str:
    """`executed`: runs with status executed and created_at within the window. This reports what
    the engine's own run log shows in that window, not a computed delta of project progress."""
    if not executed and not failed_count:
        return f"**This week ({window_days}d):** no runs completed."
    lines = [f"**This week ({window_days}d):** {len(executed)} executed, {failed_count} failed."]
    for r in executed[:10]:
        lines.append(f"- `{r.get('run_id')}` {esc(r.get('lane'), 20)}")
    return "\n".join(lines)


def briefing_message(mode: str, generated_at: str, sections: list, note: str | None = None) -> str:
    title = "Friday review" if mode == "friday" else "Morning briefing"
    out = [f"**🌅 {title}** · generated {esc(generated_at, 40)}"]
    out.extend(s for s in sections if s)
    if note:
        out.append(note)
    return "\n\n".join(out)


def alert_key(key: str) -> str:
    """Stable identity for a console briefing alert (gpu_hot:87 and gpu_hot:88 are one condition)."""
    head = key.split(":", 1)[0]
    return head if head in ("gpu_hot",) else key


HELP = """**NOMAD commands** (DM me)
- `status`: what's pending, running and failing
- `lockdown`: stop acting on anything from Zulip until unlocked on the host
- `pending`: runs waiting at the gate
- `approve <run>` · `reject <run>` · `retry <run>` (the first 4+ characters of the run id work)
- `projects`: progress on every project
- `next <project>`: queue the project's next task to the gate
- `retry task <id>` · `skip task <id>`: unblock a project task
- `capture: <goal>`: start a new run through the pipeline
- `project: <title>`: plan a new project
- `briefing`: calendar, mail needing a reply, open tasks, gate queue — read-only, DM only, on demand
- `friday review`: the same, plus what ran this week — read-only, DM only, on demand
- anything else: talk to NOMAD"""


_CMD = re.compile(r"^\s*(approve|reject|retry|skip)\s+(?:(task)\s+(\d+)|([\w-]{4,}))\s*$", re.I)


def parse_command(text: str):
    """→ (verb, arg). Verbs: help status pending projects next approve reject retry retry_task
    skip_task chat. Prefixes the engine's /chat already understands (capture:, project:) go to chat."""
    t = (text or "").strip()
    low = t.lower()
    if low in ("help", "?", "/help"):
        return "help", None
    if low in ("status", "/status"):
        return "status", None
    if low in ("pending", "gate", "/pending"):
        return "pending", None
    if low in ("projects", "/projects"):
        return "projects", None
    if low in ("lockdown", "/lockdown"):
        return "lockdown", None
    if low in ("briefing", "/briefing", "morning briefing"):
        return "briefing", None
    if low in ("friday review", "/friday", "friday-review", "weekly review"):
        return "friday_review", None
    if low.startswith("next "):
        return "next", t[5:].strip()
    m = _CMD.match(t)
    if m:
        verb, is_task = m.group(1).lower(), bool(m.group(2))
        arg = m.group(3) if is_task else m.group(4)
        if is_task:
            return (f"{verb}_task", arg) if verb in ("retry", "skip") else ("chat", t)
        if verb == "skip":
            return "chat", t
        return verb, arg
    return "chat", t


def topic_reply_intent(text: str):
    """A one-word reply inside a #gate/#alerts topic."""
    w = (text or "").strip().lower().rstrip(".!")
    return {"approve": "approve", "approved": "approve",
            "reject": "reject", "rejected": "reject", "no": "reject", "decline": "reject",
            "retry": "retry", "skip": "skip"}.get(w)
