"""NOMAD v2 · 3C — episodic recall (close the learn-loop).

The Log&Learn step writes a provenance record per run (what/why/outcome/lane). This reads the
relevant ones back so the Manager's Clarify/Route is informed by past work instead of stateless.

Structured recall (recent + keyword-relevant) — no new infra. Qdrant semantic recall (reusing
v1's nomic-embed pattern) is the scale-up when episode volume grows.
"""
import re


def _words(s):
    return set(re.findall(r"[a-z][a-z0-9]{3,}", (s or "").lower()))


# The dispatcher's answer to a project name it could not resolve. An episode that records only
# that teaches the Manager nothing but the wrong name, and recalling it is how 'claude-bridge'
# came back after #851 (run 50d61fe163ff).
_PROJECT_MISS = re.compile(r"Unknown project '|' matches \d+ projects \(")


def _project_miss(e):
    return bool(_PROJECT_MISS.search(str(e.get("outcome", ""))))


def recall(db, goal: str, limit: int = 4) -> str:
    """Return a compact summary of past episodes relevant to `goal` (for the Manager's context)."""
    try:
        eps = db.list("episodic", 60)
    except Exception:
        return ""
    eps = [e for e in eps if not _project_miss(e)]
    eps.sort(key=lambda e: e.get("created_at") or "", reverse=True)
    gw = _words(goal)

    def score(e):
        text = " ".join(str(e.get(k, "")) for k in ("what", "outcome", "agent"))
        return len(gw & _words(text))

    matched = [e for e in sorted(eps, key=score, reverse=True) if score(e) > 0][:limit]
    picks = matched or eps[:min(2, limit)]          # keyword hits, else most recent
    if not picks:
        return ""
    return "\n".join(
        f"- [{e.get('agent', '?')}] {str(e.get('what', ''))[:70]} → {str(e.get('outcome', ''))[:50]}"
        for e in picks)
