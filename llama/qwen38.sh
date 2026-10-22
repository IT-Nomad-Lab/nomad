#!/usr/bin/env bash
# Qwen3.8-27B + MTP on llama.cpp, :8240 (#1045). The `code` and `longdoc-local` aliases route here.
#
# On demand, not always on: the 27B and Ollama's `private` can't share the 24 GB card. Windows
# pages the overflow to RAM instead of failing (~13 tok/s). `start` unloads every Ollama model
# first. While :8240 is up, a `private` call reloads it and pages one of the two, so stop this
# when you are done. LiteLLM falls back to Ollama while :8240 is down.
#
# Binds: llama-server on 127.0.0.1 only. The ollama relay forwards 172.17.0.1:8240 to it for
# containers. Never 0.0.0.0.
#
# GPU admission (#1462): start and stop take /tmp/nomad-gpu.lock without waiting (llama/gpu-lock.sh);
# exit 75 and no change when another GPU workload holds it or a gpu_guard restore is pending.
#
# Usage: llama/qwen38.sh start|stop|status     Env: QWEN38_CTX (default 65536), QWEN38_MTP (1/0)
set -euo pipefail

LLAMA=${LLAMA_CPP:-$HOME/vendor/llama.cpp/build/bin/llama-server}
DIR=${QWEN38_DIR:-$HOME/vendor/Models/qwen3.8-27b}
CTX=${QWEN38_CTX:-65536}
PORT=8240
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

active() { systemctl --user -q is-active "$1"; }
healthy() { curl -sf -m2 "127.0.0.1:$PORT/health" >/dev/null; }

launch_server() {
  local spec=()
  [[ ${QWEN38_MTP:-1} == 1 ]] && spec=(-md "$DIR/MTP/mtp-Qwen3.8-27B-Q4_0.gguf" --spec-type draft-mtp --spec-draft-n-max 4 -ngld 999)
  systemctl --user reset-failed nomad-llama-8240 2>/dev/null || true
  systemd-run --user --unit=nomad-llama-8240 --collect --quiet \
    -E CUDA_VISIBLE_DEVICES=0 -E LD_LIBRARY_PATH=/usr/lib/wsl/lib \
    "$LLAMA" -m "$DIR/Qwen3.8-27B-UD-Q4_K_XL.gguf" --alias qwen3.8-27b "${spec[@]}" \
      -ngl 999 -fa on --jinja --chat-template-file "$HERE/qwen38-fixed.jinja" \
      -c "$CTX" -ctk q8_0 -ctv q8_0 --parallel 1 --load-mode none \
      --reasoning-effort low --temp 0.6 --top-p 0.95 --top-k 20 --min-p 0 \
      --host 127.0.0.1 --port "$PORT" 9>&-
}

# Idempotent (#1428): a unit that is already up is left alone, so a second start returns 0
# instead of failing on "unit already exists". The MTP draft load fails now and then with
# vector::_M_range_check (2 of 37 loads, 2026-10); the same files load on the next try, so that
# one error gets a single retry.
start() {
  if healthy && active nomad-llama-8240-bridge; then
    echo "already up on 127.0.0.1:$PORT and 172.17.0.1:$PORT"; return 0
  fi
  local since retried=0
  if ! active nomad-llama-8240; then
    gpu_admit check-restore
    unload_ollama
    since=$(date '+%F %T'); launch_server
  fi
  active nomad-llama-8240-bridge || { gpu_admit check-restore; systemd-run --user --unit=nomad-llama-8240-bridge --collect --quiet \
    -E NOMAD_BRIDGE_IP=172.17.0.1 -E NOMAD_OLLAMA_RELAY_PORT=$PORT -E NOMAD_OLLAMA_UPSTREAM=127.0.0.1:$PORT \
    /usr/bin/python3 "$HERE/../ollama-relay/relay.py" 9>&-; }
  echo -n "loading"
  until healthy; do
    if ! active nomad-llama-8240; then
      if (( ! retried )) && [[ -n ${since:-} ]] && journalctl --user -u nomad-llama-8240 --since "$since" --no-pager \
           | grep -q 'failed to load draft model'; then
        echo; echo "draft model failed to load; retrying once"
        retried=1; sleep 5; since=$(date '+%F %T'); launch_server; continue
      fi
      echo; journalctl --user -u nomad-llama-8240 -n 30 --no-pager; exit 1
    fi
    echo -n .; sleep 2
  done
  echo " up on 127.0.0.1:$PORT and 172.17.0.1:$PORT (ctx $CTX, mtp ${QWEN38_MTP:-1})"
}

stop() {
  gpu_admit
  systemctl --user stop nomad-llama-8240 nomad-llama-8240-bridge 2>/dev/null || true
  echo stopped
}

status() {
  systemctl --user --no-pager -n0 status nomad-llama-8240 nomad-llama-8240-bridge 2>/dev/null | grep -E '●|Active' || echo "not running"
}

case ${1:-} in start) start ;; stop) stop ;; status) status ;; *) echo "usage: $0 start|stop|status" >&2; exit 2 ;; esac
