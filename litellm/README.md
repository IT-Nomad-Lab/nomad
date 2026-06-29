# litellm — the model gateway config

NOMAD addresses models by **role alias**, never by raw model name. The
[LiteLLM](https://github.com/BerriAI/litellm) gateway maps each alias to a real model and falls
back automatically. **Swap models here only** — never wire a raw model name into an app.

`config.yaml` defines the aliases:

| Alias | Intended role |
|---|---|
| `deep` | Hardest reasoning (Claude Opus, via [`claude-bridge`](../claude-bridge/)). |
| `balanced` | Everyday reasoning (Claude Sonnet, via the bridge). |
| `gpt` | OpenAI cloud model. |
| `longdoc` | Long-context cloud model. |
| `fast` | Always-on local workhorse (Ollama). |
| `private` | Local reasoning / privacy-sensitive (Ollama). |
| `code` | Coding (local Ollama coder model). |

Every cloud/subscription alias ends its fallback chain with a **local Ollama model**, so NOMAD
never goes dark if a provider is throttled or offline.

## Notes & gotchas

- ⚠ **Verify every cloud model string and every Ollama tag against current provider docs before
  relying on them** — names drift. `docker exec … ollama list` shows installed tags.
- The example config reaches a **native (host) Ollama** via `host.docker.internal:11434`.
- On Docker Desktop/WSL, `docker compose restart litellm` can break the config bind-mount — use
  `docker compose up -d --force-recreate litellm` instead.
