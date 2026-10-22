"""LiteLLM gateway helper for the NOMAD v2 engine — role aliases only, never raw models."""
import json
import os
import urllib.error
import urllib.request

from nocodb import _env

_E = _env()
_BASE = _E.get("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
_KEY = _E.get("LITELLM_MASTER_KEY", "")


def chat(alias, system, user, max_tokens=400, timeout=120, extra=None):
    """One-shot completion through a LiteLLM role alias (deep/balanced/fast/...). `extra` is
    merged into the request body (e.g. {"disable_fallbacks": True})."""
    body = {"model": alias, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}], **(extra or {})}
    r = urllib.request.Request(_BASE + "/v1/chat/completions",
                               data=json.dumps(body).encode(), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", f"Bearer {_KEY}")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()


def chat_json(alias, system, user, max_tokens=400):
    """Like chat() but best-effort parse a JSON object from the reply."""
    txt = chat(alias, system + " Reply with ONLY a JSON object.", user, max_tokens)
    try:
        s, e = txt.index("{"), txt.rindex("}") + 1
        return json.loads(txt[s:e])
    except Exception:
        return {}


def stream(body, timeout=300):
    """A streamed completion: (response headers, iterator of text deltas). The connection is
    opened before returning, so a refused request raises here, with LiteLLM's error text."""
    r = urllib.request.Request(_BASE + "/v1/chat/completions",
                               data=json.dumps({**body, "stream": True}).encode(), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", f"Bearer {_KEY}")
    try:
        resp = urllib.request.urlopen(r, timeout=timeout)
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LiteLLM {e.code}: {e.read().decode(errors='replace')[:300]}") from None

    def deltas():
        with resp:
            for raw in resp:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                if msg.get("error"):
                    raise RuntimeError(str(msg["error"])[:300])
                for ch in msg.get("choices") or []:
                    text = (ch.get("delta") or {}).get("content")
                    if text:
                        yield text
    return dict(resp.headers.items()), deltas()


def complete(body, timeout=180):
    """One non-streamed completion with the body as given, tools included: (response headers,
    the assistant message dict). For Open WebUI's local tool turns (owui_chat, #1147)."""
    r = urllib.request.Request(_BASE + "/v1/chat/completions",
                               data=json.dumps({**body, "stream": False}).encode(), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", f"Bearer {_KEY}")
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return dict(resp.headers.items()), json.loads(resp.read().decode())["choices"][0]["message"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"LiteLLM {e.code}: {e.read().decode(errors='replace')[:300]}") from None
