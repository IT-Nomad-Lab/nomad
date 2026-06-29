# claude-bridge — local Claude Code → OpenAI-compatible shim

Exposes an OpenAI-compatible `/v1/chat/completions` endpoint that answers using the **local
`claude` CLI** (your Claude Code subscription/login) instead of the metered Anthropic API.
LiteLLM's `deep` and `balanced` aliases point here, so the rest of NOMAD gets Opus/Sonnet
without an API key.

- **Pure stdlib** — no pip installs. Runs **natively in WSL** (needs the `claude` binary + your
  `~/.claude` login), **not** in a container.
- Each request shells out to `claude -p <prompt> --model <opus|sonnet> --system-prompt <sys>
  --allowedTools "" --setting-sources "" --output-format json`, run in an empty workdir so no
  project `CLAUDE.md` or tools leak in.
- Returns `502` on `claude` failure, so LiteLLM fallbacks fire and NOMAD never goes dark.

| Endpoint | Purpose |
|---|---|
| `GET /v1/models` | Advertised models (`opus`, `sonnet`). |
| `POST /v1/chat/completions` | OpenAI-compatible completion via the local `claude` CLI. |

> ⚠ **Quota:** this consumes your Claude Code subscription quota (5-hour windows), not API
> credits. Heavy autonomous use can hit those limits — that's expected.

## Run it

```bash
./start-bridge.sh      # starts bridge.py natively in WSL on :8088 (logs → bridge.log)
```

Typically run as a systemd user service (`nomad-claude-bridge`) so `deep`/`balanced` survive
reboots. LiteLLM reaches it at `host.docker.internal:8088`.

## Key environment variables

| Var | Default | Meaning |
|---|---|---|
| `CLAUDE_BRIDGE_PORT` | `8088` | Listen port. |
