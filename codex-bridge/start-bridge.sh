#!/usr/bin/env bash
# Start the NOMAD Codex bridge natively in WSL.
# It serves the LiteLLM `codex` alias via your local ChatGPT (`codex login`) session.
cd "$(dirname "$0")"
if pgrep -f "codex-bridge/bridge.py" >/dev/null; then
  echo "Codex bridge already running (PID $(pgrep -f codex-bridge/bridge.py | tr '\n' ' '))."
  exit 0
fi
if ! codex login status >/dev/null 2>&1 || codex login status 2>&1 | grep -qi "not logged in"; then
  echo "WARNING: codex is not logged in. Run 'codex login' first, or the bridge 502s on every call."
fi
nohup python3 bridge.py > bridge.log 2>&1 &
sleep 1
echo "Codex bridge started (PID $!). Logs: $(pwd)/bridge.log"
