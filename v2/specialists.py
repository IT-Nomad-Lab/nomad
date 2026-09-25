"""NOMAD v2 · 3B — config-driven specialists (the full inbox).

One generic Specialist class, configured per lane. Adding a lane is now: a Skill file + an MCP
tool + one LANES entry. Each specialist Processes via the runtime (SDK-native, LiteLLM fallback)
into an action proposal for the human gate. Replaces the per-lane CommsSpecialist/Researcher.
"""
import os

import detector
import drafting
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
        # Skip failed attempts: they were never delivered, so they are not part of the thread.
        prior = [r for r in db.list("outbox", 100)
                 if (r.get("to") or "").strip().lower() == tgt and r.get("status") != "failed"]
        # `created_at` is not a column on outbox; NocoDB's own timestamp is `CreatedAt`. Reading the
        # wrong one dated every prior message as empty and ordered them arbitrarily, which is the
        # opposite of what a thread-aware lane needs.
        prior = sorted(prior, key=lambda r: str(r.get("CreatedAt") or r.get("Id") or ""))[-3:]
        block = ""
        if prior:
            block += "PRIOR MESSAGES YOU SENT TO " + (target or "them") + ":\n" + "\n".join(
                f"- ({str(p.get('CreatedAt') or '')[:10]}) {(p.get('body') or '')[:220]}" for p in prior) + "\n"
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
#         choose?(intent)->(action, args_fn)  (pick the action dynamically per run),
#         read_tools?[...]  (read-only lookups this lane may make WHILE drafting)}
#
# `read_tools` is least privilege, not a capability list: a lane gets only what its work needs, so
# the comms specialist cannot search the web merely because the researcher can. Every name must be
# in drafting.READ_TOOLS, which is the allowlist that vouches the tool is read-only. Nothing that
# changes the world goes here — that is the proposed action, and it waits for the gate.
LANES = {
    "comms":    {"skill": "comms.md", "model": "balanced", "action": "send_message", "style": "JDE_STYLE",
                 "read_tools": ["prior_messages", "recall"],
                 "args": lambda i, t, c, r: {"to": t, "subject": _subject(i), "body": c, "run_id": r}},
    "research": {"skill": "research.md", "model": "balanced", "action": "save_brief",
                 "enrich": _research_enrich,
                 "read_tools": ["web_search", "read_url", "recall"],
                 "args": lambda i, t, c, r: {"topic": t, "brief": c, "run_id": r}},
    "support":  {"skill": "support.md", "model": "balanced", "action": "send_message",
                 "enrich": _support_enrich,
                 "read_tools": ["prior_messages", "recall"],
                 "args": lambda i, t, c, r: {"to": t, "subject": _subject(i, "Re: "), "body": c, "run_id": r}},
    "ads":      {"skill": "ads.md", "model": "balanced", "action": "save_content", "style": "JDE_STYLE",
                 "choose": _ads_choose,
                 "read_tools": ["web_search", "recall"],
                 "args": lambda i, t, c, r: {"topic": t, "content": c, "run_id": r}},
    "dev":      {"skill": "dev.md", "model": "balanced", "action": "dispatch_build",
                 "read_tools": ["recall"],
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
        # Filtered against the allowlist at construction, so a typo in LANES is a tool this lane
        # silently does not get, never a name that reaches the dispatcher.
        self.read_tools = [t for t in cfg.get("read_tools", []) if t in drafting.READ_TOOLS]
        with open(os.path.join(SKILLS, cfg["skill"]), encoding="utf-8") as f:
            self.skill = f.read()
        self.style = cfg.get("style")               # append the named house-voice block
        if self.style:
            block = _prompt(self.style)
            if block:
                self.skill = f"{self.skill}\n\n=== WRITING STYLE ({self.style}) ===\n{block}"

    def process(self, intent: str, target: str):
        """Draft the output via the lane's Skill. Returns (content, lookups).

        Still reversible, and that is load-bearing: Process runs UPSTREAM of the human gate, so the
        only tools reachable from here are the read-only ones in drafting.READ_TOOLS, narrowed again
        to this lane. Nothing that changes the world can be called; that is the proposed action, and
        it waits for the operator.

        Two ways to get context, and they are not the same. `enrich` is a fixed lookup we choose and
        always run. `read_tools` lets the specialist ask for what it decides it is missing, which is
        the difference between grounding the obvious case and grounding the one we did not foresee.
        """
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
        return drafting.draft(self.skill, user, self.model_alias,
                              max_tokens=900 if evidence else 700,
                              allowed=self.read_tools)

    def _rewrite(self, draft: str, hints: str) -> str:
        """Hand the specialist its own detector findings and make it fix them. One pass, same
        Skill (so the house voice still applies), no new facts allowed."""
        user = ("This draft reads as machine-written and would be flagged. Rewrite it so it reads "
                "human, keeping every fact, name and number exactly as they are.\n\n"
                f"FINDINGS TO FIX:\n{hints}\n\n"
                "Also vary sentence length hard, keep the specifics, and cut any line that could "
                "appear in an article on a different topic.\n\n"
                f"DRAFT:\n{draft}\n\nReturn only the rewritten text.")
        return runtime.run(self.skill, user, self.model_alias, max_tokens=900)

    def propose(self, intent: str, target: str, run_id: str) -> dict:
        content, lookups = self.process(intent, target)
        report = None
        if self.style:      # a lane that writes in the house voice must clear the detector first
            content, report = detector.enforce(content, rewrite=self._rewrite)
        action, args_fn = self.choose(intent) if self.choose else (self.action, self._args)
        prop = {"action": action, "args": args_fn(intent, target, content, run_id)}
        if report:
            prop["detector"] = report
        if lookups:
            # What the draft was grounded in, carried to the gate. An operator approving a proposal
            # should be able to see where its facts came from instead of taking the prose on faith.
            prop["lookups"] = lookups
        return prop


SPECIALISTS = {lane: Specialist(lane, cfg) for lane, cfg in LANES.items()}
