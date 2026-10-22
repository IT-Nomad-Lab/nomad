"""Create one Zulip stream per nomad-pm board, with the operator and the bot subscribed (#1123).

Idempotent: an existing stream only gets its subscriptions confirmed. The relay also creates a
stream the first time a board has something to post; this script does every board up front, and
`+ New project` (docs/prd/owui-sessions.md §6.4) calls `ensure()` for a new one.

    python project_streams.py            # every board the engine lists
    python project_streams.py runematch  # just these
    python project_streams.py --dry-run
    python project_streams.py --archive runematch   # archive these streams (#1200, delete project)

Reads the bot's zuliprc and NOMAD_ZULIP_OPERATOR_ID, the same as the relay (relay.env).
"""
import os
import sys

import requests

import render as R

ENGINE = os.environ.get("NOMAD_ENGINE_URL", "http://127.0.0.1:8099")
ZULIPRC = os.environ.get("NOMAD_ZULIPRC", os.path.expanduser("~/.config/nomad/zulip-bot.zuliprc"))


def boards():
    r = requests.get(ENGINE + "/pm-projects", timeout=30)
    r.raise_for_status()
    return [p["identifier"] for p in r.json().get("projects", []) if p.get("identifier")]


def ensure(client, names, principals, dry=False):
    """{name: "ok" | "skipped: <why>" | "error: <msg>"} for each board."""
    out = {}
    for name in names:
        if R.project_stream(name, "") != name:
            out[name] = "skipped: not a usable stream name"
            continue
        if dry:
            out[name] = "would create"
            continue
        res = client.add_subscriptions(
            streams=[{"name": name, "description": R.project_stream_description(name)}],
            principals=principals, announce=False)
        out[name] = "ok" if res.get("result") == "success" else f"error: {res.get('msg')}"
    return out


def archive(client, names, dry=False):
    """{name: "ok" | "ok: no such stream" | "skipped: <why>" | "error: <msg>"}. Zulip's DELETE
    /streams/<id> archives the stream; its messages stay. Reserved streams are never touched."""
    out = {}
    for name in names:
        if R.project_stream(name, "") != name:
            out[name] = "skipped: not a project stream"
            continue
        found = client.get_stream_id(name)
        if found.get("result") != "success":
            msg = str(found.get("msg") or "")
            out[name] = "ok: no such stream" if "nvalid stream" in msg or "does not exist" in msg \
                else f"error: {msg}"
            continue
        if dry:
            out[name] = "would archive"
            continue
        res = client.call_endpoint(url=f"streams/{found['stream_id']}", method="DELETE")
        out[name] = "ok" if res.get("result") == "success" else f"error: {res.get('msg')}"
    return out


def main(argv):
    import zulip
    dry = "--dry-run" in argv
    if "--archive" in argv:
        import zulip
        names = [a for a in argv if not a.startswith("--")]
        if not names:
            raise SystemExit("--archive needs the stream names")
        results = archive(zulip.Client(config_file=ZULIPRC), names, dry=dry)
        for name, res in results.items():
            print(f"#{name}: {res}")
        return 0 if all(r.startswith("ok") or r == "would archive" for r in results.values()) else 1
    names = [a for a in argv if not a.startswith("--")] or boards()
    op = os.environ.get("NOMAD_ZULIP_OPERATOR_ID", "")
    if not op.isdigit():
        raise SystemExit("NOMAD_ZULIP_OPERATOR_ID is required (it is in ~/.config/nomad/relay.env)")
    client = zulip.Client(config_file=ZULIPRC)
    bot = client.get_profile()["user_id"]
    results = ensure(client, names, [int(op), bot], dry=dry)
    for name, res in results.items():
        print(f"#{name}: {res}")
    return 0 if all(not r.startswith("error") for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
