# Project NOMAD — Personal AI Orchestrator
**Networked Operations & Management Assistant for Decisions**
### Full Architecture & Build Plan

**Owner:** Josias
**Machine:** Argus G835L laptop — RTX 5090 (24 GB VRAM), Intel Core Ultra 9 275HX, 64 GB DDR5, 4 TB SSD, Windows
**Goal:** A central orchestrator that routes every task to the best available brain — Claude, OpenAI, Gemini, Adobe, or a fast local model — behind one assistant you reach by chat, voice, CLI, or phone.
**Profile chosen:** Low-code / hybrid · all four interfaces · routing + project mgmt + research + automation · local model for speed & always-on.

---

## 1. The Core Idea

Think of this as a **brain stem with many brains attached**. You never talk to Claude or GPT directly. You talk to *one* assistant. Behind it, a router decides — per task — which model answers, whether it stays on your machine or goes to the cloud, and which tools or automations fire. Everything is containerized on your laptop, with secure remote access so "NOMAD" follows you to your phone.

Five layers:

1. **Interface layer** — how you talk to it (chat, voice, CLI, mobile).
2. **Orchestration layer** — the router/gateway that unifies every model behind one endpoint.
3. **Model layer** — local models (always-on, fast) + cloud models (Claude / OpenAI / Gemini / Adobe).
4. **Automation & agent layer** — workflows that take actions: email, files, web, schedules, project updates.
5. **Memory & knowledge layer** — per-project context, documents, and state the assistant remembers.

---

## 2. Recommended Stack (all low-code / self-hosted, Docker-based)

| Layer | Tool | Why it wins for you |
|---|---|---|
| **Gateway / Router** | **LiteLLM** (self-hosted proxy) | One OpenAI-compatible endpoint for *all* models — Claude, GPT, Gemini, and local Ollama. Built-in cost tracking, fallbacks, retries, virtual keys, routing aliases. Free, MIT-licensed, runs on your machine. |
| **Local model serving** | **Ollama** | Dead-simple always-on local serving. One command to pull/run models. Native integration with Open WebUI and n8n. |
| **Chat UI (+ mobile)** | **Open WebUI** | Polished chat front-end, installs as a phone PWA, built-in voice (Whisper STT + TTS), per-project knowledge bases. Points straight at LiteLLM so every model shows up in one dropdown. |
| **Automation & agents** | **n8n** | Low-code visual workflows. Scheduled jobs, webhooks, 400+ integrations (email, calendar, files, Slack, etc.), native Ollama + any-LLM nodes via LiteLLM. This is where "agents that do things" live. |
| **Voice / wake-word** | Open WebUI voice (fast start) → custom **openWakeWord + faster-whisper + Kokoro TTS** | Hands-free "true NOMAD." Start with the built-in button; graduate to a wake-word pipeline (~1–2s latency on your GPU). |
| **Memory / RAG** | **Qdrant** (vector DB) + **Postgres** | Long-term memory and per-project document recall. Open WebUI and n8n both plug into it. |
| **CLI** | **aichat** or **Open Interpreter** | Terminal access pointed at the same LiteLLM endpoint — same brains, scriptable. |
| **Remote access** | **Tailscale** | Private, encrypted tunnel so your phone reaches the laptop securely — no ports exposed to the internet. |
| **Observability** | **Langfuse** (optional) | See every call, token cost, and latency across all models in one dashboard. |
| **Packaging** | **Docker Compose** | One file brings the whole stack up/down. Mirrors the popular "local-ai-packaged" pattern. |

> Everything except the cloud APIs runs on your laptop at **$0/month**. Cloud models are pay-per-use only when the router decides they're worth it.

---

## 3. The Model Layer — Who Does What

Your machine's 24 GB VRAM comfortably runs a strong local model **always-on**. Use **Q4_K_M quantization** and **llama.cpp/Ollama with flash-attention** for best speed.

### Local models (private, fast, free)

| Model | Role | Notes |
|---|---|---|
| **Qwen3.6 8B** (or similar small reasoning model) | The **always-on workhorse**: routing decisions, quick Q&A, classification, voice replies, high-volume drafting | Tiny VRAM footprint, stays loaded, sub-second responses. This is your "fast NOMAD voice." |
| **Qwen3.6 27B** (reasoning, dense) | Heavier **local reasoning & summarization**, privacy-sensitive work, offline use | Currently the top general performer in the 24 GB class. Handles 64K context. |
| **Qwen2.5-Coder 32B** | **Coding** — refactors, tab-complete (FIM), code reasoning | Best local coding model in its tier. Load on demand. |

*Run the 8B always-on; swap the 27B / Coder 32B in when a task needs them (Ollama hot-swaps automatically).*

### Cloud models (the heavy hitters — routed only when worth it)

| Provider | Best used for |
|---|---|
| **Claude (Opus / Sonnet)** | Deep reasoning, long-form writing, agentic tool-use, software architecture, nuanced judgment |
| **OpenAI (GPT)** | Alternative reasoning, strong function-calling, vision, fallback when Claude is busy |
| **Gemini** | Massive context windows (huge documents/codebases), multimodal, Google-ecosystem tasks |
| **Adobe (Firefly / Express)** | **Creative/media generation** — images, design assets. Called from n8n via the Firefly API. *Note: Adobe is a creative engine, not a text-reasoning model — it slots into the media side of workflows, not the chat router.* |

### Routing strategy (the rules the orchestrator follows)

The router picks a model along four axes:

1. **By sensitivity** → anything private/confidential stays **local** (never leaves the machine).
2. **By volume/cost** → high-frequency, simple tasks go **local** to preserve cloud credits.
3. **By difficulty** → hardest reasoning, long-form, and agentic tasks go to **Claude / GPT / Gemini**.
4. **By capability** → huge-context jobs → **Gemini**; deep coding/architecture → **Claude** or local Coder; images → **Adobe**.

In LiteLLM you encode this as named aliases (e.g. `fast`, `deep`, `code`, `longdoc`, `private`, `image`) so every interface just asks for the *role* and the gateway maps it to the right model — and falls back automatically if one is rate-limited or down.

---

## 4. How a Request Flows

```
You (voice / chat / CLI / phone)
        │
        ▼
  Open WebUI  ──────────────►  n8n  (if the task is an "action" / automation)
        │                        │
        ▼                        ▼
   LiteLLM Gateway  ◄────────────┘     ← single endpoint, routing + fallback + cost tracking
        │
   ┌────┼─────────────┬──────────────┬─────────────┐
   ▼    ▼             ▼              ▼             ▼
 Ollama  Claude      OpenAI        Gemini        Adobe Firefly
(local)  (cloud)     (cloud)       (cloud)       (media)
   │
   └──► Qdrant + Postgres (memory / per-project knowledge)
```

A quick task ("summarize this") hits the local 8B and returns instantly. A hard task ("draft the architecture for project X") routes to Claude. "Generate a hero image" routes through n8n to Adobe. "Every morning, digest my projects and email me" is an n8n scheduled workflow that itself calls the router. Same front door for all of it.

---

## 5. Interfaces — All Four, One Brain

- **Chat (web/desktop):** Open WebUI in the browser. Model dropdown shows every brain. Per-project knowledge bases for RAG.
- **Voice ("true NOMAD"):** Phase 1 = Open WebUI's built-in mic button (Whisper + TTS). Phase 2 = wake-word pipeline (openWakeWord → faster-whisper → router → Kokoro TTS) for hands-free "Hey NOMAD."
- **CLI:** `aichat` (or Open Interpreter for agentic shell actions) pointed at the LiteLLM endpoint — scriptable, same models.
- **Mobile:** Install Open WebUI as a phone PWA; connect over **Tailscale** so it securely reaches your laptop. Optional: a **Telegram or Slack bot** wired into n8n as a lightweight mobile front-door for quick asks and notifications.

---

## 6. Automation & Agent Layer (n8n)

This is what makes it feel like NOMAD rather than just a chat box. In n8n you build visual workflows that **take actions**, each able to call any model through LiteLLM:

- **Project command center:** pull tasks/status from your tools, generate a daily digest, email or message it to you.
- **Inbox triage:** classify incoming email (local 8B), draft replies (Claude), flag urgent items.
- **Research agent:** scheduled or on-demand web research → summarized brief saved to a project folder.
- **Content pipeline:** outline (Claude) → draft → Adobe Firefly for imagery → publish/draft.
- **Scheduled rituals:** morning briefing, end-of-day wrap-up, weekly project review.

n8n's webhooks also let voice/CLI/mobile *trigger* these agents.

---

## 7. Phased Build Plan

You don't build this all at once. Each phase delivers a working capability.

**Phase 0 — Prerequisites (½ day)**
Install Docker Desktop, NVIDIA drivers + CUDA, Tailscale. Gather API keys (Claude, OpenAI, Gemini, Adobe). Create a project folder + `.env` for secrets.

**Phase 1 — Local brains (½ day)**
Install Ollama. Pull `qwen3.6:8b`, `qwen3.6:27b`, `qwen2.5-coder:32b`. Confirm always-on speed and VRAM headroom.

**Phase 2 — The orchestrator (1 day)**
Stand up LiteLLM proxy via Docker. Register all cloud providers + local Ollama under one endpoint. Define routing aliases (`fast`, `deep`, `code`, `longdoc`, `private`, `image`), fallbacks, and cost tracking. **This is the keystone** — once it's up, everything else just points here.

**Phase 3 — Chat + mobile (½ day)**
Deploy Open WebUI pointed at LiteLLM. Verify every model appears. Install as a phone PWA over Tailscale.

**Phase 4 — Voice (½–1 day)**
Enable Open WebUI built-in voice first. Then (optional) build the wake-word pipeline for hands-free.

**Phase 5 — Automation & agents (ongoing)**
Deploy n8n. Connect it to LiteLLM. Build your first 2–3 workflows (daily project digest, inbox triage, research agent).

**Phase 6 — Memory & knowledge (½ day)**
Add Qdrant + Postgres. Create per-project knowledge bases so the assistant recalls context across sessions.

**Phase 7 — Polish (ongoing)**
Add CLI, wire Langfuse for observability, refine routing rules as you learn which model wins which task.

---

## 8. Design Principles to Keep It Sane

- **One front door.** Never integrate a model into an app directly — always go through the gateway. Swapping models later becomes a config change, not a rewrite.
- **Local-first for the cheap and the private.** Reserve cloud credits for tasks that genuinely need frontier intelligence.
- **Everything in Docker Compose.** One file, reproducible, easy to back up and move.
- **Secrets never in code.** `.env` / a secrets store, and Tailscale instead of open ports.
- **Start narrow, expand.** A working chat-through-router beats a half-built NOMAD. Add voice and agents once the core is solid.

---

---

# PART II — The Autonomous Multi-Agent System

**Decisions locked:** integrations = Google Workspace + Notion + GitHub · structure = manager + specialists · autonomy = full, with approval gates only on high-stakes irreversible actions · all project types (software, content, business ops, research).

The vision: you write a **goal** and its **milestones** in Notion. A manager agent picks it up, breaks it into tasks, and delegates to a team of specialist agents that work together — researching, building, writing, scheduling, emailing — until the milestone is done. They only stop to ask you before doing something irreversible.

---

## 10. Integration Layer

All connections run through **n8n**, which has native, OAuth-based nodes for everything you use. This is also where the **approval gates** live.

| Service | What agents do with it | Autonomy |
|---|---|---|
| **Gmail** | Read/triage/label, draft replies, send | Draft freely · **send external = gated** |
| **Google Calendar** | Read availability, propose & create events, reschedule | Internal blocks free · **invites to outside people = gated** |
| **Google Meet** | Schedule meetings (via Calendar), attach links | Free |
| **Google Drive / Docs** | Create/read/edit docs, save research & deliverables | Free · **share externally = gated** |
| **Notion** | Read goals/milestones, create & update tasks, log activity | Free (this is mission control) |
| **GitHub** | Issues, branches, commits, PRs, code review | Branches/PRs free · **merge to main = gated** |

---

## 11. Notion as Mission Control (workspace blueprint)

Your Notion is empty — perfect. We build it as the **shared brain** both you and the agents read and write. Six linked databases:

1. **Projects** — top-level container. Properties: name, status, owner, linked Goals.
2. **Goals** — your north stars. Properties: description, success criteria, priority, target date, linked Project.
3. **Milestones** — checkpoints under a Goal. Properties: title, due date, status, linked Goal, % complete.
4. **Tasks** — the unit of agent work. Properties: title, **assigned agent**, status (Backlog → In Progress → Review → Done), dependencies, linked Milestone, output link.
5. **Agent Activity Log** — every action an agent takes, timestamped. Your audit trail and the "notify" channel.
6. **Approvals** — pending high-stakes actions waiting for your yes/no, with a one-click approve from Notion or your phone.
7. **Knowledge Base** — research briefs, decisions, references the agents accumulate and reuse.

You live mostly in Goals + Milestones + Approvals. The agents live in Tasks + Log + Knowledge.

---

## 12. The Agent Team

A **manager + specialists** crew. Each agent is assigned the model that fits its job (via LiteLLM), so you're using all your accounts where they're strongest.

| Agent | Role | Tools | Default model |
|---|---|---|---|
| **Orchestrator** (Chief of Staff) | Reads goals/milestones, decomposes into tasks, assigns specialists, tracks progress, escalates to you | Notion (full), reads everything | Claude (deep reasoning) |
| **Planner** | Turns milestones into ordered task plans with dependencies; books time on Calendar | Notion, Calendar | Claude / local 27B |
| **Researcher** | Web + document research, synthesis, writes briefs to Knowledge Base | Web, Drive, Notion | Gemini (huge context) |
| **Builder / Dev** | GitHub work — issues, branches, code, PRs, reviews | GitHub, Drive | Claude + local Coder 32B |
| **Writer / Content** | Drafts content, marketing, docs; Adobe Firefly for imagery | Docs, Notion, Adobe | Claude / GPT |
| **Comms / Ops** | Email triage & drafting, scheduling, follow-ups | Gmail, Calendar | Local 8B (triage) → Claude (drafts) |
| **Reviewer / QA** | Checks each output against the goal's success criteria before it's marked Done — the quality gate | Reads task + output | Claude |

Start with just the **Orchestrator + Researcher + one doer** on a real project, then add specialists as you trust the system.

---

## 13. The Loop — From Goal to Done

```
You: write Goal + Milestones in Notion
        │
        ▼
Orchestrator reads it → breaks into Tasks → assigns each to a specialist
        │
        ▼
Specialists work in parallel (research, build, draft, schedule)
   └─ each writes output + logs every action to Notion
        │
        ▼
Reviewer checks output vs. success criteria
        │
        ├─ needs a high-stakes action? ──► Approvals DB ──► you tap Approve/Reject
        │                                                        │
        ▼                                                        ▼
   Task marked Done ◄───────────────────────────────────── action executes
        │
        ▼
Orchestrator advances milestone → repeats until Goal complete → notifies you
```

You interact at exactly two points: **setting goals** at the top, and **approving high-stakes actions** when they come up. Everything in between runs itself.

---

## 14. Guardrails & Approval Gates

Full autonomy, with a safety valve on the few actions you can't undo.

**Agents act freely (no approval):** create/edit Notion items, research, write drafts, create Google Docs, open GitHub branches and PRs, propose internal calendar blocks, run local-model reasoning, log activity.

**Agents must get your approval (gated):** send email to anyone outside your org, merge/push to `main`, calendar invites to external people, sharing files externally, publishing content, deleting anything, any action that spends money.

**How a gate works:** the agent stops, writes a row in the **Approvals** database with full context (what, why, the exact content), and pings you (Notion + phone/email). You approve or reject in one tap; n8n's wait-for-approval node releases or cancels the action. Nothing irreversible happens without your tap. You can later promote trusted action-types to "auto" as confidence grows.

---

## 15. Recommended Agent Stack

| Need | Tool | Why |
|---|---|---|
| Integrations, triggers, scheduling, **approval gates** | **n8n** | Already has G-Suite/Notion/GitHub nodes + human-in-the-loop wait nodes |
| The agent team (manager + specialists) | **CrewAI** | Role-based crews map *exactly* to "manager + specialists" — lowest-code path to a real team. Model-agnostic, points at LiteLLM. |
| Per-agent model assignment | **LiteLLM** | Give each agent its best brain; swap freely |
| Shared state / memory | **Notion + Qdrant** | Human-readable source of truth + semantic recall |
| *(Upgrade path)* audit trails & rollback | **LangGraph** | When you want checkpointing/time-travel and stricter control, migrate the crew here |

CrewAI runs the reasoning team; n8n is the hands (integrations + gates); Notion is the shared memory; LiteLLM is the brains. This keeps it as low-code as a true autonomous system can be.

---

## 16. Updated Build Plan — Agent Phases

Continues from Phases 0–7 (core orchestrator + chat + voice).

**Phase 8 — Connect the tools (1 day)**
In n8n, authenticate Gmail, Calendar, Drive/Docs, Notion, GitHub. Test one read + one write on each.

**Phase 9 — Build Notion mission control (½ day)**
Create the 7 databases with the properties above and link them. Add one real Goal + Milestones to test against.

**Phase 10 — Stand up the crew (1–2 days)**
Deploy CrewAI wired to LiteLLM (models) and Notion (state). Start with Orchestrator + Researcher + one doer. Have them complete one low-stakes milestone end-to-end.

**Phase 11 — Autonomy & gates (1 day)**
Add the approval-gate workflows in n8n and the activity logging. Run a real project with gates ON. As trust builds, expand the team and loosen gates on proven action-types.

---

## 17. Still Open

1. **Adobe access** — do you have Firefly *API* access, or mainly the Creative Cloud apps? Decides whether the Writer agent generates images automatically or hands off to you.
2. Want me to generate the **actual starter files** next — Docker Compose for the stack, the LiteLLM routing config, the Notion database schemas, and a first CrewAI crew definition?

---

*Architecture prepared May 30, 2026; extended for autonomous multi-agent operation. Tooling reflects the mid-2026 self-hosted AI landscape.*
