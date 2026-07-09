"""NOMAD v2 · 3B — config-driven specialists (the full inbox).

One generic Specialist class, configured per lane. Adding a lane is now: a Skill file + an MCP
tool + one LANES entry. Each specialist Processes via the runtime (SDK-native, LiteLLM fallback)
into an action proposal for the human gate. Replaces the per-lane CommsSpecialist/Researcher.
"""
import os

import runtime

HERE = os.path.dirname(os.path.abspath(__file__))
SKILLS = os.path.join(HERE, "skills")


def _prompt(name):
    """Read a named block from the NOMAD prompt registry (prompts/<NAME>.md). Fail-open: an
    unreadable name returns "" so a missing style never breaks a specialist."""
    for base in (os.environ.get("NOMAD_PROMPTS_DIR"),
                 os.path.join(HERE, "prompts"), os.path.join(HERE, "..", "prompts"), "/app/prompts"):
        if not base:
            continue
        try:
            with open(os.path.join(base, f"{name}.md"), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            continue
    return ""


def _subject(intent, prefix="NOMAD · "):
    return (prefix + (intent or "").strip())[:78]


_db = None
def _get_db():
    """Lazy shared NocoDB handle for enrich hooks (specialists are built without one)."""
    global _db
    if _db is None:
        from nocodb import NocoDB
        _db = NocoDB()
    return _db


def _research_enrich(intent, target):
    """Gather real web evidence (scrape a URL target, else web-search) to ground the brief.
    Fail-open: scraper down/slow → '' and the Researcher reasons unaided (as before)."""
    try:
        import scraper
        return scraper.as_evidence(scraper.gather(intent, target))
    except Exception:
        return ""


def _support_enrich(intent, target):
    """Thread-aware: prior messages sent to this recipient + related episodic context, so the reply
    CONTINUES the conversation instead of starting cold. Fail-open."""
    try:
        import memory
        db = _get_db()
        tgt = (target or "").strip().lower()
        prior = [r for r in db.list("outbox", 100) if (r.get("to") or "").strip().lower() == tgt]
        prior = sorted(prior, key=lambda r: r.get("created_at") or "")[-3:]
        block = ""
        if prior:
            block += "PRIOR MESSAGES YOU SENT TO " + (target or "them") + ":\n" + "\n".join(
                f"- ({(p.get('created_at') or '')[:10]}) {(p.get('body') or '')[:220]}" for p in prior) + "\n"
        ctx = memory.recall(db, intent, limit=3)
        if ctx:
            block += "\nRELATED CONTEXT:\n" + ctx
        return block.strip()
    except Exception:
        return ""


# Visual asks → the ads lane proposes a Firefly image (generate_image) instead of copy.
_IMAGE_WORDS = ("image", "banner", "logo", "graphic", "visual", "illustration", "photo", "poster",
                "thumbnail", "picture", "artwork", "mockup", "icon", "hero image", "render")
def _ads_choose(intent):
    if any(w in (intent or "").lower() for w in _IMAGE_WORDS):
        return ("generate_image", lambda i, t, c, r: {"prompt": c, "run_id": r})
    return ("save_content", lambda i, t, c, r: {"topic": t, "content": c, "run_id": r})


# lane → {skill, model, action, args(intent,target,content,run_id)->dict,
#         enrich?(intent,target)->str  (gather live context before drafting),
#         choose?(intent)->(action, args_fn)  (pick the action dynamically per run)}
LANES = {
    "comms":    {"skill": "comms.md", "model": "balanced", "action": "send_message", "style": "JDE_STYLE",
                 "args": lambda i, t, c, r: {"to": t, "subject": _subject(i), "body": c, "run_id": r}},
    "research": {"skill": "research.md", "model": "balanced", "action": "save_brief",
                 "enrich": _research_enrich,
                 "args": lambda i, t, c, r: {"topic": t, "brief": c, "run_id": r}},
    "support":  {"skill": "support.md", "model": "balanced", "action": "send_message",
                 "enrich": _support_enrich,
                 "args": lambda i, t, c, r: {"to": t, "subject": _subject(i, "Re: "), "body": c, "run_id": r}},
    "ads":      {"skill": "ads.md", "model": "balanced", "action": "save_content", "style": "JDE_STYLE",
                 "choose": _ads_choose,
                 "args": lambda i, t, c, r: {"topic": t, "content": c, "run_id": r}},
    "dev":      {"skill": "dev.md", "model": "balanced", "action": "dispatch_build",
                 "args": lambda i, t, c, r: {"project": t, "task": i, "plan": c, "run_id": r}},
}


class Specialist:
    def __init__(self, lane, cfg):
        self.lane = self.name = lane
        self.action = cfg["action"]
        self.model_alias = cfg["model"]
        self.tools = [f"{lane}.{cfg['action']}"]
        self._args = cfg["args"]
        self.enrich = cfg.get("enrich")     # optional: gather live context before drafting
        self.choose = cfg.get("choose")     # optional: pick the action dynamically per run
        with open(os.path.join(SKILLS, cfg["skill"]), encoding="utf-8") as f:
            self.skill = f.read()
        style = cfg.get("style")                    # append the named house-voice block
        if style:
            block = _prompt(style)
            if block:
                self.skill = f"{self.skill}\n\n=== WRITING STYLE ({style}) ===\n{block}"

    def process(self, intent: str, target: str) -> str:
        """Draft the output via the lane's Skill (reversible — no side effect, no tool call). If the
        lane has an `enrich` hook, gather live context first and ground the draft in it."""
        evidence = ""
        if self.enrich:
            try:
                evidence = (self.enrich(intent, target) or "").strip()
            except Exception:
                evidence = ""
        user = f"Intent: {intent}\nTarget: {target}\n"
        if evidence:
            user += ("\nUse this freshly gathered context — prefer it over assumptions, stay "
                     "consistent with it, and don't invent facts beyond it:\n\n" f"{evidence}\n")
        user += "\nProduce your output (only the content)."
        return runtime.run(self.skill, user, self.model_alias,
                           max_tokens=900 if evidence else 700)

    def propose(self, intent: str, target: str, run_id: str) -> dict:
        content = self.process(intent, target)
        action, args_fn = self.choose(intent) if self.choose else (self.action, self._args)
        return {"action": action, "args": args_fn(intent, target, content, run_id)}


SPECIALISTS = {lane: Specialist(lane, cfg) for lane, cfg in LANES.items()}
