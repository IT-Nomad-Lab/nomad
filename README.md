# Project NOMAD — Starter Kit
**Networked Operations & Management Assistant for Decisions**

Your personal AI orchestrator + autonomous agent team. This folder is the runnable
skeleton of the architecture in `NOMAD_Orchestrator_Architecture.md`.

```
nomad/
├─ docker-compose.yml      # the whole stack
├─ .env.example            # copy to .env and fill in
├─ litellm/config.yaml     # model routing (role aliases → real models)
├─ notion/
│  ├─ schema.md            # the 7 Mission-Control databases
│  └─ setup_notion.py      # creates them via the Notion API
└─ crew/                   # the CrewAI agent team
   ├─ agents.yaml          # manager + specialists
   ├─ tasks.yaml           # one milestone-execution pass
   ├─ crew.py              # builds & runs the crew
   ├─ server.py            # HTTP trigger for n8n
   ├─ tools/notion_tools.py   # shared memory + approval gate
   └─ tools/firefly_tool.py   # Adobe Firefly image generation
```

## What runs where (after `docker compose up -d`)

| Service | URL | Purpose |
|---|---|---|
| Open WebUI (chat/voice/mobile) | http://localhost:3000 | Talk to every model |
| LiteLLM gateway | http://localhost:4000 | The router (all brains) |
| n8n (automation + gates) | http://localhost:5678 | Integrations & approvals |
| Crew API | http://localhost:8001 | Trigger the agent team |
| Ollama | http://localhost:11434 | Local models |
| Qdrant | http://localhost:6333 | Vector memory |

---

## Setup, step by step

### 0. Prerequisites
- Docker Desktop with the **NVIDIA GPU** integration enabled (so Ollama sees your RTX 5090).
- API keys ready: Anthropic, OpenAI, Gemini, Adobe (client id/secret), a Notion
  integration token, a GitHub PAT.
- `cp .env.example .env` and fill everything in. Invent a long `LITELLM_MASTER_KEY`.

### 1. Local models
```bash
docker compose up -d ollama
docker exec -it nomad-ollama ollama pull qwen3.6:8b
docker exec -it nomad-ollama ollama pull qwen3.6:27b
docker exec -it nomad-ollama ollama pull qwen2.5-coder:32b
```
> Verify the exact tags on https://ollama.com/library — names evolve. Update both
> the pulls and `litellm/config.yaml` to match.

### 2. The gateway + chat
```bash
docker compose up -d litellm openwebui
```
Open http://localhost:3000. You should see the role models (deep, fast, code…) in
the dropdown. Try one of each to confirm routing works.

### 3. Notion mission control
```bash
pip install notion-client python-dotenv
python notion/setup_notion.py
```
Paste the printed database IDs into `.env`. (First create a Notion integration at
notion.so/my-integrations and share your parent page with it.)

### 4. Automation + gates (n8n)
```bash
docker compose up -d n8n
```
At http://localhost:5678, add credentials for Gmail, Google Calendar, Drive, Notion,
and GitHub. Build the **approval-gate** workflow: a webhook the crew calls for a
high-stakes action → a *Wait* node that pauses for your approval (email/Notion/
mobile) → execute on approve.

### 5. The agent crew
```bash
docker compose up -d crew
curl -X POST http://localhost:8001/run-milestone \
  -H "Content-Type: application/json" \
  -d '{"goal":"Launch the landing page","criteria":"Live, mobile-friendly, passes SEO check","milestone":"Draft copy and hero image"}'
```
Watch tasks appear in Notion and actions land in the Activity Log.

### 6. Mobile + remote
Install Tailscale on the laptop and your phone. Open WebUI installs as a PWA from
your laptop's Tailscale address — NOMAD in your pocket, securely.

---

## How autonomy + approvals work
- Agents act freely on anything reversible (Notion, research, drafts, GitHub
  branches/PRs, internal calendar blocks).
- For irreversible actions (external email, merge to main, publish, share, spend),
  an agent calls `request_approval` → a **Pending** row in the Approvals DB → you
  approve from Notion or your phone → n8n executes it. Nothing risky happens
  without your tap.
- Promote trusted action-types to "auto" later by relaxing the n8n gate.

## Security
- Real secrets live only in `.env` (git-ignore it). Never commit keys.
- Keep services on localhost / Tailscale; don't expose ports to the public internet.

## Things to verify before production
- Exact model strings in `litellm/config.yaml` against current provider docs.
- Firefly endpoint paths/payloads in `crew/tools/firefly_tool.py` against the
  current Firefly API version.
- Ollama model tags against ollama.com/library.
