# NOMAD — VS Code extension

NOMAD in the editor, where you actually work. Open any project's VS Code window (in WSL) and the
extension identifies the project from its `Nomad.md` marker and shows **that project's** live state
— then lets you act on it without switching to a browser.

## What it gives you

- **This Project** view — project name + **git state** (branch · ✎dirty · ↑ahead ↓behind · last
  commit), rolled-up **milestone/task progress**, and each milestone → its tasks with status icons.
- **Approval Queue** view — the live pipeline **human gate**; approve ✓ / reject ✗ inline (→ engine
  `/resume`). This is the same queue the console shows.
- **Platform** view — NOMAD **service health** (up/offline + latency), from the console.
- **Status bar** — `◈ NOMAD · <project> · <pct>% · ⚠ <n>` at a glance; turns amber when something
  needs you (pending approval or a service down). Click it for the action menu.
- **Commands** (Command Palette or the status-bar menu):
  - **Ask NOMAD…** — chat through the console brain (intent router + memory + gate).
  - **Dispatch Builder into this repo…** — headless Claude Code makes uncommitted edits.
  - **Plan a task (read-only)…** — dry-run plan.
  - **Plan this project** — decompose into milestones + tasks (engine `/plan-project`).
  - **Run next task through the pipeline** — queue the next backlog task at the gate.
  - Per-task **retry / skip** on blocked tasks; **Open command console / live voice**.

Everything reads the **existing** backend — the console (`:1701`) and v2 engine (`:8099`). No new
services; the extension is a thin, fail-soft client (if a service is down the views degrade instead
of erroring).

## How a workspace maps to a project

The extension reads a `Nomad.md` at the workspace root for the project `name` (falls back to the
folder name), then matches it to a mission-control goal to pull milestones/tasks. Git state is read
locally in the repo. No marker → the view shows a hint to add one.

## Install

```bash
cd nomad-vscode
npm install
npm run bundle          # -> out/extension.js
npm run package         # -> nomad.vsix
code --install-extension nomad.vsix
```

Then reload VS Code. Open a project folder that has a `Nomad.md` and the **NOMAD** icon appears in
the activity bar.

> Running in **WSL**: install it into the WSL remote (run `code --install-extension` from inside
> WSL, as above) so it can reach `localhost:1701/8099` and read the repo's git state.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `nomad.consoleUrl` | `http://localhost:1701` | Console: chat, approvals, services, dispatch. |
| `nomad.engineUrl` | `http://localhost:8099` | v2 engine: projects, tasks, gate. |
| `nomad.refreshSeconds` | `30` | Auto-refresh interval (0 disables). |

## Develop

`npm run watch` (esbuild) then press **F5** to launch an Extension Development Host. `npm run
typecheck` for types. The bundle marks `vscode` external; there are no runtime dependencies.
