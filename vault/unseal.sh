#!/usr/bin/env bash
# Unseal the NOMAD vault from the key file. Used by nomad-vault-unseal.service and bootstrap.sh.
set -euo pipefail
K="$HOME/.config/nomad/openbao/unseal.key"
C=nomad-vault-openbao-1
status() { docker exec "$C" bao status -format=json 2>/dev/null || true; }   # exits 2 while sealed
for _ in $(seq 1 60); do status | grep -q initialized && break; sleep 1; done
if status | grep -q '"sealed": true'; then
  [ -r "$K" ] || { echo "no unseal key at $K" >&2; exit 1; }
  docker exec -i "$C" bao operator unseal "$(cat "$K")" >/dev/null
fi
