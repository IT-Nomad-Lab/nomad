"""NOMAD v2 cockpit chat — long-term conversational memory (engine side).

Mirrors nomad-console/memory.py but synchronous and `requests`-based (the engine is a slim stdlib
container with requests, not httpx). Shares the SAME Qdrant collection and embedding model as the
v1 console, with an identical point payload (session_id/role/content/remember/ts) — so it's one
unified NOMAD memory: what you say in the cockpit and in the console recall each other.

Best-effort / fail-open: if Qdrant or Ollama is unreachable, every call degrades to a no-op (or
empty list) so chat never breaks because memory is down.

T2.2 (2026-09-23) — typed facts, mirrored from nomad-console/memory.py
------------------------------------------------------------------------
Same design, same shared facts collection (`{COLLECTION}_facts`), so a fact learned via the
cockpit and one learned via the console live in the same store. See that module's docstring for
the full rationale (the forget_last position-bug fix + aggregation/compound recall).

Known gap, stated plainly: `v2/server.py` is out of scope for this change (a parallel builder
owns it). Its existing call site is `memory_chat.forget(session_id, scope="last")` — no
`retracting_text`. forget() below defaults that to None, and a None retracting_text is a SAFE
no-op (returns 0, changes nothing) rather than the old blind-deletion behavior — so this mirror
already stops the collateral-damage bug for the v2 cockpit path today, it just doesn't yet
POSITIVELY resolve a retraction there until a follow-up passes the user's message text through
(a one-line change at that call site, deliberately not made here).
"""
import json
import os
import re
import sys
import time
import uuid

import requests

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("NOMAD_MEMORY_COLLECTION", "nomad_memory")  # unified with the console
VECTOR_SIZE = int(os.environ.get("NOMAD_EMBED_DIM", "768"))            # nomic-embed-text → 768

FACTS_COLLECTION = os.environ.get("NOMAD_MEMORY_FACTS_COLLECTION", f"{COLLECTION}_facts")
FACT_MODEL = os.environ.get("NOMAD_MEMORY_FACT_MODEL", "llama3.1:8b")  # local only, see console
RETRACTION_THRESHOLD = float(os.environ.get("NOMAD_MEMORY_RETRACTION_THRESHOLD", "0.55"))
_FACT_TYPES = ("preference", "decision", "commitment", "correction", "fact")

_ready = {"ok": False, "checked": 0.0}
_facts_ready = {"ok": False, "checked": 0.0}


def _embed(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        r = requests.post(f"{OLLAMA_URL}/api/embeddings",
                          json={"model": EMBED_MODEL, "prompt": text[:6000]}, timeout=30)
        vec = r.json().get("embedding")
        return vec or None
    except Exception:
        return None


def ensure_collection():
    if _ready["ok"] and time.time() - _ready["checked"] < 60:
        return True
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{COLLECTION}", timeout=10)
        if r.status_code == 404:
            requests.put(f"{QDRANT_URL}/collections/{COLLECTION}",
                         json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}}, timeout=10)
            for field, schema in (("session_id", "keyword"), ("remember", "bool")):
                try:
                    requests.put(f"{QDRANT_URL}/collections/{COLLECTION}/index",
                                 json={"field_name": field, "field_schema": schema}, timeout=10)
                except Exception:
                    pass
        _ready.update(ok=True, checked=time.time())
        return True
    except Exception:
        _ready.update(ok=False, checked=time.time())
        return False


def history(session_id: str, limit: int = 60):
    """This session's own turns, oldest first, so a page reload can rebuild the conversation.

    Distinct from recall(): that one is a semantic search across OTHER sessions and deliberately
    excludes this one. Every turn was already stored by remember(); without this the cockpit threw
    the transcript away on reload and only the client's in-memory copy held it. Off-the-record
    turns were never stored, so they correctly do not come back.

    Fail-open like the rest of this module: no Qdrant means an empty transcript, not a broken page.
    """
    if not ensure_collection():
        return []
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                          json={"limit": limit, "with_payload": True,
                                "filter": {"must": [
                                    {"key": "session_id", "match": {"value": session_id}}]}},
                          timeout=15)
        pts = r.json().get("result", {}).get("points", []) or []
    except Exception:
        return []
    turns = [{"role": p["payload"].get("role"), "content": p["payload"].get("content", ""),
              "ts": p["payload"].get("ts", 0)} for p in pts]
    turns.sort(key=lambda t: t["ts"])       # scroll does not order; ts is what we stored it for
    return turns


def _facts_not_ready(reason: str):
    """A facts-store outage must be OBSERVABLE, not silent — an empty recall_facts()/
    remember_fact() result is indistinguishable from 'this turn had nothing durable in it' unless
    the not-ready path itself leaves a trace (same principle ADR-010 states for empty vision
    responses: an empty result must read as a failure, never as an answer). stderr, not raised —
    this module has no logging module anywhere in this codebase (checked v2/server.py and
    nomad-console/server.py; neither uses it), and the engine runs under uvicorn same as the
    console, so stderr reaches container logs the same way in both places. No divergence from the
    console's version of this helper is warranted."""
    print(f"[memory_chat] facts store NOT READY: {reason}", file=sys.stderr)
    _facts_ready.update(ok=False, checked=time.time())
    return False


def _facts_bootstrap():
    """Create the facts collection if it doesn't exist. Cached for 60s (success only — a
    not-ready result is never cached). Mirrors nomad-console/memory.py's _facts_bootstrap().

    Fixed 2026-09-23 (Codex cross-vendor review of the T2.2 diff, confirmed, applied here after
    an earlier pass had already fixed the console half but not this one): the original version
    only checked `status_code == 404` on the GET and never checked the create PUT's status at all
    — `requests` doesn't raise on 4xx/5xx any more than httpx does — so a GET returning 500
    (Qdrant up but unhealthy) or a failed create both fell straight through to
    `_facts_ready.update(ok=True)` and stayed cached "ready" for 60 seconds. This is the engine
    side, which talks to `qdrant:6333` from inside a container — the more likely of the two to
    meet an unhealthy Qdrant — so it mattered here at least as much as in the console."""
    if _facts_ready["ok"] and time.time() - _facts_ready["checked"] < 60:
        return True
    try:
        r = requests.get(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}", timeout=10)
        if r.status_code == 200:
            _facts_ready.update(ok=True, checked=time.time())
            return True
        if r.status_code != 404:
            return _facts_not_ready(f"GET {FACTS_COLLECTION} -> {r.status_code}")
        create = requests.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}",
                              json={"vectors": {"size": VECTOR_SIZE, "distance": "Cosine"}}, timeout=10)
        if create.status_code not in (200, 201):
            return _facts_not_ready(f"create {FACTS_COLLECTION} -> {create.status_code} "
                                     f"{create.text[:200]}")
        for field, schema in (("session_id", "keyword"), ("subject", "keyword"),
                               ("predicate", "keyword"),
                               ("status", "keyword"), ("type", "keyword"),
                               ("contradicts_prior", "bool")):
            try:
                requests.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/index",
                             json={"field_name": field, "field_schema": schema}, timeout=10)
            except Exception:
                pass  # a payload index failing is non-fatal — filters still work, just slower
        _facts_ready.update(ok=True, checked=time.time())
        return True
    except Exception as e:
        return _facts_not_ready(f"exception: {e!r}")


def remember(session_id: str, role: str, content: str, sync_facts: bool = True):
    """Embed a single turn and store it as a Qdrant point. Best-effort. Also extracts typed facts
    (sync — this module has no background-task runtime the way the async console does, so the
    engine's synchronous request cycle just pays the extra ~1-2s directly; still fully fail-open)."""
    content = (content or "").strip()
    if not content or not ensure_collection():
        return None
    vec = _embed(f"{role}: {content}")
    if vec is None:
        return None
    point = {"id": str(uuid.uuid4()), "vector": vec,
             "payload": {"session_id": session_id or "cockpit", "role": role,
                         "content": content, "remember": True, "ts": time.time()}}
    try:
        requests.put(f"{QDRANT_URL}/collections/{COLLECTION}/points",
                     json={"points": [point]}, timeout=15)
    except Exception:
        return None
    if sync_facts:
        try:
            remember_fact(session_id, role, content)
        except Exception:
            pass
    return point["id"]


_FACT_PROMPT = """You extract STRUCTURED FACTS from ONE turn of a conversation, for a memory system.

A fact is a durable claim: a preference, decision, commitment, correction, or plain fact about a
named entity — NOT small talk, NOT a question, NOT a bare acknowledgment like "noted" or "got it"
with nothing new in it.

Rules:
- subject: a short, stable snake_case/dotted key naming what this is about, e.g.
  "kestrel.deploy_day", "kestrel.staging_url". Reuse the SAME subject key whenever the same
  real-world thing is referenced again, even if the wording differs. Disambiguate people who
  share a first name using a fuller name in the subject.
- predicate: a short verb phrase, e.g. "is", "prefers", "owns", "approved".
- value: the CURRENT value only, in plain text.
- type: one of preference, decision, commitment, correction, fact.
- If the turn corrects or supersedes an earlier claim, extract ONLY the new current value.
- If the turn is a question, greeting, or a pure acknowledgment with no durable content, return
  {{"facts": []}}.
- A turn may contain zero, one, or several facts.

Return ONLY valid JSON, no commentary: {{"facts": [{{"subject": "...", "predicate": "...", "value": "...", "type": "..."}}]}}

TURN ({role}): {content}
"""


def _extract_facts(role: str, content: str):
    content = (content or "").strip()
    if len(content) < 8:
        return []
    prompt = _FACT_PROMPT.format(role=role, content=content[:3000])
    try:
        r = requests.post(f"{OLLAMA_URL}/api/chat",
                          json={"model": FACT_MODEL,
                                "messages": [{"role": "user", "content": prompt}],
                                "format": "json", "stream": False,
                                "options": {"temperature": 0}}, timeout=20)
        text = r.json().get("message", {}).get("content", "")
        raw = json.loads(text).get("facts", []) or []
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


def _active_fact_by_subject(subject: str, predicate: str = None):
    """Keyed on (subject, predicate) — see nomad-console/memory.py's version of this function for
    why: matching on subject alone let two genuinely different facts sharing a subject (e.g. a
    database choice vs. a deploy strategy, both under "kestrel.reporting_service") falsely
    supersede each other, corrupting the active fact and logging a bogus contradiction."""
    must = [{"key": "subject", "match": {"value": subject}},
            {"key": "status", "match": {"value": "active"}}]
    if predicate:
        must.append({"key": "predicate", "match": {"value": predicate}})
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                          json={"limit": 3, "with_payload": True, "filter": {"must": must}}, timeout=10)
        pts = r.json().get("result", {}).get("points", []) or []
        return pts[0] if pts else None
    except Exception:
        return None


def _mark_fact_status(fact_id: str, status: str, extra: dict = None):
    payload = {"status": status}
    if extra:
        payload.update(extra)
    try:
        requests.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/payload",
                     json={"points": [fact_id], "payload": payload}, timeout=10)
    except Exception:
        pass


def remember_fact(session_id: str, role: str, content: str):
    if not _facts_bootstrap():
        return []
    facts = _extract_facts(role, content)
    stored = []
    for f in facts:
        try:
            vec = _embed(f"{f['subject']} {f['predicate']} {f['value']}")
            if vec is None:
                continue
            prior = _active_fact_by_subject(f["subject"], f["predicate"])
            contradicts, prior_id, prior_value = False, None, None
            if prior:
                prior_value_raw = prior["payload"].get("value", "")
                if prior_value_raw.strip().lower() == f["value"].strip().lower():
                    continue
                contradicts, prior_id, prior_value = True, prior["id"], prior_value_raw

            fid = str(uuid.uuid4())
            point = {"id": fid, "vector": vec,
                     "payload": {"session_id": session_id or "cockpit",
                                 "subject": f["subject"], "predicate": f["predicate"],
                                 "value": f["value"], "type": f["type"], "status": "active",
                                 "supersedes": [prior_id] if prior_id else [],
                                 "superseded_by": None, "contradicts_prior": contradicts,
                                 "prior_value": prior_value, "source_role": role,
                                 "source_text": content[:500], "ts": time.time()}}
            requests.put(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points",
                         json={"points": [point]}, timeout=15)
            if prior_id:
                _mark_fact_status(prior_id, "superseded", {"superseded_by": fid})
            stored.append(fid)
        except Exception:
            continue
    return stored


def list_conflicts(session_id: str = None, k: int = 50):
    if not _facts_bootstrap():
        return []
    must = [{"key": "contradicts_prior", "match": {"value": True}}]
    if session_id:
        must.append({"key": "session_id", "match": {"value": session_id}})
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                          json={"limit": k, "with_payload": True, "filter": {"must": must}}, timeout=15)
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


def recall_facts(query: str, k: int = 6, status: str = "active"):
    if not _facts_bootstrap():
        return []
    vec = _embed(query)
    if vec is None:
        return []
    must = [{"key": "status", "match": {"value": status}}] if status else []
    flt = {"must": must} if must else {}
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/search",
                          json={"vector": vec, "limit": k, "with_payload": True,
                                "filter": flt, "score_threshold": 0.30}, timeout=15)
        hits = r.json().get("result", []) or []
        return [{"role": "fact", "content": _format_fact(h["payload"]), "score": h.get("score", 0)}
                for h in hits]
    except Exception:
        return []


def _recall_facts_by_type(ftype: str, limit: int = 200):
    if not _facts_bootstrap():
        return []
    must = [{"key": "status", "match": {"value": "active"}}]
    if ftype and ftype != "any":
        must.append({"key": "type", "match": {"value": ftype}})
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/scroll",
                          json={"limit": limit, "with_payload": True, "filter": {"must": must}}, timeout=15)
        pts = r.json().get("result", {}).get("points", []) or []
        pts.sort(key=lambda p: p["payload"].get("ts", 0))
        return [{"role": "fact", "content": _format_fact(p["payload"]), "score": 1.0} for p in pts]
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
    q = (query or "").lower()
    if not any(c in q for c in _AGG_CUES):
        return None
    for word, ftype in _AGG_TYPE_WORDS.items():
        if word in q:
            return ftype
    return "any"


def _split_compound(query: str):
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


def forget(session_id: str, scope: str = "last", retracting_text: str = None):
    """Delete remembered turns/facts for a session. scope='session' wipes the whole conversation
    (raw + facts); scope='last' is CONTENT-ADDRESSED (see nomad-console/memory.py's docstring) —
    with no retracting_text (today's v2/server.py call site) this is a safe no-op returning 0,
    which is strictly safer than the old positional deletion it replaces."""
    if not ensure_collection():
        return 0
    if scope == "session":
        try:
            requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                          json={"filter": {"must": [
                              {"key": "session_id", "match": {"value": session_id}}]}}, timeout=15)
            if _facts_bootstrap():
                requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/delete",
                              json={"filter": {"must": [
                                  {"key": "session_id", "match": {"value": session_id}}]}}, timeout=15)
        except Exception:
            pass
        return -1
    if not retracting_text:
        return 0
    target = _resolve_retraction(session_id, retracting_text)
    if not target:
        return 0
    _mark_fact_status(target["id"], "retracted")
    return 1


def _resolve_retraction(session_id: str, retracting_text: str, threshold: float = None):
    threshold = RETRACTION_THRESHOLD if threshold is None else threshold
    if not _facts_bootstrap():
        return None
    vec = _embed(retracting_text)
    if vec is None:
        return None
    for flt in (
        {"must": [{"key": "session_id", "match": {"value": session_id}},
                  {"key": "status", "match": {"value": "active"}}]},
        {"must": [{"key": "status", "match": {"value": "active"}}]},
    ):
        try:
            r = requests.post(f"{QDRANT_URL}/collections/{FACTS_COLLECTION}/points/search",
                              json={"vector": vec, "limit": 5, "with_payload": True,
                                    "filter": flt, "score_threshold": threshold}, timeout=15)
            hits = r.json().get("result", []) or []
        except Exception:
            hits = []
        if hits:
            return hits[0]
    return None


# ── opt-out phrase detection (mirrors nomad-console/memory.py) ──────
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


def recall(query: str, k: int = 5, exclude_session: str = None):
    """Up to k of the most relevant remembered turns for `query`, cross-session by design. Same
    aggregation/compound handling as nomad-console/memory.py's recall() — see that module for the
    rationale."""
    if not ensure_collection() or not query:
        return []

    def _raw(q, kk):
        vec = _embed(q)
        if vec is None:
            return []
        flt = {"must": [{"key": "remember", "match": {"value": True}}]}
        if exclude_session:
            flt["must_not"] = [{"key": "session_id", "match": {"value": exclude_session}}]
        try:
            r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                              json={"vector": vec, "limit": kk, "with_payload": True,
                                    "filter": flt, "score_threshold": 0.35}, timeout=15)
            hits = r.json().get("result", []) or []
            return [{"role": h["payload"].get("role"), "content": h["payload"].get("content", ""),
                      "score": h.get("score", 0)} for h in hits]
        except Exception:
            return []

    hits = []
    agg_type = _looks_like_aggregation(query)
    compound = None if agg_type else _split_compound(query)
    if agg_type:
        hits += _recall_facts_by_type(agg_type)
        hits += _raw(query, k)
    elif compound:
        a, b = compound
        hits += _raw(a, k)
        hits += _raw(b, k)
        hits += recall_facts(query, k=k)
    else:
        hits += _raw(query, k)
        hits += recall_facts(query, k=min(k, 6))

    seen = {}
    for h in hits:
        key = (h.get("role"), h.get("content"))
        if key not in seen or h.get("score", 0) > seen[key].get("score", 0):
            seen[key] = h
    out = list(seen.values())
    out.sort(key=lambda h: h.get("score", 0), reverse=True)
    cap = max(k, 16) if (agg_type or compound) else k
    return [{"role": h["role"], "content": h["content"]} for h in out[:cap]]
