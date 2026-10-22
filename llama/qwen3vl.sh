#!/usr/bin/env bash
# Qwen3-VL-8B abliterated (Huihui weights, Q8_0) on llama.cpp, :8241 (#1046). The `vision` alias
# routes here.
#
# Why not Ollama: Ollama's packaging of this model runs the vision encoder on the CPU (~26 ms per
# image token, 105 s for one large photo). llama.cpp runs the mmproj on CUDA: ~1.3 s for a
# 1080x1350 image, 80 tok/s decode.
#
# On demand, like :8240. About 12 GB with a 32k context, so it can't sit next to Ollama's
# `private` (22 GB). `start` unloads every Ollama model first; stop this when the batch is done.
# While it is down, LiteLLM falls back to `vision-ollama` (same weights, CPU encoder, slow), and
# nomad-social, which disables fallbacks, moves on to the next alias in its own chain.
#
# Binds: llama-server on 127.0.0.1 only; the ollama relay forwards 172.17.0.1:8241 to it for
# containers. Never 0.0.0.0.
#
# GPU admission (#1462): start and stop take /tmp/nomad-gpu.lock without waiting (llama/gpu-lock.sh);
# exit 75 and no change when another GPU workload holds it or a gpu_guard restore is pending.
#
# Usage: llama/qwen3vl.sh start|stop|status     Env: QWEN3VL_CTX (default 32768)
set -euo pipefail

LLAMA=${LLAMA_CPP:-$HOME/vendor/llama.cpp/build/bin/llama-server}
DIR=${QWEN3VL_DIR:-$HOME/vendor/Models/qwen3vl-8b}
CTX=${QWEN3VL_CTX:-32768}
PORT=8241
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/gpu-lock.sh"
OLLAMA="http://$(ip route | awk '/default/ && !g {g=$3} END {print g}'):11434"

unload_ollama() {
  # Empty or non-JSON while Ollama is busy unloading: treat as nothing loaded.
  for m in $(curl -s -m10 "$OLLAMA/api/ps" | python3 -c '
import json, sys
try: [print(m["name"]) for m in json.load(sys.stdin).get("models", [])]
except ValueError: pass'); do
    echo "unloading ollama $m"
    curl -s -m30 "$OLLAMA/api/generate" -d "{\"model\":\"$m\",\"keep_alive\":0}" >/dev/null
  done
}

start() {
  gpu_admit check-restore
  unload_ollama
  systemd-run --user --unit=nomad-llama-$PORT --collect --quiet \
    -E CUDA_VISIBLE_DEVICES=0 -E LD_LIBRARY_PATH=/usr/lib/wsl/lib \
    "$LLAMA" -m "$DIR/Huihui-Qwen3-VL-8B-Instruct-abliterated.Q8_0.gguf" \
      --mmproj "$DIR/Huihui-Qwen3-VL-8B-Instruct-abliterated.mmproj-f16.gguf" --alias qwen3-vl-8b \
      -ngl 999 -fa on --jinja -c "$CTX" -ctk q8_0 -ctv q8_0 --parallel 1 --load-mode none \
      --temp 0.7 --top-p 0.8 --top-k 20 --min-p 0 \
      --host 127.0.0.1 --port "$PORT" 9>&-
  systemd-run --user --unit=nomad-llama-$PORT-bridge --collect --quiet \
    -E NOMAD_BRIDGE_IP=172.17.0.1 -E NOMAD_OLLAMA_RELAY_PORT=$PORT -E NOMAD_OLLAMA_UPSTREAM=127.0.0.1:$PORT \
    /usr/bin/python3 "$HERE/../ollama-relay/relay.py" 9>&-
  echo -n "loading"
  until curl -sf -m2 "127.0.0.1:$PORT/health" >/dev/null; do
    systemctl --user -q is-active nomad-llama-$PORT || { echo; journalctl --user -u nomad-llama-$PORT -n 30 --no-pager; exit 1; }
    echo -n .; sleep 2
  done
  echo " up on 127.0.0.1:$PORT and 172.17.0.1:$PORT (ctx $CTX)"
}

stop() {
  gpu_admit
  systemctl --user stop nomad-llama-$PORT nomad-llama-$PORT-bridge 2>/dev/null || true
  echo stopped
}

status() {
  systemctl --user --no-pager -n0 status nomad-llama-$PORT nomad-llama-$PORT-bridge 2>/dev/null | grep -E '●|Active' || echo "not running"
}

case ${1:-} in start) start ;; stop) stop ;; status) status ;; *) echo "usage: $0 start|stop|status" >&2; exit 2 ;; esac
