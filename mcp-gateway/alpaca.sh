#!/usr/bin/env bash
# Alpaca market-data MCP as Streamable HTTP for the gateway. Paper account, read-only toolsets.
# Credentials stay in the crypto-trading-bot .env; nothing is copied.
set -euo pipefail
ENV_FILE="${ALPACA_ENV_FILE:-$HOME/crypto-trading-bot/.env}"
get() { sed -n "s/^$1=//p" "$ENV_FILE" | head -1; }
export ALPACA_API_KEY="$(get ALPACA_API_KEY)" ALPACA_SECRET_KEY="$(get ALPACA_API_SECRET)"
export ALPACA_PAPER_TRADE=true
export ALPACA_TOOLSETS="${ALPACA_TOOLSETS:-account,assets,stock-data,crypto-data,news,corporate-actions}"
exec uvx alpaca-mcp-server --transport streamable-http --host "${MCP_HOST:-172.17.0.1}" --port "${MCP_PORT:-8310}"
