"""V2c · the intent router, ported from `nomad-console/intents.py`.

This is what makes NOMAD do things rather than only talk about them: "run diagnostics", "work on
your backlog", "approve" are commands, and a chat model handed those will describe the action
instead of taking it.

`classify(text)` is pure and side-effect free, which is why it is worth having as its own module:
the whole risk surface of a router like this is what it matches, and that is testable without a
running engine. The server owns the handlers that touch the pipeline.

It stays pattern-based rather than model-based, for the same reason it was here in v1: no added
latency, no cost, and a misfire is a regex you can read. An LLM classifier would be one more thing
that can decide, on its own, that "I approve of that approach" was an approval.

This is a copy, not an import. The console is going away and its copy goes with it; until then the
two are separate services and neither can reach the other's filesystem. The classifier is
unchanged from v1 on purpose, because a port that quietly retunes the patterns is not a port, and
the whole of V2c has to be verifiable as "the same thing, somewhere else".
"""
import re

# ── pipeline capture: "/capture <goal>" or "pipeline: <goal>" → straight into the gate
_CAPTURE = re.compile(r"^\s*/capture\s+(.+)$|^\s*pipeline\s*:\s*(.+)$", re.I)

# ── gate decision: approve / reject the pending run(s) by voice or text. Anchored at the start so
#    "I approve of that approach" (mid-sentence) does NOT trigger an accidental approval.
_APPROVE = re.compile(
    r"^\s*(ok(ay)?[,.\s]+|yes[,.\s]+|sure[,.\s]+|please\s+|go\s+ahead\s+(and\s+)?)?"
    r"(approve|approved|authoriz?e|authoris?e|confirm|grant\s+approval|permission\s+granted)\b", re.I)
_REJECT = re.compile(
    r"^\s*(no[,.\s]+|please\s+)?(reject|rejected|deny|denied|decline[ds]?|disapprove|"
    r"do\s*n[o']?t\s+approve)\b", re.I)

# ── action that needs the gate: an imperative external action → capture it into the pipeline so it
#    shows in the Approval Queue (instead of the chat LLM just talking about wanting approval).
_ACTION = re.compile(
    r"^\s*(please\s+)?((send|e-?mail|draft|compose|reply|respond|publish|post|share|schedule)\b"
    r"|(generate|create|make|design|write)\b[^.]{0,60}\b(image|banner|logo|graphic|poster|"
    r"illustration|ad|advert|advertisement|copy|content|email|message|post)\b)", re.I)

# ── diagnostics: "how are the systems", "status report", "run diagnostics", "sitrep"
_DIAG = re.compile(
    r"\b(run\s+)?(a\s+)?(full\s+)?(diagnostics?|self[- ]?test|sitrep|"
    r"system\s+(status|report|check|diagnostics?)|status\s+report|health\s+check|"
    r"how\s+are\s+(you|things|we|the\s+systems?)|all\s+systems?|status\s+of\s+the\s+system)\b",
    re.I,
)

# ── start a project: "start/create/spin up a (new) project (called X)"
_START = re.compile(
    r"\b(start|create|spin\s+up|kick\s+off|set\s+up|scaffold|begin|launch|initialise|initialize)\b"
    r"[^.]{0,40}?\bproject\b",
    re.I,
)
_NAME = re.compile(r"\b(?:called|named|titled|for)\s+[\"']?([A-Za-z0-9][\w \-]{1,40}?)[\"']?\s*$", re.I)
_NAME2 = re.compile(r"\bproject\s+(?:called|named|titled\s+)?[\"']?([A-Za-z0-9][\w \-]{1,40}?)[\"']?\s*$", re.I)

# ── self-development: "work on your backlog", "develop yourself", "improve yourself"
_SELFDEV = re.compile(
    r"\b(work\s+on\s+(your|the)\s+backlog|run\s+(the\s+)?(dev|engineering)?\s*backlog|"
    r"develop\s+yourself|improve\s+yourself|self[- ]?develop|build\s+your\s+backlog|"
    r"start\s+(the\s+)?self[- ]?development|process\s+(your|the)\s+backlog)\b",
    re.I,
)

# ── research / web search: "research X", "look into X", "do a websearch on X", "search for X"
_RESEARCH = re.compile(
    r"\b(research|look\s+into|investigate|dig\s+into|find\s+out\s+about|"
    r"gather\s+(?:info|information|intel)\s+on|give\s+me\s+a\s+brief\s+on|brief\s+me\s+on|"
    r"web\s*search|search\s+(?:the\s+web|online|for)|google)\b\s*[:,]?\s*(?:on\s+|about\s+|for\s+)?(.+)",
    re.I,
)

# Every intent the router can return, so the server's handler table can be checked against it
# rather than drifting from it silently. A classifier that emits an intent nothing handles falls
# through to chat, which is the one failure here that looks like normal behaviour.
INTENTS = ("pipeline_capture", "gate_decision", "diagnostics", "self_develop", "start_project",
           "research", "action_capture", "chat")


def _clean_name(s: str) -> str:
    s = re.sub(r"\b(a|an|the|new)\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip(" \"'.-")


def classify(text: str):
    """Return (intent, payload) with intent one of `INTENTS`.

    Order matters and is not alphabetical. Gate decisions come first because they are the shortest
    utterances and the most expensive to get wrong, and capture comes before them because
    `/capture approve the budget` is a goal, not a decision."""
    t = (text or "").strip()
    if not t:
        return "chat", {}

    mc = _CAPTURE.match(t)
    if mc:
        return "pipeline_capture", {"goal": (mc.group(1) or mc.group(2)).strip()}

    # gate decisions first — "approve" / "reject" (typed or spoken) resolves pending runs
    if _APPROVE.match(t):
        return "gate_decision", {"decision": "approved", "all": bool(re.search(r"\ball\b", t, re.I))}
    if _REJECT.match(t):
        return "gate_decision", {"decision": "rejected", "all": bool(re.search(r"\ball\b", t, re.I))}

    if _DIAG.search(t):
        return "diagnostics", {}

    if _SELFDEV.search(t):
        return "self_develop", {}

    if _START.search(t):
        name = ""
        for rx in (_NAME, _NAME2):
            m = rx.search(t)
            if m:
                name = _clean_name(m.group(1))
                break
        if not name:  # fallback: words after "project"
            m = re.search(r"\bproject\b\s+(.{2,40})", t, re.I)
            if m:
                name = _clean_name(m.group(1))
        return "start_project", {"name": name, "request": t}

    m = _RESEARCH.search(t)
    if m:
        topic = m.group(2).strip(" \"'.?")
        if len(topic) >= 3:
            return "research", {"topic": topic}

    # imperative external action → capture into the pipeline (so it hits the gate + queue)
    if _ACTION.match(t):
        return "action_capture", {"request": t}

    return "chat", {}
