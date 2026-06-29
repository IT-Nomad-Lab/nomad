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
"""
import os
import time
import uuid

import httpx

QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://host.docker.internal:11434")
EMBED_MODEL = os.environ.get("NOMAD_EMBED_MODEL", "nomic-embed-text")
COLLECTION = os.environ.get("NOMAD_MEMORY_COLLECTION", "nomad_memory")
VECTOR_SIZE = int(os.environ.get("NOMAD_EMBED_DIM", "768"))  # nomic-embed-text → 768

_ready = {"ok": False, "checked": 0.0}


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


# ── write ───────────────────────────────────────────────────────────
async def remember(session_id: str, role: str, content: str, remember_flag: bool = True):
    """Embed a single turn and store it as a Qdrant point. Best-effort."""
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
        return pid
    except Exception:
        return None


# ── read: semantic recall ───────────────────────────────────────────
async def recall(query: str, k: int = 6, exclude_session: str | None = None):
    """Return up to k of the most relevant remembered turns for `query`.

    Cross-session by design (NOMAD recalls from any past conversation). The
    current session's recent turns are usually already in the live window, so
    pass exclude_session to avoid echoing them back as "memories".
    """
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
async def forget(session_id: str, scope: str = "last"):
    """Delete remembered turns. scope='session' wipes the whole conversation;
    scope='last' removes the most recent few turns of that session."""
    if not await ensure_collection():
        return 0
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            if scope == "session":
                await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                             json={"filter": {"must": [
                                 {"key": "session_id", "match": {"value": session_id}}]}})
                return -1  # whole session
            # scope == "last": delete the most recent ~4 turns of this session
            recent = await history(session_id, limit=200)
            if not recent:
                return 0
            # re-scroll WITH ids so we can target them
            r = await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/scroll",
                             json={"limit": 200, "with_payload": True,
                                   "filter": {"must": [
                                       {"key": "session_id", "match": {"value": session_id}}]}})
            pts = r.json().get("result", {}).get("points", []) or []
            pts.sort(key=lambda p: p["payload"].get("ts", 0))
            victims = [p["id"] for p in pts[-4:]]
            if victims:
                await c.post(f"{QDRANT_URL}/collections/{COLLECTION}/points/delete",
                             json={"points": victims})
            return len(victims)
    except Exception:
        return 0


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
