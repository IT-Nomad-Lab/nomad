"""Claude Code stream-json events → the bridge's events (and the chat text that shows them).

Bridge events, one JSON object per line on the wire:
  {"type":"delta","text":...}        markdown for the chat, in order
  {"type":"status","text":...}       a status line (not part of the answer)
  {"type":"gate",...}                a permission request or question tied to a run id
  {"type":"file","name","mime","size","b64","path"}  a file the run wrote and the chat can show (#1331)
      + optional "download_only" (show a link, no embed), "too_large" (b64 is "": bytes not sent,
      only path/size), "reason" (why), "source" (write|scan|path). See README "File events".
  {"type":"done","run_id",...}       last event

Honesty (requirement 8): the lines about tools are generated here from tool_use and tool_result
events only. A tool_use with no tool_result yet is shown as "running"; the footer counts results,
not intentions. Model prose is passed through as the model's own words.
"""
import base64
import json
import logging
import os
import re
import time

log = logging.getLogger("agent-bridge.files")

# Files the chat may show (#1331, docs/prd/owui-inline-files.md). Extension → mime; anything else
# is never sent. Only a file the run wrote with the Write tool, confirmed by its tool_result.
FILE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".pdf": "application/pdf",
    ".csv": "text/csv", ".md": "text/markdown", ".txt": "text/plain",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}
FILE_MAX_BYTES = int(os.environ.get("AGENT_BRIDGE_FILE_MAX_BYTES") or 8 * 1024 * 1024)
FILES_PER_RUN = 10
# Types that are not shown inline but are worth a download link (#1400). Anything in neither dict
# is dropped, and the drop is logged.
DOWNLOAD_TYPES = {
    ".zip": "application/zip", ".tar": "application/x-tar", ".gz": "application/gzip",
    ".tgz": "application/gzip", ".7z": "application/x-7z-compressed",
    ".json": "application/json", ".html": "text/html", ".xml": "application/xml",
    ".tsv": "text/tab-separated-values", ".epub": "application/epub+zip",
    ".odt": "application/vnd.oasis.opendocument.text", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".flac": "audio/flac", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".bmp": "image/bmp", ".tiff": "image/tiff", ".ico": "image/x-icon",
}
# Scanning the run's cwd (#1355): bounded, and never into these.
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "env", "__pycache__", "site-packages",
             "dist", "build", "target", ".tox", ".cache"}
SCAN_MAX_FILES = 3000
SCAN_MAX_DEPTH = 4
SCAN_BUDGET_S = 0.4
NO_SCAN_TOOLS = {"Read", "Grep", "Glob", "TodoWrite", "WebSearch", "WebFetch"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
_PATH_RE = re.compile(r"(?<![\w/.:~-])(/[^\s\"'`<>|*?()\[\]{},;:=]+)")

RESULT_CHARS = 700
SUMMARY_CHARS = 160


def _clip(s, n):
    s = str(s)
    return s if len(s) <= n else s[:n - 1] + "…"


def summarize(name: str, tin: dict) -> str:
    """One line that says what a tool call does."""
    tin = tin or {}
    if name == "Bash":
        return _clip(tin.get("command") or "", SUMMARY_CHARS)
    if name in ("Edit", "Write", "MultiEdit", "Read", "NotebookEdit"):
        return str(tin.get("file_path") or tin.get("notebook_path") or "")
    if name in ("Task", "Agent"):
        return _clip(f"{tin.get('subagent_type') or 'agent'}: {tin.get('description') or ''}", SUMMARY_CHARS)
    if name in ("Grep", "Glob"):
        return _clip(tin.get("pattern") or "", SUMMARY_CHARS)
    if name.startswith("mcp__"):
        return _clip(json.dumps(tin, ensure_ascii=False, default=str), SUMMARY_CHARS)
    return _clip(json.dumps(tin, ensure_ascii=False, default=str), SUMMARY_CHARS) if tin else ""


def _icon(name: str) -> str:
    if name in ("Task", "Agent"):
        return "🤖"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "✏️"
    if name == "Bash":
        return "▶"
    if name.startswith("mcp__"):
        return "🔌"
    return "🔧"


def describe_file(path: str, guess: bool = False) -> tuple[dict | None, str]:
    """(event, reason). event is the `file` bridge event, or None when nothing can be sent; reason
    says why not (or why it is flagged). `guess` marks a path that was found, not named by a Write
    tool: it must have no dot-directory in it (~/.ssh, ~/.config)."""
    try:
        if not path or not os.path.isabs(path):
            return None, "not an absolute path"
        real = os.path.realpath(path)
        ext = os.path.splitext(real)[1].lower()
        mime = FILE_TYPES.get(ext)
        dl = DOWNLOAD_TYPES.get(ext)
        if guess and any(c.startswith(".") for c in real.split(os.sep) if c):
            return None, "inside a dot-directory"
        if not os.path.isfile(real):
            return None, "not a regular file"
        if not mime and not dl:
            return None, f"unlisted type {ext or '(no extension)'}"
        size = os.path.getsize(real)
        if size == 0:
            return None, "empty file"
        base = {"type": "file", "name": os.path.basename(real), "mime": mime or dl, "size": size,
                "path": real}
        if not mime:
            base["download_only"] = True
            base["reason"] = f"{ext} files are offered as a download, not shown"
        if size > FILE_MAX_BYTES:
            return {**base, "b64": "", "too_large": True, "download_only": True,
                    "reason": f"{size} bytes is over the {FILE_MAX_BYTES} byte limit; not sent"}, "too large"
        with open(real, "rb") as f:
            data = f.read(FILE_MAX_BYTES + 1)
        if len(data) != size:
            return None, "changed while reading"
        return {**base, "b64": base64.b64encode(data).decode("ascii")}, base.get("reason", "")
    except OSError as ex:
        return None, f"unreadable: {ex.__class__.__name__}"


def file_event(path: str) -> dict | None:
    """The `file` bridge event for a path the run wrote, with its bytes, or None when it can't be
    sent: not absolute, not a regular file, unlisted type, empty, or over FILE_MAX_BYTES."""
    ev, _ = describe_file(path)
    return ev if ev and not ev.get("too_large") else None


def _scan(root: str) -> dict[str, tuple[int, int]]:
    """path -> (mtime_ns, size) for files of a known type under root. Bounded by depth, file count
    and time; skips SKIP_DIRS, virtualenvs (pyvenv.cfg) and dotfiles."""
    out: dict[str, tuple[int, int]] = {}
    if not root or not os.path.isdir(root):
        return out
    t_end, n = time.monotonic() + SCAN_BUDGET_S, 0
    base_depth = root.rstrip(os.sep).count(os.sep)
    try:
        for d, dirs, files in os.walk(root):
            if "pyvenv.cfg" in files:
                dirs[:] = []
                continue
            if d.count(os.sep) - base_depth >= SCAN_MAX_DEPTH:
                dirs[:] = []
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS and not x.startswith(".")]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if f.startswith(".") or (ext not in FILE_TYPES and ext not in DOWNLOAD_TYPES):
                    continue
                n += 1
                if n > SCAN_MAX_FILES or time.monotonic() > t_end:
                    log.info("scan of %s stopped at %d files (bounded)", root, n - 1)
                    return out
                p = os.path.join(d, f)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                out[p] = (st.st_mtime_ns, st.st_size)
    except OSError:
        pass
    return out


def result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


class Renderer:
    def __init__(self):
        self.calls: dict[str, dict] = {}        # tool_use_id → {name, input, parent}
        self.ok = self.failed = 0
        self.todos: list[dict] = []
        self._streamed: set[str] = set()        # message ids whose text already went out as deltas
        self._cur_msg = ""
        self._tail = "\n"                       # last char emitted, to start tool lines on a new line
        self._files: set[str] = set()           # paths already sent as file events
        self._dropped: set[str] = set()         # paths already logged as dropped (log once)
        self._cands: set[str] = set()           # paths named in written scripts, not made yet
        self.cwd = ""
        self._snap: dict[str, tuple[int, int]] = {}
        self._t0 = time.time()
        self.session_id = ""
        self.api_key_source = ""

    # --- files (#1355, #1400, #1401)
    def watch(self, cwd: str):
        """Remember the run's cwd and take the baseline snapshot, before any tool runs."""
        self.cwd, self._t0 = cwd or "", time.time()
        self._snap = _scan(self.cwd)

    def _offer(self, path: str, source: str, out: list, guess: bool = False):
        """Emit (or log the drop of) one file. Deduped by real path, capped per run."""
        real = os.path.realpath(path) if path else path
        if real in self._files:
            return
        if len(self._files) >= FILES_PER_RUN:
            if real not in self._dropped:
                self._dropped.add(real)
                log.info("file dropped: %s (%s): at most %d files per run", real, source, FILES_PER_RUN)
            return
        ev, reason = describe_file(real, guess=guess)
        if ev is None:
            if not guess and real not in self._dropped:     # a guess that is no file is just noise
                self._dropped.add(real)
                log.info("file dropped: %s (%s): %s", real, source, reason)
            return
        ev["source"] = source
        self._files.add(real)
        flags = [k for k in ("download_only", "too_large") if ev.get(k)]
        log.info("file event: %s name=%s mime=%s size=%d source=%s%s", ev["path"], ev["name"], ev["mime"],
                 ev["size"], source, f" flags={','.join(flags)}" if flags else "")
        out.append(ev)

    def _find_files(self, call: dict, body: str, out: list):
        """After a non-Write tool result: files that appeared or changed under the cwd, plus
        absolute paths named in the tool's input or output that exist and are fresh."""
        if self.cwd:
            new = _scan(self.cwd)
            changed = sorted((p for p, v in new.items() if self._snap.get(p) != v),
                             key=lambda p: new[p][0])
            self._snap = new
            for p in changed:
                self._offer(p, "scan", out, guess=True)
        text = body + "\n" + json.dumps(call.get("input") or {}, ensure_ascii=False, default=str)
        self._offer_paths(text, "path", out)
        for m in list(self._cands):                 # named in a script written earlier (live test 2)
            if self._fresh(m):
                self._cands.discard(m)
                self._offer(m, "script", out, guess=True)

    def _paths(self, text: str) -> list[str]:
        """Absolute paths of a known type named in text, in order, once each."""
        out = []
        for m in dict.fromkeys(_PATH_RE.findall(text or "")):
            m = m.rstrip(".")
            ext = os.path.splitext(m)[1].lower()
            if ext in FILE_TYPES or ext in DOWNLOAD_TYPES:
                out.append(m)
        return out

    def _fresh(self, path: str) -> bool:
        """Exists and was written during this run (a file from before the run is not ours)."""
        try:
            return os.stat(path).st_mtime >= self._t0 - 2
        except OSError:
            return False

    def _offer_paths(self, text: str, source: str, out: list):
        for m in self._paths(text):
            if self._fresh(m):
                self._offer(m, source, out, guess=True)

    def _remember_paths(self, tin: dict):
        """Paths inside a script the agent wrote (Write/Edit content). The script makes them only
        when it runs, so they are checked after each later tool result."""
        body = "\n".join(str(tin.get(k) or "") for k in ("content", "new_string"))
        for ed in tin.get("edits") or []:
            if isinstance(ed, dict):
                body += "\n" + str(ed.get("new_string") or "")
        for m in self._paths(body)[:FILES_PER_RUN * 3]:
            if os.path.realpath(m) not in self._files:
                self._cands.add(m)

    # --- helpers
    def _delta(self, text: str, out: list):
        if text:
            out.append({"type": "delta", "text": text})
            self._tail = text[-1]

    def _line(self, text: str, out: list, depth: int = 0):
        pre = "" if self._tail == "\n" else "\n"
        self._delta(f"{pre}{'> ' * (depth + 1)}{text}\n\n" if depth == 0 else f"{pre}{'> ' * (depth + 1)}{text}\n", out)

    # --- main
    def feed(self, e: dict) -> list[dict]:
        out: list[dict] = []
        t = e.get("type")
        if t == "system":
            if e.get("subtype") == "init":
                self.session_id = e.get("session_id") or ""
                self.api_key_source = e.get("apiKeySource") or ""
            elif e.get("subtype") == "permission_denied":
                self._line(f"⛔ denied: {_clip(e.get('message') or e.get('tool_name') or '', 300)}", out)
        elif t == "stream_event":
            self._stream(e, out)
        elif t == "assistant":
            self._assistant(e, out)
        elif t == "user":
            self._user(e, out)
        return out

    def _stream(self, e, out):
        ev = e.get("event") or {}
        if ev.get("type") == "message_start":
            self._cur_msg = (ev.get("message") or {}).get("id") or ""
        elif ev.get("type") == "content_block_delta" and e.get("parent_tool_use_id") is None:
            d = ev.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                self._streamed.add(self._cur_msg)
                self._delta(d["text"], out)

    def _assistant(self, e, out):
        msg = e.get("message") or {}
        depth = 1 if e.get("parent_tool_use_id") else 0
        for b in msg.get("content") or []:
            if b.get("type") == "text" and depth == 0:
                self._offer_paths(b.get("text") or "", "reply", out)   # the agent names what it made
                if msg.get("id") not in self._streamed and b.get("text"):
                    self._delta(("\n" if self._tail != "\n" else "") + b["text"] + "\n", out)
                elif msg.get("id") in self._streamed and self._tail != "\n":
                    self._delta("\n", out)
            elif b.get("type") == "tool_use":
                name, tin = b.get("name") or "?", b.get("input") or {}
                self.calls[b.get("id")] = {"name": name, "input": tin, "depth": depth}
                if name == "TodoWrite":
                    self.todos = list(tin.get("todos") or [])
                self._line(f"{_icon(name)} **{name}** `{summarize(name, tin)}` …running", out, depth)

    def _user(self, e, out):
        msg = e.get("message") or {}
        content = msg.get("content")
        if not isinstance(content, list):
            return
        for b in content:
            if b.get("type") != "tool_result":
                continue
            call = self.calls.get(b.get("tool_use_id")) or {"name": "tool", "depth": 0}
            bad = bool(b.get("is_error"))
            self.failed += bad
            self.ok += not bad
            body = _clip(result_text(b.get("content")).strip(), RESULT_CHARS)
            mark = "✗ failed" if bad else "✓ done"
            if call["name"] in ("Task", "Agent"):
                mark = ("✗ subagent failed" if bad else "✓ subagent finished")
            line = f"↳ {mark}: **{call['name']}**"
            if body and (bad or call["name"] not in ("Write", "Edit", "MultiEdit")):
                line += f"\n{'> ' * (call['depth'] + 1)}<details><summary>output</summary>\n\n```\n{body}\n```\n</details>"
            self._line(line, out, call["depth"])
            if bad or call["name"] in NO_SCAN_TOOLS:
                continue
            if call["name"] == "Write":
                self._offer(str((call.get("input") or {}).get("file_path") or ""), "write", out)
            if call["name"] in EDIT_TOOLS:
                self._remember_paths(call.get("input") or {})
                if self.cwd:
                    self._snap = _scan(self.cwd)    # edits are not offered; keep the baseline current
            else:
                self._find_files(call, result_text(b.get("content")), out)

    def footer(self, run_id: str, pending_operator: list[str], pending_agent: list[str],
               status: str, cost=None, agent: str = "") -> str:
        """Counted from tool_result events only, then the numbered pending list by owner (req. 10)."""
        lines = ["", "---", f"`run {run_id}` · {status} · {agent or 'Claude Code'} · tool results: {self.ok} ok, "
                 f"{self.failed} failed"
                 + (f" · ${cost:.3f} (not billed)" if isinstance(cost, (int, float)) else ""),
                 "", "**Pending**"]
        n = 0
        for owner, items in (("Operator", pending_operator), ("Agent", pending_agent)):
            if items:
                lines.append(f"{owner}:")
                for it in items:
                    n += 1
                    lines.append(f"{n}. {it}")
        if n == 0:
            lines.append("Nothing pending.")
        return "\n".join(lines) + "\n"

    def open_todos(self) -> list[str]:
        return [str(t.get("content") or t.get("activeForm") or "")
                for t in self.todos if t.get("status") in ("pending", "in_progress")]
