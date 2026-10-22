---
name: graphify
description: "Query and update a graphify knowledge graph (graphify-out/) from a Windows or WSL session. graphify runs in WSL through nomad-run, so Windows never installs Python. Use for questions about a codebase when graphify-out/graph.json exists, and to build or refresh the code graph."
---

# graphify through WSL

graphify lives in WSL only (`uv tool install graphifyy` there). This skill runs its CLI through
the plugin's launcher, in the session's current directory, on both hosts:

```bash
"${CLAUDE_PLUGIN_ROOT}/bin/nomad-run" graphify <command> [args]
```

On Windows, run it with the Bash tool (Git Bash), not PowerShell. The launcher passes the current
directory to WSL as `/mnt/c/...`, so run it from the project folder and give paths relative to it.
Paths graphify prints back are WSL paths: `/mnt/c/Users/...` is `C:\Users\...`.

**Never install graphifyy on Windows** (`pip install`, `uv tool install` in PowerShell or Git
Bash). A native Windows Python is a non-goal of the claude-parity setup. If the launcher says
graphify isn't installed, tell the operator to install it in WSL.

## Commands

| want | run |
|---|---|
| answer a question from the graph | `graphify query "<question>"` (add `--dfs` to trace one path, `--budget N` to cap) |
| how two things connect | `graphify path "A" "B"` |
| explain one node | `graphify explain "X"` |
| what a change to X affects | `graphify affected "X"` |
| build or refresh the code graph (no LLM) | `graphify update .` |
| re-cluster and redo the report | `graphify cluster-only .` |
| add a web page to the corpus | `graphify add <url>` |
| is a semantic re-extraction pending | `graphify check-update .` |

**Fast path:** when `graphify-out/graph.json` exists and the user asks a question about the code,
run `query` at once. Don't rebuild first. Then read `graphify-out/GRAPH_REPORT.md` if the answer
needs the big picture.

## What this skill doesn't do

The full `/graphify` pipeline (semantic extraction of docs, papers and images by subagents, the
Obsidian vault, the wiki) is the upstream skill, which runs Python step by step on its host. From
Windows:
- for a code-only graph, `graphify update .` is enough;
- for docs, papers or images, open a WSL session in the same folder (`/mnt/c/...`) and run
  `/graphify` there. The graph it writes is in the folder, so Windows sessions query it after.
