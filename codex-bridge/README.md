# codex-bridge — the backup brain

An OpenAI-compatible endpoint answered by the local **OpenAI Codex CLI** on your ChatGPT
subscription. LiteLLM's `codex` alias points here, and every cloud alias now falls through it.

## Why this exists

NOMAD had one subscription-backed CLI. Every `deep` and `balanced` call, every Builder dispatch,
went through `claude`. When the MAX subscription hit a 5-hour window the whole stack fell to
llama3.1:8b, which is a fine workhorse and a poor architect.

Codex is a second vendor with a separate quota. One throttled account no longer degrades
everything downstream of it.

```
deep  →  balanced  →  codex  →  gpt  →  fast
code  →  codex     →  gpt    →  fast
```

`balanced` is tried before `codex` because it is the same (cheaper) bridge; `codex` before `gpt`
because the subscription is already paid for and `gpt` bills per token; `fast` last because local
always answers.

## How it works

Same shape as [`claude-bridge/`](../claude-bridge/): pure stdlib, no pip installs, runs **natively
in WSL** (it needs the `codex` binary and your `~/.codex` login — it cannot run in a container).

Each request shells out to:

```
codex exec --skip-git-repo-check --ephemeral --ignore-user-config \
           -s read-only -C _workdir --color never --json -o <tmp> -
```

The prompt arrives on stdin. Those flags matter:

| flag | why |
|---|---|
| `-s read-only` | this is a **model endpoint, not an agent**. It cannot write anything. |
| `-C _workdir` | an empty scratch dir, so no project file or `AGENTS.md` leaks into the context |
| `--ignore-user-config` | a personal `config.toml` must not silently change behaviour here |
| `--ephemeral` | no session files accumulate in `~/.codex` |
| `-o <tmp>` | the final message, on a documented path (the `--json` event schema is not a contract) |

`OPENAI_API_KEY` is blanked in the subprocess environment. A key present there would silently
switch billing from the subscription to metered API credits — the same guard the Claude bridge uses.

## Install

```bash
npm install -g @openai/codex     # already done on this box: codex-cli 0.154.0
codex login                      # browser OAuth against your ChatGPT account
./start-bridge.sh                # foreground-ish, logs to bridge.log
./install_service.sh             # or: systemd user service, survives reboot
```

Manage it like the other sidecars: `systemctl --user status nomad-codex-bridge`,
`journalctl --user -u nomad-codex-bridge -f`.

## Check it

```bash
curl -s localhost:8089/health
curl -s localhost:8089/v1/chat/completions -H 'content-type: application/json' \
  -d '{"model":"codex","messages":[{"role":"user","content":"Reply with exactly: BRIDGE OK"}]}' \
  | jq -r '.choices[0].message.content'
```

Through the gateway, which is what actually matters:

```bash
curl -s localhost:4000/v1/chat/completions -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H 'content-type: application/json' -d '{"model":"codex","messages":[{"role":"user","content":"hi"}]}'
```

## Codex can generate images — the bridge deliberately cannot

`codex features list` shows `image_generation` as **stable and on by default** (alongside
`computer_use` and `browser_use`). Verified on this box: a 1536x1024 PNG, rendered on the ChatGPT
subscription with `OPENAI_API_KEY` blanked, so it is not the metered Images API. Codex writes it to
`~/.codex/generated_images/<session>/` and copies it where you asked.

The bridge cannot use this, by construction. Image generation has to write a file, and the bridge
runs `-s read-only` in an empty workdir. That is the same choice that makes it a safe model
endpoint, so the fix is not to loosen the bridge — it is a separate call with a writable workdir:

```bash
OPENAI_API_KEY= codex exec --skip-git-repo-check --ephemeral \
  -s workspace-write -C <output dir> "generate ... and save it as out.png"
```

Prompt adherence beats local SDXL by a wide margin (side-by-side on the same prompt: SDXL gave a
desktop chassis at dusk, Codex gave the actual rack, the single amber LED and the rain-streaked
window), and it renders legible text, which SDXL does not.

That is exposed as an MCP tool you call BY NAME — **`codex_image(prompt, subdir, filename)`**
(`mcp-local/nomad_codex_image.py`). It is NOT in the `generate_image` provider chain (still
`comfyui -> openai gpt-image-1 -> firefly`): it spends subscription quota and is slower than
ComfyUI, so local SDXL stays the default and Codex is asked for deliberately.

## Known limits

- **It consumes ChatGPT subscription quota**, not API credits. Heavy autonomous use will hit those
  limits, exactly like the Claude bridge does.
- **No streaming.** The endpoint returns one complete response. Fine for LiteLLM routing and for
  the specialists; a chat UI wanting token-by-token output should not point here.
- **Failure takes ~14 seconds.** When not logged in, the CLI retries its websocket before giving up,
  and only then does the bridge return a 502 for LiteLLM to fail over on. It fails correctly, not
  quickly.
- **Token counts are best-effort.** They are scraped from the `--json` event stream, whose schema is
  not a stable contract. Worst case usage reports zero; nothing else breaks.
- `codex login` needs a browser. On a truly headless box use `codex login --device-auth`.

Ports on this box: 8088 claude-bridge · **8089 codex-bridge** · 8200 voice · 8210 scraper ·
8212 stt · 8214 detect.
