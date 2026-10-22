"""
title: NOMAD project
author: NOMAD
version: 0.2.0
description: Adds the project picked in NOMAD's Project dropdown as context for any model you pick, and honours model overrides (/deep, !local, …). NOMAD Auto does this itself.
required_open_webui_version: 0.12.3
"""
# The repo copy is the source of truth (docs/prd/owui-sessions.md §6.5, #1136):
# scripts/owui-sync-pipe.py installs it as a global filter. Don't edit it in the UI.
#
# The Project dropdown (owui/nomad_ui.js) stores the pick in the chat variable `nomad_project`.
# For a model other than NOMAD's own, this filter asks the engine (POST /owui/context) for that
# project's context and prepends it as a system message. The engine binds the chat to its
# project on first use, so a later request naming another project still gets the bound one, and
# the filter writes that back to the chat variable. General and + New project add nothing. If the
# engine can't answer, the message goes out without context and a status line says so.
#
# Overrides (#1199). A message starting with `/deep`, `!local`, `!codex`, … is decided by the
# engine with the same rules as NOMAD Auto (owui_router.override): the reply to /owui/context
# carries `override` {model, text, refused, note}. The inlet switches body["model"] to that alias
# (Open WebUI sends the request to whatever the model holds after the inlet filters; the LiteLLM
# models keep their alias as id), and replaces the message with the prefix stripped (a bare
# `!deep` re-asks the previous question). A cloud override in a local-only chat is refused: the
# model becomes `private`. The stored reply would show the picked model, so the outlet appends
# who answered.
import re

import aiohttp
from pydantic import BaseModel, Field

ENGINE = "nomad-v2-engine"
VAR = "nomad_project"
NO_CONTEXT = ("", "general")
LOOKS_LIKE_OVERRIDE = re.compile(r"^\s*[!/][a-z]")      # cheap check before asking the engine


def user_text(content) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text") or "" for p in content or []
                      if isinstance(p, dict) and p.get("type") == "text")


def with_text(content, text: str):
    """`content` (a string or a list of parts) with its text replaced by `text`."""
    if isinstance(content, str) or not content:
        return text
    parts, done = [], False
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            if not done:
                parts.append({**p, "text": text})
                done = True
            continue
        parts.append(p)
    return parts if done else [{"type": "text", "text": text}] + parts


async def save_project(chat_id: str, variables: dict, entry: str):
    """Write the chat's bound entry into its chat variables (Open WebUI's own store)."""
    from open_webui.models.chats import Chats
    await Chats.update_chat_variables_by_id(chat_id, {**variables, VAR: entry})


class Filter:
    class Valves(BaseModel):
        priority: int = Field(default=0, description="Filter order; lower runs first.")
        ENGINE_URL: str = Field(default=f"http://{ENGINE}:8099",
                                description="The NOMAD engine, inside the compose network.")
        NOMAD_TOKEN: str = Field(default="", description="Shared with the engine (owui-token).",
                                 json_schema_extra={"input": {"type": "password"}})
        TIMEOUT_S: int = Field(default=15, description="Longest the context fetch may take.")

    def __init__(self):
        self.valves = self.Valves()
        self._answered = {}             # chat_id -> footer line for the reply being generated

    async def _status(self, emit, text: str):
        if emit:
            await emit({"type": "status", "data": {"description": text, "done": True}})

    async def inlet(self, body: dict, __metadata__: dict | None = None, __model__: dict | None = None,
                    __event_emitter__=None) -> dict:
        meta = __metadata__ or {}
        model = str((__model__ or {}).get("id") or body.get("model") or "")
        variables = meta.get("chat_variables") or {}
        entry = str(variables.get(VAR) or "")
        chat_id = str(meta.get("chat_id") or "")
        msgs = list(body.get("messages") or [])
        last = next((i for i in range(len(msgs) - 1, -1, -1) if msgs[i].get("role") == "user"), None)
        asks = last is not None and bool(LOOKS_LIKE_OVERRIDE.match(user_text(msgs[last].get("content"))))
        if model.startswith("nomad.") or meta.get("task") or (entry in NO_CONTEXT and not asks):
            return body
        try:
            timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT_S)
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(self.valves.ENGINE_URL.rstrip("/") + "/owui/context",
                                  json={"chat_id": chat_id, "entry": entry or "general", "model": model,
                                        "messages": [{"role": "user", "content": m.get("content")}
                                                     for m in msgs if m.get("role") == "user"]
                                        if asks else []},
                                  headers={"X-Nomad-Token": self.valves.NOMAD_TOKEN}) as r:
                    r.raise_for_status()
                    ctx = await r.json()
        except Exception as e:
            await self._status(__event_emitter__, f"⚠ NOMAD project context unavailable "
                                                  f"({e.__class__.__name__}); answering without it")
            return body
        bound = ctx.get("entry")
        if chat_id and bound and bound != entry:
            try:
                await save_project(chat_id, variables, bound)
            except Exception:
                pass
        system = ctx.get("system") or ""
        ov = ctx.get("override") if asks else None
        if ov and ov.get("model"):
            body, msgs = self._apply_override(body, msgs, last, ov, chat_id)
        if system:
            if msgs and msgs[0].get("role") == "system" and isinstance(msgs[0].get("content"), str):
                msgs[0] = {**msgs[0], "content": system + "\n\n" + msgs[0]["content"]}
            else:
                msgs.insert(0, {"role": "system", "content": system})
            body = {**body, "messages": msgs}
        note = ctx.get("note") or ""
        if ov and ov.get("note"):
            note = " · ".join(t for t in (note, ov["note"]) if t)
        if system or note:
            await self._status(__event_emitter__, " · ".join(
                t for t in (f"project: {ctx.get('name') or bound}" if system else "", note) if t))
        return body

    def _apply_override(self, body: dict, msgs: list, last: int, ov: dict, chat_id: str):
        """Switch the model and strip the prefix from the last user message. With nothing to
        answer (a bare `!deep` and no earlier question) the model stays as it was."""
        text = (ov.get("text") or "").strip()
        if not text:
            return body, msgs
        msgs[last] = {**msgs[last], "content": with_text(msgs[last].get("content"), text)}
        body = {**body, "model": ov["model"], "messages": msgs}
        line = f"override: {ov['model']} ({ov.get('cmd') or 'override'})"
        if ov.get("refused") and ov.get("note"):
            line += f" · {ov['note']}"
        if chat_id:
            self._answered[chat_id] = line
        return body, msgs

    async def outlet(self, body: dict, __metadata__: dict | None = None, __model__: dict | None = None) -> dict:
        """Say who answered when the inlet switched the model: the stored reply carries the
        name of the model the operator picked, not the one that answered."""
        chat_id = str((__metadata__ or {}).get("chat_id") or body.get("chat_id") or "")
        line = self._answered.pop(chat_id, "") if chat_id else ""
        msgs = list(body.get("messages") or [])
        if not line or not msgs or msgs[-1].get("role") != "assistant":
            return body
        content = msgs[-1].get("content")
        if not isinstance(content, str) or line in content:
            return body
        msgs[-1] = {**msgs[-1], "content": f"{content}\n\n---\n{line}"}
        return {**body, "messages": msgs}
