#!/usr/bin/env python3
"""One MCP registry for Claude Code, Codex and the local model (PRD docs/prd/mcp-all-backends.md).

Sources, read in this order (a name in both is taken from the first):
  ~/nomad/.mcp.json        project servers
  ~/.claude.json           user-level `mcpServers`

Outputs, written only when the content changed:
  ~/.codex/config.toml     a generated `[mcp_servers.*]` block between marker comments; the rest
                           of the file is untouched. Also `mcp_oauth_credentials_store = "file"`
                           so the headless bridge can read OAuth tokens.
  ~/.config/nomad/mcp-all.json   `{"mcpServers": {...}}` for `claude --mcp-config` (local runs)

Claude Code reads both source files itself, so it needs no output.
`~/.config/nomad/mcp-backends.json` ({"codex": [names], "local": [names]}) withholds servers from
one backend. Empty by default: every backend gets every server.

  mcp-sync.py            sync now, print what changed
  mcp-sync.py --check    exit 1 when an output is out of date, change nothing
"""
import json
import os
import re
import sys
import tempfile

HOME = os.path.expanduser("~")
PROJECT_MCP = os.environ.get("MCP_SYNC_PROJECT", os.path.join(HOME, "nomad", ".mcp.json"))
USER_CLAUDE = os.environ.get("MCP_SYNC_USER", os.path.join(HOME, ".claude.json"))
CODEX_TOML = os.environ.get("MCP_SYNC_CODEX", os.path.join(HOME, ".codex", "config.toml"))
LOCAL_JSON = os.environ.get("MCP_SYNC_LOCAL", os.path.join(HOME, ".config", "nomad", "mcp-all.json"))
BACKENDS = os.environ.get("MCP_SYNC_BACKENDS", os.path.join(HOME, ".config", "nomad", "mcp-backends.json"))
BEGIN = "# >>> nomad mcp-sync (generated from ~/nomad/.mcp.json and ~/.claude.json; do not edit) >>>"
END = "# <<< nomad mcp-sync <<<"
STORE_KEY = 'mcp_oauth_credentials_store = "file"'


def _load(path: str) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def registry() -> dict:
    """name -> normalized server: {"command","args","env"} or {"url","headers"}."""
    out = {}
    for src in (_load(PROJECT_MCP).get("mcpServers"), _load(USER_CLAUDE).get("mcpServers")):
        for name, s in (src or {}).items():
            if name in out or not isinstance(s, dict):
                continue
            if s.get("url"):
                e = {"url": s["url"]}
                if s.get("headers"):
                    e["headers"] = s["headers"]
                if s.get("oauth"):      # pre-registered client (Google has no dynamic registration)
                    e["oauth"] = s["oauth"]
            elif s.get("command"):
                e = {"command": s["command"]}
                if s.get("args"):
                    e["args"] = [str(a) for a in s["args"]]
                if s.get("env"):
                    e["env"] = {k: str(v) for k, v in s["env"].items()}
            else:
                continue
            out[name] = e
    return out


def withheld(backend: str) -> set:
    return set(_load(BACKENDS).get(backend) or [])


def _q(s: str) -> str:
    return json.dumps(s, ensure_ascii=False)       # a JSON string is a valid TOML basic string


def _key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else _q(name)


def codex_block(servers: dict) -> str:
    lines = [BEGIN]
    for name in sorted(servers):
        s = servers[name]
        lines.append(f"[mcp_servers.{_key(name)}]")
        # codex exec has no approval channel: without this a tool that isn't marked read-only is
        # refused ("requires approval, but approval policy is never"). Operator decision 2026-10-14.
        if "url" in s:
            lines.append(f"url = {_q(s['url'])}")
            if s.get("headers"):
                lines.append("http_headers = {" + ", ".join(
                    f"{_key(k)} = {_q(str(v))}" for k, v in s["headers"].items()) + "}")
        else:
            lines.append(f"command = {_q(s['command'])}")
            if s.get("args"):
                lines.append("args = [" + ", ".join(_q(a) for a in s["args"]) + "]")
            if s.get("env"):
                lines.append("env = {" + ", ".join(
                    f"{_key(k)} = {_q(v)}" for k, v in s["env"].items()) + "}")
        lines.append('default_tools_approval_mode = "approve"')
        lines.append("")
    lines.append(END)
    return "\n".join(lines)


def merge_codex(text: str, block: str) -> tuple[str, list]:
    """Replace (or append) the generated block. Servers whose table is already defined by hand
    outside the block are dropped from it (a duplicate table is a TOML error); returns those names."""
    a, b = text.find(BEGIN), text.find(END)
    if a != -1 and b > a:
        outside = text[:a] + text[b + len(END):]
    else:
        outside = text
    hand = set(re.findall(r"(?m)^\[mcp_servers\.([^\].]+|\"[^\"]+\")(?:\.[^\]]*)?\]", outside))
    skipped = sorted(n for n in re.findall(r"(?m)^\[mcp_servers\.([^\]]+)\]", block)
                     if n in hand or f'"{n}"' in hand)
    if skipped:
        keep = []
        for chunk in re.split(r"(?m)^(?=\[mcp_servers\.)", block):
            m = re.match(r"\[mcp_servers\.([^\]]+)\]", chunk)
            if m and m.group(1) in skipped:
                continue
            keep.append(chunk)
        block = "".join(keep)
        if END not in block:        # END rides on the last chunk; it must survive a skipped server
            block = block.rstrip("\n") + "\n" + END
    if a != -1 and b > a:
        new = text[:a] + block + text[b + len(END):]
    else:
        new = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block + "\n"
    if STORE_KEY.split(" = ")[0] not in re.sub(r"(?m)^\[.*", "", new.split("\n[", 1)[0]):
        new = STORE_KEY + "\n" + new
    return new, skipped


def _write(path: str, content: str, mode: int | None = None) -> bool:
    try:
        with open(path) as f:
            if f.read() == content:
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".mcp-sync-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    if mode is not None:
        os.chmod(tmp, mode)
    os.replace(tmp, path)
    return True


def outputs() -> tuple[str, str, list]:
    reg = registry()
    codex_srv = {n: s for n, s in reg.items() if n not in withheld("codex")}
    local_srv = {n: s for n, s in reg.items() if n not in withheld("local")}
    try:
        with open(CODEX_TOML) as f:
            cur = f.read()
    except OSError:
        cur = ""
    toml, skipped = merge_codex(cur, codex_block(codex_srv))
    local = json.dumps({"mcpServers": {n: ({"type": "http", **s} if "url" in s else s)
                                       for n, s in sorted(local_srv.items())}}, indent=2) + "\n"
    return toml, local, skipped


def sync(check: bool = False) -> dict:
    toml, local, skipped = outputs()
    if check:
        def same(path, content):
            try:
                with open(path) as f:
                    return f.read() == content
            except OSError:
                return False
        return {"codex": not same(CODEX_TOML, toml), "local": not same(LOCAL_JSON, local),
                "skipped": skipped}
    return {"codex": _write(CODEX_TOML, toml), "local": _write(LOCAL_JSON, local, 0o600),
            "skipped": skipped}


def main(argv: list) -> int:
    check = "--check" in argv
    r = sync(check)
    reg = registry()
    print(f"{len(reg)} servers; " + ", ".join(
        f"{k}: {'out of date' if check and r[k] else 'changed' if r[k] else 'unchanged'}" for k in ("codex", "local")))
    if r["skipped"]:
        print("defined by hand in config.toml, left alone: " + ", ".join(r["skipped"]))
    return 1 if check and (r["codex"] or r["local"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
