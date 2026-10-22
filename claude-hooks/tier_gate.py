#!/usr/bin/env python3
"""PreToolUse hook: the SDLC red tier, enforced in every interactive session.

docs/SDLC.md sorts actions into green (do it), yellow (do it, then tell) and red (ask first).
This hook asks on red and stays out of the way for the rest. It is installed once at user level
(~/.claude/hooks/tier-gate.py, wired in ~/.claude/settings.json), so it runs whatever directory a
session starts in. The repo guard (.claude/hooks/guard.py) still runs on top in ~/nomad sessions,
for the secret scan and the gate's own files.

Decisions:
  - force push, in any form           -> "deny"  (never, even with an OK)
  - the cockpit's guardrail save      -> "deny"  (the operator's alone, #1396)
  - red-tier command or tool          -> "ask"   (the operator confirms)
  - anything else                     -> no decision, normal permissions apply

A local `git merge` is yellow and passes. A push is red wherever it comes from, including
`git -C <dir> push`, which a `Bash(git push:*)` permission rule can't see.

Fail-safe: a command that can't be parsed but mentions a red keyword is asked about, never let
through. One that can't be parsed and mentions none is left to normal permissions.

Windows sessions send `PowerShell` tool calls too (through nomad-operator's bin/nomad-run).
judge_powershell applies the same tier in PowerShell syntax; the Bash rules don't change.
"""
import json
import os
import re
import shlex
import sys

SEPARATORS = {"&&", "||", ";", "|", "&", "(", ")"}

# Outbound, irreversible or spending tools reached through MCP. Matched on the tool name.
RED_TOOLS = re.compile(
    r"^mcp__.*("
    r"Gmail__(send_message|reply|forward)"
    r"|Slack__slack_(send_message|schedule_message|create_conversation)"
    r"|Google_Calendar__(create_event|update_event|delete_event|respond_to_event)"
    r"|Google_Drive__(share_file|trash_file)"
    r")$")

# Whole-command patterns, checked case-insensitively against the raw text.
RED_TEXT = [
    (r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b", "drops a table or database"),
    (r"\bTRUNCATE\b", "truncates a table"),
    (r"\bDELETE\s+FROM\b", "deletes rows"),
    (r"\btailscale\s+funnel\b", "opens a public funnel"),
    (r"\bcloudflared\s+tunnel\b", "opens a public tunnel"),
    (r"(^|[\s/])publish\.sh\b", "publishes"),
]

# A guardrail is saved only by the operator, from the cockpit (#1396). The dispatcher accepts that
# save only with the cockpit token, which only the engine sends, so an agent is denied the token
# file and the route by name: in a command, a path, a URL or an MCP call. Edit and Write content is
# not checked, so the cockpit's own code can still name the route. A name match is a speed bump,
# not a wall: an agent set on it could spell the name another way (operator decision 2026-10-16).
COCKPIT_ONLY = re.compile(r"cockpit[-_]token|guardrail[-_]save|X-Nomad-Cockpit", re.I)
PATH_KEYS = ("file_path", "path", "notebook_path", "url", "pattern", "glob")

# A command that touches a prod target: a deploy-ish tool plus a prod/production name segment.
PROD_TOOLS = re.compile(r"\b(ssh|scp|rsync|kubectl|helm|terraform|docker|psql|mysql)\b")
PROD_NAME = re.compile(r"(^|[\s\-_./:@=])(prod|production)($|[\s\-_./:])", re.I)

TEMP_PREFIXES = ("/tmp/", "/var/tmp/")
DISPOSABLE = {"__pycache__", "node_modules", ".pytest_cache", ".mypy_cache", ".ruff_cache"}

RED_KEYWORDS = re.compile(
    r"\b(push|publish|upload|funnel|tunnel|DROP|TRUNCATE|DELETE|rm|prune|destroy|release)\b",
    re.I)


def segments(command):
    """Split a shell command into simple commands (lists of words). None if unparseable.

    A newline separates commands in the shell, but shlex reads it as plain whitespace, which
    would hide a `git push` on the line after an `echo`. So newlines become `;` first, after
    joining backslash continuations. Inside quotes that only changes a string's content."""
    command = command.replace("\\\n", " ").replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lex.whitespace_split = True
        words = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for w in words:
        if w in SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def strip_prefix(seg):
    """Drop VAR=x assignments and wrappers (sudo, env, time, nohup) in front of a command."""
    while seg and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0])
                   or seg[0] in ("sudo", "env", "time", "nohup", "command", "exec")):
        seg = seg[1:]
    return seg


def git_sub(seg):
    """(subcommand, args) for a git command, skipping global options like -C and -c."""
    i = 1
    while i < len(seg) and seg[i].startswith("-"):
        i += 2 if seg[i] in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(seg):
        return None, []
    return seg[i], seg[i + 1:]


def is_force_push(args):
    for a in args:
        if a in ("-f", "--force", "--force-with-lease", "--force-if-includes", "--mirror"):
            return True
        if a.startswith("--force-with-lease=") or a.startswith("--force"):
            return True
        if re.match(r"^-[a-zA-Z]*f[a-zA-Z]*$", a):          # bundled short flags, e.g. -uf
            return True
        if a.startswith("+") and len(a) > 1:                # +refspec forces that ref
            return True
        if a.startswith("--delete") or a == "-d":
            return True                                     # deleting a remote branch
        if a.startswith(":") and len(a) > 1:                # :branch deletes it
            return True
    return False


def rm_is_red(args):
    recursive = any(a in ("-r", "-R", "--recursive") or
                    (re.match(r"^-[a-zA-Z]+$", a) and ("r" in a or "R" in a)) for a in args)
    if not recursive:
        return False
    targets = [a for a in args if not a.startswith("-")]
    if not targets:
        return False
    for t in targets:
        p = os.path.normpath(os.path.expanduser(t))
        if p.startswith(TEMP_PREFIXES) and ".." not in t:
            continue
        if os.path.basename(p) in DISPOSABLE:
            continue
        return True
    return False


def judge_segment(seg):
    """('deny'|'ask', reason) for one simple command, or None."""
    seg = strip_prefix(seg)
    if not seg:
        return None
    prog = os.path.basename(seg[0])
    args = seg[1:]

    if prog == "git":
        sub, rest = git_sub(seg)
        if sub == "push":
            if is_force_push(rest):
                return "deny", "force-pushes or deletes a remote ref. Never, per SDLC.md"
            return "ask", "pushes to a remote"
        if sub == "send-email":
            return "ask", "sends email"
        return None

    if prog == "gh":
        if len(args) >= 2 and args[0] == "pr" and args[1] in ("create", "merge", "close", "review"):
            return "ask", f"gh pr {args[1]} acts on GitHub"
        if len(args) >= 2 and args[0] == "release" and args[1] in ("create", "delete", "upload", "edit"):
            return "ask", f"gh release {args[1]} publishes"
        if len(args) >= 2 and args[0] == "repo" and args[1] in ("create", "delete", "edit", "archive", "rename"):
            return "ask", f"gh repo {args[1]} changes a repository"
        if args and args[0] == "api" and any(
                a.upper() in ("POST", "PUT", "PATCH", "DELETE") or
                re.match(r"^(-X|--method)=?(POST|PUT|PATCH|DELETE)$", a, re.I) for a in args):
            return "ask", "gh api writes to GitHub"
        return None

    if prog in ("npm", "pnpm", "yarn") and args[:1] == ["publish"]:
        return "ask", "publishes a package"
    if prog == "twine" and args[:1] == ["upload"]:
        return "ask", "publishes a package"
    if prog == "cargo" and args[:1] == ["publish"]:
        return "ask", "publishes a crate"

    if prog == "docker":
        if args[:1] == ["push"]:
            return "ask", "pushes an image"
        if args[:2] == ["volume", "rm"] or args[:2] == ["volume", "prune"]:
            return "ask", "deletes Docker volumes"
        if args[:2] == ["system", "prune"] and any(a in ("--volumes", "-a", "--all") for a in args):
            return "ask", "prunes Docker data"
        if args[:1] == ["compose"] and "down" in args and any(
                a in ("-v", "--volumes") for a in args):
            return "ask", "removes compose volumes"
        return None

    if prog == "terraform" and args[:1] and args[0] in ("apply", "destroy"):
        return "ask", f"terraform {args[0]}"
    if prog == "kubectl" and args[:1] and args[0] in ("apply", "delete", "replace", "scale"):
        return "ask", f"kubectl {args[0]}"

    if prog == "rm" and rm_is_red(args):
        return "ask", "deletes a directory tree outside temp paths"
    if prog == "crontab" and "-r" in args:
        return "ask", "removes every cron job"
    return None


HEREDOC = re.compile(r"<<-?[ \t]*(['\"]?)(\w+)\1[^\n]*\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)


def without_heredocs(command):
    """The command with heredoc bodies cut out, so a Python script fed to `python3 -` isn't
    parsed as shell. The raw text is still checked for SQL and the other RED_TEXT patterns."""
    return HEREDOC.sub("<<HEREDOC", command)


def judge_bash(command):
    if not command.strip():
        return None
    segs = segments(without_heredocs(command))
    if segs is None:
        if RED_KEYWORDS.search(command):
            return "ask", "couldn't parse this command and it mentions a red-tier action"
        return None

    worst = None
    for seg in segs:
        verdict = judge_segment(seg)
        if verdict and verdict[0] == "deny":
            return verdict
        worst = worst or verdict
    if worst:
        return worst

    for pat, why in RED_TEXT:
        if re.search(pat, command, re.I):
            return "ask", why
    if PROD_TOOLS.search(command) and PROD_NAME.search(command):
        return "ask", "touches a target named prod/production"
    return None


# ── PowerShell (Windows sessions) ──
#
# Same red tier, PowerShell syntax. Commands are split on ; | && || newlines, braces and
# parentheses, with ` as the escape character so C:\paths stay literal. Native programs (git, gh,
# docker...) go through judge_segment like in bash; cmdlets with their own red forms are below.
# Nested shells (cmd /c, bash -c, wsl, powershell -c) are judged by the rules of their shell.

PS_SEPARATORS = SEPARATORS | {"{", "}"}
PS_REMOVE = {"remove-item", "rm", "del", "erase", "rd", "rmdir", "ri"}
PS_WRITE = {"set-content", "sc", "add-content", "ac", "out-file", "new-item", "ni", "copy-item",
            "cp", "copy", "cpi", "move-item", "mv", "move", "mi", "tee-object", "tee",
            "remove-item", "ri", "del", "rm"}
PS_WEB = {"invoke-restmethod", "irm", "invoke-webrequest", "iwr", "curl", "wget"}
PS_PATH_PARAMS = {"-path", "-literalpath", "-lp", "-pspath"}
PS_VALUE_PARAMS = {"-filter", "-include", "-exclude", "-credential", "-stream"}
# Hosts a write to is red: the same places the bash rules and RED_TOOLS cover.
RED_HOSTS = re.compile(
    r"^https?://([^/]*\.)?(api\.github\.com|uploads\.github\.com|github\.com|slack\.com"
    r"|gmail\.googleapis\.com|www\.googleapis\.com|registry\.npmjs\.org|upload\.pypi\.org"
    r"|crates\.io)(:\d+)?(/|$)", re.I)
PS_KEYWORDS = re.compile(RED_KEYWORDS.pattern + r"|remove-item|runas|executionpolicy|\$profile",
                         re.I)


def ps_segments(command, escape="`"):
    """Split a PowerShell command into simple commands. None if unparseable. cmd.exe lines
    (inside `cmd /c`) split the same way with ^ as the escape."""
    command = command.replace(escape + "\r\n", " ").replace(escape + "\n", " ")
    command = command.replace("\r\n", "\n").replace("\n", " ; ")
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}")
        lex.whitespace_split = True
        lex.escape = escape
        words = list(lex)
    except ValueError:
        return None
    out, cur = [], []
    for w in words:
        if w in PS_SEPARATORS or set(w) <= set(";&|(){}"):
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    if cur:
        out.append(cur)
    return out


def ps_prog(word):
    """`C:\\Program Files\\Git\\cmd\\git.exe` -> `git`."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def ps_temp(target):
    t = target.replace("\\", "/").lower().strip("'\"")
    if ".." in t:
        return False
    if os.path.basename(t.rstrip("/")) in DISPOSABLE:
        return True
    return (t.startswith(("$env:temp/", "$env:tmp/", "c:/windows/temp/", "/tmp/", "/var/tmp/"))
            or "/appdata/local/temp/" in t)


def ps_remove_is_red(args):
    """Remove-Item (or an alias) with -Recurse outside temp paths, or with no path at all
    (it then deletes whatever the pipeline feeds it)."""
    recursive, targets, i = False, [], 0
    while i < len(args):
        a = args[i]
        if a.startswith("-") and len(a) > 1:
            name, _, inline = a.lower().partition(":")
            if (len(name) > 1 and "-recurse".startswith(name)) or name in ("-rf", "-fr"):
                recursive = True                            # -rf is bash habit, asked anyway
            elif name in PS_PATH_PARAMS:
                if inline:
                    targets.append(a.split(":", 1)[1])
                elif i + 1 < len(args):
                    targets.append(args[i + 1])
                    i += 1
            elif name in PS_VALUE_PARAMS and not inline:
                i += 1
        else:
            targets.extend(t for t in a.split(",") if t)
        i += 1
    if not recursive:
        return False
    return not targets or not all(ps_temp(t) for t in targets)


def ps_web_is_red(args):
    method = None
    for i, a in enumerate(args):
        low = a.lower()
        if low.startswith("-method"):
            method = low.split(":", 1)[1] if ":" in low else (args[i + 1].lower()
                                                              if i + 1 < len(args) else "")
    if method not in ("post", "put", "patch", "delete"):
        return False
    return any(RED_HOSTS.match(a.strip("'\"")) or (a.lower().startswith("http") and
                                                   PROD_NAME.search(a)) for a in args)


def cmd_segment(seg):
    """One cmd.exe command (inside `cmd /c`)."""
    prog = ps_prog(seg[0])
    args = seg[1:]
    if prog in ("rd", "rmdir", "del", "erase") and any(a.lower() == "/s" for a in args):
        targets = [a for a in args if not a.startswith("/")]
        if not targets or not all(ps_temp(t) for t in targets):
            return "ask", f"{prog} /s deletes a directory tree outside temp paths"
        return None
    return judge_segment([prog] + args)


def nested(prog, args, depth):
    """The verdict for a shell started from PowerShell, judged as that shell."""
    low = [a.lower() for a in args]
    if prog in ("cmd",):
        for i, a in enumerate(low):
            if a in ("/c", "/k"):
                inner = " ".join(args[i + 1:])
                segs = ps_segments(inner, escape="^")
                if segs is None:
                    return ("ask", "couldn't parse this cmd command and it mentions a red-tier "
                            "action") if PS_KEYWORDS.search(inner) else None
                return worst_of(cmd_segment(s) for s in segs if s)
        return None
    if prog in ("bash", "sh", "zsh"):
        for i, a in enumerate(args):
            if a == "-c" or (re.match(r"^-[a-z]*c$", a) and a.startswith("-")):
                return judge_bash(" ".join(args[i + 1:i + 2]))
        return None
    if prog == "wsl":
        i = 0
        while i < len(args):
            a = low[i]
            if a in ("--unregister",):
                return "ask", "wsl --unregister deletes a distro and its data"
            if a in ("-d", "--distribution", "-u", "--user", "--cd", "--shell-type"):
                i += 2
                continue
            if a in ("-e", "--exec"):
                return wsl_command(args[i + 1:], True, depth)
            if a == "--":
                return wsl_command(args[i + 1:], False, depth)
            if a.startswith("-"):
                i += 1
                continue
            return wsl_command(args[i:], False, depth)
        return None
    if prog in ("powershell", "pwsh"):
        for i, a in enumerate(low):
            if a in ("-c", "-command", "-com", "-comm", "-comma", "-comman"):
                return judge_powershell(" ".join(args[i + 1:]), depth + 1)
            if a in ("-encodedcommand", "-enc", "-e", "-ec"):
                return "ask", "runs an encoded command the gate can't read"
        return None
    return None


def wsl_command(argv, exec_form, depth):
    """What wsl runs: argv as is (-e), or a command line for the distro's shell. A shell at the
    front (wsl -e bash -c '...') is judged by what it runs, not as a bare `bash`."""
    if argv and ps_prog(argv[0]) in ("bash", "sh", "zsh"):
        return nested(ps_prog(argv[0]), argv[1:], depth + 1)
    return judge_bash(shlex.join(argv) if exec_form else " ".join(argv))


def worst_of(verdicts):
    worst = None
    for v in verdicts:
        if v and v[0] == "deny":
            return v
        worst = worst or v
    return worst


def ps_segment(seg, depth):
    # $x = git push  /  $x=git push
    if len(seg) > 2 and seg[0].startswith("$") and seg[1] == "=":
        seg = seg[2:]
    elif seg and re.match(r"^\$[\w:]+=.", seg[0]):
        seg = [seg[0].split("=", 1)[1]] + seg[1:]
    if not seg:
        return None
    prog = ps_prog(seg[0])
    args = seg[1:]
    low = [a.lower() for a in args]

    if prog in ("cmd", "bash", "sh", "zsh", "wsl", "powershell", "pwsh"):
        return nested(prog, args, depth)
    if prog in PS_REMOVE:
        if ps_remove_is_red(args):
            return "ask", "deletes a directory tree outside temp paths"
        return None
    if prog in PS_WEB:
        if ps_web_is_red(args):
            return "ask", "writes to a remote service over HTTP"
        return None
    if prog == "set-executionpolicy":
        return "ask", "changes the PowerShell execution policy"
    if prog in ("start-process", "saps", "start") and any(
            a.startswith("-verb") for a in low) and "runas" in low:
        return "ask", "starts a process as administrator"
    if prog in PS_WRITE and any(a.startswith("$profile") for a in low):
        return "ask", "edits the PowerShell profile"
    return judge_segment([prog] + args)


def judge_powershell(command, depth=0):
    if not command.strip():
        return None
    if depth > 4:
        return "ask", "nests shells too deep to judge"
    segs = ps_segments(command)
    if segs is None:
        if PS_KEYWORDS.search(command):
            return "ask", "couldn't parse this command and it mentions a red-tier action"
        return None
    verdict = worst_of(ps_segment(s, depth) for s in segs)
    if verdict:
        return verdict
    if re.search(r">>?\s*\$profile\b", command, re.I):
        return "ask", "edits the PowerShell profile"
    for pat, why in RED_TEXT:
        if re.search(pat, command, re.I):
            return "ask", why
    if PROD_TOOLS.search(command) and PROD_NAME.search(command):
        return "ask", "touches a target named prod/production"
    return None


def cockpit_only(tool, args):
    """The text an agent could reach the cockpit token or the guardrail save through."""
    if tool in ("Bash", "PowerShell"):
        return args.get("command") or ""
    if tool.startswith("mcp__"):
        return json.dumps(args)
    return " ".join(str(args.get(k) or "") for k in PATH_KEYS)


def judge(data):
    tool = data.get("tool_name") or ""
    args = data.get("tool_input") or {}
    if isinstance(args, dict) and COCKPIT_ONLY.search(cockpit_only(tool, args)):
        return "deny", "reaches the cockpit token or the guardrail save, which only the operator uses"
    if RED_TOOLS.match(tool):
        return "ask", f"{tool.split('__')[-1]} sends or shares outside this machine"
    if tool == "Bash":
        return judge_bash((data.get("tool_input") or {}).get("command") or "")
    if tool == "PowerShell":
        return judge_powershell((data.get("tool_input") or {}).get("command") or "")
    return None


def main():
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return
    verdict = judge(data)
    if not verdict:
        return
    kind, why = verdict
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": kind,
        "permissionDecisionReason": f"SDLC red tier: this {why}."
        + ("" if kind == "deny" else " Approve to proceed."),
    }}))


if __name__ == "__main__":
    main()
