"""LiteLLM gateway helper for the NOMAD v2 engine — role aliases only, never raw models."""
import json
import os
import urllib.request

from nocodb import _env

_E = _env()
_BASE = _E.get("LITELLM_BASE_URL", "http://localhost:4000").rstrip("/")
_KEY = _E.get("LITELLM_MASTER_KEY", "")


def chat(alias, system, user, max_tokens=400):
    """One-shot completion through a LiteLLM role alias (deep/balanced/fast/...)."""
    body = {"model": alias, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}]}
    r = urllib.request.Request(_BASE + "/v1/chat/completions",
                               data=json.dumps(body).encode(), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", f"Bearer {_KEY}")
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode())["choices"][0]["message"]["content"].strip()


def chat_json(alias, system, user, max_tokens=400):
    """Like chat() but best-effort parse a JSON object from the reply."""
    txt = chat(alias, system + " Reply with ONLY a JSON object.", user, max_tokens)
    try:
        s, e = txt.index("{"), txt.rindex("}") + 1
        return json.loads(txt[s:e])
    except Exception:
        return {}
