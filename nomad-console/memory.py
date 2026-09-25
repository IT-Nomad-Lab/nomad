"""NOMAD long-term conversational memory.

Phase-1 memory layer for the assistant: every conversation turn is embedded
(nomic-embed-text on the native Ollama GPU) and stored as a point in Qdrant.
Before each reply NOMAD recalls the most semantically relevant past turns and
injects them into context — so it "remembers all conversations" without ever
overflowing the prompt window.

Design notes
------------
* httpx-only — talks to Qdrant + Ollama over REST. No new pip deps, no extra
  client libraries to version-pin.
* Best-effort / fail-open — if Qdrant or Ollama is unreachable, every function
  degrades to a no-op (or empty list) so chat never breaks because memory is down.
* Durability — the Qdrant volume persists across restarts, so the transcript and
  its vectors survive reboots. Each point carries the full turn in its payload,
  so Qdrant doubles as the ordered transcript store (no separate Postgres table
  needed for Phase 1).
* Opt-out — a turn stored with remember=False is excluded from recall; an
  explicit forget() deletes points. Default is to remember everything.

Phase-2 (T2.2, 2026-09-23) — typed facts on top of raw turns
--------------------------------------------------------------
Per docs/research/T1.3-memory-bakeoff.md + T1.3b: the raw-turn design scored
17.5/20 on the locked benchmark. Two real defects, both fixed here without
replacing anything:

1. `forget_last` used to delete "the last 4 points in this session" BY
   POSITION — a real, measured bug (Q15) that deleted an unrelated colleague's
   name as collateral damage. It is now CONTENT-ADDRESSED: it resolves the
   specific claim being retracted via a typed-facts search, and only ever
   flips that one fact's status. If nothing resolves with confidence, it does
   NOTHING — no blind deletion. See forget()/_resolve_retraction() below.
2. Compound ("X and Y") and aggregation ("list every X") queries structurally
   lose to top-k crowding in a flat similarity search (Q17, Q20). A typed
   facts store — extracted at write time via the local `fast`-alias model,
   keyed by a stable `subject`, with explicit supersession — lets aggregation
   queries enumerate every matching fact directly instead of gambling on
   embedding distance, and lets compound questions get answered as two
   sub-queries. See recall()/_looks_like_aggregation()/_split_compound().

Migration: raw turns are UNCHANGED and UNTOUCHED — same collection, same
schema, same functions, same behavior for every existing caller. Facts are a
NEW, separate Qdrant collection (`{COLLECTION}_facts` by default) built up
going forward from new turns; nothing is backfilled from the 91 existing
points and nothing about them is deleted or migrated. See
docs/research/T2.2-memory-facts.md for the full design + benchmark re-run.
"""
import json
import os
import re
import sys
import time
import uuid

import httpx

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("NOMAD_MEMORY_COLLECTION", "nomad_memory")
VECTOR_SIZE = int(os.environ.get("NOMAD_EMBED_DIM", "768"))  # nomic-embed-text → 768

# ── Phase-2: typed facts store ───────────────────────────────────────
FACTS_COLLECTION = os.environ.get("NOMAD_MEMORY_FACTS_COLLECTION", f"{COLLECTION}_facts")
# Extraction stays OFF the LiteLLM gateway and talks straight to the native Ollama, same as the
# embedding call above (an established exception to "route through role aliases," not a new one —
# this is an infra-level extraction step, not an agent). NOMAD_MEMORY_FACT_MODEL defaults to the
# model backing the `fast` alias (litellm/config.yaml: fast -> ollama/llama3.1:8b, fallback
# -> private, ALSO local) so real operator content extracted here never leaves the machine.
FACT_MODEL = os.environ.get("NOMAD_MEMORY_FACT_MODEL", "llama3.1:8b")
RETRACTION_THRESHOLD = float(os.environ.get("NOMAD_MEMORY_RETRACTION_THRESHOLD", "0.55"))
_FACT_TYPES = ("preference", "decision", "commitment", "correction", "fact")

_ready = {"ok": False, "checked": 0.0}
_facts_ready = {"ok": False, "checked": 0.0, "reason": "not checked yet"}


# ── embeddings ──────────────────────────────────────────────────────
async def _embed(text: str):
    """Return a 768-d embedding for `text`, or None if Ollama is unreachable."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(f"{OLLAMA_URL}/api/embeddings",
                             json={"model": EMBED_MODEL, "prompt": text[:6000]})
            vec = r.json().get("embedding")
            return vec if vec else None
    except Exception:
        return None


# ── collection bootstrap ────────────────────────────────────────────
async def ensure_collection():
    """Create the Qdrant collection if it doesn't exist. Cached for 60s."""
    if _ready["ok"] and time.time() - _ready["checked"] < 60:
        return True
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{QDRANT_URL}/collections/{COLLECTION}")
            if r.status_code == 404:
                await c.put(f"{QDRANT_URL}/collections/{COLLECTION}",
                            json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}})
                # index session_id + remember so filtered scroll/recall stay fast
                for field, schema in (("session_id", "keyword"), ("remember", "bool")):
                    try:
                        await c.put(f"{QDRANT_URL}/collections/{COLLECTION}/index",
                                    json={"field_name": field, "field_schema": schema})
                    except Exception:
                        pass
            _ready.update(ok=True, checked=time.time())
            return True
    except Exception:
        _ready.update(ok=False, checked=time.time())
        return False


def _facts_not_ready(reason: str):
    """A facts-store outage must be OBSERVABLE, not silent — an empty recall_facts()/
    remember_fact() result is indistinguishable from 'this turn had nothing durable in it'
    unless the not-ready path itself leaves a trace somewhere a human can find it (same
    principle ADR-010 states for empty vision responses: an empty result must read as a
    failure, never as an answer). stderr, not raised, so it never breaks the fail-open
    contract — it just stops the failure from being silent."""
    print(f"[memory] facts store NOT READY: {reason}", file=sys.stderr)
    _facts_ready.update(ok=False, checked=time.time())
    return False


async def _facts_bootstrap():
    """Create the facts collection if it doesn't exist. Cached for 60s (success only — a
    not-ready result is never cached, so the next call retries instead of waiting out the
    window). Separate from ensure_collection() so a facts-store outage never blocks raw-turn
    storage/recall.

    Fixed 2026-09-23 (Codex cross-vendor review of the T2.2 diff, confirmed): the original
    version only checked `status_code == 404` on the GET and never checked the create PUT's
    status at all. A GET returning 500 (Qdrant up but unhealthy) or a create that failed for
    any reason both fell straight through to `_facts_ready.update(ok=True)` and stayed cached
    "ready" for 60 seconds — every fact write in that window silently no-ops. Now every branch
    that isn't a confirmed-ready collection returns False through _facts_not_ready() instead of
    being cached True."""
    if _facts_ready["ok"] and time.time() - _facts_ready["checked"] < 60:
        return True
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}")
            if r.status_code == 200:
                _facts_ready.update(ok=True, checked=time.time())
                return True
            if r.status_code != 404:
                return _facts_not_ready(f"GET {FACTS_COLLECTION} -> {r.status_code}")
            create = await c.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}",
                                  json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}})
            if create.status_code not in (200, 201):
                return _facts_not_ready(f"create {FACTS_COLLECTION} -> {create.status_code} "
                                         f"{create.text[:200]}")
            for field, schema in (("session_id", "keyword"), ("subject", "keyword"),
                                   ("predicate", "keyword"),
                                   ("status", "keyword"), ("type", "keyword"),
                                   ("contradicts_prior", "bool")):
                try:
                    await c.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/index",
                                json={"field_name": field, "field_schema": schema})
                except Exception:
                    pass  # a payload index failing is non-fatal — filters still work, just slower
            _facts_ready.update(ok=True, checked=time.time())
            return True
    except Exception as e:
        return _facts_not_ready(f"exception: {e!r}")


# ── write: raw turn (unchanged behavior) ─────────────────────────────
async def remember(session_id: str, role: str, content: str, remember_flag: bool = True,
                    sync_facts: bool = False):
    """Embed a single turn and store it as a Qdrant point. Best-effort.

    Also kicks off typed-fact extraction for this turn (Phase-2, see module docstring). That step
    is fully independent of raw-turn storage: it never blocks or fails this function, and by
    default it runs fire-and-forget (sync_facts=False) so it adds zero latency to the caller —
    pass sync_facts=True (used by the benchmark harness) to await it deterministically instead.
    """
    content = (content or "").strip()
    if not content or not await ensure_collection():
        return None
    vec = await _embed(f"{role}: {content}")
    if vec is None:
        return None
    pid = str(uuid.uuid4())
    point = {
        "id": pid,
        "vector": vec,
        "payload": {
            "session_id": session_id or "default",
            "role": role,
            "content": content,
            "remember": bool(remember_flag),
            "ts": time.time(),
        },
    }
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.put(f"{QDRANT_URL}/collections/{COLLECTION}/points",
                        json={"points": [point]})
    except Exception:
        return None

    if remember_flag:
        if sync_facts:
            try:
                await remember_fact(session_id, role, content)
            except Exception:
                pass
        else:
            try:
                import asyncio
                asyncio.create_task(_remember_fact_safe(session_id, role, content))
            except Exception:
                pass
    return pid


async def _remember_fact_safe(session_id, role, content):
    """Wrapper for the fire-and-forget path — swallow everything so a bad extraction never
    produces an 'exception was never retrieved' warning or touches the caller."""
    try:
        await remember_fact(session_id, role, content)
    except Exception:
        pass


# ── Phase-2: typed-fact extraction + conflict-aware storage ─────────
_FACT_PROMPT = """You extract STRUCTURED FACTS from ONE turn of a conversation, for a memory system.

A fact is a durable claim: a preference, decision, commitment, correction, or plain fact about a
named entity — NOT small talk, NOT a question, NOT a bare acknowledgment like "noted" or "got it"
with nothing new in it.

Rules:
- subject: a short, stable snake_case/dotted key naming what this is about, e.g.
  "kestrel.deploy_day", "kestrel.staging_url". Reuse the SAME subject key whenever the same
  real-world thing is referenced again, even if the wording differs — this is how the memory
  system tracks a value changing over time. Disambiguate people who share a first name by using
  a fuller name in the subject (e.g. "kestrel.alex_duarte.request" vs "internal.alex_ferreira.task")
  — never collapse two different people into one subject just because they share a first name.
- predicate: a short verb phrase, e.g. "is", "prefers", "owns", "approved".
- value: the CURRENT value only, in plain text.
- type: one of preference, decision, commitment, correction, fact.
- If the turn corrects or supersedes an earlier claim, extract ONLY the new current value, not the
  old, superseded one.
- If the turn is a question, greeting, or a pure acknowledgment with no durable content, return
  {{"facts": []}}.
- A turn may contain zero, one, or several facts.

Return ONLY valid JSON, no commentary: {{"facts": [{{"subject": "...", "predicate": "...", "value": "...", "type": "..."}}]}}

TURN ({role}): {content}
"""


async def _extract_facts(role: str, content: str):
    """Best-effort structured-fact extraction. Talks directly to the local Ollama model backing
    the `fast` alias (see module docstring) — never the LiteLLM gateway's cloud-capable aliases.
    Returns [] on ANY failure (timeout, bad JSON, model down) so this is always fail-open."""
    content = (content or "").strip()
    if len(content) < 8:
        return []
    prompt = _FACT_PROMPT.format(role=role, content=content[:3000])
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{OLLAMA_URL}/api/chat",
                             json={"model": FACT_MODEL,
                                   "messages": [{"role": "user", "content": prompt}],
                                   "format": "json", "stream": False,
                                   "options": {"temperature": 0}})
            text = r.json().get("message", {}).get("content", "")
        parsed = json.loads(text)
        raw = parsed.get("facts", []) or []
    except Exception:
        return []
    out = []
    for f in raw:
        try:
            subj = str(f.get("subject", "")).strip().lower()
            val = str(f.get("value", "")).strip()
            pred = str(f.get("predicate", "")).strip()
            ftype = str(f.get("type", "fact")).strip().lower()
        except Exception:
            continue
        if not subj or not val:
            continue
        if ftype not in _FACT_TYPES:
            ftype = "fact"
        out.append({"subject": subj, "predicate": pred, "value": val, "type": ftype})
    return out


async def _active_fact_by_subject(subject: str, predicate: str = None):
    """Exact-match lookup (not similarity) — subjects are stable keys, so this is a precise
    membership check, avoiding the false-negative risk of a similarity threshold miss.

    Keyed on (subject, predicate), not subject alone (found during T2.2's own benchmark re-run,
    not in the original T1.3 bake-off): the extractor can legitimately emit two DIFFERENT facts
    that share a subject but describe different attributes of it — e.g. "kestrel.reporting_service
    uses Postgres" (predicate "uses") and, in a later turn, "kestrel.reporting_service will adopt
    blue-green deploy strategy" (predicate "will adopt"). Matching on subject alone treated the
    second as CORRECTING the first, silently marking a still-true fact "superseded" and logging a
    bogus contradiction. Matching on (subject, predicate) treats same-predicate re-statements as
    the value evolving (correct) and different-predicate statements as two independent facts about
    the same subject (correct) — see docs/research/T2.2-memory-facts.md for the worked example."""
    must = [{"key": "subject", "match": {"value": subject}},
            {"key": "status", "match": {"value": "active"}}]
    if predicate:
        must.append({"key": "predicate", "match": {"value": predicate}})
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                             json={"limit": 3, "with_payload": True, "filter": {"must": must}})
        pts = r.json().get("result", {}).get("points", []) or []
        return pts[0] if pts else None
    except Exception:
        return None


async def _mark_fact_status(fact_id: str, status: str, extra: dict = None):
    payload = {"status": status}
    if extra:
        payload.update(extra)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/payload",
                        json={"points": [fact_id], "payload": payload})
    except Exception:
        pass


async def remember_fact(session_id: str, role: str, content: str):
    """Extract typed facts from one turn and store them with explicit conflict handling, keyed on
    (subject, predicate) — same key + same value = no-op (dedupe, not a duplicate point); same
    key + a DIFFERENT value = the prior fact is marked superseded (never deleted) and the new one
    is marked contradicts_prior=True, so a contradiction is FLAGGED (see list_conflicts()) instead
    of two unrelated-looking facts just sitting side by side in the store. A different predicate
    on the same subject is treated as an independent fact, not a correction. Fully best-effort —
    any failure here is swallowed; it never affects raw-turn storage."""
    if not await _facts_bootstrap():
        return []
    facts = await _extract_facts(role, content)
    stored = []
    for f in facts:
        try:
            vec = await _embed(f"{f['subject']} {f['predicate']} {f['value']}")
            if vec is None:
                continue
            prior = await _active_fact_by_subject(f["subject"], f["predicate"])
            contradicts = False
            prior_id = None
            prior_value = None
            if prior:
                prior_value_raw = prior["payload"].get("value", "")
                if prior_value_raw.strip().lower() == f["value"].strip().lower():
                    continue  # identical restatement — nothing new to store
                contradicts = True
                prior_id = prior["id"]
                prior_value = prior_value_raw

            fid = str(uuid.uuid4())
            point = {
                "id": fid, "vector": vec,
                "payload": {
                    "session_id": session_id or "default",
                    "subject": f["subject"], "predicate": f["predicate"], "value": f["value"],
                    "type": f["type"], "status": "active",
                    "supersedes": [prior_id] if prior_id else [],
                    "superseded_by": None,
                    "contradicts_prior": contradicts,
                    "prior_value": prior_value,
                    "source_role": role, "source_text": content[:500],
                    "ts": time.time(),
                },
            }
            async with httpx.AsyncClient(timeout=15) as c:
                await c.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points",
                            json={"points": [point]})
            if prior_id:
                await _mark_fact_status(prior_id, "superseded", {"superseded_by": fid})
            stored.append(fid)
        except Exception:
            continue
    return stored


async def list_conflicts(session_id: str = None, k: int = 50):
    """Return the flagged contradiction log: every fact that superseded a DIFFERENT prior value
    for the same subject, oldest first. This is the demonstrable half of "flagged, not silently
    stored twice" — see docs/research/T2.2-memory-facts.md for a worked example."""
    if not await _facts_bootstrap():
        return []
    must = [{"key": "contradicts_prior", "match": {"value": True}}]
    if session_id:
        must.append({"key": "session_id", "match": {"value": session_id}})
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                             json={"limit": k, "with_payload": True, "filter": {"must": must}})
        pts = r.json().get("result", {}).get("points", []) or []
        pts.sort(key=lambda p: p["payload"].get("ts", 0))
        return [{"subject": p["payload"].get("subject"),
                  "old_value": p["payload"].get("prior_value"),
                  "new_value": p["payload"].get("value"),
                  "session_id": p["payload"].get("session_id"),
                  "ts": p["payload"].get("ts")} for p in pts]
    except Exception:
        return []


def _format_fact(p: dict) -> str:
    note = "" if p.get("status") == "active" else " [SUPERSEDED — later replaced, not current]"
    return f"[FACT{note}] {p.get('subject')} {p.get('predicate')} {p.get('value')} (session {p.get('session_id')})"


async def recall_facts(query: str, k: int = 6, exclude_session: str = None, status: str = "active"):
    """Similarity search over the facts store — the same shape as recall() below, but against
    typed facts instead of raw turns. Used both standalone (aggregation path) and folded into
    recall() (default path, so a single question also benefits from a clean current-state fact
    alongside the raw turns that give it context/citation)."""
    if not await _facts_bootstrap():
        return []
    vec = await _embed(query)
    if vec is None:
        return []
    must = [{"key": "status", "match": {"value": status}}] if status else []
    flt = {"must": must} if must else {}
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/search",
                             json={"vector": vec, "limit": k, "with_payload": True,
                                   "filter": flt, "score_threshold": 0.30})
        hits = r.json().get("result", []) or []
        return [{"role": "fact", "content": _format_fact(h["payload"]),
                  "ts": h["payload"].get("ts", 0), "score": h.get("score", 0)} for h in hits]
    except Exception:
        return []


async def _recall_facts_by_type(ftype: str, limit: int = 200):
    """Deterministic enumeration — NOT a similarity top-k. Every active fact of a type (or, if
    ftype == 'any', every active fact) is returned, so a 'list every decision' query cannot lose
    an item to top-k crowding the way a flat vector search structurally can (Q17's failure mode)."""
    if not await _facts_bootstrap():
        return []
    must = [{"key": "status", "match": {"value": "active"}}]
    if ftype and ftype != "any":
        must.append({"key": "type", "match": {"value": ftype}})
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                             json={"limit": limit, "with_payload": True, "filter": {"must": must}})
        pts = r.json().get("result", {}).get("points", []) or []
        pts.sort(key=lambda p: p["payload"].get("ts", 0))
        return [{"role": "fact", "content": _format_fact(p["payload"]),
                  "ts": p["payload"].get("ts", 0), "score": 1.0} for p in pts]
    except Exception:
        return []


_AGG_TYPE_WORDS = {"decision": "decision", "decisions": "decision",
                    "preference": "preference", "preferences": "preference",
                    "commitment": "commitment", "commitments": "commitment",
                    "correction": "correction", "corrections": "correction"}
_AGG_CUES = ("list every", "list all", "every ", "all the", "each of the",
             "name every", "what are all")
_WH_WORDS = ("what", "who", "when", "where", "why", "how", "which")


def _looks_like_aggregation(query: str):
    """Return a fact `type` to enumerate deterministically, or None if this isn't a
    list-everything question. 'any' means the cue fired but no specific type word was found —
    enumerate every active fact and let the reader narrow it down (see
    docs/research/T2.2-memory-facts.md for why the type filter alone can't be trusted to be the
    only gate)."""
    q = (query or "").lower()
    if not any(c in q for c in _AGG_CUES):
        return None
    for word, ftype in _AGG_TYPE_WORDS.items():
        if word in q:
            return ftype
    return "any"


def _split_compound(query: str):
    """Very targeted heuristic: an 'X and Y' question where BOTH halves independently look like
    a question (contain a wh-word). Splits Q20-shaped questions into two sub-queries so neither
    half has to win a ranking contest against the other for space in a single top-k."""
    q = (query or "").strip()
    low = q.lower()
    for m in re.finditer(r"\band\b", low):
        idx = m.start()
        first = q[:idx].strip().rstrip(",")
        rest = q[idx + 3:].strip()
        if not rest:
            continue
        if any(rest.lower().startswith(w) for w in _WH_WORDS) and any(w in first.lower() for w in _WH_WORDS):
            return first, rest
    return None


# ── read: semantic recall (raw turns) ────────────────────────────────
async def _raw_recall(query: str, k: int = 6, exclude_session: str = None):
    """The original top-k raw-turn search, unchanged in behavior — kept as its own function so
    recall() can call it multiple times for a split compound query."""
    if not await ensure_collection():
        return []
    vec = await _embed(query)
    if vec is None:
        return []
    must = [{"key": "remember", "match": {"value": True}}]
    must_not = []
    if exclude_session:
        must_not.append({"key": "session_id", "match": {"value": exclude_session}})
    flt = {"must": must}
    if must_not:
        flt["must_not"] = must_not
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                             json={"vector": vec, "limit": k, "with_payload": True,
                                   "filter": flt, "score_threshold": 0.35})
            hits = r.json().get("result", []) or []
        return [{"role": h["payload"].get("role"),
                 "content": h["payload"].get("content", ""),
                 "ts": h["payload"].get("ts", 0),
                 "score": h.get("score", 0)} for h in hits]
    except Exception:
        return []


async def recall(query: str, k: int = 6, exclude_session: str | None = None):
    """Return up to k of the most relevant remembered turns for `query`.

    Cross-session by design (NOMAD recalls from any past conversation). The
    current session's recent turns are usually already in the live window, so
    pass exclude_session to avoid echoing them back as "memories".

    Phase-2: also folds in typed facts (recall_facts), and detects two shapes of query the flat
    raw-turn search structurally struggles with (see module docstring) — aggregation ("list every
    X") gets a deterministic full enumeration of matching facts instead of a similarity top-k, and
    compound ("X and Y") gets split into two sub-queries so one half can't crowd out the other.
    Both are ADDITIVE: the plain raw-turn search still runs too, so this can only add coverage,
    never remove what recall() already returned before.
    """
    if not query:
        return []

    hits = []
    agg_type = _looks_like_aggregation(query)
    compound = None if agg_type else _split_compound(query)

    if agg_type:
        hits += await _recall_facts_by_type(agg_type)
        hits += await _raw_recall(query, k=k, exclude_session=exclude_session)
    elif compound:
        a, b = compound
        hits += await _raw_recall(a, k=k, exclude_session=exclude_session)
        hits += await _raw_recall(b, k=k, exclude_session=exclude_session)
        hits += await recall_facts(query, k=k, exclude_session=exclude_session)
    else:
        hits += await _raw_recall(query, k=k, exclude_session=exclude_session)
        hits += await recall_facts(query, k=min(k, 6), exclude_session=exclude_session)

    seen = {}
    for h in hits:
        key = (h.get("role"), h.get("content"))
        if key not in seen or h.get("score", 0) > seen[key].get("score", 0):
            seen[key] = h
    out = list(seen.values())
    out.sort(key=lambda h: h.get("score", 0), reverse=True)
    cap = max(k, 16) if (agg_type or compound) else k
    return out[:cap]


# ── read: ordered transcript for one session (rehydrate on reload) ──
async def history(session_id: str, limit: int = 60):
    if not await ensure_collection():
        return []
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                             json={"limit": limit, "with_payload": True,
                                   "filter": {"must": [
                                       {"key": "session_id", "match": {"value": session_id}}]}})
            pts = r.json().get("result", {}).get("points", []) or []
        turns = [{"role": p["payload"].get("role"),
                  "content": p["payload"].get("content", ""),
                  "ts": p["payload"].get("ts", 0)} for p in pts]
        turns.sort(key=lambda t: t["ts"])
        return turns
    except Exception:
        return []


# ── forget ──────────────────────────────────────────────────────────
async def _resolve_retraction(session_id: str, retracting_text: str, threshold: float = None):
    """Find the SPECIFIC fact being retracted, by content — never by position. Tries the current
    session's active facts first, then falls back to a cross-session search (a retraction can
    refer to something said in an earlier session). Returns None (do nothing) if nothing clears
    the confidence threshold, rather than guessing and deleting the wrong thing."""
    threshold = RETRACTION_THRESHOLD if threshold is None else threshold
    if not await _facts_bootstrap():
        return None
    vec = await _embed(retracting_text)
    if vec is None:
        return None
    for flt in (
        {"must": [{"key": "session_id", "match": {"value": session_id}},
                  {"key": "status", "match": {"value": "active"}}]},
        {"must": [{"key": "status", "match": {"value": "active"}}]},
    ):
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                r = await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/search",
                                 json={"vector": vec, "limit": 5, "with_payload": True,
                                       "filter": flt, "score_threshold": threshold})
            hits = r.json().get("result", []) or []
        except Exception:
            hits = []
        if hits:
            return hits[0]
    return None


async def forget(session_id: str, scope: str = "last", retracting_text: str = None):
    """Delete/retract remembered content.

    scope='session' wipes the whole conversation (raw turns AND facts) — unchanged, still a hard
    delete, because this is an explicit, unambiguous user request ("wipe your memory") with
    nothing to disambiguate.

    scope='last' ("forget that" / "scratch that" / …) used to delete the last 4 raw points in the
    session BY POSITION — a real bug (see module docstring, Q15). It is now CONTENT-ADDRESSED: it
    requires the retracting utterance's own text, searches the typed-facts store for the specific
    claim it names, and — if and only if a confident match is found — marks THAT ONE fact
    retracted (never deleted, so an "as of" question about the past can still see it existed).
    Nothing else is ever touched. Without retracting_text (an old caller that doesn't pass it,
    or no confident match), this is a safe no-op: it returns 0 and changes nothing, which is a
    strictly smaller blast radius than the bug it replaces, even though it means such a caller
    won't actively resolve the retraction until it's updated to pass the text.
    """
    if not await ensure_collection():
        return 0
    if scope == "session":
        try:
            async with httpx.AsyncClient(timeout=15) as c:
                await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                             json={"filter": {"must": [
                                 {"key": "session_id", "match": {"value": session_id}}]}})
                if await _facts_bootstrap():
                    await c.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/delete",
                                 json={"filter": {"must": [
                                     {"key": "session_id", "match": {"value": session_id}}]}})
        except Exception:
            pass
        return -1

    # scope == "last"
    if not retracting_text:
        return 0
    target = await _resolve_retraction(session_id, retracting_text)
    if not target:
        return 0
    await _mark_fact_status(target["id"], "retracted")
    return 1


# ── opt-out phrase detection ────────────────────────────────────────
_FORGET_WIPE = ("forget everything", "wipe your memory", "wipe my memory",
                "delete this conversation", "forget this conversation",
                "clear your memory", "forget all of this")
_FORGET_LAST = ("forget that", "forget the last", "don't remember that",
                "do not remember that", "forget what i just", "scratch that")
_OFF_RECORD = ("off the record", "don't remember this", "do not remember this",
               "don't save this", "do not save this", "don't store this",
               "keep this private", "this is off the record")


def classify_memory_intent(text: str):
    """Return one of: 'wipe', 'forget_last', 'off_record', or None."""
    t = (text or "").lower()
    if any(p in t for p in _FORGET_WIPE):
        return "wipe"
    if any(p in t for p in _FORGET_LAST):
        return "forget_last"
    if any(p in t for p in _OFF_RECORD):
        return "off_record"
    return None
