# nomad-console — the operator dashboard (LCARS)

The operator-facing command center: a Star-Trek-LCARS-styled web UI plus a FastAPI backend that
serves telemetry, mission control, an intent-routed NOMAD chat, and on-device voice. It's how a
human watches and drives the system without living in a terminal.

## What it does

- **Telemetry** — system/GPU stats, service health tiles (LiteLLM, bridge, crew, n8n, engine,
  voice…), live sparklines.
- **Mission control** — Projects, Agent roster, Activity (mission log), and the Approval Queue
  (red-alert when something is pending), read from NocoDB or Notion (selected by `NOMAD_SOURCE`).
- **NOMAD chat** — text + voice, with an **intent router** (`intents.py`) that turns natural
  language into ACTIONS rather than just replies (run diagnostics, research a topic, start a
  project, capture a pipeline goal, approve/reject the gate).
- **Voice** — push-to-talk 🎤 + "Hey Jarvis" wake word 👂 + spoken replies 🔊 (all proxied to
  `nomad-voice`), plus a **live-voice** button ▮▮ that opens a **real-time, interruptible**
  conversation (WebRTC → `nomad-voice`). The real-time reply step runs through this same
  `/api/chat` brain, so voice gets the intent router, memory, and human gate too.
- **Conversational memory** (`memory.py`) — every turn is embedded (`nomic-embed-text` on Ollama)
  and stored in Qdrant; relevant past turns are recalled into context. Fail-open: chat still
  works if memory is down.
- **Project terminal** — embeds the interactive `claude` PTY served by the
  [`dispatcher/`](../dispatcher/) `termd` daemon (xterm.js over a proxied WebSocket).

## Files

| File | Role |
|---|---|
| `server.py` | FastAPI backend: telemetry, services, projects/agents/activity/approvals, chat proxy, voice proxy, terminal proxy, conditional Basic Auth. |
| `intents.py` | Pure, pattern-based natural-language → action classifier (zero added latency). |
| `memory.py` | Long-term conversational memory over Qdrant + Ollama (httpx-only, fail-open). |
| `sync_projects.py` | Upserts `Nomad.md` project markers into mission control (NocoDB or Notion). |
| `static/` | The LCARS UI (`index.html`, `lcars.css`, `app.js`), PWA manifest/icons, vendored xterm.js, voice assets. |
| `publish.sh` | Opens an auth-protected Cloudflare quick tunnel over the internal docker network. |

## API surface (selected)

`GET /healthz` (always open) · `GET /api/version` · telemetry/services/projects/agents/activity/
approvals endpoints · `POST /api/chat` (intent-routed) · `/api/tts` + `/api/stt` (proxied to
`nomad-voice`) · `/ws/terminal` (proxied to `termd`).

## Run it

```bash
docker compose up -d nomad-console     # → http://127.0.0.1:1701
```

## Access model & auth

The host port binds to **`127.0.0.1:1701`** — not reachable from the LAN/internet. Auth is
**conditional**: a direct local hit runs login-free; any request arriving via a proxy/tunnel
(detected from forwarding headers) **requires HTTP Basic Auth and fails closed (403) if creds
aren't set**, so it can't be exposed without a password. `NOMAD_FORCE_AUTH=1` forces login even
locally. `/healthz` is always open.

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `NOMAD_SOURCE` | `notion` | Mission-control data source: `nocodb` or `notion`. |
| `NOMAD_MODEL` | `deep` | Default chat role alias. |
| `LITELLM_BASE_URL` / `LITELLM_MASTER_KEY` | — | Model gateway for chat. |
| `NOMAD_AUTH_USER` / `NOMAD_AUTH_PASS` | — | Basic Auth creds (required for proxied access). |
| `NOMAD_FORCE_AUTH` | off | Force login even for local requests. |
| `QDRANT_URL` / `OLLAMA_URL` / `NOMAD_EMBED_MODEL` | service hosts / `nomic-embed-text` | Conversational memory. |
| `NOMAD_ENGINE_URL` / `NOMAD_CREW_URL` / `NOMAD_VOICE_URL` / `NOMAD_DISPATCH_URL` / `NOMAD_TERM_URL` | service hosts | Companion services. |
| `NC_BASE_URL` / `NC_API_TOKEN` | — | NocoDB (when `NOMAD_SOURCE=nocodb`). |
| `NOTION_TOKEN` / `NOTION_DB_*` | — | Notion (when `NOMAD_SOURCE=notion`). |
