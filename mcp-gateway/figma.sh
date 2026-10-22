#!/usr/bin/env bash
# Figma (Framelink figma-developer-mcp, read design data) as Streamable HTTP for the gateway.
# Personal access token from ~/nomad/.env (FIGMA_API_TOKEN). It expires: rotation reminder in nomad-pm.
set -euo pipefail
ENV_FILE="${NOMAD_ENV_FILE:-$HOME/nomad/.env}"
TOKEN="$(sed -n 's/^FIGMA_API_TOKEN=//p' "$ENV_FILE" | head -1 | tr -d "\"'")"
[ -n "$TOKEN" ] || { echo "FIGMA_API_TOKEN missing in $ENV_FILE" >&2; exit 1; }
IMG="${FIGMA_IMAGE_DIR:-$HOME/.cache/nomad-figma-images}"
mkdir -p "$IMG"
export FIGMA_API_KEY="$TOKEN"
exec npx -y figma-developer-mcp@0.13.2 --figma-api-key "$TOKEN" --no-telemetry --image-dir "$IMG" \
  --host "${MCP_HOST:-127.0.0.1}" --port "${MCP_PORT:-18311}"
