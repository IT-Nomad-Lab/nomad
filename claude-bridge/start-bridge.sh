#!/usr/bin/env bash
# Start the NOMAD Claude Code bridge natively in WSL.
# It serves deep/balanced for LiteLLM via your local `claude` login.
cd "$(dirname "$0")"
if pgrep -f "bridge.py" >/dev/null; then
  echo "Bridge already running (PID $(pgrep -f bridge.py | tr '\n' ' '))."
  exit 0
fi
nohup python3 bridge.py > bridge.log 2>&1 &
sleep 1
echo "Claude bridge started (PID $!). Logs: $(pwd)/bridge.log"
