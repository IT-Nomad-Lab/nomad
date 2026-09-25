#!/usr/bin/env bash
# Launch one Hostinger API MCP server (hostinger-api-mcp npm package).
# Usage: hostinger.sh <bin>   e.g. hostinger.sh hostinger-vps-mcp
# The token lives in nomad/.env (gitignored), never in .mcp.json.
set -euo pipefail
ENV_FILE="$(dirname "$0")/../.env"
HOSTINGER_API_TOKEN="$(grep -E '^HOSTINGER_API_TOKEN=' "$ENV_FILE" | sed -n '$p' | cut -d= -f2-)"
[ -n "$HOSTINGER_API_TOKEN" ] || { echo "HOSTINGER_API_TOKEN missing from $ENV_FILE" >&2; exit 1; }
export HOSTINGER_API_TOKEN
export PATH="/home/linuxbrew/.linuxbrew/bin:$PATH"
exec npx -y --package=hostinger-api-mcp@1.61.1 "$1"
