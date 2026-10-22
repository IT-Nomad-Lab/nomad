#!/usr/bin/env python3
"""Claude Code token use from the local transcripts (#1091).

Reads ~/.claude/projects/**/*.jsonl and totals the `usage` of every assistant call in the last
N days: by model, by project folder, the size of each session's first call (a proxy for the fixed
context a session starts with), and the largest sessions. Read-only, stdlib only.

    python3 scripts/claude-usage.py              # last 14 days
    python3 scripts/claude-usage.py --days 1 --top 10

Calls are de-duplicated on (message id, request id): Claude Code writes one line per content
block, each carrying the same usage. Token counts, not dollars; on the Max plan nothing is billed
per token, and Anthropic doesn't publish how each token type weighs against the plan limits.
"""
import argparse
import collections
import datetime as dt
import glob
import json
import os
import statistics

FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def context(c):
    """Input-side tokens of a call or a total: uncached + cache writes + cache reads."""
    return c["input_tokens"] + c["cache_creation_input_tokens"] + c["cache_read_input_tokens"]


def scan(base, since):
    by_model = collections.defaultdict(collections.Counter)
    by_project = collections.defaultdict(collections.Counter)
    sessions, first_main, first_sub, seen = {}, [], [], set()
    for path in glob.glob(os.path.join(base, "**", "*.jsonl"), recursive=True):
        if os.path.getmtime(path) < since.timestamp():
            continue
        rel = os.path.relpath(path, base)
        project = rel.split(os.sep)[0]
        is_sub = "/subagents/" in rel or os.path.basename(path).startswith("agent-")
        total, model, first_done = collections.Counter(), None, False
        with open(path, errors="ignore") as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                msg = ev.get("message") or {}
                usage = msg.get("usage") if ev.get("type") == "assistant" else None
                if not usage or msg.get("model") == "<synthetic>":
                    continue
                key = (msg.get("id"), ev.get("requestId"))
                if msg.get("id") and key in seen:
                    continue
                seen.add(key)
                try:
                    if dt.datetime.fromisoformat(ev["timestamp"].replace("Z", "+00:00")) < since:
                        continue
                except (KeyError, ValueError):
                    pass
                model = msg.get("model", "?")
                call = collections.Counter({k: usage.get(k) or 0 for k in FIELDS})
                call["calls"] = 1
                by_model[model] += call
                by_project[project] += call
                total += call
                if not first_done:
                    (first_sub if is_sub or ev.get("isSidechain") else first_main).append(context(call))
                    first_done = True
        if total:
            sessions[rel] = (total, model, is_sub)
    return by_model, by_project, sessions, first_main, first_sub


def table(title, rows, top):
    print(f"\n## {title}")
    print(f"{'':50s} {'calls':>7} {'uncached':>9} {'cache write':>12} {'cache read':>14} {'output':>10} {'read%':>6}")
    for name, c in sorted(rows.items(), key=lambda kv: -context(kv[1]))[:top]:
        share = 100 * c["cache_read_input_tokens"] / max(1, context(c))
        print(f"{name[:50]:50s} {c['calls']:7d} {c['input_tokens']:9d} {c['cache_creation_input_tokens']:12d} "
              f"{c['cache_read_input_tokens']:14d} {c['output_tokens']:10d} {share:6.1f}")


def spread(name, values):
    if values:
        v = sorted(values)
        print(f"{name}: n={len(v)} median={statistics.median(v):.0f} p90={v[9 * len(v) // 10]} max={v[-1]}")


def main():
    ap = argparse.ArgumentParser(description="Claude Code token use from the local transcripts.")
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--base", default=os.path.expanduser("~/.claude/projects"))
    args = ap.parse_args()
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    by_model, by_project, sessions, first_main, first_sub = scan(args.base, since)
    if not sessions:
        print(f"no assistant calls with usage in the last {args.days:g} days under {args.base}")
        return
    print(f"last {args.days:g} days: {len(sessions)} transcripts with usage")
    table("total", {"TOTAL": sum(by_model.values(), collections.Counter())}, 1)
    table("by model", by_model, args.top)
    table("by project folder", by_project, args.top)
    print("\n## first call of a session (fixed context + first prompt)")
    spread("main sessions", first_main)
    spread("subagents", first_sub)
    print("\n## largest sessions")
    for rel, (c, model, is_sub) in sorted(sessions.items(), key=lambda kv: -context(kv[1][0]))[:args.top]:
        print(f"{rel[:80]:80s} {model} {'sub ' if is_sub else ''}calls={c['calls']} "
              f"context={context(c)} avg/call={context(c) // c['calls']} out={c['output_tokens']}")


if __name__ == "__main__":
    main()
