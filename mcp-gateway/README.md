# NOMAD MCP gateway

A dedicated LiteLLM instance (`127.0.0.1:4010`, own Postgres, project `nomad-mcp-gateway`) that
hands MCP servers to projects by key. PRD: `docs/prd/mcp-gateway.md`. Registry of every server:
`docs/MCP-REGISTRY.md`. Project map: `docs/prd/mcp-project-map.md`.

- Run: `cd ~/nomad/mcp-gateway && docker compose up -d` (secrets in `~/.config/nomad/mcp-gateway.env`).
- Host servers are wrapped as HTTP units bound to `172.17.0.1` (`alpaca.sh` +
  `~/.config/systemd/user/nomad-mcp-alpaca.service`; generic launcher `mcp-local/serve_http.py`).

## Grant a server to a project

```
python3 mcp-gateway/grant.py --servers                    # what is registered
python3 mcp-gateway/grant.py <project> alpaca             # grant (creates the project's key)
python3 mcp-gateway/grant.py <project> --revoke alpaca
python3 mcp-gateway/grant.py <project> --show
```

The key lands in `~/.config/nomad/mcp-keys/<project>` (0600). Servers are registered with
`allow_all_keys: false`, so a server is invisible to a project until granted here.

## Connect a project (Claude Code)

`.mcp.json` in the project:

```json
{"mcpServers": {"mcp-gateway": {"type": "http", "url": "http://127.0.0.1:4010/mcp",
  "headers": {"x-litellm-api-key": "Bearer ${MCP_GATEWAY_KEY}"}}}}
```

and `MCP_GATEWAY_KEY` in the project's `.claude/settings.local.json` under `env` (gitignored).
Tools appear as `<server>-<tool>`. Codex and the local model use the same URL and header.

## Add a server

1. Host stdio server: run it over streamable HTTP on `172.17.0.1:<port>` as a systemd user unit.
2. Register: `POST /v1/mcp/server` with `allow_all_keys: false` and the URL.
3. Give it a tier in `scripts/mcp-registry.py`, run it, commit `docs/MCP-REGISTRY.md`.
4. Grant with `grant.py`.

Known limits: OAuth servers whose provider refuses dynamic client registration (Figma's official
server, 403) cannot be proxied as-is, so Figma goes through `figma.sh` (Framelink, personal access
token, read tools only: `get_figma_data`, `download_figma_images`). The token expires; see #1268.

## Local model server set

The local model's window (65k) holds about eight servers. The bridge reads
`~/.config/nomad/mcp-grants.json` (example: `mcp-gateway/mcp-grants.example.json`) and a local run
loads only `default` plus that project's list; over `AGENT_BRIDGE_LOCAL_MAX_SERVERS` (8) the run is
refused with a message. Without the file every server is loaded, as before. Claude and Codex are not
limited by this file.

## Wrapper authentication

Wrappers listen on `127.0.0.1`; `auth_proxy.py` (units in `systemd/`) listens on `172.17.0.1` and
needs `Authorization: Bearer $MCP_WRAPPER_SECRET` (in `~/.config/nomad/mcp-gateway.env`). Register the
server with `auth_type: bearer_token` and `credentials: {"auth_value": "<secret>"}` (PUT
`/v1/mcp/server`); the gateway returns the credential hidden.

## Google (Gmail, Drive, Calendar)

`gmail`, `gdrive`, `gcalendar` point at `google_proxy.py` (172.17.0.1:8312), which reads the OAuth
client and refresh token from the OpenBao vault (`vault/`) and refreshes the 1 h access token itself,
so no backend does a Google login. Gateway names cannot contain `-`. One consent for all three
(`google_consent.py`, then `vault/seed_google.py`); grant per project with `grant.py`.
