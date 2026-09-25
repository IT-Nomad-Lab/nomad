"""NOMAD Penpot navigator (MCP) — lets the Penpot MCP work on ANY file, not just the open tab.

Penpot's own MCP (`penpot` → execute_code, export_shape, …) talks to a plugin that lives inside a
browser tab, and the plugin API has `openPage` but no `openFile`: it can only touch the file that
tab has open, and its sandboxed iframe can't navigate the tab either. So the switch has to happen
outside the plugin. This server owns a Chromium tab (Playwright, persistent profile) and moves it
between files by URL. Penpot 2.17 starts its bundled MCP plugin on every workspace load when MCP is
enabled on the profile, so after a navigation the plugin reconnects by itself and the `penpot`
tools act on the new file.

Tools:
  penpot_status()                     → login, current file, plugin connection
  penpot_list_files(query)            → teams / projects / files visible to the logged-in user
  penpot_open_file(file, page)        → point the tab at a file (name or id), wait for the plugin
  penpot_create_file(name, project)   → create a file, then open it

One-time login (the profile keeps the session; no password or token is stored here):
  .venv/bin/python nomad_penpot_nav.py login

Constraints worth knowing:
  - The Penpot MCP server accepts ONE plugin connection per MCP key and rejects the rest. If your
    own browser has a Penpot file open, that tab holds the connection and this one can't connect.
    Close it (or pick "Disconnect" in its MCP menu) while automating.
  - A Chromium profile can only be used by one process, so only one Claude Code session at a time
    can run this server. A second one fails to start with a clear error.
"""
import asyncio
import json
import os
import re
import sys
import time
import uuid

from mcp.server.fastmcp import FastMCP
from playwright.async_api import async_playwright

PENPOT_URL = os.environ.get("PENPOT_URL", "http://localhost:9001").rstrip("/")
PROFILE_DIR = os.path.expanduser(
    os.environ.get("NOMAD_PENPOT_PROFILE", "~/.config/nomad/penpot-nav-profile"))
# Headed by default: WSLg shows the window, so you can watch the agent work. Set 1 to hide it.
HEADLESS = os.environ.get("NOMAD_PENPOT_HEADLESS", "0") == "1"
CONNECT_TIMEOUT = float(os.environ.get("NOMAD_PENPOT_CONNECT_TIMEOUT", "60"))

mcp = FastMCP("nomad-penpot-nav")

_lock = asyncio.Lock()
_pw = None
_ctx = None
_page = None
_mcp_status = {"value": None}  # last "MCP STATUS" the workspace logged
_current = {"file": None}


# --- browser ----------------------------------------------------------------------------------

def _on_console(msg) -> None:
    text = msg.text
    if "MCP STATUS" in text:
        m = re.search(r":status\s+\"?([a-z-]+)", text) or re.search(r"(connected|connecting|disconnected|error)", text)
        if m:
            _mcp_status["value"] = m.group(1)


async def _browser(headless: bool = HEADLESS):
    global _pw, _ctx, _page
    if _page and not _page.is_closed():
        return _page
    os.makedirs(PROFILE_DIR, mode=0o700, exist_ok=True)  # holds the Penpot session cookie
    _pw = _pw or await async_playwright().start()
    try:
        _ctx = await _pw.chromium.launch_persistent_context(
            PROFILE_DIR, headless=headless, viewport={"width": 1600, "height": 1000})
    except Exception as e:
        raise RuntimeError(
            f"could not start Chromium on {PROFILE_DIR} — is another session already using it? ({e})")
    _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
    _page.on("console", _on_console)
    await _page.goto(PENPOT_URL + "/", wait_until="domcontentloaded")
    return _page


async def _rpc(method: str, params: dict | None = None):
    """Call the Penpot backend from inside the tab, riding its session cookie."""
    page = await _browser()
    res = await page.evaluate(
        """async ([url, body]) => {
             const r = await fetch(url, {method: "POST", credentials: "include",
               headers: {"Content-Type": "application/json", "Accept": "application/json"},
               body: JSON.stringify(body)});
             return {status: r.status, text: await r.text()};
           }""",
        [f"{PENPOT_URL}/api/main/methods/{method}", params or {}])
    try:
        body = json.loads(res["text"]) if res["text"] else None
    except ValueError:
        body = res["text"]
    if res["status"] >= 400:
        raise RuntimeError(f"{method} → HTTP {res['status']}: {str(body)[:300]}")
    return body


def _g(obj: dict, key: str):
    """Penpot's JSON encoding may be kebab-case or camelCase depending on the endpoint."""
    if key in obj:
        return obj[key]
    camel = re.sub(r"-([a-z])", lambda m: m.group(1).upper(), key)
    return obj.get(camel)


async def _profile():
    prof = await _rpc("get-profile")
    if not prof or _g(prof, "id") == "00000000-0000-0000-0000-000000000000":
        raise RuntimeError(
            "not logged in to Penpot. Run once: "
            "~/nomad/mcp-local/.venv/bin/python ~/nomad/mcp-local/nomad_penpot_nav.py login")
    return prof


async def _all_files():
    await _profile()
    out = []
    for team in await _rpc("get-teams"):
        tid = _g(team, "id")
        for proj in await _rpc("get-projects", {"team-id": tid}):
            pid = _g(proj, "id")
            for f in await _rpc("get-project-files", {"project-id": pid}):
                out.append({"id": _g(f, "id"), "name": _g(f, "name"), "project": _g(proj, "name"),
                            "project_id": pid, "team": _g(team, "name"), "team_id": tid})
    return out


def _is_uuid(s: str) -> bool:
    try:
        uuid.UUID(s)
        return True
    except ValueError:
        return False


def _pick(items: list[dict], wanted: str, what: str) -> dict:
    w = wanted.strip()
    if _is_uuid(w):
        hit = [i for i in items if i["id"] == w]
    else:
        hit = [i for i in items if i["name"].lower() == w.lower()] or \
              [i for i in items if w.lower() in i["name"].lower()]
    if len(hit) == 1:
        return hit[0]
    if not hit:
        raise RuntimeError(f"no {what} matches {wanted!r}")
    names = ", ".join(f"{i['name']} ({i.get('project', '')} · {i['id']})" for i in hit[:10])
    raise RuntimeError(f"{wanted!r} matches {len(hit)} {what}s — use the id: {names}")


async def _wait_connected() -> str:
    deadline = time.monotonic() + CONNECT_TIMEOUT
    dot = '[data-testid="mcp-btn"] span[class*="connected"]'
    while time.monotonic() < deadline:
        if _mcp_status["value"] == "connected":
            return "connected"
        try:
            if await _page.locator(dot).count():
                return "connected"
        except Exception:
            pass
        await asyncio.sleep(0.5)
    return _mcp_status["value"] or "unknown"


# --- tools ------------------------------------------------------------------------------------

@mcp.tool()
async def penpot_status() -> dict:
    """Report whether the navigator is logged in, which file its tab has open, and whether the
    Penpot MCP plugin in that tab is connected."""
    async with _lock:
        prof = await _profile()
        return {"user": _g(prof, "email") or _g(prof, "fullname"), "file": _current["file"],
                "mcp_plugin": _mcp_status["value"], "url": _page.url}


@mcp.tool()
async def penpot_list_files(query: str = "") -> list[dict]:
    """List Penpot files the logged-in user can see (optionally filtered by a name substring)."""
    async with _lock:
        files = await _all_files()
    q = query.strip().lower()
    return [f for f in files if q in f["name"].lower()] if q else files


@mcp.tool()
async def penpot_open_file(file: str, page: str = "") -> dict:
    """Open a Penpot file in the automation tab so the `penpot` MCP tools act on it.

    file: file name (exact, else substring) or file id. page: optional page name or id.
    Waits until the bundled MCP plugin reconnects in the new file, then returns.
    """
    async with _lock:
        target = _pick(await _all_files(), file, "file")
        page_id = ""
        if page.strip():
            data = await _rpc("get-file", {"id": target["id"]})
            d = _g(data, "data") or {}
            index = _g(d, "pages-index") or {}
            pages = [{"id": pid, "name": _g(p, "name")} for pid, p in index.items()]
            page_id = _pick(pages, page, "page")["id"]

        # Drop the old workspace first: the MCP server rejects a second connection on the same key
        # until the previous socket is gone, and a hash-only change would not reload the app.
        _mcp_status["value"] = None
        await _page.goto("about:blank")
        await asyncio.sleep(1.5)
        url = f"{PENPOT_URL}/#/workspace?team-id={target['team_id']}&file-id={target['id']}"
        if page_id:
            url += f"&page-id={page_id}"
        await _page.goto(url, wait_until="domcontentloaded")
        _current["file"] = {k: target[k] for k in ("id", "name", "project", "team")}

        status = await _wait_connected()
        result = {"opened": _current["file"], "page_id": page_id or None, "mcp_plugin": status}
        if status != "connected":
            result["hint"] = ("plugin did not report connected. Most likely another tab (your own "
                              "browser) holds this MCP key — close it or disconnect it, then retry. "
                              "Also check MCP is enabled in Penpot → Your account → Integrations.")
        return result


@mcp.tool()
async def penpot_create_file(name: str, project: str = "") -> dict:
    """Create a Penpot file (in `project`, by name or id; default: the first team's Drafts) and
    open it for the `penpot` tools."""
    async with _lock:
        await _profile()
        projects = []
        for team in await _rpc("get-teams"):
            for p in await _rpc("get-projects", {"team-id": _g(team, "id")}):
                projects.append({"id": _g(p, "id"), "name": _g(p, "name"),
                                 "default": bool(_g(p, "is-default"))})
        if project.strip():
            proj = _pick(projects, project, "project")
        else:
            proj = next((p for p in projects if p["default"]), None) or projects[0]
        created = await _rpc("create-file", {"name": name, "project-id": proj["id"]})
        file_id = _g(created, "id")
    return await penpot_open_file(file_id)


# --- one-time login ---------------------------------------------------------------------------

async def _login() -> None:
    page = await _browser(headless=False)
    print(f"Log in to Penpot in the window that opened ({PENPOT_URL}). Waiting up to 5 minutes…")
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        try:
            prof = await _rpc("get-profile")
            if prof and _g(prof, "id") != "00000000-0000-0000-0000-000000000000":
                print(f"Logged in as {_g(prof, 'email') or _g(prof, 'fullname')}. Session saved in {PROFILE_DIR}.")
                await _ctx.close()
                return
        except Exception:
            pass
        await asyncio.sleep(2)
    await _ctx.close()
    sys.exit("timed out waiting for login")


if __name__ == "__main__":
    if sys.argv[1:] == ["login"]:
        asyncio.run(_login())
    else:
        mcp.run()
