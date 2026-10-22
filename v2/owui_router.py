"""Which model answers an Open WebUI turn (#1116, docs/prd/owui-sessions.md §6.2 step 2).

In order, the first that applies decides:

1. An override at the start of the message (`!deep`, `!local`, `!code`, …). No router call.
   `!deep` on its own re-asks the previous question (U7). `/deep` works too where it gets
   through, but Open WebUI's slash-command menu swallows Enter on a lone `/word` (#1180).
2. Rules: an attached image goes to `vision`. A conversation too long for the 8k local models goes
   to `longdoc` (`longdoc-local` in a local-only chat).
3. The router: one call to `fast`, fallbacks off, asking for strict JSON
   `{"model", "reason", "needs_tools"}`. The model must be in the entry's allowed set.
4. Anything else (router down, slow, bad JSON, a model outside the set): `balanced` for a project
   chat, `private` for a local-only one. The footer says the router was skipped.

General and `local_only` projects only ever get a local alias: a cloud override is refused there,
and their calls carry a local-only fallback list (LOCAL_FALLBACKS), because LiteLLM's own `code`
chain ends at `codex`. `gpt` is in no set and no list (#960).
"""
import json
import re
from dataclasses import dataclass, field

LOCAL = ("fast", "private", "code", "vision", "longdoc-local")
CLOUD = ("balanced", "deep", "codex", "longdoc")
PAID = ("gpt",)                     # never chosen, never a fallback

# Context windows in tokens. fast and private run at num_ctx 8192 (litellm/config.yaml).
WINDOW = {"fast": 8192, "private": 8192, "vision": 32768, "code": 65536, "longdoc-local": 65536,
          "balanced": 200000, "deep": 200000, "codex": 200000, "longdoc": 1000000}
REPLY_RESERVE = 1500                # tokens kept free for the answer

# A local-only chat's per-request fallbacks, replacing LiteLLM's chain. longdoc-local has none:
# private's 8k window can't take what was sent there.
LOCAL_FALLBACKS = {"fast": ["private"], "private": ["fast"], "code": ["code-ollama", "private"],
                   "vision": ["vision-ollama"], "longdoc-local": []}

OVERRIDES = {"deep": "deep", "balanced": "balanced", "codex": "codex", "longdoc": "longdoc",
             "local": "private", "private": "private", "fast": "fast", "code": "code",
             "vision": "vision", "longdoc-local": "longdoc-local"}
_OVERRIDE = re.compile(r"^\s*([!/])(longdoc-local|[a-z]+)\b[ \t]*(.*)$", re.S)

ROUTER_TIMEOUT_S = 8
ROUTER_SYSTEM = """You route one chat message to the model that should answer it. Reply with ONLY a JSON object:
{"model": "<one of the allowed models>", "reason": "<under 12 words>", "needs_tools": true|false}

Models:
- fast: small talk, short factual answers, rewording, quick lookups. Fastest.
- private: local reasoning, sensitive or personal topics, summaries, moderate questions.
- code: writing, reviewing or debugging code, done locally.
- longdoc-local: very long pasted documents that must stay local.
- balanced: most project work: planning, explaining the project, deciding next steps, writing.
- deep: hard architecture, difficult trade-offs, careful multi-step reasoning.
- codex: code-heavy work in a project: implementation detail, scripts, debugging.
- longdoc: very long documents when local isn't required.
needs_tools is true when answering needs live data or an action (tasks, runs, creating work).

Examples:
"thanks!" -> {"model": "fast", "reason": "small talk", "needs_tools": false}
"what's blocking the next milestone?" -> {"model": "balanced", "reason": "project status", "needs_tools": true}
"should we split the engine into services? weigh it" -> {"model": "deep", "reason": "architecture trade-off", "needs_tools": false}
"why does this regex miss the last line" -> {"model": "codex", "reason": "debugging code", "needs_tools": false}
"draft a note to my doctor about my results" -> {"model": "private", "reason": "personal and sensitive", "needs_tools": false}

Choose ONLY from the allowed models listed in the message."""


@dataclass
class Decision:
    model: str
    reason: str
    by: str                             # override | rule | router | default
    message: str                        # the text to answer, override prefix removed
    needs_tools: bool = False
    note: str = ""                      # shown in the footer when something was refused/skipped
    fallbacks: list | None = field(default=None)   # None = LiteLLM's own chain


def allowed(local_only: bool) -> tuple:
    return LOCAL if local_only else LOCAL + CLOUD


def default(local_only: bool) -> str:
    return "private" if local_only else "balanced"


def tokens(text: str) -> int:
    """A rough count (4 chars per token), enough to compare against a window."""
    return (len(text or "") + 3) // 4


def _finish(d: Decision, local_only: bool) -> Decision:
    if local_only:
        d.fallbacks = list(LOCAL_FALLBACKS.get(d.model, []))
    assert d.model not in PAID
    return d


def parse(text: str, local_only: bool) -> dict | None:
    """The router's JSON, or None when it isn't a usable choice."""
    try:
        s, e = text.index("{"), text.rindex("}") + 1
        d = json.loads(text[s:e])
    except (ValueError, TypeError):
        return None
    if not isinstance(d, dict) or d.get("model") not in allowed(local_only):
        return None
    return {"model": d["model"], "reason": str(d.get("reason") or "")[:120],
            "needs_tools": d.get("needs_tools") is True}


def bare(text) -> bool:
    """An override with no question after it (`!deep`), which a later `!local` skips over."""
    m = _OVERRIDE.match(text or "") if isinstance(text, str) else None
    return bool(m and m.group(2) in OVERRIDES and not m.group(3).strip())


def override(message: str, *, local_only: bool, history=()) -> Decision | None:
    """The decision for a message that starts with an override (`!deep`, `/local`, …), else None.
    Shared by route() and the Open WebUI filter (#1199), so both apply the same rules."""
    m = _OVERRIDE.match(message or "")
    if not (m and m.group(2) in OVERRIDES):
        return None
    cmd = m.group(1) + m.group(2)
    want, rest = OVERRIDES[m.group(2)], m.group(3).strip()
    if not rest:                    # "!deep" alone: the same question again, by that model
        rest = next((h.get("content") or "" for h in reversed(list(history))
                     if h.get("role") == "user" and not bare(h.get("content"))), "")
    if want in allowed(local_only):
        return _finish(Decision(want, cmd, "override", rest), local_only)
    return _finish(Decision(default(local_only), f"{cmd} refused", "override", rest,
                            note=f"{cmd} is a cloud model; this chat stays local"), local_only)


def route(message: str, *, local_only: bool, complete, history=(), has_image=False,
          context_tokens=0, loaded="") -> Decision:
    """Pick the model for one turn. `complete(alias, system, user, timeout)` returns the router
    model's text; `history` is the earlier [{role, content}] of this chat. `loaded` names a local
    alias already holding the GPU (NOMAD Local's `code`, #1436): a local-only chat stays on it
    without the router call, because loading `fast` or `private` next to it overflows VRAM."""
    text = message or ""
    d = override(text, local_only=local_only, history=history)
    if d:
        return d

    if has_image:
        return _finish(Decision("vision", "image attached", "rule", text), local_only)
    size = tokens(text) + sum(tokens(h.get("content") or "") for h in history)
    if local_only and loaded in LOCAL and WINDOW[loaded] >= size + REPLY_RESERVE + (
            context_budget(loaded) if context_tokens else 0):
        return _finish(Decision(loaded, f"{loaded} is already loaded", "rule", text), local_only)
    # Only models whose window holds the conversation, plus the project context at that model's
    # budget. A local-only chat past the 8k models goes straight to longdoc-local; a project chat
    # past every window but longdoc's, to longdoc.
    def need(a):
        return size + REPLY_RESERVE + (context_budget(a) if context_tokens else 0)
    fits = tuple(a for a in allowed(local_only) if WINDOW[a] >= need(a) and a != "vision")
    if local_only and "private" not in fits:
        fits = ()
    if not fits or fits == ("longdoc",):
        big = "longdoc-local" if local_only else "longdoc"
        return _finish(Decision(big, f"~{size} tokens of conversation", "rule", text), local_only)

    names = ", ".join(fits)
    try:
        raw = complete("fast", ROUTER_SYSTEM,
                       f"Allowed models: {names}\n\nMessage:\n{text[:2000]}", ROUTER_TIMEOUT_S)
        got = parse(raw, local_only)
        if got and got["model"] not in fits:
            got, raw = None, f"chose {got['model']}, which doesn't fit"
    except Exception as e:
        got, raw = None, f"{type(e).__name__}"
    if got:
        return _finish(Decision(got["model"], got["reason"], "router", text,
                                needs_tools=got["needs_tools"]), local_only)
    return _finish(Decision(default(local_only), "default", "default", text,
                            note=f"router skipped ({str(raw)[:40]!r})"), local_only)


def context_budget(model: str) -> int:
    """Tokens of project context that fit next to the conversation in `model`'s window."""
    return max(1000, min(8000, int(WINDOW.get(model, 8192) * 0.35)))
