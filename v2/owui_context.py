"""Project context for an Open WebUI project chat (#1115, docs/prd/owui-sessions.md §6.2 step 1).

Three parts, each clipped to its share of the budget: the project's CLAUDE.md, its PRD (both read
through the dispatcher's GET /project-context, since the engine container can't see the repos),
and a nomad-pm summary of the project's board: open Epics, then In Progress, Ready and Blocked
work, each line with its id so the answer can quote it. Cached per chat for 5 minutes.

A part that can't be read says so in its place, rather than disappearing: an answer built on a
missing PRD should be able to tell the operator the PRD was missing.
"""
import os
import threading
import time

BUDGET_TOKENS = int(os.environ.get("NOMAD_OWUI_CONTEXT_TOKENS") or 8000)
CHARS_PER_TOKEN = 4
SHARES = {"claude_md": 0.4, "prd": 0.35, "pm": 0.25}
TTL_S = 300
PM_SECTIONS = (("Open Epics", {"type": "Epic"}),
               ("In Progress", {"status": "In Progress"}),
               ("Ready", {"status": "Ready"}),
               ("Blocked", {"status": "Blocked"}))
PM_LIMIT = 15

_cache: dict = {}
_lock = threading.Lock()


def clip(text: str, limit: int) -> str:
    """`text` cut to `limit` chars, saying so when it was cut."""
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:max(limit - 40, 0)].rstrip() + f"\n… [clipped, {len(text) - limit} more chars]"


def pm_summary(board: str, search) -> str:
    """The board's open work, from `search` (chat_pm_tools.pm_search or a fake)."""
    out = []
    for label, f in PM_SECTIONS:
        try:
            res = search(project=board, limit=PM_LIMIT, **f)
        except Exception as e:
            res = f"(unavailable: {type(e).__name__}: {str(e)[:120]})"
        out.append(f"{label}:\n{res}")
    return "\n\n".join(out)


def build(project_id: str, fetch_context, search, budget_tokens: int | None = None) -> str:
    """The context block for one project. `fetch_context(id)` returns the dispatcher's
    /project-context dict or raises; `search` is pm_search. `budget_tokens` (default
    BUDGET_TOKENS) shrinks it for the 8k local models: owui_router.context_budget(model)."""
    budget = (budget_tokens or BUDGET_TOKENS) * CHARS_PER_TOKEN
    try:
        ctx = fetch_context(project_id) or {}
        err = "" if ctx else "the dispatcher doesn't know this project"
    except Exception as e:
        ctx, err = {}, f"{type(e).__name__}: {str(e)[:120]}"
    name = ctx.get("name") or project_id
    board = ctx.get("board") or project_id
    parts = [f"PROJECT: {name} (registry id {project_id}, nomad-pm board {board})"]
    for key, title in (("claude_md", "CLAUDE.md"), ("prd", "PRD")):
        doc = ctx.get(key)
        if doc and doc.get("text"):
            body = clip(doc["text"], int(budget * SHARES[key]))
            parts.append(f"=== {title} ({doc.get('path') or title}) ===\n{body}")
        else:
            parts.append(f"=== {title} ===\n(not available{': ' + err if err else ''})")
    if ctx.get("board") is None and ctx:
        parts.append("=== nomad-pm ===\n(this project has no board)")
    else:
        parts.append("=== nomad-pm board " + board + " ===\n"
                     + clip(pm_summary(board, search), int(budget * SHARES["pm"])))
    return "\n\n".join(parts)


def load(chat_id: str, project_id: str, fetch_context, search, now=time.monotonic,
         budget_tokens: int | None = None) -> str:
    """build(), cached per (chat, project, budget) for TTL_S."""
    key = (chat_id, project_id, budget_tokens)
    with _lock:
        hit = _cache.get(key)
        if hit and now() - hit[0] < TTL_S:
            return hit[1]
    text = build(project_id, fetch_context, search, budget_tokens)
    with _lock:
        _cache[key] = (now(), text)
        if len(_cache) > 256:                      # old chats fall out
            for k in sorted(_cache, key=lambda k: _cache[k][0])[:64]:
                _cache.pop(k, None)
    return text
