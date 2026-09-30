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

## Fallback alert

Because the chains keep answering, a fall-through is silent by default. `fallback_alert.py` is a
LiteLLM callback (registered under `litellm_settings.callbacks`) that posts to Zulip
`#alerts > litellm-fallback` when a Claude alias (`deep`, `balanced`) is served by `codex` or a local
model (`fast`, `private`, `code`): the alias requested, the tier that answered, and the time. It uses
the shared bot zuliprc, mounted read-only by `docker-compose.yml`, and holds repeats of one pair for
five minutes. Landing on `gpt` does not alert. Check it before relying on it:

```bash
python3 litellm/fallback_alert.py --dry-run                            # formatting only
docker exec nomad-litellm python /app/fallback_alert.py --test-alert   # real message, container path
python3 -m pytest litellm/test_fallback_alert.py -q
```

A config or callback change needs `docker compose up -d --force-recreate litellm`.

## Notes & gotchas

- ⚠ **Verify every cloud model string and every Ollama tag against current provider docs before
  relying on them** — names drift. `docker exec … ollama list` shows installed tags.
- The example config reaches a **native (host) Ollama** via `host.docker.internal:11434`.
- On Docker Desktop/WSL, `docker compose restart litellm` can break the config bind-mount — use
  `docker compose up -d --force-recreate litellm` instead.
