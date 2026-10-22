"""
title: NOMAD
author: NOMAD
version: 0.10.0
description: NOMAD Auto, the local router, for the project picked in NOMAD's Project dropdown. The engine routes each turn to a model and says which one served it.
required_open_webui_version: 0.12.3
"""
# The repo copy is the source of truth (docs/prd/owui-sessions.md §6.1, #1118):
# scripts/owui-sync-pipe.py installs it through Open WebUI's API. Don't edit it in the UI.
#
# The engine does the work (v2/owui_chat.py). This pipe only lists the entries from
# GET /owui/models and forwards each turn to POST /owui/chat, turning the engine's JSON lines into
# Open WebUI status events and answer text. Traffic stays on the compose network.
#
# Since §6.5 the operator picks the project in a Project dropdown (owui/nomad_ui.js), which
# stores it in the chat variable `nomad_project`. `nomad.auto` is the one model the operator sees;
# it answers for that project. The other entries stay listed, for M1 chats and as the dropdown's
# source, and scripts/owui-sync-pipe.py hides them from the model dropdown. The engine binds a
# chat to its project; when its answer names another entry (a + New project chat renamed in M3),
# the pipe writes that back into the chat variable so the dropdown follows.
import asyncio
import base64
import csv
import html
import io
import importlib.util
import json
import logging
import re
import uuid
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from pydantic import BaseModel, Field

MODELS_TTL_S = 60
ENGINE = "nomad-v2-engine"
AUTO = {"id": "auto", "name": "NOMAD Auto"}
LOCAL = {"id": "local", "name": "NOMAD Local"}
GENERAL = {"id": "general", "name": "General"}
# A request to do something to files, repos or the machine (not just to answer): Auto hands it to Claude Code.
WORK = re.compile(
    r"(?i)(^\s*!(bg|deep)\b|\b(edit|modify|refactor|rename|implement|fix|patch|commit|merge|rebase|"
    r"install|uninstall|deploy|restart|build|compile|debug|delete|remove|create|write|add|update|"
    r"run|execute|launch|spawn|scaffold|migrate|revert)\b.{0,80}"
    r"\b(file|files|code|script|repo|repository|branch|test|tests|function|class|module|service|"
    r"command|shell|bash|terminal|config|docs?|readme|task|subagent|agent|container|folder|directory|"
    r"git|npm|pip|docker|pytest)\b)")
# A request to make a picture or a document (#1399): only the agent can hand the file back to the chat.
# Questions about such things ("what is a pdf", "how do I make a chart") stay with the engine.
MAKE = re.compile(
    r"(?i)^(?!\s*(what|whats|why|how|who|which|where|when|explain|describe|tell me|is|are|does|do)\b)"
    r".*?\b(create|generate|make|draw|produce|render|write|build|design|export|paint|sketch)\b.{0,80}?"
    r"\b(images?|pictures?|photos?|pdfs?|docx|xlsx|charts?|diagrams?)\b")


def is_work(text: str) -> bool:
    return bool(WORK.search(text) or MAKE.search(text))


AGENT = {"id": "agent", "name": "NOMAD Agent (Claude Code)"}
VAR = "nomad_project"
RPC = "nomad_rpc"                       # the Dispatch panel's call (#1441), never a chat turn
RPC_OPS = ("items", "status", "dispatch")
log = logging.getLogger("nomad_pipe")

# --- inline ask cards (#1362, docs/prd/project-types-and-inline-asks.md §6.1) -------------------
# An Open WebUI function is one file, so owui/ask_card.py cannot be imported there.
# scripts/owui-sync-pipe.py replaces the marker line below with ask_card.py's source (cut before its
# samples), which defines build_card. In the repo, and in tests, the sibling file is loaded instead.
build_card = None
# @@ASK_CARD@@
if build_card is None:
    try:
        _spec = importlib.util.spec_from_file_location("ask_card", Path(__file__).with_name("ask_card.py"))
        _mod = importlib.util.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        build_card = _mod.build_card
    except Exception:                      # no sibling file: asks fall back to the typed reply
        build_card = None
ANSWER_MSG = re.compile(r"^\s*answer\s+(ask-[0-9a-f]+)\b", re.I)


async def save_project(chat_id: str, variables: dict, entry: str):
    """Write the chat's bound entry into its chat variables (Open WebUI's own store)."""
    from open_webui.models.chats import Chats
    await Chats.update_chat_variables_by_id(chat_id, {**variables, VAR: entry})


# --- files in the chat (#1332, docs/prd/owui-inline-files.md) -----------------------------------
# The bridge sends {"type":"file","name","mime","size","b64"} for a file the agent wrote. The pipe
# uploads it with Open WebUI's own upload handler as the chat's user (same origin, same login) and
# shows it by type. SVG only ever goes through an <img>, never an iframe or inline markup.
CSV_PREVIEW_ROWS = 10
CSV_PREVIEW_COLS = 8


UPLOAD_MAX_BYTES = 20 * 1024 * 1024        # same cap as the bridge inbox
LINE_MAX_BYTES = 32 * 1024 * 1024          # a file event is one line of base64 (bridge cap 8 MB)


async def _ndjson(content):
    """Yield the JSON objects of an NDJSON body. `async for raw in r.content` stops at aiohttp's
    512 KB line limit, and a file event line is the whole file in base64 (live test 2026-10-16)."""
    buf = bytearray()
    async for chunk in content.iter_any():
        start = len(buf)
        buf += chunk
        while (i := buf.find(b"\n", start)) >= 0:
            line, buf, start = bytes(buf[:i]), buf[i + 1:], 0
            if (ev := _json_line(line)) is not None:
                yield ev
        if len(buf) > LINE_MAX_BYTES:
            raise ValueError(f"an event line is over {LINE_MAX_BYTES} bytes")
    if (ev := _json_line(bytes(buf))) is not None:
        yield ev


def _json_line(raw: bytes):
    line = raw.decode(errors="replace").strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return None


def _safe_name(name: str) -> str:
    return re.sub(r"[\[\]()<>`|:/\\\r\n]", "_", name or "file")[:120] or "file"


def _csv_table(data: bytes) -> str:
    try:
        rows = list(csv.reader(io.StringIO(data[:65536].decode("utf-8", "replace"))))
    except csv.Error:
        return ""
    rows = [r[:CSV_PREVIEW_COLS] for r in rows[:CSV_PREVIEW_ROWS + 1] if r]
    if not rows:
        return ""
    width = max(len(r) for r in rows)

    def cell(c) -> str:
        return html.escape(str(c), quote=False).replace("|", "\\|").replace("\n", " ")[:60]
    lines = ["| " + " | ".join(cell(c) for c in rows[0] + [""] * (width - len(rows[0]))) + " |",
             "|" + "---|" * width]
    for r in rows[1:]:
        lines.append("| " + " | ".join(cell(c) for c in r + [""] * (width - len(r))) + " |")
    return "\n".join(lines)


EMBED_MAX_BYTES = 3 * 1024 * 1024      # bytes carried inside an embed; the chat stores them
PDFJS = "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174"
_SIZE_JS = ("function sendH(){parent.postMessage({type:'iframe:height',height:Math.ceil("
            "document.documentElement.getBoundingClientRect().height)+4},'*')}"
            "if(window.ResizeObserver)new ResizeObserver(sendH).observe(document.body);sendH();")


def _media_embed(tag: str, src: str) -> str:
    return (f'<!doctype html><html><body style="margin:0"><{tag} controls preload="metadata" '
            f'src="{src}" style="width:100%;display:block"></{tag}><script>{_SIZE_JS}</script>'
            f'</body></html>')


def _pdf_embed(b64: str, pages: int = 10) -> str:
    """PDF.js draws the pages to canvases: a browser's own viewer is blocked in a sandboxed frame
    and phones have none. At most `pages` pages; the link has the rest."""
    return ('<!doctype html><html><body style="margin:0;background:#525659">'
            '<div id="p" style="padding:6px"></div><div id="m" style="color:#ddd;font:13px sans-serif;'
            'padding:4px 8px"></div>'
            f'<script src="{PDFJS}/pdf.min.js"></script><script>'
            f'var B="{b64}",N={pages};{_SIZE_JS}'
            "var m=document.getElementById('m'),p=document.getElementById('p');"
            "try{pdfjsLib.GlobalWorkerOptions.workerSrc='" + PDFJS + "/pdf.worker.min.js';"
            "var raw=atob(B),u=new Uint8Array(raw.length);for(var i=0;i<raw.length;i++)u[i]=raw.charCodeAt(i);"
            "pdfjsLib.getDocument({data:u}).promise.then(function(d){var n=Math.min(d.numPages,N);"
            "if(d.numPages>N)m.textContent='First '+N+' of '+d.numPages+' pages. Download for the rest.';"
            "var w=p.clientWidth||600,ch=Promise.resolve();for(var k=1;k<=n;k++)(function(k){ch=ch.then("
            "function(){return d.getPage(k)}).then(function(pg){var v=pg.getViewport({scale:1}),"
            "s=w/v.width,vp=pg.getViewport({scale:s}),c=document.createElement('canvas');"
            "c.width=vp.width;c.height=vp.height;c.style.cssText='display:block;margin:0 auto 6px;"
            "background:#fff;max-width:100%';p.appendChild(c);return pg.render({canvasContext:"
            "c.getContext('2d'),viewport:vp}).promise}).then(sendH)})(k);"
            "}).catch(function(e){m.textContent='Could not draw the PDF here ('+e.message+'). Download it.';sendH()})"
            "}catch(e){m.textContent='PDF viewer did not load. Download it.';sendH()}"
            "</script></body></html>")


def render_file(name: str, mime: str, size: int, url: str, data: bytes = b"",
                download_only: bool = False) -> tuple[str, str]:
    """(markdown for the answer, HTML for an embed or ""). The link is always there. With
    `download_only` it is the only thing: no inline image, embed or table."""
    name = _safe_name(name)
    kb = f"{max(1, size // 1024)} KB" if size else ""
    link = f"📎 [{name}]({url})" + (f" · {kb}" if kb else "")
    if download_only:
        return f"\n{link}\n", ""
    if mime.startswith("image/"):
        return f"\n![{name}]({url})\n\n{link}\n", ""
    # An embed is an opaque-origin srcdoc iframe: a request from it to /api/v1/files carries no
    # login and gets 401 (live test 2026-10-16). So the bytes travel inside the embed instead,
    # up to EMBED_MAX_BYTES; a bigger file keeps the link only.
    if mime == "application/pdf" or mime.startswith(("audio/", "video/")):
        if not data or len(data) > EMBED_MAX_BYTES:
            what = "view" if mime == "application/pdf" else "play"
            note = f" · too big to {what} here, download it" if data else ""
            return f"\n{link}{note}\n", ""
        b64 = base64.b64encode(data).decode()
        if mime == "application/pdf":
            return f"\n{link}\n", _pdf_embed(b64)
        tag = "audio" if mime.startswith("audio/") else "video"
        return f"\n{link}\n", _media_embed(tag, f"data:{html.escape(mime, quote=True)};base64,{b64}")
    if mime == "text/csv":
        table = _csv_table(data)
        return (f"\n{table}\n\n{link}\n" if table else f"\n{link}\n"), ""
    return f"\n{link}\n", ""


UPLOAD_MARKER = "nomad_pipe_upload"


async def upload_file(request, user, name: str, mime: str, data: bytes) -> str:
    """Upload through Open WebUI's own handler, as the chat's user. Returns the file id. Only
    importable inside Open WebUI, so tests replace Pipe._upload instead."""
    from fastapi import UploadFile
    from open_webui.models.users import Users
    from open_webui.routers.files import upload_file_handler
    owner = await Users.get_user_by_id(user["id"])
    up = UploadFile(file=io.BytesIO(data), filename=name, headers={"content-type": mime})
    # The marker lands in the file's meta.data; scripts/owui-file-retention.py deletes only files
    # that carry it (#1356). Never rename or drop it without changing the script.
    res = await upload_file_handler(request, file=up, metadata={UPLOAD_MARKER: True}, process=False,
                                    process_in_background=False, user=owner)
    return res["id"] if isinstance(res, dict) else res.id


def typed_answer(ask: dict, text: str) -> dict | None:
    """A plain reply as the payload of `answer <ask_id> <json>` for `ask` (a /agent/pending row),
    or None when it can't be read as an answer (the bridge then says what it is waiting for)."""
    t = text.strip()
    low = t.lower()
    word = low.split(None, 1)[0] if low else ""
    denies = ("deny", "reject", "cancel") + (() if ask.get("kind") == "question" else ("no", "n"))
    if word.rstrip(":,.!") in denies:
        note = t[len(word):].strip(" :,-")
        return {"decision": "deny", **({"note": note} if note else {})}
    yes = low.rstrip(".! ") in ("approve", "yes", "y", "ok", "allow")
    if ask.get("kind") != "question":
        return {"decision": "approve"} if yes else None
    qs = ask.get("questions") or []
    if not qs or yes:
        return None
    parts = [p.strip() for p in t.split(";")] if len(qs) > 1 else [t]
    answers = []
    for i, q in enumerate(qs):
        raw = parts[i] if i < len(parts) else parts[-1]
        labels = [str(o.get("label") or "") for o in q.get("options") or []]
        multi = bool(q.get("multiSelect"))
        picked = []
        for tok in ([x.strip() for x in re.split(r",|\band\b", raw)] if multi and labels else [raw]):
            if not tok:
                continue
            if tok.isdigit() and 1 <= int(tok) <= len(labels):
                picked.append(labels[int(tok) - 1])
            else:
                picked.append(next((l for l in labels if l.lower() == tok.lower()), tok))
        if not picked:
            return None
        answers.append({"question": str(q.get("question") or f"q{i + 1}"),
                        "answer": picked if multi else picked[0]})
    return {"answers": answers}


class Pipe:
    class Valves(BaseModel):
        ENGINE_URL: str = Field(default=f"http://{ENGINE}:8099",
                                description="The NOMAD engine, inside the compose network.")
        NOMAD_TOKEN: str = Field(default="", description="Shared with the engine (owui-token).",
                                 json_schema_extra={"input": {"type": "password"}})
        TIMEOUT_S: int = Field(default=600, description="Longest a turn may take.")
        AGENT_ENABLED: bool = Field(default=False, description="Feature flag: list NOMAD Agent, "
                                    "the Claude Code session per chat (agent-bridge/).")
        AGENT_AUTO: bool = Field(default=True, description="With AGENT_ENABLED, NOMAD Auto hands "
                                 "work requests (edit, run, commit, ...) and chats already in an "
                                 "agent session to Claude Code. `!chat` forces one turn to the "
                                 "engine, `!agent` forces one turn to the agent.")
        AGENT_URL: str = Field(default="http://172.17.0.1:8093",
                               description="nomad-agent-bridge, on the docker0 gateway.")
        AGENT_TIMEOUT_S: int = Field(default=3600, description="Longest an agent turn may take.")
        FILES_ENABLED: bool = Field(default=False, description="Show files the agent wrote "
                                    "(images, PDF, csv, audio, video, office) in the chat "
                                    "(docs/prd/owui-inline-files.md). Off: behaviour as before.")
        ASK_POLL_S: float = Field(default=3.0, description="How often a shown ask card's "
                                  "ask is checked: answered in the cockpit, the run's rest is streamed "
                                  "into the same message (#1406).")
        ASK_WATCH_S: int = Field(default=7200, description="How long a shown card is watched.")

    def __init__(self):
        self.valves = self.Valves()
        self._models = (0.0, [])
        self._watches: dict = {}           # ask id -> the task watching its card (#1406)

    async def _upload(self, request, user, name: str, mime: str, data: bytes) -> str:
        return await upload_file(request, user, name, mime, data)

    async def _read_uploads(self, files, user) -> list:
        """The files the user attached to this chat, as bridge attachments (#1333). Reads OWUI's own
        store in-process, only files the chat's user owns; anything unreadable is left out."""
        out = []
        if not files or not user or not user.get("id"):
            return out
        try:
            from open_webui.models.files import Files
            from open_webui.storage.provider import Storage
        except Exception:
            return out
        for f in files[:10]:
            fid = str((f or {}).get("id") or ((f or {}).get("file") or {}).get("id") or "")
            if not fid:
                continue
            try:
                rec = await Files.get_file_by_id_and_user_id(fid, user["id"])
                if rec is None or not rec.path:
                    continue
                data = await asyncio.to_thread(lambda p=rec.path: open(Storage.get_file(p), "rb").read())
                if 0 < len(data) <= UPLOAD_MAX_BYTES:
                    out.append({"id": fid, "name": rec.filename or "file",
                                "b64": base64.b64encode(data).decode()})
            except Exception:
                continue
        return out

    async def _show_file(self, e: dict, request, user, embeds: list) -> str:
        """Markdown for one file event; an embed goes onto `embeds`. A failure says so and where
        the file is, it never drops the file silently."""
        name = _safe_name(str(e.get("name") or "file"))
        log.info("file event: name=%s mime=%s size=%s path=%s source=%s reason=%s too_large=%s "
                 "download_only=%s has_user=%s has_request=%s", name, e.get("mime"), e.get("size"),
                 e.get("path"), e.get("source"), e.get("reason"), bool(e.get("too_large")),
                 bool(e.get("download_only")), bool(user), request is not None)
        if e.get("too_large"):                  # the bridge sent no bytes: say where the file is
            mb = (int(e.get("size") or 0) / (1024 * 1024))
            log.warning("file %s is too large to show (%.1f MB)", name, mb)
            return f"\n⚠ `{name}` ({mb:.1f} MB) is too large to show. It is at `{e.get('path') or name}`.\n"
        try:
            data = base64.b64decode(e.get("b64") or "", validate=True)
            if not data or not user or request is None:
                raise ValueError("no chat user or request to upload as")
            fid = await self._upload(request, user, name, str(e.get("mime") or "application/octet-stream"), data)
        except Exception as ex:
            log.warning("file %s not shown in the chat: %s: %s", name, ex.__class__.__name__,
                        str(ex)[:200], exc_info=True)
            return (f"\n⚠ Couldn't show `{name}` in the chat ({ex.__class__.__name__}: {str(ex)[:120]}). "
                    f"It is at `{e.get('path') or name}`.\n")
        md, emb = render_file(name, str(e.get("mime") or ""), int(e.get("size") or len(data)),
                              f"/api/v1/files/{fid}/content", data,
                              download_only=bool(e.get("download_only")))
        log.info("file %s uploaded as %s, shown as %s", name, fid, "embed" if emb else "markdown")
        if emb:
            embeds.append(emb)
        return md

    def _headers(self) -> dict:
        return {"X-Nomad-Token": self.valves.NOMAD_TOKEN}

    def _down(self, why: str) -> str:
        return (f"⚠ The NOMAD engine ({ENGINE}, {self.valves.ENGINE_URL}) can't answer: {why}. "
                "Pick a plain model from the list (e.g. balanced or private) until it's back.")

    async def pipes(self) -> list:
        """NOMAD Auto, then the engine's entries, cached for a minute. If the engine is down: the
        last list that worked, or General alone, so a chat can still say what's wrong."""
        return [AUTO, LOCAL] + await self._entries()

    async def _entries(self) -> list:
        at, rows = self._models
        if rows and time.monotonic() - at < MODELS_TTL_S:
            return rows
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                async with s.get(self.valves.ENGINE_URL.rstrip("/") + "/owui/models",
                                 headers=self._headers()) as r:
                    r.raise_for_status()
                    data = await r.json()
            rows = [{"id": m["id"], "name": m["name"]} for m in data.get("models") or []]
            self._models = (time.monotonic(), rows)
        except Exception:
            rows = rows or [GENERAL]
        return rows

    async def _rpc(self, rpc) -> str:
        """The Dispatch panel's call (owui/nomad_ui.js, #1441), sent as a completion with no chat
        so Open WebUI checks the login and saves nothing. The answer is the engine's JSON, as text."""
        if not isinstance(rpc, dict) or rpc.get("op") not in RPC_OPS:
            return json.dumps({"ok": False, "error": f"op must be one of {', '.join(RPC_OPS)}"})
        body = {k: rpc[k] for k in ("op", "entry", "id", "ids") if k in rpc}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60, sock_connect=10)) as s:
                async with s.post(self.valves.ENGINE_URL.rstrip("/") + "/owui/dispatch", json=body,
                                  headers=self._headers()) as r:
                    if r.status != 200:
                        return json.dumps({"ok": False, "error": self._down(f"HTTP {r.status}")})
                    return json.dumps(await r.json())
        except Exception as e:
            return json.dumps({"ok": False, "error": self._down(f"{e.__class__.__name__}: {str(e)[:200]}")})

    async def _events(self, payload: dict):
        url = self.valves.ENGINE_URL.rstrip("/") + "/owui/chat"
        timeout = aiohttp.ClientTimeout(total=self.valves.TIMEOUT_S, sock_connect=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(url, json=payload, headers=self._headers()) as r:
                    if r.status in (401, 503):
                        yield {"type": "delta", "text": self._down(
                            f"HTTP {r.status}, the pipe's NOMAD_TOKEN valve doesn't match the "
                            "engine's owui-token")}
                        return
                    if r.status != 200:
                        yield {"type": "delta", "text": self._down(f"HTTP {r.status}")}
                        return
                    async for ev in _ndjson(r.content):
                        yield ev
        except aiohttp.ClientConnectorError as e:
            yield {"type": "delta", "text": self._down(f"not reachable ({e.__class__.__name__})")}
        except Exception as e:
            yield {"type": "delta", "text": self._down(f"{e.__class__.__name__}: {str(e)[:200]}")}

    async def pipe(self, body: dict, __metadata__: dict | None = None, __event_emitter__=None,
                   __task__=None, __chat_id__=None,
                   __request__=None, __user__=None):
        meta = __metadata__ or {}
        if RPC in body and not __task__ and not (__chat_id__ or meta.get("chat_id")):
            return await self._rpc(body.get(RPC))
        variables = meta.get("chat_variables") or {}
        entry = str(body.get("model") or "").split(".", 1)[-1]
        if entry == AGENT["id"] and self.valves.AGENT_ENABLED and not __task__:
            gen = self._agent(body, variables, __chat_id__ or meta.get("chat_id") or "",
                              __event_emitter__, request=__request__, user=__user__)
            if body.get("stream", False):
                return gen
            return "".join([t async for t in gen])
        local = entry == LOCAL["id"]
        loaded = ""
        if local:
            # NOMAD Local (#1436): the engine answers on a local model the router picks (a light
            # one for talk), and only work goes to the agent, on the local backend (Qwen3.8 in the
            # Claude Code harness: strong but slow). While that model is on the GPU, chat stays on
            # it too, so the two never swap VRAM back and forth.
            entry = str(variables.get(VAR) or GENERAL["id"])
            cid = __chat_id__ or meta.get("chat_id") or ""
            state: dict = {}
            if (self.valves.AGENT_ENABLED and not __task__ and entry != "new-project"
                    and await self._route_to_agent(body, entry, cid, sticky=False, state=state)):
                gen = self._agent(body, variables, cid, __event_emitter__, backend="local",
                                  request=__request__, user=__user__)
                if body.get("stream", False):
                    return gen
                return "".join([t async for t in gen])
            loaded = "code" if state.get("local_up") else ""
        if entry == AGENT["id"]:                     # a task (title, tags) or the flag is off
            entry = GENERAL["id"]
        if entry == AUTO["id"]:
            entry = str(variables.get(VAR) or GENERAL["id"])
            cid = __chat_id__ or meta.get("chat_id") or ""
            if (self.valves.AGENT_ENABLED and self.valves.AGENT_AUTO and not __task__
                    and entry != "new-project" and await self._route_to_agent(body, entry, cid)):
                gen = self._agent(body, variables, cid, __event_emitter__,
                                  request=__request__, user=__user__)
                if body.get("stream", False):
                    return gen
                return "".join([t async for t in gen])
        payload = {"entry": entry,
                   "chat_id": __chat_id__ or meta.get("chat_id") or "",
                   "messages": body.get("messages") or [], "files": body.get("files") or [],
                   "task": __task__}
        if local:
            payload.update(local_only=True, local_loaded=loaded)
        if not body.get("stream", False):          # tasks and API callers: one string
            out = []
            async for e in self._events(payload):
                if e.get("type") == "delta":
                    out.append(e.get("text") or "")
                elif e.get("type") == "done":
                    await self._follow(payload, variables, e)
            return "".join(out)
        return self._stream(payload, variables, __event_emitter__)

    async def _follow(self, payload: dict, variables: dict, done: dict):
        """Keep the chat variable on the engine's bound entry. Tasks don't bind."""
        bound = done.get("entry")
        if payload["task"] or not payload["chat_id"] or not bound or variables.get(VAR) == bound:
            return
        try:
            await save_project(payload["chat_id"], variables, bound)
        except Exception:
            pass                        # the dropdown shows the old label; the engine still binds

    async def _stream(self, payload: dict, variables: dict, emit):
        async for e in self._events(payload):
            kind = e.get("type")
            if kind == "delta":
                yield e.get("text") or ""
            elif kind == "status" and emit:
                await emit({"type": "status", "data": {"description": e.get("text"), "done": False}})
            elif kind == "done":
                await self._follow(payload, variables, e)
            if kind == "done" and emit:
                served = e.get("served") or e.get("routed")
                await emit({"type": "status",
                            "data": {"description": f"served by {served}" if served else "done",
                                     "done": True}})

    # ── NOMAD Agent: a Claude Code session per chat (agent-bridge/, docs/prd/owui-agent-bridge.md) ──
    async def _agent_lines(self, method: str, path: str, payload: dict | None = None):
        url = self.valves.AGENT_URL.rstrip("/") + path
        timeout = aiohttp.ClientTimeout(total=self.valves.AGENT_TIMEOUT_S, sock_connect=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.request(method, url, json=payload, headers=self._headers()) as r:
                    if r.status != 200:
                        yield {"type": "delta", "text": f"⚠ The agent bridge answered HTTP {r.status} "
                               f"({self.valves.AGENT_URL}). Nothing was run."}
                        return
                    async for ev in _ndjson(r.content):
                        yield ev
        except aiohttp.ClientConnectorError as e:
            yield {"type": "delta", "text": f"⚠ The agent bridge isn't reachable "
                   f"({e.__class__.__name__}): nomad-agent-bridge is down. Nothing was run."}

    async def _post(self, path: str, payload: dict) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as s:
                async with s.post(self.valves.AGENT_URL.rstrip("/") + path, json=payload,
                                  headers=self._headers()) as r:
                    return await r.json()
        except Exception as e:
            return {"ok": False, "error": f"{e.__class__.__name__}: {str(e)[:120]}"}

    async def _route_to_agent(self, body: dict, project: str, chat_id: str, sticky: bool = True,
                              state: dict | None = None) -> bool:
        """NOMAD Auto's choice for one turn. `!chat` / `!agent` force it (and are stripped); else
        a chat already in an agent session or with a gate waiting stays there, and otherwise a
        work request goes to Claude Code. Anything the bridge can't serve (local_only project,
        bridge down) stays on the engine path. NOMAD Local passes sticky=False: a chat that ran
        the agent once still talks on the light model. `state` receives the bridge's answer."""
        msgs = body.get("messages") or []
        last = next((m for m in reversed(msgs) if m.get("role") == "user"), None)
        text = last.get("content") if last else ""
        if not isinstance(text, str):
            return False
        m = re.match(r"\s*!(chat|agent)\b[ \t]*", text)
        forced = m.group(1) if m else ""
        if forced:
            last["content"] = text[m.end():]
        if forced == "chat":
            return False
        if not forced and re.match(r"\s*!(claude|codex|local)\b", text):
            forced = "agent"                 # naming a backend means the agent; the bridge strips it
        if not chat_id:                  # an API call: no session to look up, judge the text
            return forced == "agent" or is_work(text)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as s:
                async with s.get(self.valves.AGENT_URL.rstrip("/") + "/agent/state",
                                 params={"chat_id": chat_id, "project": project},
                                 headers=self._headers()) as r:
                    if r.status != 200:
                        return False
                    st = await r.json()
        except Exception:
            return False
        if state is not None:
            state.update(st)
        if not st.get("eligible"):
            return False
        if (forced == "agent" or (sticky and st.get("session")) or st.get("pending")
                or ANSWER_MSG.match(text)):
            return True
        return is_work(text)

    async def _agent(self, body: dict, variables: dict, chat_id: str, emit, backend: str = "",
                     request=None, user=None):
        msgs = body.get("messages") or []
        last = next((m for m in reversed(msgs) if m.get("role") == "user"), {})
        content = last.get("content")
        message = content if isinstance(content, str) else "\n".join(
            p.get("text") or "" for p in content or [] if isinstance(p, dict) and p.get("type") == "text")
        project = str(variables.get(VAR) or "general")
        waiting = False
        embeds: list = []
        attachments = (await self._read_uploads(body.get("files"), user)
                       if self.valves.FILES_ENABLED else [])
        # a call through the API has no chat: give it a one-off id (no resume across calls)
        chat_id = chat_id or "api-" + uuid.uuid4().hex[:12]
        if build_card is not None:
            message, reply = await self._inline_reply(message, chat_id, embeds)
            if reply is not None:
                if embeds and emit:
                    await emit({"type": "embeds", "data": {"embeds": embeds}})
                yield reply
                return
        try:
            async for e in self._agent_lines("POST", "/agent/chat", {
                    "chat_id": chat_id, "project": project, "message": message,
                    **({"attachments": attachments} if attachments else {}),
                    **({"backend": backend} if backend else {})}):
                kind = e.get("type")
                if kind == "delta":
                    yield e.get("text") or ""
                elif kind == "status" and emit:
                    await emit({"type": "status", "data": {"description": e.get("text"), "done": False}})
                elif kind == "gate" and self._inline(e, emit):
                    waiting = True
                    yield await self._inline_gate(e, emit, embeds, chat_id, request, user)
                    return
                elif kind == "gate":                  # no card possible: answer by message
                    waiting = True
                    yield (f"\n\n⏸ **Waiting for you** (run `{e.get('run_id')}`): "
                           f"{e.get('tool')} `{e.get('summary')}`.\nSay `approve {e.get('run_id')}` "
                           f"or `deny {e.get('run_id')}`.\n")
                    return
                elif kind == "file" and self.valves.FILES_ENABLED:
                    yield await self._show_file(e, request, user, embeds)
                elif kind == "file":
                    log.info("file event for %s ignored: the FILES_ENABLED valve is off", e.get("name"))
                elif kind == "done" and emit:
                    if embeds:
                        await emit({"type": "embeds", "data": {"embeds": embeds}})
                    await emit({"type": "status", "data": {
                        "description": f"agent run {e.get('run_id', '')}: {e.get('status')}"
                        + (f" · {e['agent']}" if e.get("agent") else ""), "done": True}})
        except (asyncio.CancelledError, GeneratorExit):
            if not waiting:                       # the stop button: stop the real process too
                await self._post("/agent/stop", {"chat_id": chat_id})
            raise

    # ── inline asks (#1362) ──
    def _inline(self, g: dict, emit) -> bool:
        """True when this gate is shown as a card: the bridge gave it an ask_id, the card builder
        is there and there is an emitter. Otherwise the turn ends with how to answer by message
        (the popup path went in #1364)."""
        return bool(build_card is not None and emit and g.get("ask_id"))

    @staticmethod
    def _ask_of(g: dict) -> dict:
        """The card's ask from a gate event or a /agent/pending row."""
        q = g.get("kind") == "question"
        out = {"id": g.get("ask_id") or "", "kind": "questions" if q else "approve"}
        if q:
            out["questions"] = g.get("questions") or []
        else:
            out.update(tool=str(g.get("tool") or ""), summary=str(g.get("summary") or ""),
                       reason=str(g.get("reason") or ""))
        return out

    @staticmethod
    def _answered_of(ask: dict, payload: dict | None = None, row: dict | None = None) -> dict:
        """What the read-only card shows: the payload just sent, or the settled row's decision."""
        if payload is not None:
            return payload
        row = row or {}
        if row.get("state") == "answered":
            if ask["kind"] == "questions" and row.get("answers"):
                return {"answers": [{"question": k, "answer": v} for k, v in row["answers"].items()]}
            return {"decision": row.get("decision") or "deny"}
        return {"answers": []} if ask["kind"] == "questions" else {
            "decision": "deny", "note": str(row.get("state") or "closed")}

    async def _inline_gate(self, g: dict, emit, embeds: list, chat_id: str = "", request=None,
                           user=None) -> str:
        """Show the ask as a card (an embed; its buttons send `answer <ask_id> <json>`) and end this
        turn. The numbered text is the fallback when the card does not render: a typed reply works.
        A watcher follows the ask, for an answer that comes from the cockpit (#1406)."""
        ask = self._ask_of(g)
        log.info("inline ask %s (%s) for run %s", ask["id"], ask["kind"], g.get("run_id"))
        await emit({"type": "embeds", "data": {"embeds": embeds + [build_card(ask)], "replace": True}})
        self._watch(g, chat_id, emit, embeds, request, user)
        await emit({"type": "status", "data": {"description": f"waiting for your answer ({ask['id']})",
                                               "done": True}})
        if ask["kind"] == "questions":
            body = "\n\n".join(
                f"**{q.get('question')}**\n" + "\n".join(
                    f"{i}. {o.get('label')}" for i, o in enumerate(q.get("options") or [], 1))
                for q in ask["questions"])
            how = ("Answer on the card, or reply with the option number (`2,3` for several; `;` between "
                   "questions) or your own words.")
        else:
            body = f"{g.get('tool')} `{g.get('summary')}`" + (f"\n{g.get('reason')}" if g.get("reason") else "")
            how = "Answer on the card, or reply `approve` or `deny`."
        return f"\n\n⏸ **Waiting for you** (ask `{ask['id']}`)\n\n{body}\n\n{how}\n"

    def _watch(self, g: dict, chat_id: str, emit, embeds: list, request=None, user=None):
        aid = g.get("ask_id") or ""
        if not aid or not chat_id or chat_id.startswith("api-") or aid in self._watches:
            return
        t = asyncio.ensure_future(self._watch_ask(g, chat_id, emit, list(embeds), request, user))
        self._watches[aid] = t
        t.add_done_callback(lambda _t, a=aid: self._watches.pop(a, None))

    async def _watch_ask(self, g: dict, chat_id: str, emit, embeds: list, request, user):
        """The card ended the turn, so only a chat answer opens a turn that streams the rest of the
        run. An answer from anywhere else (the cockpit: `via` api) has nobody streaming: this
        streams it into the same message, through the turn's emitter, which OWUI also saves to the
        chat, so a reload shows it. A closed ask (expired, timeout, cancelled) is said the same way."""
        aid, rid = g["ask_id"], str(g.get("run_id") or "")
        ask = self._ask_of(g)
        deadline = time.monotonic() + self.valves.ASK_WATCH_S
        row = None
        try:
            while time.monotonic() < deadline:
                await asyncio.sleep(self.valves.ASK_POLL_S)
                rows = await self._asks(chat_id)
                row = next((r for r in rows or [] if r.get("ask_id") == aid), None)
                if row is not None and row.get("state") != "pending":
                    break
            if row is None or row.get("state") == "pending":
                return
            if row.get("state") == "answered" and row.get("via") != "api":
                return                                   # answered in this chat: that turn streams it
            shown = embeds + [build_card(ask, answered=self._answered_of(ask, row=row))]
            await emit({"type": "embeds", "data": {"embeds": shown, "replace": True}})
            if row.get("state") != "answered":
                why = {"expired": "expired when the bridge restarted, so the agent that asked is gone",
                       "timeout": "timed out and was denied",
                       "cancelled": "was cancelled when the run stopped"}.get(row.get("state"), "is closed")
                await emit({"type": "message", "data": {"content": f"\n\nThis question (`{aid}`) {why}.\n"}})
                await emit({"type": "status", "data": {"description": f"ask {aid}: {row.get('state')}",
                                                       "done": True}})
                return
            log.info("ask %s answered outside the chat: streaming run %s into it", aid, rid)
            await emit({"type": "message", "data": {"content": f"\n\n✔ Answered in the cockpit: "
                                                               f"{self._answer_line(row)}.\n\n"}})
            files: list = []
            path = "/agent/attach?" + urlencode({"run_id": rid, "cursor": "delivered"})
            async for e in self._agent_lines("GET", path):
                kind = e.get("type")
                if kind == "delta":
                    await emit({"type": "message", "data": {"content": e.get("text") or ""}})
                elif kind == "status":
                    await emit({"type": "status", "data": {"description": e.get("text"), "done": False}})
                elif kind == "file" and self.valves.FILES_ENABLED:
                    md = await self._show_file(e, request, user, files)
                    await emit({"type": "message", "data": {"content": md}})
                elif kind == "gate" and self._inline(e, emit):
                    text = await self._inline_gate(e, emit, shown + files, chat_id, request, user)
                    await emit({"type": "message", "data": {"content": text}})
                    return
                elif kind == "gate":
                    await emit({"type": "message", "data": {"content": (
                        f"\n\n⏸ **Waiting for you** (run `{e.get('run_id')}`): {e.get('tool')} "
                        f"`{e.get('summary')}`.\nSay `approve {e.get('run_id')}` or `deny {e.get('run_id')}`.\n")}})
                    return
                elif kind == "done":
                    if files:
                        await emit({"type": "embeds", "data": {"embeds": shown + files, "replace": True}})
                    await emit({"type": "status", "data": {
                        "description": f"agent run {e.get('run_id', rid)}: {e.get('status')}"
                        + (f" · {e['agent']}" if e.get("agent") else ""), "done": True}})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("watching ask %s failed", aid)

    @staticmethod
    def _answer_line(row: dict) -> str:
        if row.get("answers"):
            return "; ".join(str(v) for v in row["answers"].values())
        return "approved" if row.get("decision") == "approve" else "denied"

    async def _asks(self, chat_id: str, state: str = "") -> list | None:
        """The chat's asks from the bridge, oldest first; None when the bridge can't say."""
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
                async with s.get(self.valves.AGENT_URL.rstrip("/") + "/agent/pending",
                                 params={"chat_id": chat_id, **({"state": state} if state else {})},
                                 headers=self._headers()) as r:
                    if r.status != 200:
                        return None
                    rows = (await r.json()).get("pending") or []
        except Exception:
            return None
        return sorted(rows, key=lambda x: x.get("created") or 0)

    async def _inline_reply(self, message: str, chat_id: str, embeds: list) -> tuple:
        """(message to send, None) or (message, text to answer with and stop). First answer wins:
        an `answer <ask_id> ...` for an ask that is no longer pending is refused here, politely, and
        the bridge is never asked. A plain reply answers the oldest pending ask of the chat."""
        m = ANSWER_MSG.match(message)
        if m:
            rows = await self._asks(chat_id) or []
            row = next((r for r in rows if r.get("ask_id") == m.group(1).lower()), None)
            if row is None or row.get("state") == "pending":
                if row is not None:
                    self._remember(embeds, row, message)
                return message, None
            ask = self._ask_of(row)
            embeds.append(build_card(ask, answered=self._answered_of(ask, row=row)))
            why = {"answered": "was already answered, and the first answer stands",
                   "expired": "expired when the bridge restarted, so the agent that asked is gone",
                   "timeout": "timed out and was denied",
                   "cancelled": "was cancelled when the run stopped"}.get(row.get("state"), "is closed")
            log.info("answer to %s refused: state %s", row.get("ask_id"), row.get("state"))
            return message, (f"This question (`{row.get('ask_id')}`) {why}. Nothing was changed. "
                             "If it is still needed, ask the agent again.")
        if not message.strip() or re.match(r"\s*(!|status\b|stop\b|approve\s+\S|deny\s+\S)", message, re.I):
            return message, None
        rows = await self._asks(chat_id, "pending")
        if not rows:
            return message, None
        row = rows[0]
        payload = typed_answer(row, message)
        if payload is None:
            return message, None
        log.info("typed reply answers ask %s", row.get("ask_id"))
        self._remember(embeds, row, None, payload)
        return f"answer {row['ask_id']} {json.dumps(payload)}", None

    def _remember(self, embeds: list, row: dict, message: str | None, payload: dict | None = None):
        """Put the read-only card for an answer being sent onto the embeds."""
        if payload is None and message:
            try:
                payload = json.loads(message.strip().split(None, 2)[2])
            except Exception:
                return
        ask = self._ask_of(row)
        embeds.append(build_card(ask, answered=self._answered_of(ask, payload)))
