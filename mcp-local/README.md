# mcp-local — NOMAD's local agents, exposed to Claude Code

Phase 1 of using **Claude Code as the orchestrator** over local + cloud agents. These are MCP
servers Claude Code loads (via the repo's `.mcp.json`) so it can call your local capabilities as
tools — keeping cheap/private/bulk work on your hardware while Claude conducts.

## Tools

**`nomad-local`** (`nomad_local.py`) — delegate to a local model on your GPU (native Ollama):
- `local_llm(prompt, system, model, max_tokens)` — run a prompt locally. `model` is a role alias
  (`fast`=llama3.1:8b, `private`=deepseek-r1:32b, `code`=qwen2.5-coder:32b) or a full Ollama tag.
- `list_local_models()` — what's installed.

**`nomad-scrape`** (`nomad_scrape.py`) — web research from the editor (the nomad-scraper service):
- `scrape(url, prompt)` — scrape one page.
- `web_search(query, prompt)` — search + scrape top results.

**`nomad-diagram`** (`nomad_diagram.py`) — render diagrams + charts from a TEXT spec (the Kroki
service), saved into the current project. **No image model** — a layout engine places everything,
so arrows are routed and text never overflows (the fix for matplotlib diagrams / diffusion "graphs"):
- `render_diagram(source, diagram_type, output_format, subdir, filename)`.
- `diagram_type`: `mermaid` | `graphviz` | `d2` | `plantuml` (box/arrow diagrams) | `vegalite`
  (data charts), + other Kroki types. `output_format`: `svg` (default) | `png` (mermaid → svg).
- Claude writes the spec (Mermaid/DOT/Vega-Lite); Kroki renders it deterministically.

**`nomad-image`** (`nomad_image.py`) — generate an image on the LOCAL GPU (ComfyUI/SDXL) and save
it into the current project. Free, no quota — **the default** for image work:
- `generate_image(prompt, subdir, filename)`.

**`nomad-codex-image`** (`nomad_codex_image.py`) — generate an image with **OpenAI Codex** instead,
on the ChatGPT subscription. Ask for it BY NAME; it is deliberately not a silent fallback in the
`generate_image` chain, because it spends subscription quota:
- `codex_image(prompt, subdir, filename)`.
- Use it when prompt adherence matters. On the same brief SDXL gave a desktop chassis at dusk;
  Codex gave the rack, the single amber LED and the rain-streaked window — and it renders legible
  text, which SDXL does not.
- It shells out to `codex exec -s workspace-write` in a scratch dir, NOT through `codex-bridge`
  (:8089) — the bridge is `-s read-only` on purpose and generation has to write a file.

**`nomad-audio`** (`nomad_audio.py`) — generate music on the LOCAL GPU (the nomad-audio service,
:8220, ACE-Step v1, Apache-2.0) and save the WAV into the current project:
- `generate_music(prompt, lyrics, duration, seed, subdir, filename)`.
- `prompt` = style tags ("calm fantasy, celtic harp, soft flute, 80 bpm"); empty `lyrics` = an
  instrumental; `duration` up to 240 s; a fixed `seed` reproduces the take byte for byte.
- Never overwrites: an existing name gets `-2`, `-3`… so a game's live `bgm.wav` is safe.
- Limits: text-only (no covers/remixes of a real melody), bpm is a hint, no seamless loops, not
  for sub-second SFX. Needs `systemctl --user start nomad-audio` if the service is down.

**`nomad-detect`** (`nomad_detect.py`) — check a draft for AI tells before it ships (the
nomad-detect service, :8214):
- `detect_ai(text)` — Binoculars score on the GPU + style linter + verdict.
- `lint_style(text)` — style only, instant, returns the exact lines to fix.

**`nomad-penpot-nav`** (`nomad_penpot_nav.py`) — lets the Penpot MCP (`penpot`) work on any file.
Penpot's plugin API can't open another file, so this server keeps its own Chromium tab
(Playwright, profile in `~/.config/nomad/penpot-nav-profile`) and moves it between files by URL;
Penpot's bundled MCP plugin reconnects on each load.
- `penpot_list_files(query)`, `penpot_open_file(file, page)`, `penpot_create_file(name, project)`,
  `penpot_status()`. Open a file, then use the `penpot` tools as usual.
- One-time login: `.venv/bin/python nomad_penpot_nav.py login` (a window opens; the session is
  kept in the profile, no password stored). MCP must be enabled in Penpot → Integrations.
- The Penpot MCP key allows one plugin connection: close Penpot files in your own browser while
  automating. The window is visible via WSLg; `NOMAD_PENPOT_HEADLESS=1` hides it.

These auto-detect their endpoint (Ollama on the Windows host; the scraper/Kroki containers on
loopback or the host gateway), so they work whether Claude Code runs in WSL or natively. The
`nomad-diagram` tool needs the `kroki` + `kroki-mermaid` compose services up
(`docker compose up -d kroki kroki-mermaid`).

## Setup
Deps live in `.venv` (gitignored): `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`
(plus `.venv/bin/python -m playwright install chromium` for `nomad-penpot-nav`).
`.mcp.json` at the repo root points Claude Code at `.venv/bin/python` running each server.
After it's created, **enable the servers when Claude Code prompts** (project MCP servers need
approval), or reload the window.

## Notes
- `nomad-local` is verified end-to-end. `nomad-scrape` needs the editor's process to reach the
  scraper container (`:8210`) — if it reports "scraper error", we bind the scraper to a
  WSL-reachable interface (+ a token) as a quick follow-up.
- Next phases: expose the repo Builder + the gated pipeline as MCP, and move the approval gate into
  a `PreToolUse` hook so irreversible actions pause inside Claude Code.
