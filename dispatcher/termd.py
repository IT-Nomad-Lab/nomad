#!/usr/bin/env python3
"""NOMAD terminal daemon — interactive `claude` sessions over WebSocket.

The headline of the "work inside a project" feature: a real PTY running interactive Claude Code in
a selected repo, streamed to the browser (xterm.js). Runs NATIVELY in WSL (where `claude`, the
~/.claude login, and the repos live) — the console proxies the browser WS here.

  WS /term?project=<name>&cols=&rows=   attach an interactive claude session in the repo
  GET /health                           {ok}

Sessions persist per project: closing the browser tab leaves the claude session alive, and a
reconnect re-attaches (with scrollback). Idle sessions are reaped. Project resolution + the claude
path are reused from dispatch.py, so the Nomad.md whitelist is identical to the Builder dispatcher.

This is the OPERATOR's own interactive session — they drive it. acceptEdits/full claude is fine;
commit/push are their call here (distinct from the autonomous crew's commit-gated guardrails).
"""
import asyncio
import fcntl
import hmac
import http
import json
import os
import pty
import secrets
import signal
import struct
import termios
import time
import urllib.parse

from websockets.asyncio.server import serve   # modern asyncio API (websockets >= 13)

from dispatch import resolve, CLAUDE, ROOTS   # reuse the Builder's project whitelist + claude path

HOST = os.environ.get("NOMAD_TERM_HOST", "0.0.0.0")
PORT = int(os.environ.get("NOMAD_TERM_PORT", "8091"))
IDLE_REAP_SECS = int(os.environ.get("NOMAD_TERM_IDLE", "1800"))   # kill sessions idle > 30 min
SCROLLBACK = 100_000                                              # bytes of replay on reconnect
TERM_CMD = os.environ.get("NOMAD_TERM_CMD", CLAUDE)               # what the PTY runs (default: claude)

SESSIONS = {}   # project name -> Session


def _load_or_make_token():
    """Shared secret gating WS attach. termd binds 0.0.0.0 (so the console container can reach it
    via host.docker.internal), which also exposes it to the LAN — so a token is required: only the
    console (which reads this same file via its read-only /host mount) can attach. Auto-generated;
    no manual config. NOMAD_TERM_TOKEN overrides. Fail-open to '' only if the FS is unwritable."""
    env = os.environ.get("NOMAD_TERM_TOKEN")
    if env:
        return env
    path = os.path.expanduser("~/.config/nomad/term-token")
    try:
        if os.path.exists(path):
            return open(path).read().strip()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tok = secrets.token_urlsafe(24)
        with open(path, "w") as f:
            f.write(tok)
        os.chmod(path, 0o600)
        return tok
    except Exception:
        return ""


TOKEN = _load_or_make_token()


class Session:
    """One persistent PTY running `claude` in a repo; fan-out to any attached websockets."""

    def __init__(self, project, path):
        self.project = project
        self.path = path
        self.clients = set()
        self.buffer = bytearray()
        self.last = time.time()
        self.loop = asyncio.get_event_loop()
        self.pid, self.master = self._spawn(path)
        os.set_blocking(self.master, False)
        self.loop.add_reader(self.master, self._on_read)

    def _spawn(self, path):
        pid, master = pty.fork()
        if pid == 0:                              # child → becomes the claude TUI
            try:
                os.chdir(path)
                os.environ["TERM"] = "xterm-256color"
                os.execvp(TERM_CMD, [TERM_CMD])
            except Exception as e:                # pragma: no cover
                os.write(2, f"failed to launch {TERM_CMD}: {e}\n".encode())
                os._exit(127)
        return pid, master                        # parent

    def _on_read(self):
        try:
            data = os.read(self.master, 65536)
        except OSError:
            data = b""
        if not data:                              # claude exited / PTY closed
            self.close()
            return
        self.buffer += data
        if len(self.buffer) > SCROLLBACK:
            del self.buffer[:-SCROLLBACK]
        self.last = time.time()
        for ws in list(self.clients):
            asyncio.create_task(self._safe_send(ws, data))

    async def _safe_send(self, ws, data):
        try:
            await ws.send(data)
        except Exception:
            self.clients.discard(ws)

    def write(self, data: bytes):
        try:
            os.write(self.master, data)
            self.last = time.time()
        except OSError:
            pass

    def resize(self, rows, cols):
        try:
            fcntl.ioctl(self.master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", max(rows, 1), max(cols, 1), 0, 0))
        except OSError:
            pass

    def close(self):
        try:
            self.loop.remove_reader(self.master)
        except Exception:
            pass
        for ws in list(self.clients):
            asyncio.create_task(self._safe_close(ws))
        try:
            os.close(self.master)
        except Exception:
            pass
        try:
            os.kill(self.pid, signal.SIGKILL)
        except Exception:
            pass
        SESSIONS.pop(self.project, None)

    async def _safe_close(self, ws):
        try:
            await ws.close(code=1000, reason="claude session ended")
        except Exception:
            pass


async def handler(websocket, *args):
    path = args[0] if args else websocket.request.path
    q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
    token = (q.get("token") or [""])[0]
    if TOKEN and not hmac.compare_digest(token, TOKEN):
        await websocket.close(code=4401, reason="unauthorized")
        return
    project = (q.get("project") or [""])[0]
    cols = int((q.get("cols") or ["80"])[0] or 80)
    rows = int((q.get("rows") or ["24"])[0] or 24)

    r = resolve(project)
    if not r:
        await websocket.close(code=4404, reason="unknown project")
        return
    key = r["name"]
    sess = SESSIONS.get(key)
    if sess is None:
        try:
            sess = Session(key, r["path"])
            SESSIONS[key] = sess
        except Exception as e:
            await websocket.close(code=4500, reason=f"spawn failed: {str(e)[:80]}")
            return

    sess.clients.add(websocket)
    sess.resize(rows, cols)
    if sess.buffer:                               # replay scrollback so a reconnect sees context
        try:
            await websocket.send(bytes(sess.buffer[-SCROLLBACK:]))
        except Exception:
            pass
    try:
        async for msg in websocket:
            if isinstance(msg, bytes):
                sess.write(msg)
            elif msg.startswith("{"):             # control frame (resize) — else treat as keystrokes
                try:
                    ctl = json.loads(msg)
                    if ctl.get("type") == "resize":
                        sess.resize(int(ctl["rows"]), int(ctl["cols"]))
                        continue
                except Exception:
                    pass
                sess.write(msg.encode())
            else:
                sess.write(msg.encode())
    finally:
        sess.clients.discard(websocket)


async def _reaper():
    while True:
        await asyncio.sleep(60)
        now = time.time()
        for sess in list(SESSIONS.values()):
            if not sess.clients and now - sess.last > IDLE_REAP_SECS:
                sess.close()


def _process_request(connection, request):
    # Runs DURING the handshake, before the WS upgrade — the right place to gate auth so a bad
    # token fails the client's connect() outright (a post-handshake close still shows "connected").
    if request.path.split("?")[0] == "/health":
        body = json.dumps({"ok": True, "service": "nomad-term",
                           "roots": ROOTS, "sessions": list(SESSIONS)}) + "\n"
        return connection.respond(http.HTTPStatus.OK, body)
    q = urllib.parse.parse_qs(urllib.parse.urlparse(request.path).query)
    token = (q.get("token") or [""])[0]
    if TOKEN and not hmac.compare_digest(token, TOKEN):
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "unauthorized\n")
    return None   # authorized → proceed to the WS handshake


async def main():
    asyncio.create_task(_reaper())
    async with serve(handler, HOST, PORT, process_request=_process_request,
                     max_size=4 * 1024 * 1024):
        print(f"NOMAD term daemon on ws://{HOST}:{PORT}/term (claude={CLAUDE}, roots={ROOTS})",
              flush=True)
        await asyncio.Future()   # run forever


if __name__ == "__main__":
    asyncio.run(main())
