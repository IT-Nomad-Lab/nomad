"""NOMAD v2 cockpit chat — long-term conversational memory (engine side).

Mirrors nomad-console/memory.py but synchronous and `requests`-based (the engine is a slim stdlib
container with requests, not httpx). Shares the SAME Qdrant collection and embedding model as the
v1 console, with an identical point payload (session_id/role/content/remember/ts) — so it's one
unified NOMAD memory: what you say in the cockpit and in the console recall each other.

Best-effort / fail-open: if Qdrant or Ollama is unreachable, every call degrades to a no-op (or
empty list) so chat never breaks because memory is down.
"""
import os
import time
import uuid

import requests

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("NOMAD_MEMORY_COLLECTION", "nomad_memory")  # unified with the console
VECTOR_SIZE = int(os.environ.get("NOMAD_EMBED_DIM", "768"))            # nomic-embed-text → 768

_ready = {"ok": False, "checked": 0.0}


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


def remember(session_id: str, role: str, content: str):
    """Embed a single turn and store it as a Qdrant point. Best-effort."""
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
        return point["id"]
    except Exception:
        return None


def forget(session_id: str, scope: str = "last"):
    """Delete remembered turns for a session. scope='session' wipes the whole conversation;
    scope='last' removes the most recent ~4 turns. Returns -1 (whole session) or the count removed."""
    if not ensure_collection():
        return 0
    try:
        if scope == "session":
            requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                          json={"filter": {"must": [
                              {"key": "session_id", "match": {"value": session_id}}]}}, timeout=15)
            return -1
        # scope == "last": scroll the session WITH ids, drop the most recent ~4 by ts
        r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                          json={"limit": 200, "with_payload": True, "filter": {"must": [
                              {"key": "session_id", "match": {"value": session_id}}]}}, timeout=15)
        pts = r.json().get("result", {}).get("points", []) or []
        pts.sort(key=lambda p: p["payload"].get("ts", 0))
        victims = [p["id"] for p in pts[-4:]]
        if victims:
            requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                          json={"points": victims}, timeout=15)
        return len(victims)
    except Exception:
        return 0


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
    """Up to k of the most relevant remembered turns for `query`, cross-session by design. The
    current session's recent turns are already in the live window, so exclude it to avoid echoes."""
    if not ensure_collection():
        return []
    vec = _embed(query)
    if vec is None:
        return []
    flt = {"must": [{"key": "remember", "match": {"value": True}}]}
    if exclude_session:
        flt["must_not"] = [{"key": "session_id", "match": {"value": exclude_session}}]
    try:
        r = requests.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/search",
                          json={"vector": vec, "limit": k, "with_payload": True,
                                "filter": flt, "score_threshold": 0.35}, timeout=15)
        hits = r.json().get("result", []) or []
        return [{"role": h["payload"].get("role"), "content": h["payload"].get("content", "")}
                for h in hits]
    except Exception:
        return []
