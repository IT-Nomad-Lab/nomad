#!/usr/bin/env python3
"""Grant or revoke gateway MCP servers for a project (its key), by server name.

  grant.py <project> <server>[,<server>...]    add to the project's grants (creates the key)
  grant.py <project> --revoke <server>[,...]   remove
  grant.py <project> --show                    print the project's current grants
  grant.py --servers                           registered gateway servers and who may use them

The key is stored 0600 at ~/.config/nomad/mcp-keys/<project>; the project's `.mcp.json` reads it
as MCP_GATEWAY_KEY (see README). The master key comes from ~/.config/nomad/mcp-gateway.env.
"""
import json
import os
import re
import sys
import urllib.request

BASE = os.environ.get("MCP_GATEWAY_URL", "http://127.0.0.1:4010")
KEYDIR = os.path.expanduser("~/.config/nomad/mcp-keys")
ENV = os.path.expanduser("~/.config/nomad/mcp-gateway.env")


def master():
    m = re.search(r"(?m)^LITELLM_MASTER_KEY=(.+)$", open(ENV).read())
    return m.group(1).strip()


def call(path, body=None, method=None):
    req = urllib.request.Request(BASE + path, method=method or ("POST" if body is not None else "GET"),
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + master(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def servers():
    return {s["server_name"]: s for s in call("/v1/mcp/server")}


def current(key):
    info = call("/key/info?key=" + key)["info"]
    return list((info.get("object_permission") or {}).get("mcp_servers") or [])


def main(argv):
    if argv == ["--servers"]:
        for n, s in sorted(servers().items()):
            print(f"{n:20} {'all keys' if s.get('allow_all_keys') else 'granted keys only':18} {s.get('status')}")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    project = argv[0]
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", project):
        raise SystemExit("project must be a lowercase slug")
    srv = servers()
    path = os.path.join(KEYDIR, project)
    key = open(path).read().strip() if os.path.exists(path) else ""
    have = current(key) if key else []
    if argv[1] == "--show":
        ids = {s["server_id"]: n for n, s in srv.items()}
        print(", ".join(ids.get(i, i) for i in have) or "(none, or no key yet)")
        return 0
    revoke = argv[1] == "--revoke"
    names = [n for n in (argv[2] if revoke else argv[1]).split(",") if n]
    bad = [n for n in names if n not in srv]
    if bad:
        raise SystemExit("not registered in the gateway: " + ", ".join(bad) + " (see --servers)")
    ids = [srv[n]["server_id"] for n in names]
    want = sorted(set(have) - set(ids)) if revoke else sorted(set(have) | set(ids))
    perm = {"object_permission": {"mcp_servers": want}}
    if key:
        call("/key/update", {"key": key, **perm})
    else:
        os.makedirs(KEYDIR, mode=0o700, exist_ok=True)
        key = call("/key/generate", {"key_alias": "project-" + project, **perm})["key"]
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
    print(f"{project}: " + (", ".join(n for n, s in srv.items() if s["server_id"] in want) or "(none)"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
