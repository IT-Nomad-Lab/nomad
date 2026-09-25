"""Cross-vendor code review: an independent second opinion on Claude-authored diffs from
OpenAI's Codex CLI (a different vendor, a different subscription, reached through the
`codex` LiteLLM alias) before NOMAD's own Reviewer agent -- itself running on Claude --
gives its verdict.

Why this exists: without it, Claude reviews Claude's own diffs and both sides share the
same blind spots. It also moves review load off the Claude MAX 5-hour window onto the
separate ChatGPT subscription (docs/ROADMAP-2026-09-intake.md, Wave 0, track T0.4;
CLAUDE.md's codex-bridge section).

Codex runs in a READ-ONLY sandbox with an empty workdir (codex-bridge/bridge.py) -- it
cannot open repo files. So this module sends the diff and goal directly in the prompt; do
not expect Codex to go look at anything.

CRITICAL, read before touching this file: the call below sets `disable_fallbacks: True`
and then checks the response's `model` field is literally "codex" before trusting the
content. Without both of these, a codex-bridge outage would silently fall back to
`balanced` (Claude Sonnet) or `gpt`, and the review would present itself as an independent
second opinion while actually being Claude (or GPT) reviewing Claude -- worse than not
running it, because it would look like it happened. This mirrors the guard in
`nomad-social/analyze.py` (read its module docstring): on 2026-09-22 a `gpt` vision call
there silently fell back to `balanced`, which confidently described frames it never saw.
Every vision reply there records the alias that actually served it; this module does the
same for review.

Fails open, LOUDLY: if the bridge is down, Codex errors, times out, or a non-codex model
answers, `codex_review()` returns `ran: False` with a clear `note`. Callers MUST surface
that note into the log and into the Reviewer's prompt -- the pipeline must never silently
proceed as though cross-vendor review happened when it did not.
"""
import os

import requests

LITELLM_BASE = os.environ.get("LITELLM_BASE_URL", "http://litellm:4000")
LITELLM_KEY = os.environ.get("LITELLM_MASTER_KEY", "")
TIMEOUT = int(os.environ.get("NOMAD_CODEX_REVIEW_TIMEOUT", "180"))
EVIDENCE_CHARS = int(os.environ.get("NOMAD_CODEX_REVIEW_EVIDENCE_CHARS", "12000"))

REVIEW_PROMPT = """You are an independent, second-vendor code reviewer. You did not write \
this code and you cannot open any files -- you have only the goal and the diff/evidence \
pasted below. Review it for:
  - correctness bugs
  - security issues (secrets written to logs or code, injection, unchecked input or index, \
missing auth, unsafe eval/exec, etc.)
  - scope creep: changes beyond what the stated goal calls for
  - missing error handling

STATED GOAL: {goal}
TARGET PROJECT/REPO: {project}

DIFF AND VERIFICATION EVIDENCE (already collected, read-only -- judge only what is below):
{evidence}

Respond in EXACTLY this format and nothing else:
VERDICT: CLEAN or FLAGGED
ISSUES:
- one issue per line, each starting with [BUG] [SECURITY] [SCOPE] or [ERROR-HANDLING]
- write "- none" if there are no issues and VERDICT is CLEAN
"""


def codex_review(project: str, goal: str, evidence: str) -> dict:
    """Send the goal plus the already-collected diff/evidence to the `codex` LiteLLM alias
    (OpenAI Codex CLI, ChatGPT subscription) for an independent review.

    Returns:
        {"ran": bool, "served_by": str|None, "verdict": "CLEAN"|"FLAGGED"|None,
         "issues": list[str], "raw": str, "note": str}

    `ran` is False whenever the result must NOT be trusted as a genuine cross-vendor pass
    (bridge unreachable, Codex error, malformed response, or LiteLLM served a different
    model despite `disable_fallbacks`) -- `note` always explains why in that case.
    """
    prompt = REVIEW_PROMPT.format(goal=goal, project=project, evidence=evidence[:EVIDENCE_CHARS])
    body = {
        "model": "codex",
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1200,
        "temperature": 0.1,
        # Never let this call fall back to another alias. A silent fallback (e.g. to
        # `balanced`, which is Claude Sonnet) would produce Claude reviewing Claude while
        # every log and prompt downstream keeps calling it a cross-vendor pass.
        "disable_fallbacks": True,
    }
    try:
        r = requests.post(f"{LITELLM_BASE}/v1/chat/completions", json=body,
                          headers={"Authorization": f"Bearer {LITELLM_KEY}"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        return _fail(None, f"codex bridge unreachable: {e}")
    if r.status_code != 200:
        return _fail(None, f"codex alias returned HTTP {r.status_code}: {r.text[:300]}")
    try:
        d = r.json()
    except ValueError:
        return _fail(None, "codex alias returned a non-JSON body")

    served = d.get("model")
    if served != "codex":
        # The exact failure mode the roadmap calls out: LiteLLM answered 200, but not with
        # Codex. That is a failed cross-vendor pass, not a successful one -- reject it.
        return _fail(served, f"REJECTED: LiteLLM served '{served}', not 'codex' -- this "
                     f"would be {served or 'an unknown model'} reviewing Claude's own work "
                     f"under a false cross-vendor label; discarding the response")
    try:
        text = d["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return _fail(served, "codex responded 200 but with no message content")

    verdict, issues = _parse(text)
    return {"ran": True, "served_by": served, "verdict": verdict, "issues": issues,
            "raw": text, "note": "reviewed by OpenAI Codex CLI (ChatGPT subscription), a "
                                  "different vendor's model, independent of Claude"}


def _fail(served, reason: str) -> dict:
    return {"ran": False, "served_by": served, "verdict": None, "issues": [],
            "raw": "", "note": f"cross-vendor review did NOT run: {reason}"}


def _parse(text: str):
    verdict = None
    issues = []
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            v = line.split(":", 1)[1].strip().upper()
            verdict = "FLAGGED" if "FLAG" in v else ("CLEAN" if "CLEAN" in v else v)
        elif line.startswith("-"):
            item = line.lstrip("- ").strip()
            if item and item.lower() != "none":
                issues.append(item)
    return verdict, issues


def format_for_prompt(result: dict) -> str:
    """Render a codex_review() result for injection into the Reviewer's task prompt,
    labelled clearly as a different vendor so the agent -- and anyone reading the log --
    cannot mistake it for another Claude pass. If the pass did not run, says so plainly
    instead of leaving a gap that could read as silent agreement."""
    if not result.get("ran"):
        return (f"CROSS-VENDOR REVIEW DID NOT RUN ({result.get('note')}). Proceed on your "
                "own judgment and the verification evidence alone. Do not claim a second "
                "opinion was obtained -- state in your summary that it was unavailable.")
    lines = [f"Source: OpenAI Codex CLI via the `codex` LiteLLM alias "
             f"(served_by={result['served_by']!r}, a different vendor than this Reviewer).",
             f"Verdict: {result.get('verdict') or '(unparsed)'}"]
    if result.get("issues"):
        lines.append("Issues raised:")
        lines += [f"  - {i}" for i in result["issues"]]
    else:
        lines.append("Issues raised: none")
    if not result.get("verdict"):
        lines.append(f"Raw response (VERDICT line not found as expected):\n{result['raw'][:1500]}")
    return "\n".join(lines)
