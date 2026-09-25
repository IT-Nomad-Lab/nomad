"""Pure pieces of the relay: message text, topic names, command parsing, emoji mapping.

No I/O here, so every rule about what the operator sees and what a reaction means is unit-testable
without Zulip or the engine running.
"""
import datetime as _dt
import json
import re

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
# topic names are visible in lock-screen previews and the channel list.
def run_topic(detail: dict) -> str:
    return f"run {(detail.get('run_id') or '?')[:12]}"


def project_topic(goal_id) -> str:
    return f"project {goal_id}"


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


def gate_view(d: dict, remote_ok: bool = True, limit: int = 1500, arg_limit: int = 300):
    """→ (message, complete). `complete` is False when anything the engine would execute had to be
    cut to fit; the relay then refuses remote approval, because you'd be approving text you didn't see."""
    a = d.get("args") or {}
    complete = True
    lines = [f"**🛂 Approval needed** · {code(d.get('lane'), 20)} · run `{d.get('run_id')}`",
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
    if not remote_ok:
        lines.append(f"🔒 {code(d.get('action'), 40)} is cockpit-only. React ❌ to reject; approve it in the cockpit.")
    elif not complete:
        lines.append("🔒 Too long to show in full, so approve it in the cockpit. React ❌ to reject.")
    else:
        lines.append("React ✅ to approve or ❌ to reject, or reply `approve` / `reject` here.")
    return "\n".join(lines), complete and remote_ok


def gate_message(d: dict, remote_ok: bool = True, limit: int = 1500) -> str:
    return gate_view(d, remote_ok, limit)[0]


STATUS_LINE = {
    "new": "🟦 captured, specialist is drafting",
    "awaiting-approval": "🛂 waiting at the gate",
    "executing": "▶️ approved, executing",
    "executed": "✅ executed",
    "failed": "💥 failed",
    "declined": "🚫 declined",
}


def status_message(d: dict, status: str) -> str:
    line = STATUS_LINE.get(status, f"status → `{status}`")
    if status == "failed" and d.get("error"):
        line += f"\n{_fence(clip(d['error'], 800))}"
    return line


def failed_alert(d: dict) -> str:
    return (f"**💥 Run failed** · {code(d.get('lane'), 20)} · run `{d.get('run_id')}`\n"
            f"**Action:** {code(d.get('action'), 40)}\n"
            f"{_fence(clip(d.get('error') or 'no error text recorded', 800))}\n"
            "React 🔁 to put it back at the gate, or reply `retry`.")


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
